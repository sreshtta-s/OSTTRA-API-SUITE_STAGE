"""Step 15b: wait for the Domain B settlement to reach SETTLEMENT_COMPLETE.

Polls the Domain B settlement ledger for the valueDate step 04 recorded until
the entry for step 09b's settlementIdB reports SETTLEMENT_COMPLETE. The entry
is also checked for a PVP settlement on the injected trade's value date, with
the parties seen from the counterparty's side. It writes nothing to run state:
the Excel report's Settlement State column comes from step 15.
"""
import types

import pytest

from api.payloads.settlement_ledger_payload import get_ledger_payload
from api.services.base_service import is_ok
from api.services.settlement_service import SettlementService
from utils.api_logger import log_ids
from utils.describe import netted_line
from utils.state_helper import StateHelper
from utils.wait import GIVE_UP, wait_until

RED = '\x1b[31m'
RESET = '\x1b[0m'

# How long settlement gets to reach SETTLEMENT_COMPLETE, and how often to check.
# Polled rather than slept; override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
SETTLEMENT_TIMEOUT_SECONDS = 120
SETTLEMENT_POLL_SECONDS = 3
COMPLETE = 'SETTLEMENT_COMPLETE'
# The rule set step 02 creates settles PVP.
PVP = 'PVP'


@pytest.fixture(scope='module')
def ctx(session):
    """Wait for the Domain B settlement to complete once for every test in this module.

    If a prerequisite is missing, no call is made and res_status stays None, so
    the API test skips.
    """
    c = types.SimpleNamespace(res_status=None, ledger_item=None)

    state = StateHelper.get_state()
    # Domain B uses its own settlement id (from step 09b). There is no fallback
    # to Domain A's, which belongs to a different group.
    c.target_settlement_id = state.get('settlementIdB')
    c.value_date = state.get('valueDate')
    # Recorded by step 04 from the injected payload, so they are right in every
    # injection mode.
    c.expected_party = state.get('reqPartyId')
    c.expected_counter_party = state.get('counterPartyId')
    # Names the settlement, its netting group and what it nets in every
    # assertion message.
    c.netting_group_id = state.get('nettingGroupIdStringB')
    c.settlement = (f'Domain B settlement [{c.target_settlement_id}] (netting group '
                    f'[{c.netting_group_id}])')
    c.trade = netted_line(state)

    # Kept local, not on the namespace: pytest prints the namespace in failure
    # messages, which end up in the HTML report.
    token_b = state.get('tokenB')

    if not (c.target_settlement_id and c.value_date and token_b):
        print('WARN: Skipping test. Settlement ID, Value Date, or Token B missing from state.')
        return c

    ledger_service = SettlementService(session, domain='B', token=token_b)
    payload = get_ledger_payload(c.value_date, c.target_settlement_id)

    print(f'INFO: Fetching Ledger for Domain B looking for Settlement ID: '
          f'[{c.target_settlement_id}]')

    # Settlement completes asynchronously, so the ledger is re-read until this
    # settlement reports SETTLEMENT_COMPLETE. An entry in any other status is
    # kept, so a timeout reports the status it saw.
    def settlement_complete():
        res = ledger_service.get_all_settlements(payload)
        c.res_status = res.status_code

        if not is_ok(res):
            print(f'{RED}ERROR: Domain B Ledger API Failed. Status: {res.status_code}{RESET}')
            return GIVE_UP

        items = res.json().get('items') or []

        # Other settlements on the value date are listed too; match this one by id.
        c.ledger_item = next(
            (item for item in items if item.get('settlementInstanceId') == c.target_settlement_id),
            None,
        )
        return c.ledger_item if (c.ledger_item or {}).get('settlementStatus') == COMPLETE else None

    wait_until(settlement_complete,
               f'settlement [{c.target_settlement_id}] to reach {COMPLETE} on Domain B',
               timeout=SETTLEMENT_TIMEOUT_SECONDS, interval=SETTLEMENT_POLL_SECONDS)

    print(f'INFO: Domain B Settlement Ledger API responded with status: {c.res_status}')

    if c.ledger_item:
        log_ids('STEP 15b ledger (B)',
                settlementId=c.target_settlement_id,
                settlementState=c.ledger_item.get('settlementStatus'),
                valueDate=c.value_date)

    return c


