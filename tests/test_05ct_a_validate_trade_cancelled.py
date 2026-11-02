# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 05ct_a: check the Domain A ledger shows step 04's trade as cancelled.

Runs after step 04 cancel has resent the trade with metaData.status
"Cancelled". Searches the same trade-management ledger as step 05, by the
injected trade id, until the record's status turns CANCELLED, then asserts
it. The cancel updates the trade's own record (its version goes up) rather
than booking a second one, so exactly one record must carry the id. Writes
nothing back to state. Step 05ct_b makes the same checks on Domain B.

runner.py runs it after step 05b, straight after step 04 cancel, only for a
trade run_pipeline() is told to cancel; for a netting row's cancelTrade, straight
after step 04 cancel, once the group step it names has run (07b in TC034; 08b
in TC035, where the closed group refuses the cancel and this step is expected
to fail).
"""
import types

import pytest

from api.services.transaction_service import TransactionService, describe_lookup
from utils.describe import trade_line
from utils.state_helper import StateHelper
from utils.wait import GIVE_UP, wait_until

# The status the ledger gives a cancelled trade, on both domains.
LEDGER_CANCELLED_STATUS = 'CANCELLED'

# The state key holding each domain's token, as steps 05 / 05b read it.
TOKEN_KEYS = {'A': 'token', 'B': 'tokenB'}

# How long the ledger gets to apply the cancel, and how often to check: every
# 3s for up to 15s (checks at 0, 3, 6, 9, 12 and 15s). Checks stop as soon as
# it is cancelled. Override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
CANCEL_TIMEOUT_SECONDS = 15
CANCEL_POLL_SECONDS = 3


# ============================================================================
# TEST SUITE: VALIDATE TRADE CANCELLED ON LEDGER (DOMAIN A)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Wait for the trade to show as cancelled on the Domain A ledger, once for every test in this module."""
    return find_cancelled_trade(session, 'A')


def find_cancelled_trade(session, domain):
    """Search one domain's ledger for the injected trade until it shows as CANCELLED.

    Shared with step 05ct_b, which differs only in the domain it passes.

    :returns: the namespace every test in this module reads.
    """
    c = types.SimpleNamespace(domain=domain, res_status=None, matched_trade=None,
                              lookup=None, lookup_detail=None)

    state = StateHelper.get_state()
    # Kept local, not on the namespace: pytest prints the namespace in failure
    # messages, which end up in the HTML report.
    token = state.get(TOKEN_KEYS[domain])
    c.token_present = token is not None
    c.expected_trade_id = state.get('injectedTradeId')
    c.trade = trade_line(state)
    c.record = f'Domain {domain} ledger'

    if not token or not c.expected_trade_id:
        print(f'WARN: Skipping Domain {domain} cancel check. Missing '
              f'{TOKEN_KEYS[domain]} or injectedTradeId.')
        return c

    txn_service = TransactionService(session, domain=domain, token=token)

    # The trade is listed before the cancel is applied, so the search repeats
    # until its status is CANCELLED, not merely until it appears. c.lookup holds
    # the latest attempt, reported even when the wait gives up.
    def look_for_cancelled_trade():
        c.lookup = txn_service.find_trade_by_trade_id(c.expected_trade_id,
                                                      trade_date=state.get('tradeDate'))
        if not c.lookup.ok:
            return GIVE_UP
        trade = c.lookup.trade
        return c.lookup if trade and trade.get('status') == LEDGER_CANCELLED_STATUS else None

    wait_until(look_for_cancelled_trade,
               f'Domain {domain} ledger to show trade [{c.expected_trade_id}] as '
               f'{LEDGER_CANCELLED_STATUS}',
               timeout=CANCEL_TIMEOUT_SECONDS, interval=CANCEL_POLL_SECONDS)

    lookup = c.lookup
    c.res_status = lookup.status
    c.matched_trade = lookup.trade
    c.lookup_detail = describe_lookup(lookup)
    if c.matched_trade:
        c.record = (f'Domain {domain} ledger record [tradeRecordId '
                    f'{c.matched_trade.get("tradeRecordId")}, transactionId '
                    f'{c.matched_trade.get("transactionId")}]')

    print(f'INFO: Domain {domain} Trade Validation API responded with status: {c.res_status}')
    if lookup.ok:
        status = c.matched_trade.get('status') if c.matched_trade else 'NOT FOUND'
        print(f'INFO: Trade ID [{c.expected_trade_id}] -> {status} on Domain {domain}; '
              f'{c.lookup_detail}')
    else:
        print(f'ERROR: Domain {domain} lookup failed ({c.res_status}): {lookup.error_text}')

    return c


# --- PRE-FLIGHT ---
def test_auth_token_present(ctx):
    """Verify auth token is present in state"""
    assert ctx.token_present, \
        (f'Domain {ctx.domain} token is missing: .state/default.json has no '
         f'"{TOKEN_KEYS[ctx.domain]}". Did step 01 run?\n{ctx.trade}')


def test_injected_trade_id_present(ctx):
    """Verify injected Trade ID is present in state"""
    assert ctx.expected_trade_id is not None, \
        (f'injectedTradeId is missing from state, so there is no trade to check on '
         f'the Domain {ctx.domain} ledger. Did step 04 run and get a 2xx?\n{ctx.trade}')


# --- API HEALTH ---
def test_request_returns_2xx(ctx):
    """Verify request returns a 2xx success status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed.')
    assert 200 <= ctx.res_status < 300, \
        (f'Domain {ctx.domain} ledger search for trade [{ctx.expected_trade_id}] should '
         f'return a 2xx status, but returned {ctx.res_status}. '
         f'Reason: {ctx.lookup.error_text or "none given"}\n{ctx.trade}')


# --- TRADE EXISTENCE ---
def test_trade_found_on_ledger(ctx):
    """Verify injected Trade ID is found on the ledger"""
    assert ctx.matched_trade is not None, \
        (f'Trade [{ctx.expected_trade_id}] was not found on the Domain {ctx.domain} ledger. '
         f'{ctx.lookup_detail or "The ledger was not searched: a pre-flight check above failed."}\n{ctx.trade}')


def test_trade_appears_exactly_once(ctx):
    """Verify the cancel left exactly one ledger record carrying the injected Trade ID"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain {ctx.domain} ledger.')

    count = len(ctx.lookup.matches)
    transaction_ids = ', '.join(str(m.get('transactionId')) for m in ctx.lookup.matches)
    assert count == 1, \
        (f'Trade [{ctx.expected_trade_id}] should appear on the Domain {ctx.domain} ledger '
         f'exactly once after the cancel, but {count} records carry it (transactionIds: '
         f'{transaction_ids}). A second record means the cancel was booked as a new '
         f'trade instead of cancelling this one. {ctx.lookup_detail}\n{ctx.trade}')


# --- STATUS ---
def test_trade_status_is_cancelled(ctx):
    """Verify the trade's status on the ledger is CANCELLED"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain {ctx.domain} ledger.')
    status = ctx.matched_trade.get('status')
    assert status == LEDGER_CANCELLED_STATUS, \
        (f'{ctx.record}: status should be {LEDGER_CANCELLED_STATUS} once step 04 cancel '
         f'has resent the trade as Cancelled, but was still [{status}] when the wait '
         f'ended (version {ctx.matched_trade.get("version")}).\n{ctx.trade}')
