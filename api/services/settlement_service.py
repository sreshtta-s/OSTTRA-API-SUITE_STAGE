"""Settlement client: initiate, cancel and list settlements (the ledger).

Steps 13 / 13b / 13c initiate; steps 13cs_a / 13cs_b cancel; steps 15 / 15b
poll the list until the settlement reports SETTLEMENT_COMPLETE, and steps
15cs_a / 15cs_b until a cancelled one reports CANCELLED.
"""

from api.services.base_service import BaseService

PREFIX = '/access-gateway/api/settlement-candidate-suite/settlement'


class SettlementService(BaseService):
    """Client for the settlement endpoints on one domain."""

    def initiate_settlement(self, payload):
        """Initiate a settlement on behalf of the payload's trading party."""
        return self.post(f'{PREFIX}/initiate', payload)

    def cancel_settlement(self, settlement_id, payload):
        """Cancel settlement_id. The id is in the path; the body is empty."""
        return self.post(f'{PREFIX}/{settlement_id}/cancel', payload)

    def get_all_settlements(self, payload):
        """List the settlements matching the payload's filters."""
        return self.post(f'{PREFIX}/getAll', payload)
