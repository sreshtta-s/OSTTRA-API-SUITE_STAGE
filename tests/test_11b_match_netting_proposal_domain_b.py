# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 11b: match the same netting proposals from Domain B's side.

Step 10 saved the link ids as Domain A sees them. Domain B's OUTGOING proposal
is the one Domain A received, so the ids are posted reversed: incomingProposalLinkId
as the outgoing id and outgoingProposalLinkId as the incoming id, to the
affirmation-service match action, as Domain B. Steps 12 and 12b then check both
netting groups are CONFIRMED.
"""
import json
import types

import pytest

from api.payloads.proposal_payload import get_proposal_match_payload
from api.services.base_service import is_ok
from api.services.proposal_service import ProposalService
from utils.describe import netted_line
from utils.state_helper import StateHelper


# ============================================================================
# TEST SUITE: STEP 11b - MATCH NETTING PROPOSALS (DOMAIN B)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Match the netting proposals as Domain B once for every test in this module.

    If a prerequisite is missing, no call is made and res_status stays None, so
    the API test skips while the pre-flight tests report what is missing.
    """
    c = types.SimpleNamespace(res_status=None, response_body=None, rejection=None)

    state = StateHelper.get_state()
    # Kept local, not on the namespace: pytest prints the namespace in failure
    # messages, which end up in the HTML report.
    token = state.get('tokenB')
    c.token_present = token is not None
    # Saved by step 10 from Domain A's view, so reversed for Domain B: its
    # OUTGOING proposal is Domain A's INCOMING one, and the other way round.
    c.outgoing_link_id = state.get('incomingProposalLinkId')
    c.incoming_link_id = state.get('outgoingProposalLinkId')

    # Names the proposals' netting group, and what it nets, in every assertion
    # message.
    c.netting_group_id = state.get('nettingGroupIdStringB')
    c.trade = netted_line(state)

    if not (token and c.outgoing_link_id and c.incoming_link_id):
        print('WARN: Skipping Domain B API call. tokenB, incomingProposalLinkId or '
              'outgoingProposalLinkId is missing from state.')
        return c

    print(f'INFO: Attempting to match outgoing proposal [{c.outgoing_link_id}] with '
          f'incoming proposal [{c.incoming_link_id}] as Domain B...')

    proposal_service = ProposalService(session, domain='B', token=token)
    payload = get_proposal_match_payload(c.outgoing_link_id, c.incoming_link_id)

    res = proposal_service.match_proposals(payload)
    c.res_status = res.status_code
    print(f'INFO: Domain B Proposal Match API responded with status: {c.res_status}')

    if is_ok(res):
        text = res.text
        try:
            c.response_body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # The endpoint may acknowledge with plain text rather than JSON.
            c.response_body = {'rawText': text}
        print(f'INFO: Proposals Matched Successfully. Server replied: {text}')
    else:
        error_text = c.rejection = res.text
        try:
            # Kept so test_match_returns_2xx can quote the server's rejection.
            c.response_body = json.loads(error_text)
        except json.JSONDecodeError:
            c.response_body = {'message': error_text}
        print(f'ERROR: API Request Failed with status: {c.res_status}')
        print(f'SERVER REJECTION REASON: {error_text}')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def test_auth_token_present(ctx):
    """Pre-flight: Domain B auth token is present in state"""
    assert ctx.token_present, \
        ('Domain B token is missing: .state/default.json has no "tokenB". '
         f'Did step 01 run?\n{ctx.trade}')


def test_proposal_link_ids_present(ctx):
    """Pre-flight: Outgoing and incoming proposal link ids are present in state"""
    assert ctx.outgoing_link_id is not None, \
        ('incomingProposalLinkId is missing from state, so there is no Domain B OUTGOING '
         f'proposal to match. Did step 10 find the proposals of netting group '
         f'[{ctx.netting_group_id}]?\n{ctx.trade}')
    assert ctx.incoming_link_id is not None, \
        ('outgoingProposalLinkId is missing from state, so there is no Domain B INCOMING '
         f'proposal to match with. Did step 10 find the proposals of netting group '
         f'[{ctx.netting_group_id}]?\n{ctx.trade}')


def test_match_returns_2xx(ctx):
    """API: Proposal match endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')

    # 2xx only: a rejected match means this run did not match the proposals, so
    # it must not pass.
    body = ctx.response_body if isinstance(ctx.response_body, dict) else {}
    message = body.get('message') or ctx.rejection or 'Unknown'
    assert 200 <= ctx.res_status < 300, \
        (f'Matching OUTGOING proposal [{ctx.outgoing_link_id}] with INCOMING proposal '
         f'[{ctx.incoming_link_id}] as Domain B (netting group [{ctx.netting_group_id}]) '
         f'should return a 2xx status, but returned {ctx.res_status}. Reason: {message}'
         f'\n{ctx.trade}')
