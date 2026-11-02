"""Request bodies for a settlement's netting proposals: fetch them (step 10) and
match them (steps 11 and 11b)."""


def get_proposal_fetch_payload(proposal_key, tenant_id):
    """Return a body for the first 100 proposals under one key, as one tenant sees them.

    :param proposal_key: the settlement id step 09 saved (the netting group's
        commonGroupId).
    :param tenant_id: the trading party whose view of the proposals is listed.
    """
    return {
        'filters': [
            {
                'fieldName': 'proposalKey',
                'operator': 'EQUAL',
                'value': [proposal_key],
            },
            {
                'fieldName': 'tenantId',
                'operator': 'EQUAL',
                'value': [tenant_id],
            },
        ],
        'sortBy': [],
        'paging': {
            'limit': 100,
            'offset': 0,
        },
    }


def get_proposal_match_payload(outgoing_proposal_link_id, incoming_proposal_link_id):
    """Return the body that matches an OUTGOING proposal with its INCOMING counterpart.

    :param outgoing_proposal_link_id: outgoingProposalLinkId, saved by step 10.
    :param incoming_proposal_link_id: incomingProposalLinkId, saved by step 10.
    """
    return {
        'outgoingProposalLinkId': outgoing_proposal_link_id,
        'incomingProposalLinkId': incoming_proposal_link_id,
    }
