'''
Copyright (C) 2017-2021  Bryant Moscon - bmoscon@gmail.com

Please see the LICENSE file for the terms and conditions
associated with this software.
'''

import asyncio
from collections import defaultdict
import logging
from decimal import Decimal
import hmac
from time import time
import zlib
from typing import Dict, Iterable, Tuple

from sortedcontainers import SortedDict as sd
from yapic import json

from cryptofeed.connection import AsyncConnection
from cryptofeed.defines import BID, ASK, BUY, FUTURES, ORDER_INFO, PERPETUAL, SPOT, USER_FILLS
from cryptofeed.defines import FTX as FTX_id
from cryptofeed.defines import FUNDING, L2_BOOK, LIQUIDATIONS, OPEN_INTEREST, SELL, TICKER, TRADES, FILLED
from cryptofeed.exceptions import BadChecksum
from cryptofeed.feed import Feed
from cryptofeed.symbols import Symbol
from cryptofeed.exchanges.mixins.ftx_rest import FTXRestMixin


LOG = logging.getLogger('feedhandler')


class FTX(Feed, FTXRestMixin):
    id = FTX_id
    symbol_endpoint = "https://ftx.com/api/markets"
    websocket_channels = {
        L2_BOOK: 'orderbook',
        TRADES: 'trades',
        TICKER: 'ticker',
        FUNDING: 'funding',
        OPEN_INTEREST: 'open_interest',
        LIQUIDATIONS: 'trades',
        ORDER_INFO: 'orders',
        USER_FILLS: 'fills',
    }
    request_limit = 30

    @classmethod
    def _parse_symbol_data(cls, data: dict) -> Tuple[Dict, Dict]:
        ret = {}
        info = defaultdict(dict)

        for d in data['result']:
            if not d['enabled']:
                continue
            expiry = None
            stype = SPOT
            # FTX Futures contracts are stable coin settled, but
            # prices quoted are in USD, see https://help.ftx.com/hc/en-us/articles/360024780791-What-Are-Futures
            if "-MOVE-" in d['name']:
                stype = FUTURES
                base, expiry = d['name'].rsplit("-", maxsplit=1)
                quote = 'USD'
                if 'Q' in expiry:
                    year, quarter = expiry.split("Q")
                    year = year[2:]
                    date = ["0325", "0624", "0924", "1231"]
                    expiry = year + date[int(quarter) - 1]
            elif "-" in d['name']:
                base, expiry = d['name'].split("-")
                quote = 'USD'
                stype = FUTURES
                if expiry == 'PERP':
                    expiry = None
                    stype = PERPETUAL
            elif d['type'] == SPOT:
                base, quote = d['baseCurrency'], d['quoteCurrency']
            else:
                # not enough info to construct a symbol - this is usually caused
                # by non crypto futures, i.e. TRUMP2024 or other contracts involving
                # betting on world events
                continue

            s = Symbol(base, quote, type=stype, expiry_date=expiry)
            ret[s.normalized] = d['name']
            info['tick_size'][s.normalized] = d['priceIncrement']
            info['quantity_step'][s.normalized] = d['sizeIncrement']
            info['instrument_type'][s.normalized] = s.type
        return ret, info

    def __init__(self, **kwargs):
        super().__init__('wss://ftexchange.com/ws/', **kwargs)

    def __reset(self):
        self._l2_book = {}
        self._funding_cache = {}
        self._open_interest_cache = {}

    async def generate_token(self, conn: AsyncConnection):
        ts = int(time() * 1000)
        msg = {
            'op': 'login',
            'args':
            {
                'key': self.key_id,
                'sign': hmac.new(self.key_secret.encode(), f'{ts}websocket_login'.encode(), 'sha256').hexdigest(),
                'time': ts,
            }
        }
        if self.subaccount:
            msg['args']['subaccount'] = self.subaccount
        await conn.write(json.dumps(msg))

    async def authenticate(self, conn: AsyncConnection):
        if self.requires_authentication:
            await self.generate_token(conn)

    async def subscribe(self, conn: AsyncConnection):
        self.__reset()
        for chan in self.subscription:
            symbols = self.subscription[chan]
            if chan == FUNDING:
                asyncio.create_task(self._funding(symbols))  # TODO: use HTTPAsyncConn
                continue
            if chan == OPEN_INTEREST:
                asyncio.create_task(self._open_interest(symbols))  # TODO: use HTTPAsyncConn
                continue
            if self.is_authenticated_channel(self.exchange_channel_to_std(chan)):
                await conn.write(json.dumps(
                    {
                        "channel": chan,
                        "op": "subscribe"
                    }
                ))
                continue
            for pair in symbols:
                await conn.write(json.dumps(
                    {
                        "channel": chan,
                        "market": pair,
                        "op": "subscribe"
                    }
                ))

    def __calc_checksum(self, pair):
        bid_it = reversed(self._l2_book[pair][BID])
        ask_it = iter(self._l2_book[pair][ASK])

        bids = [f"{bid}:{self._l2_book[pair][BID][bid]}" for bid in bid_it]
        asks = [f"{ask}:{self._l2_book[pair][ASK][ask]}" for ask in ask_it]

        if len(bids) == len(asks):
            combined = [val for pair in zip(bids, asks) for val in pair]
        elif len(bids) > len(asks):
            combined = [val for pair in zip(bids[:len(asks)], asks) for val in pair]
            combined += bids[len(asks):]
        else:
            combined = [val for pair in zip(bids, asks[:len(bids)]) for val in pair]
            combined += asks[len(bids):]

        computed = ":".join(combined).encode()
        return zlib.crc32(computed)

    async def _open_interest(self, pairs: Iterable):
        """
            {
              "success": true,
              "result": {
                "volume": 1000.23,
                "nextFundingRate": 0.00025,
                "nextFundingTime": "2019-03-29T03:00:00+00:00",
                "expirationPrice": 3992.1,
                "predictedExpirationPrice": 3993.6,
                "strikePrice": 8182.35,
                "openInterest": 21124.583
              }
            }
        """
        while True:
            for pair in pairs:
                # OI only for perp and futures, so check for / in pair name indicating spot
                if '/' in pair:
                    continue
                end_point = f"https://ftx.com/api/futures/{pair}/stats"
                data = await self.http_conn.read(end_point)
                data = json.loads(data, parse_float=Decimal)
                if 'result' in data:
                    oi = data['result']['openInterest']
                    if oi != self._open_interest_cache.get(pair, None):
                        await self.callback(OPEN_INTEREST,
                                            feed=self.id,
                                            symbol=pair,
                                            open_interest=oi,
                                            timestamp=time(),
                                            receipt_timestamp=time()
                                            )
                        self._open_interest_cache[pair] = oi
                        await asyncio.sleep(1)
            await asyncio.sleep(60)

    async def _funding(self, pairs: Iterable):
        """
            {
              "success": true,
              "result": [
                {
                  "future": "BTC-PERP",
                  "rate": 0.0025,
                  "time": "2019-06-02T08:00:00+00:00"
                }
              ]
            }
        """
        while True:
            for pair in pairs:
                if '-PERP' not in pair:
                    continue
                data = await self.http_conn.read(f"https://ftx.com/api/funding_rates?future={pair}")
                data = json.loads(data, parse_float=Decimal)
                data2 = await self.http_conn.read(f"https://ftx.com/api/futures/{pair}/stats")
                data2 = json.loads(data2, parse_float=Decimal)
                data['predicted_rate'] = Decimal(data2['result']['nextFundingRate'])

                last_update = self._funding_cache.get(pair, None)
                update = str(data['result'][0]['rate']) + str(data['result'][0]['time']) + str(data['predicted_rate'])
                if last_update and last_update == update:
                    continue
                else:
                    self._funding_cache[pair] = update

                await self.callback(FUNDING, feed=self.id,
                                    symbol=self.exchange_symbol_to_std_symbol(data['result'][0]['future']),
                                    rate=data['result'][0]['rate'],
                                    predicted_rate=data['predicted_rate'],
                                    timestamp=self.timestamp_normalize(data['result'][0]['time']),
                                    receipt_timestamp=time()
                                    )
                await asyncio.sleep(0.1)
            await asyncio.sleep(60)

    async def _trade(self, msg: dict, timestamp: float):
        """
        example message:

        {"channel": "trades", "market": "BTC-PERP", "type": "update", "data": [{"id": null, "price": 10738.75,
        "size": 0.3616, "side": "buy", "liquidation": false, "time": "2019-08-03T12:20:19.170586+00:00"}]}
        """
        for trade in msg['data']:
            await self.callback(TRADES, feed=self.id,
                                symbol=self.exchange_symbol_to_std_symbol(msg['market']),
                                side=BUY if trade['side'] == 'buy' else SELL,
                                amount=Decimal(trade['size']),
                                price=Decimal(trade['price']),
                                order_id=trade['id'],
                                timestamp=float(self.timestamp_normalize(trade['time'])),
                                receipt_timestamp=timestamp)
            if bool(trade['liquidation']):
                await self.callback(LIQUIDATIONS,
                                    feed=self.id,
                                    symbol=self.exchange_symbol_to_std_symbol(msg['market']),
                                    side=BUY if trade['side'] == 'buy' else SELL,
                                    leaves_qty=Decimal(trade['size']),
                                    price=Decimal(trade['price']),
                                    order_id=trade['id'],
                                    status=FILLED,
                                    timestamp=float(self.timestamp_normalize(trade['time'])),
                                    receipt_timestamp=timestamp)

    async def _ticker(self, msg: dict, timestamp: float):
        """
        example message:

        {"channel": "ticker", "market": "BTC/USD", "type": "update", "data": {"bid": 10717.5, "ask": 10719.0,
        "last": 10719.0, "time": 1564834587.1299787}}
        """
        await self.callback(TICKER, feed=self.id,
                            symbol=self.exchange_symbol_to_std_symbol(msg['market']),
                            bid=Decimal(msg['data']['bid'] if msg['data']['bid'] else 0.0),
                            ask=Decimal(msg['data']['ask'] if msg['data']['ask'] else 0.0),
                            timestamp=float(msg['data']['time']),
                            receipt_timestamp=timestamp)

    async def _book(self, msg: dict, timestamp: float):
        """
        example messages:

        snapshot:
        {"channel": "orderbook", "market": "BTC/USD", "type": "partial", "data": {"time": 1564834586.3382702,
        "checksum": 427503966, "bids": [[10717.5, 4.092], ...], "asks": [[10720.5, 15.3458], ...], "action": "partial"}}

        update:
        {"channel": "orderbook", "market": "BTC/USD", "type": "update", "data": {"time": 1564834587.1299787,
        "checksum": 3115602423, "bids": [], "asks": [[10719.0, 14.7461]], "action": "update"}}
        """
        check = msg['data']['checksum']
        if msg['type'] == 'partial':
            # snapshot
            pair = self.exchange_symbol_to_std_symbol(msg['market'])
            self._l2_book[pair] = {
                BID: sd({
                    Decimal(price): Decimal(amount) for price, amount in msg['data']['bids']
                }),
                ASK: sd({
                    Decimal(price): Decimal(amount) for price, amount in msg['data']['asks']
                })
            }
            if self.checksum_validation and self.__calc_checksum(pair) != check:
                raise BadChecksum
            await self.book_callback(self._l2_book[pair], L2_BOOK, pair, True, None, float(msg['data']['time']), timestamp)
        else:
            # update
            delta = {BID: [], ASK: []}
            pair = self.exchange_symbol_to_std_symbol(msg['market'])
            for side in ('bids', 'asks'):
                s = BID if side == 'bids' else ASK
                for price, amount in msg['data'][side]:
                    price = Decimal(price)
                    amount = Decimal(amount)
                    if amount == 0:
                        delta[s].append((price, 0))
                        del self._l2_book[pair][s][price]
                    else:
                        delta[s].append((price, amount))
                        self._l2_book[pair][s][price] = amount
            if self.checksum_validation and self.__calc_checksum(pair) != check:
                raise BadChecksum
            await self.book_callback(self._l2_book[pair], L2_BOOK, pair, False, delta, float(msg['data']['time']), timestamp)

    async def _fill(self, msg: dict, timestamp: float):
        """
        example message:
        {
            "channel": "fills",
            "data": {
                "fee": 78.05799225,
                "feeRate": 0.0014,
                "future": "BTC-PERP",
                "id": 7828307,
                "liquidity": "taker",
                "market": "BTC-PERP",
                "orderId": 38065410,
                "tradeId": 19129310,
                "price": 3723.75,
                "side": "buy",
                "size": 14.973,
                "time": "2019-05-07T16:40:58.358438+00:00",
                "type": "order"
            },
            "type": "update"
        }
        """
        fill = msg['data']
        await self.callback(USER_FILLS, feed=self.id,
                            symbol=self.exchange_symbol_to_std_symbol(fill['market']),
                            side=BUY if fill['side'] == 'buy' else SELL,
                            amount=Decimal(fill['size']),
                            price=Decimal(fill['price']),
                            liquidity=fill['liquidity'],
                            order_id=fill['orderId'],
                            trade_id=fill['tradeId'],
                            timestamp=float(self.timestamp_normalize(fill['time'])),
                            receipt_timestamp=timestamp)

    async def _order(self, msg: dict, timestamp: float):
        """
        example message:
        {
            "channel": "orders",
            "data": {
                "id": 24852229,
                "clientId": null,
                "market": "XRP-PERP",
                "type": "limit",
                "side": "buy",
                "size": 42353.0,
                "price": 0.2977,
                "reduceOnly": false,
                "ioc": false,
                "postOnly": false,
                "status": "closed",
                "filledSize": 0.0,
                "remainingSize": 0.0,
                "avgFillPrice": 0.2978
            },
            "type": "update"
        }
        """
        order = msg['data']
        await self.callback(ORDER_INFO, feed=self.id,
                            symbol=self.exchange_symbol_to_std_symbol(order['market']),
                            status=order['status'],
                            order_id=order['id'],
                            side=BUY if order['side'].lower() == 'buy' else SELL,
                            order_type=order['type'],
                            avg_fill_price=Decimal(order['avgFillPrice']) if order['avgFillPrice'] else None,
                            filled_size=Decimal(order['filledSize']),
                            remaining_size=Decimal(order['remainingSize']),
                            amount=Decimal(order['size']),
                            timestamp=timestamp,
                            receipt_timestamp=timestamp,
                            )

    async def message_handler(self, msg: str, conn, timestamp: float):
        msg = json.loads(msg, parse_float=Decimal)
        if 'type' in msg:
            if msg['type'] == 'subscribed':
                if 'market' in msg:
                    LOG.info('%s: Subscribed to %s channel for %s', self.id, msg['channel'], msg['market'])
                else:
                    LOG.info('%s: Subscribed to %s channel', self.id, msg['channel'])
            elif msg['type'] == 'error':
                LOG.error('%s: Received error message %s', self.id, msg)
                raise Exception('Error from %s: %s', self.id, msg)
            elif 'channel' in msg:
                if msg['channel'] == 'orderbook':
                    await self._book(msg, timestamp)
                elif msg['channel'] == 'trades':
                    await self._trade(msg, timestamp)
                elif msg['channel'] == 'ticker':
                    await self._ticker(msg, timestamp)
                elif msg['channel'] == 'fills':
                    await self._fill(msg, timestamp)
                elif msg['channel'] == 'orders':
                    await self._order(msg, timestamp)
                else:
                    LOG.warning("%s: Invalid message type %s", self.id, msg)
            else:
                LOG.warning("%s: Invalid message type %s", self.id, msg)
        else:
            LOG.warning("%s: Invalid message type %s", self.id, msg)
