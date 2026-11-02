"""Rule-center rule-set client on Domain A, used by step 03.

Posts to DOMAIN_A directly rather than through BaseService, so its calls are
logged but not added to the report's call log.
"""

import os

from utils.api_logger import log_request


class RuleSetService:
    """Client for the Domain A rule-center rule-set endpoints (create, list and detail)."""

    def __init__(self, session, token):
        self.session = session
        self.token = token

    @property
    def _headers(self):
        """JSON request headers carrying the bearer token."""
        return {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        }

    def create_rule(self, payload):
        """Create a rule set from a get_rule_set_payload() body."""
        log_request('POST', 'A rule-center/rule-sets', payload)
        return self.session.post(
            f'{os.environ["DOMAIN_A"]}/access-gateway/api/reference-data/rule-center/rule-sets',
            headers=self._headers,
            json=payload,
        )

    def get_rule_sets(self, folder_id, payload):
        """List the rule sets in one rule-center folder."""
        log_request('POST', f'A rule-center/folders/{folder_id}/rule-sets/list', payload)
        return self.session.post(
            f'{os.environ["DOMAIN_A"]}/access-gateway/api/reference-data/rule-center'
            f'/folders/{folder_id}/rule-sets/list',
            headers=self._headers,
            json=payload,
        )

    def get_rule_set(self, rule_set_id):
        """Fetch one rule set's detail, including its scopes (product and currency pair)."""
        log_request('GET', f'A rule-center/rule-sets/{rule_set_id}')
        return self.session.get(
            f'{os.environ["DOMAIN_A"]}/access-gateway/api/reference-data/rule-center'
            f'/rule-sets/{rule_set_id}',
            headers=self._headers,
        )
