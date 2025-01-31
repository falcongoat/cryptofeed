from decimal import Decimal

from cryptofeed.defines import ASK, BID
from cryptofeed.exchanges import Bitmex


def test_rest_bitmex():
    ret = []

    for data in Bitmex().trades_sync('BTC-USD-PERP'):
        ret.extend(data)

    assert len(ret) > 0
    assert ret[0]['feed'] == 'BITMEX'
    assert ret[0]['symbol'] == 'BTC-USD-PERP'


def test_ticker():
    ret = Bitmex().ticker_sync('BTC-USD-PERP')
    assert isinstance(ret, dict)
    assert ret['feed'] == 'BITMEX'
    assert ret['symbol'] == 'BTC-USD-PERP'
    assert ret['bid'] > 0
    assert ret['ask'] > 0


def test_book():
    ret = Bitmex().l2_book_sync('BTC-USD-PERP')
    assert BID in ret
    assert ASK in ret
    assert len(ret[BID]) > 0
    assert len(ret[ASK]) > 0
    for price, size in ret[ASK].items():
        assert isinstance(price, Decimal)
        assert isinstance(size, Decimal)
