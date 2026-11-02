"""Run state shared between pipeline steps, each of which runs in its own process.

State is one JSON file, .state/default.json, read and merged into through
StateHelper. This module also resolves the netting rule an injected trade is
expected to net under, and that rule's affirmation mode, from
data/trade_scenarios.py NETTING_RULES.
"""

import json
import logging
import os
from pathlib import Path

from data.trade_scenarios import (
    AFFIRMATION_MODES,
    FALLBACK_MODE,
    FALLBACK_RULE_ID,
    NETTING_RULES,
)

# Warnings go through logging, which pytest.ini streams live (log_cli). print()
# output is captured and shown only for failing tests.
logger = logging.getLogger('osttra.api')

STATE_DIR = Path(__file__).resolve().parent.parent / '.state'

# One fixed file for the whole suite, with deliberately no environment override
# (runner.py's STATE_ROW is not read here): a step pointed at another file would
# read None for every value the other steps wrote.
STATE_FILE = STATE_DIR / 'default.json'


def state_file():
    """Return the path of the run-state file."""
    return STATE_FILE


def _read(file):
    """Return the parsed state file, or {} if it does not exist.

    A corrupt or unreadable file raises RuntimeError rather than reading as
    empty, which would silently drop every value in it.
    """
    path = Path(file)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as err:
        raise RuntimeError(f'Corrupt state file {path}: {err}') from err


def _write(file, data):
    """Write data to file atomically, readable by the owner only."""
    path = Path(file)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a temp file and rename it over the target, so a crash mid-write
    # cannot leave a truncated state file.
    tmp = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    # Owner-only (0o600): the file holds live access tokens, and the default
    # umask would leave it readable by other users on this machine.
    tmp.chmod(0o600)
    tmp.replace(path)


class StateHelper:
    """Read and write the shared run state."""

    @staticmethod
    def save_state(data):
        """Merge data into the existing state; its keys overwrite existing ones."""
        _write(STATE_FILE, {**_read(STATE_FILE), **data})

    @staticmethod
    def replace_state(data):
        """Replace the whole state with data, dropping every other key; {} empties it."""
        _write(STATE_FILE, dict(data))

    @staticmethod
    def get_state():
        """Return the whole state as a dict ({} if there is no state file)."""
        return _read(STATE_FILE)


def _rule_key(party, counter_party, currency_pair, product_type):
    """Build the NETTING_RULES lookup key for one trade.

    Fields are stripped; party ids and the pair compare upper-case and the
    product lower-case, so a static payload's spelling still matches the table.
    A rule covers both parties and both currencies whichever way round the trade
    states them, so each is sorted: BNKB-HK / BNKA-HK USDCNH finds the
    BNKA-HK / BNKB-HK CNHUSD entry.
    """
    pair = str(currency_pair or '').strip().upper()
    return (
        tuple(sorted(str(p or '').strip().upper() for p in (party, counter_party))),
        ''.join(sorted((pair[:3], pair[3:]))),
        str(product_type or '').strip().lower(),
    )


def registered_rules(state):
    """Return the NETTING_RULES entries that match the trade in state.

    Matched on reqPartyId, counterPartyId, tradeCurrencyPair and productType,
    which hold the injected trade's values once step 04 has run. More than one
    match means the table lists the trade twice.
    """
    key = _rule_key(state.get('reqPartyId'), state.get('counterPartyId'),
                    state.get('tradeCurrencyPair'), state.get('productType'))
    return [
        rule for rule in NETTING_RULES
        if _rule_key(rule.get('party'), rule.get('counterParty'),
                     rule.get('tradeCurrencyPair'), rule.get('productType')) == key
    ]


def trade_name(state):
    """Name the trade in state for a warning: parties, pair and product."""
    return (f'{state.get("reqPartyId")} / {state.get("counterPartyId")} '
            f'{state.get("tradeCurrencyPair")} {state.get("productType")}')


def resolve_rule_id(state=None):
    """Return the netting rule id the injected trade is expected to net under.

    Spot and forward trades, and different party pairs on the same product, net
    under different rules, so one trade's rule id never stands in for another's:
    the wrong rule finds no group, or worse, matches an unrelated group that rule
    left open. The entry is found by registered_rules.

    - One entry, or several agreeing on one ruleId: that ruleId.
    - Entries that disagree: None, so the pre-flight checks in steps 06 to 07b
      fail. A warning names the entry to fix.
    - No entry, or a blank ruleId (a rule still to be onboarded):
      FALLBACK_RULE_ID. It matches no netting group, so such a trade can never
      net under a real rule by accident.
    """
    state = state or {}
    trade = trade_name(state)

    matches = registered_rules(state)
    if matches:
        rule_ids = {(rule.get('ruleId') or '').strip() for rule in matches}
        if len(rule_ids) > 1:
            logger.warning(
                'WARN: data/trade_scenarios.py NETTING_RULES lists %s more than once, '
                'with different ruleIds (%s). Keep one entry per trade - neither is used.',
                trade, ', '.join(sorted(rule_id or '<blank>' for rule_id in rule_ids)),
            )
            return None

        rule_id = rule_ids.pop()
        if rule_id:
            return rule_id

        logger.warning(
            'WARN: data/trade_scenarios.py NETTING_RULES lists %s with a blank ruleId; '
            'using the invalid FALLBACK_RULE_ID %s. Fill in the rule this environment '
            'routes that trade under.',
            trade, FALLBACK_RULE_ID,
        )
        return FALLBACK_RULE_ID

    logger.warning(
        'WARN: %s has no entry in data/trade_scenarios.py NETTING_RULES; using '
        'the invalid FALLBACK_RULE_ID %s.',
        trade, FALLBACK_RULE_ID,
    )
    return FALLBACK_RULE_ID


def resolve_affirmation_mode(state=None):
    """Return how the injected trade's netting group is affirmed: 'auto' or 'manual'.

    Read from the mode of the trade's NETTING_RULES entry, found by
    registered_rules. runner.py skips steps 10, 11 and 11b for 'auto'. Matching
    ignores case and surrounding spaces.

    - One entry, or several agreeing on one valid mode: that mode.
    - No entry, entries that disagree, or a mode that is blank or neither auto
      nor manual: FALLBACK_MODE (auto), which skips steps 10, 11 and 11b, as
      only a manual entry runs them. A warning names the trade.
    """
    state = state or {}
    modes = {str(rule.get('mode') or '').strip().lower() for rule in registered_rules(state)}
    if len(modes) == 1 and next(iter(modes)) in AFFIRMATION_MODES:
        return modes.pop()

    if not modes:
        problem = 'has no entry'
    elif len(modes) > 1:
        problem = ('has entries with different modes ('
                   + ', '.join(sorted(mode or '<blank>' for mode in modes)) + ')')
    elif not next(iter(modes)):
        problem = 'has no mode'
    else:
        problem = f'has mode [{modes.pop()}], not {" or ".join(AFFIRMATION_MODES)},'
    logger.warning(
        'WARN: data/trade_scenarios.py NETTING_RULES %s for %s; running it as %s.',
        problem, trade_name(state), FALLBACK_MODE,
    )
    return FALLBACK_MODE
