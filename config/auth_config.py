"""Auth domain definitions (A, B, GLOBAL), resolved from environment variables.

Mirrors config/auth.config.js: one place maps each domain key to the env vars
holding its URLs and credentials, so no service hard-codes a domain. Importing
this module loads .env.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# load_dotenv never overrides a variable that is already set, so the process
# environment wins, then this project's .env, then a shared .env in the parent
# directory, which lets side-by-side suites use one set of credentials.
load_dotenv(ROOT / '.env')
load_dotenv(ROOT.parent / '.env')

# Per domain: its display label, the run-state key its token is saved under,
# and the env var behind each URL and credential.
DEFS = {
    'A': {
        'label': 'Domain A',
        'stateKey': 'token',
        'vars': {
            'baseURL': 'DOMAIN_A',
            'authURL': 'KEYCLOAK_URL',
            'client_id': 'CLIENT_ID',
            'client_secret': 'CLIENT_SECRET',
            'username': 'API_USERNAME',
            'password': 'API_PASSWORD',
        },
    },
    'B': {
        'label': 'Domain B',
        'stateKey': 'tokenB',
        'vars': {
            'baseURL': 'DOMAIN_B',
            'authURL': 'KEYCLOAK_URL_B',
            'client_id': 'CLIENT_ID_B',
            'client_secret': 'CLIENT_SECRET_B',
            'username': 'API_USERNAME_B',
            'password': 'API_PASSWORD_B',
        },
    },
    'GLOBAL': {
        'label': 'Global Operator',
        'stateKey': 'token-global',
        'vars': {
            'baseURL': 'DOMAIN_GLOBAL',
            'authURL': 'KEYCLOAK_URL_GLOBAL',
            'client_id': 'CLIENT_ID_GLOBAL',
            'client_secret': 'CLIENT_SECRET_GLOBAL',
            'username': 'API_USERNAME_GLOBAL',
            'password': 'API_PASSWORD_GLOBAL',
        },
    },
}


class Domain:
    """One resolved auth domain: its URLs, run-state key and credentials."""

    def __init__(self, key, label, state_key, base_url, auth_url, creds):
        self.key = key
        self.label = label
        self.state_key = state_key
        self.base_url = base_url
        self.auth_url = auth_url
        self.creds = creds


def get_domain(key):
    """Resolve a domain key to a Domain built from the environment.

    :raises ValueError: for an unknown key, or listing every env var the domain
        is missing.
    """
    definition = DEFS.get(key)
    if not definition:
        raise ValueError(
            f'Unknown auth domain "{key}". Valid keys: {", ".join(DEFS.keys())}'
        )

    missing = [name for name in definition['vars'].values() if not os.environ.get(name)]
    if missing:
        raise ValueError(
            f'[{definition["label"]}] missing env vars in .env:\n  '
            + '\n  '.join(missing)
        )

    def v(k):
        return os.environ[definition['vars'][k]]

    return Domain(
        key=key,
        label=definition['label'],
        state_key=definition['stateKey'],
        base_url=v('baseURL'),
        auth_url=v('authURL'),
        creds={
            'grant_type': 'password',
            'client_id': v('client_id'),
            'client_secret': v('client_secret'),
            'username': v('username'),
            'password': v('password'),
        },
    )


def active_domain_keys():
    """Return the domain keys step 01 authenticates, all of them by default.

    AUTH_DOMAINS narrows them to a comma-separated subset, e.g.
    AUTH_DOMAINS=A,B pytest tests/test_01_auth.py. Keys are upper-cased here;
    unknown ones are rejected by get_domain.
    """
    raw = (os.environ.get('AUTH_DOMAINS') or '').strip()
    if not raw:
        return list(DEFS.keys())
    return [part.strip().upper() for part in raw.split(',') if part.strip()]


def mask_creds(creds):
    """Return a copy of creds with the password and client secret masked.

    Use it whenever credentials are logged: raw secrets must never reach the
    log or the report.
    """
    return {**creds, 'password': '***', 'client_secret': '***'}
