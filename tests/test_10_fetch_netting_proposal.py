# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 10: fetch the settlement's netting proposals and save their link ids.

Lists the affirmation-service proposals whose proposalKey is the settlementId
step 09 saved, as Domain A's trading party (reqPartyId, the tenantId) sees
them. One key can hold the proposals of several netting groups, so the
OUTGOING proposal is the one raised for the group step 09 found (its
entityId is nettingGroupIdString), and the INCOMING proposal is the
counterparty's side of the same affirmation (the same affirmationId). Their
proposalLinkIds are saved to run state as outgoingProposalLinkId and
incomingProposalLinkId, which step 11 matches as Domain A and step 11b, reversed,
as Domain B.
"""
import types

import pytest

from api.payloads.proposal_payload import get_proposal_fetch_payload
from api.services.base_service import is_ok
from api.services.proposal_service import ProposalService
from utils.api_logger import log_ids
from utils.describe import netted_line
from utils.helpers import is_number
from utils.state_helper import StateHelper
from utils.wait import GIVE_UP, wait_until

# How long the group's proposals get to appear, and how often to check.
# Polled rather than slept; override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
PROPOSAL_TIMEOUT_SECONDS = 30
PROPOSAL_POLL_SECONDS = 3

OUTGOING = 'OUTGOING'
INCOMING = 'INCOMING'


def latest(proposals):
    """Return the proposal with the highest proposalId, or None if there is none."""
    return max(proposals, key=lambda p: p.get('proposalId') or 0, default=None)


def find_proposal_pair(items, netting_group_id):
    """Return (outgoing, incoming) for one netting group; either is None if absent.

    The OUTGOING proposal is the one whose entityId is the netting group. The
    INCOMING proposal is the counterparty's side of the same affirmation, so it
    carries the same affirmationId. A group proposed more than once yields its
    latest proposal.
    """
    outgoing = latest([p for p in items
                       if p.get('proposalDirection') == OUTGOING
                       and p.get('entityId') == netting_group_id])
    affirmation_id = (outgoing or {}).get('affirmationId')
    if not affirmation_id:
        return outgoing, None

    incoming = latest([p for p in items
                       if p.get('proposalDirection') == INCOMING
                       and p.get('affirmationId') == affirmation_id])
    return outgoing, incoming


def first_page_note(ctx):
    """Say when the response held more proposals than the one page that was read."""
    if is_number(ctx.total_items) and ctx.total_items > len(ctx.items):
        return (f' (the first {len(ctx.items)} of totalItems {ctx.total_items}; later pages '
                'were not read)')
    return ''


# ============================================================================
# TEST SUITE: STEP 10 - FETCH NETTING PROPOSALS (DOMAIN A)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Fetch the settlement's proposals once for every test in this module.

    If a prerequisite is missing, no call is made and res_status stays None, so
    the API test skips while the pre-flight tests report what is missing.
    """
    c = types.SimpleNamespace(res_status=None, response_body=None, server_error=None,
                              items=[], total_items=None, outgoing=None, incoming=None)

    state = StateHelper.get_state()
    # Kept local, not on the namespace: pytest prints the namespace in failure
    # messages, which end up in the HTML report.
    token = state.get('token')
    c.token_present = token is not None
    # The settlement id is the proposal key, and Domain A's trading party is the
    # tenant whose view of the proposals is listed.
    c.proposal_key = state.get('settlementId')
    c.tenant_id = state.get('reqPartyId')
    c.counter_party = state.get('counterPartyId')
    # Domain A's netting group picks the OUTGOING proposal; Domain B's should be
    # the entity of the INCOMING one.
    c.netting_group_id = state.get('nettingGroupIdString')
    c.netting_group_id_b = state.get('nettingGroupIdStringB')

    # Names what the group nets in every assertion message.
    c.trade = netted_line(state)

    if not (token and c.proposal_key and c.tenant_id and c.netting_group_id):
        print('WARN: Skipping API call. Token, Settlement ID, Trading Party or Netting '
              'Group ID is missing from state.')
        return c

    proposal_service = ProposalService(session, domain='A', token=token)
    payload = get_proposal_fetch_payload(c.proposal_key, c.tenant_id)

    print(f'INFO: Fetching proposals for settlement [{c.proposal_key}] as tenant '
          f'[{c.tenant_id}]...')

    # The proposals can lag the confirmed group, so the list is re-read until the
    # group's OUTGOING proposal and its INCOMING counterpart are both present. A
    # rejected call stops the wait at once.
    def fetch_proposal_pair():
        res = proposal_service.fetch_proposals(payload)
        c.res_status = res.status_code

        if not is_ok(res):
            c.server_error = res.text
            return GIVE_UP

        c.response_body = res.json()
        body = c.response_body if isinstance(c.response_body, dict) else {}
        c.items = body.get('items') if isinstance(body.get('items'), list) else []
        c.total_items = body.get('totalItems')

        c.outgoing, c.incoming = find_proposal_pair(c.items, c.netting_group_id)
        return (c.outgoing, c.incoming) if c.outgoing and c.incoming else None

    wait_until(fetch_proposal_pair,
               f'OUTGOING and INCOMING proposals of netting group [{c.netting_group_id}] '
               'on Domain A',
               timeout=PROPOSAL_TIMEOUT_SECONDS, interval=PROPOSAL_POLL_SECONDS)

    print(f'INFO: Domain A Proposal Fetch API responded with status: {c.res_status}')

    if c.server_error is not None:
        print(f'ERROR: API Request Failed with status: {c.res_status}')
        print(f'SERVER REJECTION REASON: {c.server_error}')
    elif c.outgoing and c.incoming:
        outgoing_link = c.outgoing.get('proposalLinkId')
        incoming_link = c.incoming.get('proposalLinkId')

        # Saved together, and only once both proposals are found.
        StateHelper.save_state({
            'outgoingProposalLinkId': outgoing_link,
            'incomingProposalLinkId': incoming_link,
        })
        log_ids('STEP 10 netting proposals (A)',
                affirmationId=c.outgoing.get('affirmationId'),
                outgoingProposalLinkId=outgoing_link,
                incomingProposalLinkId=incoming_link)
        print('INFO: Saved outgoingProposalLinkId and incomingProposalLinkId to state.')
    else:
        print(f'WARN: The OUTGOING / INCOMING proposal pair for netting group '
              f'[{c.netting_group_id}] was not found among {len(c.items)} proposal(s).')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