def missing_entry(ctx):
    """Say why the settlement has no ledger entry: not on the ledger, or never looked up."""
    if ctx.res_status:
        return (f'{ctx.settlement} is not on the Domain B settlement ledger for value date '
                f'[{ctx.value_date}].\n{ctx.trade}')
    return (f'The Domain B settlement ledger was not searched for {ctx.settlement}: '
            f'settlementIdB, valueDate or the Domain B token is missing from state.'
            f'\n{ctx.trade}')


def test_ledger_returns_200(ctx):
    """API: Ledger endpoint returns a 200 Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed.')
    assert ctx.res_status == 200, \
        (f'Domain B settlement ledger for value date [{ctx.value_date}] should return '
         f'200, but returned {ctx.res_status}. It was searched for {ctx.settlement}.'
         f'\n{ctx.trade}')


def test_ledger_entry_exists(ctx):
    """Ledger entry successfully exists for the Settlement ID"""
    assert ctx.ledger_item is not None, missing_entry(ctx)


def test_settlement_status_complete(ctx):
    """Final Settlement Status is strictly SETTLEMENT_COMPLETE"""
    assert ctx.ledger_item is not None, missing_entry(ctx)
    status = ctx.ledger_item.get('settlementStatus')
    assert status == COMPLETE, \
        (f'{ctx.settlement}: settlementStatus should be {COMPLETE}, but is [{status}].'
         f'\n{ctx.trade}')


# --- SETTLEMENT DETAILS (AS INJECTED) ---
# Skipped rather than failed without an entry: test_ledger_entry_exists already
# reports that.
def test_settlement_type_is_pvp(ctx):
    """Business Logic: Settlement Type is PVP"""
    if not ctx.ledger_item:
        pytest.skip(f'{ctx.settlement} was not found on the Domain B settlement ledger.')
    settlement_type = ctx.ledger_item.get('settlementType')
    assert settlement_type == PVP, \
        (f'{ctx.settlement}: settlementType should be {PVP}, but is [{settlement_type}].'
         f'\n{ctx.trade}')


# Domain B sees the settlement from the counterparty's side, so its trading
# party is the injected counterparty and its counterparty the injected party.
def test_trading_party_is_flipped(ctx):
    """Business Logic: Trading Party matches the injected counterparty (Flipped for Domain B)"""
    if not ctx.ledger_item:
        pytest.skip(f'{ctx.settlement} was not found on the Domain B settlement ledger.')
    trading_party = ctx.ledger_item.get('tradingParty')
    assert trading_party == ctx.expected_counter_party, \
        (f'{ctx.settlement}: tradingParty should be the injected counterparty '
         f'[{ctx.expected_counter_party}], since Domain B sees the settlement from the '
         f'counterparty\'s side, but is [{trading_party}].\n{ctx.trade}')


def test_counterparty_is_flipped(ctx):
    """Business Logic: Counterparty matches the injected party (Flipped for Domain B)"""
    if not ctx.ledger_item:
        pytest.skip(f'{ctx.settlement} was not found on the Domain B settlement ledger.')
    counterparty = ctx.ledger_item.get('counterparty')
    assert counterparty == ctx.expected_party, \
        (f'{ctx.settlement}: counterparty should be the injected party '
         f'[{ctx.expected_party}], since Domain B sees the settlement from the '
         f'counterparty\'s side, but is [{counterparty}].\n{ctx.trade}')


def test_value_date_matches_injected(ctx):
    """Business Logic: Value Date matches the injected trade's value date"""
    if not ctx.ledger_item:
        pytest.skip(f'{ctx.settlement} was not found on the Domain B settlement ledger.')
    value_date = ctx.ledger_item.get('valueDate')
    assert value_date == ctx.value_date, \
        (f'{ctx.settlement}: valueDate should be the injected trade\'s value date '
         f'[{ctx.value_date}], but is [{value_date}].\n{ctx.trade}')
