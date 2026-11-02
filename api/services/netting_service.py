"""Netting group client: list groups, read one group's details, close a group
and list a group's settlement candidates.

Used on both domains by steps 06 to 09 and 12, and by the 07c netting-total check.
"""

from api.services.base_service import BaseService

SUITE = '/access-gateway/api/settlement-candidate-suite'
PREFIX = f'{SUITE}/netting/nettingGroup'


class NettingService(BaseService):
    """Client for the netting group endpoints on one domain."""

    # Steps 06, 07, 09 and 12 poll these endpoints with near-identical filter bodies,
    # so only the endpoint and ids are logged.
    log_payload = False

    def get_all_netting_groups(self, payload):
        """List the netting groups matching the payload's filters."""
        return self.post(f'{PREFIX}/getAll', payload)

    def get_netting_group_details(self, payload):
        """Fetch one group's details; steps 09 / 09b and 12 / 12b read status and commonGroupId."""
        return self.post(f'{PREFIX}/getDetails', payload)

    def close_netting_group(self, payload):
        """Force-close a netting group (steps 08 / 08b).

        Groups do not close on their own, so each domain force-closes its group
        with the same timeOfUpdate.
        """
        return self.post(f'{PREFIX}/force-close', payload)

    def get_all_settlement_candidates(self, payload):
        """List the settlement candidates of the groups in the payload's filters.

        The netting screen's candidate view: one candidate per currency of a
        confirmed group, carrying its net amount (steps 12 / 12b). Served at the
        suite's root, not under /netting.
        """
        return self.post(f'{SUITE}/getAll', payload)