# --- PRE-FLIGHT CHECKS ---
def test_auth_token_present(ctx):
    """Pre-flight: Auth token is present in state"""
    assert ctx.token_present, \
        ('Domain A token is missing: .state/default.json has no "token". '
         f'Did step 01 run?\n{ctx.trade}')


def test_settlement_id_and_party_present(ctx):
    """Pre-flight: Settlement ID and Trading Party are present in state"""
    assert ctx.proposal_key is not None, \
        ('settlementId is missing from state, so there is no proposal key to fetch the '
         f'proposals by. Did step 09 find netting group [{ctx.netting_group_id}]?'
         f'\n{ctx.trade}')
    assert ctx.tenant_id is not None, \
        ('reqPartyId is missing from state, so there is no tenant to fetch the proposals '
         f'of settlement [{ctx.proposal_key}] as.\n{ctx.trade}')


def test_netting_group_id_present(ctx):
    """Pre-flight: Netting Group ID is present in state"""
    assert ctx.netting_group_id is not None, \
        ('nettingGroupIdString is missing from state, so there is no Domain A netting '
         f'group to pick the proposals of. Did step 09 find one?\n{ctx.trade}')


# --- API HEALTH & SCHEMA ---
def test_fetch_returns_2xx(ctx):
    """API: Proposal fetch endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')
    assert 200 <= ctx.res_status < 300, \
        (f'Fetching the proposals of settlement [{ctx.proposal_key}] as tenant '
         f'[{ctx.tenant_id}] should return a 2xx status, but returned {ctx.res_status}. '
         f'Reason: {ctx.server_error or "none given"}\n{ctx.trade}')


def test_response_has_items_array(ctx):
    """Schema: Response contains a valid items array"""
    if not ctx.response_body:
        pytest.skip('Response data is unavailable.')
    items = ctx.response_body.get('items') if isinstance(ctx.response_body, dict) else None
    assert isinstance(items, list), \
        (f'The proposals of settlement [{ctx.proposal_key}] should carry an "items" list, '
         f'but "items" is {type(items).__name__}.\n{ctx.trade}')


# --- PROPOSAL PAIR ---
def test_outgoing_proposal_found(ctx):
    """Data: The netting group's OUTGOING proposal is in the response"""
    # Fails rather than skips: without it there is no outgoingProposalLinkId.
    if not ctx.res_status:
        problem = 'The proposals were not fetched: a pre-flight check above failed.'
    elif ctx.server_error is not None:
        problem = (f'The proposal fetch was rejected with status {ctx.res_status}, so there '
                   'is no proposal to pick.')
    else:
        groups = ', '.join(sorted({str(p.get('entityId')) for p in ctx.items
                                   if p.get('proposalDirection') == OUTGOING})) or 'none'
        problem = (f'No OUTGOING proposal for Domain A netting group [{ctx.netting_group_id}] '
                   f'is among the {len(ctx.items)} proposal(s) of settlement '
                   f'[{ctx.proposal_key}]{first_page_note(ctx)}. The OUTGOING proposals are '
                   f'for netting groups [{groups}].')
    assert ctx.outgoing is not None, f'{problem}\n{ctx.trade}'


