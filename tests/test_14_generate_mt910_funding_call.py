# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 14: fund the settlement by pumping MT910s for its funding-call tasks.

After steps 13-13c initiate settlement, the simulator raises one funding-call
task per currency to be funded. This step finds the tasks whose payload names
step 09's settlementId and asks the simulator to generate MT910s for them, with
the valueDate step 04 recorded. Steps 15 and 15b then wait for completion.
"""
import json
import time
import types

import pytest

from api.payloads.funding_call_payload import get_funding_call_payload
from api.services.base_service import is_ok
from api.services.funding_call_service import FundingCallService
from utils.api_logger import log_ids
from utils.describe import netted_line
from utils.wait import GIVE_UP, wait_until
from utils.state_helper import StateHelper

RED = '\x1b[31m'
RESET = '\x1b[0m'

# How long the simulator gets to raise this settlement's tasks, and how often to check.
# Polled rather than slept; override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
TASKS_TIMEOUT_SECONDS = 5
TASKS_POLL_SECONDS = 5

# Fixed pause between finding the tasks and pumping them; its use below says
# why it is not polled.
LEDGER_SYNC_SECONDS = 5


# ============================================================================
# TEST SUITE: STEP 14 - GENERATE MT910 (FUNDING CALL PUMP)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Pump MT910s for the settlement's tasks once for every test in this module.

    If a prerequisite is missing or no task is found, no pump is made and
    res_status stays None, so the pump test skips.
    """
    c = types.SimpleNamespace(res_status=None, response_body=None, rejection=None,
                              discovered_message_ids=[])

    state = StateHelper.get_state()
    c.target_settlement_id = state.get('settlementId')
    c.value_date = state.get('valueDate')
    # Names the settlement's netting group, and what it nets, in every assertion
    # message.
    c.netting_group_id = state.get('nettingGroupIdString')
    c.trade = netted_line(state)

    if not (c.target_settlement_id and c.value_date):
        print('WARN: Skipping API call. Settlement ID or Value Date is missing from state.')
        return c

    funding_call_service = FundingCallService(session)

    # ========================================================================
    # STEP A: GET MESSAGE IDs FROM SIMULATOR
    # ========================================================================
    print(f'INFO: Fetching simulator tasks to find message_ids for settlement: '
          f'{c.target_settlement_id}')

    # The simulator raises its funding-call tasks shortly after settlement is
    # initiated, so the list is re-read until this settlement's tasks appear.
    def find_message_ids():
        task_res = funding_call_service.get_funding_tasks()
        if not is_ok(task_res):
            print(f'{RED}ERROR: Failed to fetch tasks. Status: {task_res.status_code}{RESET}')
            return GIVE_UP

        # Each task's payload is a JSON string that carries its settlement_id.
        found = []
        for task in task_res.json():
            if task.get('payload'):
                try:
                    parsed_payload = json.loads(task['payload'])
                    if parsed_payload.get('settlement_id') == c.target_settlement_id:
                        found.append(task['message_id'])
                except (json.JSONDecodeError, TypeError):
                    # Skip a task whose payload does not parse; raising here would
                    # fail the whole step over one malformed task.
                    pass

        c.discovered_message_ids = found
        return found or None

    wait_until(find_message_ids,
               f'simulator tasks for settlement [{c.target_settlement_id}]',
               timeout=TASKS_TIMEOUT_SECONDS, interval=TASKS_POLL_SECONDS)

    if not c.discovered_message_ids:
        print(f'{RED}ERROR: Found no tasks for settlement ID: {c.target_settlement_id}{RESET}')
        return c

    # A fixed sleep, not a poll: nothing the simulator exposes says the ledger
    # has caught up with the tasks just read, so there is no condition to wait on.
    time.sleep(LEDGER_SYNC_SECONDS)

    # The simulator raises one task per currency to be funded, and the second can
    # land a second after the first. The wait above returns on the first read
    # that finds any, so the list is read again after the pause and new tasks are
    # added. Pumping only the first leaves the settlement parked at FUNDING_CALL.
    first_seen = c.discovered_message_ids
    find_message_ids()
    c.discovered_message_ids = first_seen + [
        message_id for message_id in c.discovered_message_ids if message_id not in first_seen
    ]

    log_ids('STEP 14 funding call',
            settlementId=c.target_settlement_id,
            message_ids=c.discovered_message_ids,
            valueDate=c.value_date)
    print(f'INFO: Successfully extracted {len(c.discovered_message_ids)} message_ids: '
          f'[{", ".join(str(i) for i in c.discovered_message_ids)}]')

    # ========================================================================
    # STEP B: BUILD STRICT PAYLOAD AND PUMP MT910
    # ========================================================================
    payload = get_funding_call_payload(c.discovered_message_ids, c.value_date)

    # Printed in full so a rejected pump can be checked against the exact body sent.
    print('\n=== STRICT PAYLOAD VERIFICATION ===')
    print(json.dumps(payload, indent=2))
    print('===================================\n')

    res = funding_call_service.generate_mt910(payload)
    c.res_status = res.status_code
    print(f'INFO: MT910 Funding Call Pump API responded with status: {c.res_status}')

    if is_ok(res):
        text = res.text
        try:
            c.response_body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # The simulator may reply with plain text rather than JSON.
            c.response_body = {'rawText': text}
        print(f'INFO: MT910 Generated Successfully. Server replied: {text}')
    else:
        error_text = c.rejection = res.text
        try:
            # Kept so test_pump_returns_2xx can quote the server's rejection message.
            c.response_body = json.loads(error_text)
        except json.JSONDecodeError:
            c.response_body = {'message': error_text}
        print(f'{RED}========================================={RESET}')
        print(f'{RED}ERROR: Simulator API Request Failed{RESET}')
        print(f'{RED}STATUS: {c.res_status}{RESET}')
        print(f'{RED}REJECTION REASON: {res.text}{RESET}')
        print(f'{RED}========================================={RESET}')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def test_settlement_id_and_value_date_present(ctx):
    """Pre-flight: Settlement ID and Value Date are present in state"""
    assert ctx.target_settlement_id is not None, \
        ('settlementId is missing from state, so there is no settlement to fund. Did '
         f'step 09 find netting group [{ctx.netting_group_id}]?\n{ctx.trade}')
    assert ctx.value_date is not None, \
        (f'valueDate is missing from state, so the MT910s for settlement '
         f'[{ctx.target_settlement_id}] have no value date. Did step 04 record it?'
         f'\n{ctx.trade}')


def test_message_ids_discovered(ctx):
    """Simulator: Fetched valid message_ids for current settlement"""
    if ctx.target_settlement_id and ctx.value_date:
        problem = (f'The simulator raised no funding-call tasks for settlement '
                   f'[{ctx.target_settlement_id}] (netting group [{ctx.netting_group_id}], '
                   f'value date [{ctx.value_date}]), so there is no message_id to pump an '
                   f'MT910 for.')
    else:
        problem = 'Simulator tasks were not fetched: a pre-flight check above failed.'
    assert len(ctx.discovered_message_ids) > 0, f'{problem}\n{ctx.trade}'


def test_pump_returns_2xx(ctx):
    """API: Funding Call Pump endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')
    body = ctx.response_body if isinstance(ctx.response_body, dict) else {}
    message = body.get('message') or ctx.rejection or 'Unknown'
    message_ids = ', '.join(str(i) for i in ctx.discovered_message_ids)
    assert 200 <= ctx.res_status < 300, \
        (f'Pumping MT910s for settlement [{ctx.target_settlement_id}] (message_ids '
         f'[{message_ids}], value date [{ctx.value_date}]) should return a 2xx status, '
         f'but returned {ctx.res_status}. Reason: {message}\n{ctx.trade}')
