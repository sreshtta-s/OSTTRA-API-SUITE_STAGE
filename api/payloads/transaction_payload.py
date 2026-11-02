"""Request body that searches the trade-management ledger for one trade id (steps 05 / 05b)."""

from datetime import date


def trade_date_window(trade_date=None, today=None):
    """Return [start, end] as YYYY-MM-DD: the trade date on both ends, as the ledger screen searches.

    A missing or unparseable trade_date searches today instead. A ledger that
    recorded the wrong trade date hides the trade from this search, so step 05
    reports it as not found rather than as a tradeDate mismatch.
    """
    today = today or date.today()
    try:
        day = date.fromisoformat(str(trade_date))
    except ValueError:
        day = today
    return [day.isoformat(), day.isoformat()]


def get_transaction_search_payload(trade_id, window, offset=0, limit=20):
    """Return the body that searches the ledger for one injected trade id.

    The request the ledger screen sends: sourceTradeId CONTAINS the id, trade
    date within window, newest first. CONTAINS also matches longer ids that hold
    this one, so find_trade_by_trade_id() keeps exact matches only.

    :param window: [start, end] trade dates, from trade_date_window().
    :param offset: index of the first record to return; find_trade_by_trade_id()
        advances it a page at a time.
    """
    return {
        'filters': [
            {'fieldName': 'sourceTradeId', 'operator': 'CONTAINS', 'value': [trade_id]},
            {'fieldName': 'tradeDate', 'operator': 'BETWEEN', 'value': window},
        ],
        'sortBy': [{'fieldName': 'createdTimestamp', 'order': 'DESC'}],
        'paging': {'offset': offset, 'limit': limit},
    }