def test_incoming_proposal_found(ctx):
    """Data: The INCOMING proposal of the same affirmation is in the response"""
    if not ctx.outgoing:
        pytest.skip(f'No OUTGOING proposal was found for netting group [{ctx.netting_group_id}].')
    assert ctx.incoming is not None, \
        (f'No INCOMING proposal carries affirmationId [{ctx.outgoing.get("affirmationId")}] '
         f'of OUTGOING proposal [{ctx.outgoing.get("proposalId")}] (Domain A netting group '
         f'[{ctx.netting_group_id}]), among the {len(ctx.items)} proposal(s) of settlement '
         f'[{ctx.proposal_key}]{first_page_note(ctx)}.\n{ctx.trade}')


def test_proposal_link_ids_present(ctx):
    """State Persistence: Both proposalLinkIds are present and distinct"""
    if not (ctx.outgoing and ctx.incoming):
        pytest.skip('The OUTGOING and INCOMING proposals were not both found.')
    outgoing_link = ctx.outgoing.get('proposalLinkId')
    incoming_link = ctx.incoming.get('proposalLinkId')
    assert outgoing_link, \
        (f'OUTGOING proposal [{ctx.outgoing.get("proposalId")}] (netting group '
         f'[{ctx.netting_group_id}]) has no proposalLinkId, so there is no '
         f'outgoingProposalLinkId to save.\n{ctx.trade}')
    assert incoming_link, \
        (f'INCOMING proposal [{ctx.incoming.get("proposalId")}] (affirmationId '
         f'[{ctx.incoming.get("affirmationId")}]) has no proposalLinkId, so there is no '
         f'incomingProposalLinkId to save.\n{ctx.trade}')
    assert outgoing_link != incoming_link, \
        (f'The OUTGOING and INCOMING proposals should carry different proposalLinkIds, '
         f'but both carry [{outgoing_link}].\n{ctx.trade}')


def test_incoming_proposal_is_domain_b_group(ctx):
    """Data: The INCOMING proposal is for the Domain B netting group"""
    if not ctx.incoming:
        pytest.skip('No INCOMING proposal was found.')
    if not ctx.netting_group_id_b:
        pytest.skip('nettingGroupIdStringB is absent from state; nothing to compare against.')
    entity_id = ctx.incoming.get('entityId')
    assert entity_id == ctx.netting_group_id_b, \
        (f'INCOMING proposal [{ctx.incoming.get("proposalId")}] (affirmationId '
         f'[{ctx.incoming.get("affirmationId")}]) should be for Domain B netting group '
         f'[{ctx.netting_group_id_b}], but its entityId is [{entity_id}].\n{ctx.trade}')


# --- PARTIES ---
def test_outgoing_proposal_parties(ctx):
    """Business Logic: The OUTGOING proposal is sent by the party to the counterparty"""
    if not ctx.outgoing:
        pytest.skip(f'No OUTGOING proposal was found for netting group [{ctx.netting_group_id}].')
    if not ctx.counter_party:
        pytest.skip('counterPartyId is absent from state; nothing to compare against.')
    sender, receiver = ctx.outgoing.get('sender'), ctx.outgoing.get('receiver')
    assert (sender, receiver) == (ctx.tenant_id, ctx.counter_party), \
        (f'OUTGOING proposal [{ctx.outgoing.get("proposalId")}] (netting group '
         f'[{ctx.netting_group_id}]) should be sent by the injected party [{ctx.tenant_id}] '
         f'to the counterparty [{ctx.counter_party}], but is sent by [{sender}] to '
         f'[{receiver}].\n{ctx.trade}')


def test_incoming_proposal_parties(ctx):
    """Business Logic: The INCOMING proposal is sent by the counterparty to the party"""
    if not ctx.incoming:
        pytest.skip('No INCOMING proposal was found.')
    if not ctx.counter_party:
        pytest.skip('counterPartyId is absent from state; nothing to compare against.')
    sender, receiver = ctx.incoming.get('sender'), ctx.incoming.get('receiver')
    assert (sender, receiver) == (ctx.counter_party, ctx.tenant_id), \
        (f'INCOMING proposal [{ctx.incoming.get("proposalId")}] (affirmationId '
         f'[{ctx.incoming.get("affirmationId")}]) should be sent by the counterparty '
         f'[{ctx.counter_party}] to the injected party [{ctx.tenant_id}], but is sent by '
         f'[{sender}] to [{receiver}].\n{ctx.trade}')
