"""Affirmation-service proposal client: list the proposals under a proposal key,
and match an OUTGOING proposal with its INCOMING counterpart.

Step 10 reads a settlement's netting proposals with it, for their
proposalLinkIds; steps 11 (Domain A) and 11b (Domain B) match them.
"""

from api.services.base_service import BaseService

PREFIX = '/access-gateway/api/affirmation-service/proposal'


class ProposalService(BaseService):
    """Client for the affirmation-service proposal endpoints on one domain."""

    def fetch_proposals(self, payload):
        """List the proposals matching the payload's filters."""
        return self.post(f'{PREFIX}/fetch', payload)

    def match_proposals(self, payload):
        """Match the payload's OUTGOING proposal with its INCOMING one, by link id."""
        return self.post(f'{PREFIX}/actions/match', payload)
