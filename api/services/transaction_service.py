"""Trade-management ledger client, used by steps 05 / 05b to find the trade.

find_trade_by_trade_id() searches the ledger by the trade's id, as the ledger
screen does, and returns a TradeLookup; describe_lookup() turns that into the
detail shown in assertion messages.
"""

from api.payloads.transaction_payload import get_transaction_search_payload, trade_date_window
from api.services.base_service import BaseService, is_ok

PREFIX = '/access-gateway/api/trade-management/api/v1/transactions'

# A search by id returns a handful of records, but every page is still read so
# a duplicate on a later page is counted. The page cap only stops a paging flag
# that never turns false from looping.
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 40


class TradeLookup:
    """Result of a paged ledger search, with diagnostics on how much was searched."""

    def __init__(self):
        self.trade = None       # first match, or None if no record matched
        # Every record carrying the trade id, not only the first: a duplicate
        # that should have been dropped appears here as a second entry.
        self.matches = []
        self.first_page = None  # page 0 body, for the schema and pagination assertions
        # Diagnostics, reported by describe_lookup() and the step 05 / 05b logs.
        self.status = None
        self.ok = False
        self.error_text = None
        self.scanned = 0
        self.pages_fetched = 0
        self.total_count = None
        self.capped_out = False
        self.sample_ids = []
        self.window = None      # [start, end] trade dates the search covered


class TransactionService(BaseService):
    """Client for the trade-management transactions ledger on one domain."""

    def get_all_transactions(self, payload):
        """Fetch one page of ledger records; the payload sets the filters and paging."""
        return self.post(f'{PREFIX}/getAll', payload)

    def find_trade_by_trade_id(self, trade_id, trade_date=None, page_size=DEFAULT_PAGE_SIZE,
                               max_pages=DEFAULT_MAX_PAGES):
        """Collect every ledger record for an injected trade id into a TradeLookup.

        The id is the sourcePlatform tradeId step 04 injected, which the ledger
        exposes as sourceTradeId. The ledger is searched for it within
        trade_date_window(trade_date), and pages are read until the results run
        out, so a miss means the ledger holds no record with that id, unless
        max_pages cut the search short (capped_out). A non-2xx response does not
        raise: its status and body are recorded on the result, so callers assert
        on the diagnostics rather than on a bare None.

        The search does not stop at the first hit, because a duplicate that should
        have been dropped is a second record under the same id. `trade` is the
        first match, which the field assertions read.

        :param trade_date: the injected trade's date (YYYY-MM-DD), recorded by step 04.
        """
        result = TradeLookup()
        result.window = trade_date_window(trade_date)

        for page_no in range(max_pages):
            res = self.get_all_transactions(get_transaction_search_payload(
                trade_id, result.window, offset=page_no * page_size, limit=page_size))
            result.status = res.status_code

            if not is_ok(res):
                result.error_text = res.text
                return result
            result.ok = True

            body = res.json()
            result.pages_fetched += 1
            if page_no == 0:
                result.first_page = body
                total = body.get('totalCount')
                # bool is excluded because it is a subclass of int.
                result.total_count = total if isinstance(total, (int, float)) and not isinstance(total, bool) else None

            trades = body.get('trades')
            trades = trades if isinstance(trades, list) else []
            result.scanned += len(trades)
            # The first 10 ids returned, so a miss can show what the search did find.
            if len(result.sample_ids) < 10:
                room = 10 - len(result.sample_ids)
                result.sample_ids.extend(t.get('sourceTradeId') for t in trades[:room])

            # Exact matches only: the CONTAINS filter also returns longer ids
            # that hold this one.
            result.matches.extend(t for t in trades if t.get('sourceTradeId') == trade_id)
            if result.matches and result.trade is None:
                result.trade = result.matches[0]

            # Stop on an empty page even when hasMore is true, so a wrong flag
            # cannot drive empty calls up to the page cap.
            if not trades or body.get('hasMore') is not True:
                return result

            # Reached only when more pages remain; on the last allowed page that
            # means the search was cut short.
            result.capped_out = page_no == max_pages - 1

        return result


def describe_lookup(r):
    """Summarise what a TradeLookup inspected, for use in assertion messages.

    A "not found" failure then states how many records and pages were read and
    whether the page cap cut the search short.
    """
    if not r.ok:
        return f'Ledger call failed with status {r.status}.'
    parts = [f'searched by sourceTradeId with tradeDate {r.window[0]} to {r.window[1]}; '
             f'returned {r.scanned} record(s) across {r.pages_fetched} page(s)']
    if len(r.matches) > 1:
        parts.append(f'found {len(r.matches)} records carrying that trade id')
    if r.total_count is not None:
        parts.append(f'search reports totalCount={r.total_count}')
    if r.capped_out:
        parts.append(
            f'STOPPED at the {DEFAULT_MAX_PAGES}-page cap - more pages remain, '
            'so this may be a false negative'
        )
    if r.sample_ids:
        parts.append('sample sourceTradeIds seen: [' + ', '.join(str(i) for i in r.sample_ids) + ']')
    return '; '.join(parts) + '.'
