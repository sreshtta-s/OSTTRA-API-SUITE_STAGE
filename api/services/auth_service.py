"""Keycloak token client for one auth domain (A, B or Global Operator).

Used by step 01, which saves the returned access token to run state under the
domain's state key.
"""

from api.payloads.auth_payload import get_auth_payload
from config.auth_config import get_domain, mask_creds
from utils.api_logger import log_request


class AuthService:
    """Client for one domain's Keycloak token endpoint (password grant).

    :param domain_key: 'A', 'B' or 'GLOBAL' (see config/auth_config.py).
    """

    def __init__(self, session, domain_key='A'):
        self.session = session
        self.domain_key = domain_key
        self.domain = get_domain(domain_key)

    @property
    def label(self):
        return self.domain.label

    @property
    def state_key(self):
        """Run-state key that holds this domain's token (e.g. 'token', 'tokenB')."""
        return self.domain.state_key

    @property
    def payload(self):
        """Password-grant form body for this domain.

        request_token() posts this same body, so what step 01 prints (masked) is
        what gets sent.
        """
        return get_auth_payload(self.domain_key)

    def request_token(self):
        """Post the credentials to the domain's token endpoint and return the response.

        The caller checks the status and adds the call to the report's call log.
        """
        # Masked before logging: the password and client secret never reach the log.
        log_request('POST', f'{self.domain_key} keycloak/token', mask_creds(self.payload))
        return self.session.post(
            self.domain.auth_url,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            data=self.payload,
        )
