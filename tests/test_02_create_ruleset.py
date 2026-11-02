# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 02: list the Domain A rule-center folders for step 03.

Each bilateral folder holds the netting rule sets for one party pair. Its id,
name, partyId and participants are saved to run state as ruleCenterFolders;
step 03 lists each folder's rule sets and writes their rule ids into
data/trade_scenarios.py NETTING_RULES.

Not in runner.py's STEP_TOKENS. Run it with steps 01 and 03 whenever the rules
are recreated in the rule center:

    AUTH_DOMAINS=A python -m pytest tests/test_01_auth.py \\
        tests/test_02_create_ruleset.py tests/test_03_validate_ruleset.py
"""
import types

import pytest

from api.payloads.folder_payload import get_folder_list_payload
from api.services.base_service import is_ok
from api.services.folder_service import FolderService
from utils.state_helper import StateHelper


# ============================================================================
# TEST SUITE: FETCH RULE-CENTER FOLDERS
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """List the rule-center folders once for every test in this module."""
    c = types.SimpleNamespace(res_status=None, response_data=None, folders=[], skipped=[])

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in failure
    # messages, which end up in the HTML report.
    c.token_present = state.get('token') is not None
    if not c.token_present:
        return c

    res = FolderService(session, state['token']).get_folders(get_folder_list_payload())
    c.res_status = res.status_code
    if not is_ok(res):
        return c

    c.response_data = res.json()

    # A NETTING_RULES entry pairs one party with one counterparty, so only a
    # folder shared by exactly two participants can hold its rules.
    for folder in c.response_data.get('items') or []:
        participants = folder.get('participants') or []
        if folder.get('id') is not None and len(participants) == 2:
            c.folders.append({
                'id': folder['id'],
                'name': folder.get('name'),
                'partyId': folder.get('partyId'),
                'participants': participants,
            })
        else:
            c.skipped.append(f'{folder.get("id")} ({folder.get("name")})')

    return c


# --- PRE-FLIGHT CHECKS ---
def test_auth_token_present(ctx):
    """Verify auth token is present in state"""
    assert ctx.token_present, 'Auth token is missing. Did test_01_auth.py run?'


# --- API STATUS ---
def test_status_200(ctx):
    """Verify folder list returns HTTP status 200 OK"""
    if ctx.res_status is None:
        pytest.skip('API call was not executed due to missing prerequisites.')
    assert ctx.res_status == 200, f'Expected HTTP Status 200 OK, got {ctx.res_status}'


# --- SCHEMA VALIDATIONS ---
def test_items_is_array(ctx):
    """Verify response items property is a valid array"""
    if not ctx.response_data:
        pytest.skip('Response data is unavailable.')
    assert isinstance(ctx.response_data.get('items'), list), 'Items should be an array'


def test_every_folder_listed(ctx):
    """Verify every folder fits in one page"""
    if not ctx.response_data:
        pytest.skip('Response data is unavailable.')
    listed = len(ctx.response_data.get('items') or [])
    total = ctx.response_data.get('totalItems')
    assert listed == total, \
        (f'Listed {listed} of {total} folders. Raise page_size in '
         f'api/payloads/folder_payload.py, or step 03 misses the rest.')


# --- FOLDER SELECTION ---
def test_bilateral_folder_found(ctx):
    """Verify at least one bilateral folder is listed"""
    if not ctx.response_data:
        pytest.skip('Response data is unavailable.')
    if ctx.skipped:
        print(f'Skipped folders without exactly two participants: {", ".join(ctx.skipped)}')
    assert ctx.folders, 'No folder with exactly two participants was listed'


# --- STATE PERSISTENCE ---
def test_folders_saved_to_state(ctx):
    """Save folder ids to state for step 03"""
    if not ctx.folders:
        pytest.skip('No bilateral folder to save.')

    StateHelper.save_state({'ruleCenterFolders': ctx.folders})

    saved = StateHelper.get_state().get('ruleCenterFolders') or []
    assert [folder['id'] for folder in saved] == [folder['id'] for folder in ctx.folders], \
        'ruleCenterFolders in state does not hold the listed folders'

    for folder in ctx.folders:
        print(f'Folder {folder["id"]}: {folder["name"]} {folder["participants"]}')
