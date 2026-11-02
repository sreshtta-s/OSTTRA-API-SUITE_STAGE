# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 01: fetch a Keycloak access token for each auth domain.

The runner runs this step once, before the first trade, and again before each
trade of a batch (MULTIPLE_FILE, IMPORT, IMPORT_EXCEL and multi-trade SCENARIO),
so every injection is made on fresh tokens. It is parametrised over
active_domain_keys(): Domain A, Domain B and the Global operator by default, or
the subset named in AUTH_DOMAINS. Each token is saved to .state/default.json
under its domain's state key (token, tokenB, token-global) for later steps.
"""
import json

import pytest

from api.services.auth_service import AuthService
from config.auth_config import active_domain_keys, mask_creds
from utils.reporter import log_call
from utils.state_helper import StateHelper


# ============================================================================
# TEST SUITE: AUTHENTICATION FLOW
# ============================================================================
@pytest.mark.parametrize('domain_key', active_domain_keys())
def test_generate_token_and_save_to_state(session, domain_key):
    """Generate token and save to state"""

    # --- PREPARE AUTHENTICATION PAYLOAD ---
    auth = AuthService(session, domain_key)
    print(f'--- {auth.label} PAYLOAD ---')
    # Masked so the password and client secret never reach the log.
    print(mask_creds(auth.payload))

    # --- EXECUTE POST REQUEST TO AUTHENTICATION SERVICE ---
    response = auth.request_token()
    response_text = response.text

    # AuthService does not extend BaseService, which records its calls itself,
    # so the token request is added to the report's call log here.
    log_call(f'{domain_key} keycloak/token', response.status_code, 'POST')

    if response.status_code != 200:
        print(f'--- {auth.label} KEYCLOAK REJECTION REASON ---')
        print(response_text)

    # --- VALIDATE AUTHENTICATION RESPONSE ---
    assert response.status_code == 200, \
        (f'{auth.label} Keycloak token request should return 200, but returned '
         f'{response.status_code}. Keycloak response: {response_text}')

    response_body = json.loads(response_text)
    assert response_body.get('access_token') is not None, \
        (f'{auth.label} Keycloak returned 200 but no access_token, so there is no token '
         f'to save to state as "{auth.state_key}".')

    # --- PERSIST TOKEN TO STATE FOR DOWNSTREAM TESTS ---
    # The state file is written owner-only (0o600) because it holds live tokens.
    StateHelper.save_state({auth.state_key: response_body['access_token']})
    print(f'{auth.label} token saved to state as "{auth.state_key}".')
