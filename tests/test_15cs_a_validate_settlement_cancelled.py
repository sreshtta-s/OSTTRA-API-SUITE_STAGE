# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 15cs_a: check the Domain A settlement ledger shows the settlement as cancelled.

Runs after steps 13cs_a / 13cs_b have cancelled the settlements. Lists the
settlements on the valueDate step 04 recorded, through the same getAll as step
15, until the entry for step 09's settlementId reports CANCELLED. Only the
status is checked. Writes nothing back to state. Step 15cs_b makes the same
check on Domain B (settlementIdB).

runner.py runs it straight after step 13cs_b (SETTLEMENT_CANCEL_STEPS), only
for a scenario that sets "cancelSettlement": true.
"""
import types

import pytest

from api.payloads.settlement_ledger_payload import get_ledger_payload
from api.services.base_service import is_ok
from api.services.settlement_service import SettlementService
from utils.describe import netted_line
from utils.state_helper import StateHelper
from utils.wait import GIVE_UP, wait_until

# The status the settlement ledger gives a cancelled settlement, on both domains.
CANCELLED = 'CANCELLED'

# Each domain's state keys: its token, its settlement id (from step 09 / 09b)
# and its netting group id.
STATE_KEYS = {
    'A': ('token', 'settlementId', 'nettingGroupIdString'),
    'B': ('tokenB', 'settlementIdB', 'nettingGroupIdStringB'),
}

# How long the ledger gets to apply the cancel, and how often to check. Checks
# stop as soon as it is cancelled. Override with WAIT_TIMEOUT_SECONDS /
# WAIT_INTERVAL_SECONDS.
CANCEL_TIMEOUT_SECONDS = 60
CANCEL_POLL_SECONDS = 3


# ============================================================================
# TEST SUITE: VALIDATE SETTLEMENT CANCELLED (DOMAIN A)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Wait for the settlement to show as cancelled on Domain A, once for every test in this module."""
    return find_cancelled_settlement(session, 'A')


def find_cancelled_settlement(session, domain):
    """List one domain's settlements until this settlement shows as CANCELLED.

    Shared with step 15cs_b, which differs only in the domain it passes.

    :returns: the namespace every test in this module reads.
    """
    token_key, settlement_key, group_key = STATE_KEYS[domain]
    c = types.SimpleNamespace(domain=domain, res_status=None, ledger_item=None)

    state = StateHelper.get_state()
    # Kept local, not on the namespace: pytest prints the namespace in failure
    # messages, which end up in the HTML report.
    token = state.get(token_key)
    c.token_present = token is not None
    c.settlement_key = settlement_key
    c.target_settlement_id = state.get(settlement_key)
    c.value_date = state.get('valueDate')
    c.settlement = (f'Domain {domain} settlement [{c.target_settlement_id}] (netting group '
                    f'[{state.get(group_key)}])')
    c.trade = netted_line(state)

    if not (token and c.target_settlement_id and c.value_date):
        print(f'WARN: Skipping Domain {domain} settlement cancel check. Missing '
              f'{token_key}, {settlement_key} or valueDate.')
        return c

    ledger_service = SettlementService(session, domain=domain, token=token)
    payload = get_ledger_payload(c.value_date, c.target_settlement_id)

    # The cancel is applied asynchronously, so the list is re-read until this
    # settlement reports CANCELLED. An entry in any other status is kept, so a
    # timeout reports the status it saw.
    def settlement_cancelled():
        res = ledger_service.get_all_settlements(payload)
        c.res_status = res.status_code
        if not is_ok(res):
            return GIVE_UP

        items = res.json().get('items') or []
        # Other settlements on the value date are listed too; match this one by id.
        c.ledger_item = next(
            (item for item in items if item.get('settlementInstanceId') == c.target_settlement_id),
            None,
        )
        return c.ledger_item if (c.ledger_item or {}).get('settlementStatus') == CANCELLED else None

    wait_until(settlement_cancelled,
               f'settlement [{c.target_settlement_id}] to reach {CANCELLED} on Domain {domain}',
               timeout=CANCEL_TIMEOUT_SECONDS, interval=CANCEL_POLL_SECONDS)

    status = (c.ledger_item or {}).get('settlementStatus', 'NOT FOUND')
    print(f'INFO: Domain {domain} Settlement Ledger API responded with status: {c.res_status}; '
          f'settlement [{c.target_settlement_id}] -> {status}')
    return c


# --- STATUS ---
def test_settlement_status_is_cancelled(ctx):
    """Settlement Status on the ledger is CANCELLED"""
    if not ctx.res_status:
        pytest.skip(f'The Domain {ctx.domain} settlement ledger was not searched: '
                    f'{ctx.settlement_key}, valueDate or the token is missing from state.')
    assert ctx.res_status == 200, \
        (f'Domain {ctx.domain} settlement ledger for value date [{ctx.value_date}] should '
         f'return 200, but returned {ctx.res_status}. It was searched for '
         f'{ctx.settlement}.\n{ctx.trade}')
    assert ctx.ledger_item is not None, \
        (f'{ctx.settlement} is not on the Domain {ctx.domain} settlement ledger for value '
         f'date [{ctx.value_date}].\n{ctx.trade}')
    status = ctx.ledger_item.get('settlementStatus')
    assert status == CANCELLED, \
        (f'{ctx.settlement}: settlementStatus should be {CANCELLED} once step 13cs has '
         f'cancelled it, but was still [{status}] when the wait ended.\n{ctx.trade}')
