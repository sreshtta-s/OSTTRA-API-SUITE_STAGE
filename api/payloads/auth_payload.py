"""Keycloak token request body for every auth domain (step 01).

The domain -> env var mapping lives in config/auth_config.py; this module only
selects the domain.

  get_auth_payload('A')      -> Domain A credentials
  get_auth_payload('B')      -> Domain B credentials
  get_auth_payload('GLOBAL') -> Global Operator credentials
"""

from config.auth_config import get_domain


def get_auth_payload(domain_key='A'):
    """Return the password-grant form body for one domain."""
    return get_domain(domain_key).creds
