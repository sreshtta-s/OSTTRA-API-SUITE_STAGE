"""Rule-center folder client on Domain A, used by the disabled step 02.

Posts to DOMAIN_A directly rather than through BaseService, so its calls are
logged but not added to the report's call log.
"""

import os

from utils.api_logger import log_request


class FolderService:
    """Client for the Domain A rule-center folder listing."""

    def __init__(self, session, token):
        self.session = session
        self.token = token

    def get_folders(self, payload):
        """List rule-center folders; step 02 picks the one shared by both parties."""
        log_request('POST', 'A rule-center/folders/list', payload)
        return self.session.post(
            f'{os.environ["DOMAIN_A"]}/access-gateway/api/reference-data/rule-center/folders/list',
            headers={
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json',
            },
            json=payload,
        )
