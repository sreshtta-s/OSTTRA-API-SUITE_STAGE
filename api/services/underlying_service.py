"""Netting underlying client: list the trade legs held in a netting group.

Used on both domains by steps 07 / 07b and by the 07c netting-total check.
"""

from api.services.base_service import BaseService

PREFIX = '/access-gateway/api/settlement-candidate-suite/netting/underlying'


class UnderlyingService(BaseService):
    """Client for the netting underlying endpoint on one domain."""

    # Queried once per candidate group on every polling pass, and only the group
    # id changes between calls, so only the endpoint and ids are logged.
    log_payload = False

    def get_all_underlyings(self, payload):
        """List the underlying legs matching the payload's filters."""
        return self.post(f'{PREFIX}/getAll', payload)
