"""Step registry: the spec file, report label and prerequisites of each step token.

Mirrors config/steps.js. runner.py resolves tokens to spec files with get_step,
conftest.py stamps each step's blob with its label and file, utils/reporter.py
derives the Excel column pairs from the enabled steps in token order, and
api/payloads/trade_payload.py rejects scenario step tokens (delayBefore,
runUpTo) it does not list. The order steps run in is runner.py's STEP_TOKENS.
"""

import functools
import re

# 'enabled': False drops a step from the Excel columns; steps 02 and 03 are also
# absent from runner.py's STEP_TOKENS, and are run with pytest to refresh
# data/trade_scenarios.py NETTING_RULES from the rule center. 'requires' lists the steps each one
# depends on, as in config/steps.js; nothing in this suite enforces it.
STEPS = {
    '1':   {'file': 'tests/test_01_auth.py',                                  'requires': [],     'label': 'Auth (A, B, Global)'},
    '2':   {'file': 'tests/test_02_create_ruleset.py',                        'requires': ['1'],  'enabled': False, 'label': 'Fetch rule-center folders'},
    '3':   {'file': 'tests/test_03_validate_ruleset.py',                      'requires': ['2'],  'enabled': False, 'label': 'Sync netting rule ids'},
    '4':   {'file': 'tests/test_04_inject_trade.py',                          'requires': ['1'],  'label': 'Inject trade'},
    '5':   {'file': 'tests/test_05_validate_trade.py',                        'requires': ['4'],  'label': 'Validate trade (A)'},
    '5b':  {'file': 'tests/test_05b_validate_trade_domain_b.py',              'requires': ['4'],  'label': 'Validate trade (B)'},
    '6':   {'file': 'tests/test_06_validate_netting.py',                      'requires': ['4'],  'label': 'Netting group (A)'},
    '6b':  {'file': 'tests/test_06b_validate_netting_domain_b.py',            'requires': ['4'],  'label': 'Netting group (B)'},
    '7':   {'file': 'tests/test_07_validate_netting_underlying.py',           'requires': ['6'],  'label': 'Netting underlying (A)'},
    '7b':  {'file': 'tests/test_07b_validate_netting_underlying_domain_b.py', 'requires': ['6b'], 'label': 'Netting underlying (B)'},
    '8':   {'file': 'tests/test_08_close_netting_group.py',                   'requires': ['6'],  'label': 'Close netting group (A)'},
    '8b':  {'file': 'tests/test_08b_close_netting_group_domain_b.py',         'requires': ['6b'], 'label': 'Close netting group (B)'},
    '9':   {'file': 'tests/test_09_validate_netting_group_status.py',         'requires': ['8'],  'label': 'Netting group status (A)'},
    '9b':  {'file': 'tests/test_09b_validate_netting_group_status_domain_b.py', 'requires': ['8b'], 'label': 'Netting group status (B)'},
    '10':  {'file': 'tests/test_10_fetch_netting_proposal.py',                'requires': ['9'],  'label': 'Netting proposal (A)'},
    '11':  {'file': 'tests/test_11_match_netting_proposal.py',                'requires': ['10'], 'label': 'Match netting proposal (A)'},
    '11b': {'file': 'tests/test_11b_match_netting_proposal_domain_b.py',      'requires': ['10'], 'label': 'Match netting proposal (B)'},
    '12':  {'file': 'tests/test_12_validate_netting_group_confirmed.py',      'requires': ['11'], 'label': 'Netting group confirmed (A)'},
    '12b': {'file': 'tests/test_12b_validate_netting_group_confirmed_domain_b.py', 'requires': ['11b'], 'label': 'Netting group confirmed (B)'},
    '13':  {'file': 'tests/test_13_initiate_settlement.py',                   'requires': ['12'], 'label': 'Initiate settlement (A)'},
    '13b': {'file': 'tests/test_13b_initiate_settlement_domain_b.py',         'requires': ['12b'], 'label': 'Initiate settlement (B)'},
    '13c': {'file': 'tests/test_13c_initiate_settlement_global.py',           'requires': ['12'], 'label': 'Initiate settlement (Global)'},
    '14':  {'file': 'tests/test_14_generate_mt910_funding_call.py',           'requires': ['13'], 'label': 'Generate MT910 funding call'},
    '15':  {'file': 'tests/test_15_validate_ledger.py',                       'requires': ['14'], 'label': 'Validate ledger (A)'},
    '15b': {'file': 'tests/test_15b_validate_ledger_domain_b.py',             'requires': ['14'], 'label': 'Validate ledger (B)'},
}

# The status of a closed netting group awaiting its proposals' match. Under a
# manual netting rule (data/trade_scenarios.py) the group stays in it until
# steps 10, 11 and 11b match them; an auto rule confirms the group by itself,
# and runner.py skips those steps. Steps 12 and 12b then require both groups
# to be CONFIRMED.
PENDING_AFFIRMATION = 'PENDING_AFFIRMATION'

# A token is a step number with an optional lower-case suffix: '5', '5b', '10c'.
TOKEN_RE = re.compile(r'^(\d+)([a-z]*)$')


def order_key(token):
    """Split a token into its (number, suffix) sort key, e.g. '10b' -> (10, 'b')."""
    match = TOKEN_RE.match(token)
    if not match:
        raise ValueError(f'Unknown flow token "{token}".')
    return int(match.group(1)), match.group(2)


def compare_tokens(a, b):
    """Compare two tokens by number, then suffix, so '9b' < '10' < '10b'."""
    an, a_suffix = order_key(a)
    bn, b_suffix = order_key(b)
    if an != bn:
        return an - bn
    return (a_suffix > b_suffix) - (a_suffix < b_suffix)


def sort_tokens(tokens):
    """Return the tokens sorted by compare_tokens: number, then suffix."""
    return sorted(tokens, key=functools.cmp_to_key(compare_tokens))


def normalise(token):
    """Return a token stripped and lower-cased, so '5B' finds '5b'."""
    return str(token).strip().lower()


def get_step(token):
    """Return the registry entry for a token.

    :raises ValueError: for a token not in STEPS.
    """
    step = STEPS.get(normalise(token))
    if not step:
        raise ValueError(f'Unknown flow token "{token}".')
    return step
