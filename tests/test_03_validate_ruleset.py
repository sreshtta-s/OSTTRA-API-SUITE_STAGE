# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 03: write the rule-center rule ids into data/trade_scenarios.py.

For each folder step 02 saved (ruleCenterFolders), lists the folder's rule sets
and reads each ACTIVE one's detail. The folder's participants give the party
pair, the detail's scopes the product (PRODUCT_SUBTYPE SPOT or FORWARD) and
currency pair, and the name's last word the affirmation mode (AUTO in
1-ANY-BNY-SPOT-CENT-AUTO). Each rule set then sets the ruleId (its
ruleSetLogicalId) and mode of the NETTING_RULES entry for that trade, or is
added as a new entry when there is none.

Only those two values are rewritten, so every other line of the file, comments
included, stays as it is. An entry no rule set matches is left unchanged and
named in a warning. Two rule sets for the same trade fail the step, and neither
is written.
"""
import ast
import logging
import os
import re
import runpy
import shutil
import types
from pathlib import Path

import pytest

from api.payloads.ruleset_payload import get_rule_set_list_payload
from api.services.base_service import is_ok
from api.services.ruleset_service import RuleSetService
from data.trade_scenarios import AFFIRMATION_MODES
from utils.state_helper import StateHelper, _rule_key

logger = logging.getLogger('osttra.api')

SCENARIOS_FILE = Path(__file__).resolve().parent.parent / 'data' / 'trade_scenarios.py'

# A rule set's PRODUCT_SUBTYPE scope, as the NETTING_RULES productType.
PRODUCT_TYPES = {'SPOT': 'FxSpot', 'FORWARD': 'FxForward'}

# The scope types naming a rule set's currency pair: CURRENCY as the rule center
# returns it, CURRENCY_PAIR as api/payloads/ruleset_payload.py creates it.
CURRENCY_SCOPES = ('CURRENCY', 'CURRENCY_PAIR')


# ============================================================================
# RULE SET -> NETTING_RULES ENTRY
# ============================================================================
def scope_values(detail, *scope_types):
    """Return the values of a rule set's scopes of the given types."""
    return [scope.get('scopeValue') for scope in detail.get('scopes') or []
            if scope.get('scopeType') in scope_types and scope.get('scopeValue')]


def to_pair(value):
    """Return a scope's pair unseparated in alphabetical order ('USD-CNH' -> 'CNHUSD').

    None if it is not two three-letter currencies.
    """
    currencies = sorted(part.strip().upper() for part in re.split(r'[-/]', str(value)))
    if len(currencies) != 2 or any(len(currency) != 3 for currency in currencies):
        return None
    return ''.join(currencies)


def name_mode(name):
    """Return the mode a rule set's name ends in ('...-AUTO' -> 'auto'), or None."""
    mode = str(name or '').rsplit('-', 1)[-1].strip().lower()
    return mode if mode in AFFIRMATION_MODES else None


def entries_for(folder, detail):
    """Return the NETTING_RULES entries one rule set covers, and why if none.

    One entry per product and currency pair it scopes. The folder's partyId is
    the entry's party and its other participant the counterParty.
    """
    participants = folder['participants']
    party = folder.get('partyId') if folder.get('partyId') in participants else participants[0]
    counter_party = next((p for p in participants if p != party), party)

    subtypes = scope_values(detail, 'PRODUCT_SUBTYPE')
    products = [PRODUCT_TYPES.get(str(subtype).upper()) for subtype in subtypes]
    if not products or None in products:
        return [], f'product {subtypes or "<none>"} is not {" or ".join(PRODUCT_TYPES)}'

    scoped_pairs = scope_values(detail, *CURRENCY_SCOPES)
    pairs = [to_pair(value) for value in scoped_pairs]
    if not pairs or None in pairs:
        return [], f'currency pair {scoped_pairs or "<none>"} is not one pair of currencies'

    return [{
        'party': party, 'counterParty': counter_party,
        'tradeCurrencyPair': pair, 'productType': product,
        'ruleId': detail['ruleSetLogicalId'],
        'mode': name_mode(detail.get('name')),
        'source': f'rule set {detail.get("name")} in rule-center folder {folder["id"]} '
                  f'({folder.get("name")})',
    } for product in products for pair in pairs], None


def entry_key(entry):
    """Return the lookup key resolve_rule_id matches a trade to an entry with."""
    return _rule_key(entry.get('party'), entry.get('counterParty'),
                     entry.get('tradeCurrencyPair'), entry.get('productType'))


def entry_label(entry):
    """Name an entry for the log: parties, pair and product."""
    return (f'{entry.get("party")} / {entry.get("counterParty")} '
            f'{entry.get("tradeCurrencyPair")} {entry.get("productType")}')


# ============================================================================
# NETTING_RULES WRITER
# ============================================================================
def set_string(entry_text, field, value):
    """Replace one string field's value in an entry's source text.

    :raises ValueError: if the entry has no such field holding a string.
    """
    pattern = re.compile(rf"""(['"]{field}['"]\s*:\s*)(['"]).*?\2""")
    text, count = pattern.subn(lambda m: m.group(1) + repr(value), entry_text, count=1)
    if not count:
        raise ValueError(f'NETTING_RULES entry has no {field} string to set:\n{entry_text}')
    return text


def new_entry_text(rule):
    """Return the source of a new NETTING_RULES entry, laid out as the others."""
    return (
        f'    # Added by step 03 from {rule["source"]}.\n'
        '    {\n'
        f'        \'party\': {rule["party"]!r}, \'counterParty\': {rule["counterParty"]!r},\n'
        f'        \'tradeCurrencyPair\': {rule["tradeCurrencyPair"]!r}, '
        f'\'productType\': {rule["productType"]!r},\n'
        f'        \'ruleId\': {rule["ruleId"]!r},\n'
        f'        \'mode\': {(rule["mode"] or "")!r},\n'
        '    },\n'
    )


def write_netting_rules(rules):
    """Write each rule's ruleId and mode into SCENARIOS_FILE's NETTING_RULES.

    :param rules: {entry_key: rule} with one rule per trade.
    :returns: (updated, added, stale): the (entry, rule) pairs whose values
        changed, the rules added as new entries, and the entries no rule matched.

    An entry matching a rule gets its ruleId, and its mode unless the rule's name
    gives none. The file is written only if something changed, atomically, and
    keeps its permissions.
    """
    source = SCENARIOS_FILE.read_text(encoding='utf-8')
    lines = source.splitlines(keepends=True)

    def offset(lineno, col):
        """Return the character offset of an ast position (1-based line, byte column)."""
        line = lines[lineno - 1]
        return (sum(len(text) for text in lines[:lineno - 1])
                + len(line.encode('utf-8')[:col].decode('utf-8')))

    rules_node = next(
        node.value for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'NETTING_RULES'
                for target in node.targets)
    )

    edits, updated, stale, matched = [], [], [], set()
    for node in rules_node.elts:
        entry = ast.literal_eval(node)
        key = entry_key(entry)
        rule = rules.get(key)
        if rule is None:
            stale.append(entry)
            continue

        matched.add(key)
        start = offset(node.lineno, node.col_offset)
        end = offset(node.end_lineno, node.end_col_offset)
        text = set_string(source[start:end], 'ruleId', rule['ruleId'])
        if rule['mode']:
            text = set_string(text, 'mode', rule['mode'])
        if text != source[start:end]:
            edits.append((start, end, text))
            updated.append((entry, rule))

    # New entries go just before the list's closing bracket, after the last one.
    added = [rule for key, rule in rules.items() if key not in matched]
    if added:
        close = offset(rules_node.end_lineno, rules_node.end_col_offset) - 1
        line_start = source.rfind('\n', 0, close) + 1
        text = ''.join(new_entry_text(rule) for rule in added)
        if source[line_start:close].strip():
            edits.append((close, close, '\n' + text))
        else:
            edits.append((line_start, line_start, text))

    # Applied from the end of the file back, so earlier offsets stay valid.
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]

    if edits:
        tmp = SCENARIOS_FILE.with_suffix(f'.py.{os.getpid()}.tmp')
        tmp.write_text(source, encoding='utf-8')
        shutil.copymode(SCENARIOS_FILE, tmp)
        tmp.replace(SCENARIOS_FILE)

    return updated, added, stale


# ============================================================================
# TEST SUITE: SYNC NETTING RULE IDS
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """List and read every folder's rule sets once for every test in this module."""
    c = types.SimpleNamespace(
        folders=[], list_failures=[], truncated=[], detail_failures=[],
        unmapped=[], rules={},
    )

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in failure
    # messages, which end up in the HTML report.
    c.token_present = state.get('token') is not None
    c.folders = state.get('ruleCenterFolders') or []
    if not (c.token_present and c.folders):
        return c

    service = RuleSetService(session, state['token'])
    for folder in c.folders:
        res = service.get_rule_sets(folder['id'], get_rule_set_list_payload())
        if not is_ok(res):
            c.list_failures.append(f'folder {folder["id"]}: HTTP {res.status_code}')
            continue

        body = res.json()
        items = body.get('items') or []
        if (body.get('totalItems') or 0) > len(items):
            c.truncated.append(f'folder {folder["id"]}: {len(items)} of {body["totalItems"]}')

        for item in items:
            if item.get('status') != 'ACTIVE':
                logger.info('Skipping rule set %s in folder %s: status %s',
                            item.get('name'), folder['id'], item.get('status'))
                continue

            detail_res = service.get_rule_set(item['id'])
            if not is_ok(detail_res):
                c.detail_failures.append(f'rule set {item["id"]}: HTTP {detail_res.status_code}')
                continue

            entries, reason = entries_for(folder, detail_res.json())
            if reason:
                c.unmapped.append(f'{item.get("name")} ({item.get("ruleSetLogicalId")}): {reason}')
            for entry in entries:
                c.rules.setdefault(entry_key(entry), []).append(entry)

    return c


# --- PRE-FLIGHT CHECKS ---
def test_auth_token_present(ctx):
    """Verify auth token is present in state"""
    assert ctx.token_present, 'Auth token is missing. Did test_01_auth.py run?'


def test_folders_present_in_state(ctx):
    """Verify step 02 saved the rule-center folders"""
    assert ctx.folders, 'ruleCenterFolders is missing from state. Did test_02 run?'


# --- API STATUS ---
def test_every_folder_listed(ctx):
    """Verify every folder's rule sets are listed"""
    if not (ctx.token_present and ctx.folders):
        pytest.skip('API calls were not executed due to missing prerequisites.')
    assert not ctx.list_failures, f'Rule-set list failed for {"; ".join(ctx.list_failures)}'
    assert not ctx.truncated, \
        (f'Rule sets cut off by paging ({"; ".join(ctx.truncated)}). Raise the paging limit in '
         f'api/payloads/ruleset_payload.py get_rule_set_list_payload.')


def test_every_rule_set_detail_fetched(ctx):
    """Verify every active rule set's detail is fetched"""
    if not (ctx.token_present and ctx.folders):
        pytest.skip('API calls were not executed due to missing prerequisites.')
    assert not ctx.detail_failures, \
        f'Rule-set detail failed for {"; ".join(ctx.detail_failures)}'


# --- MAPPING ---
def test_every_rule_set_maps_to_a_trade(ctx):
    """Verify every active rule set maps to a NETTING_RULES entry"""
    if not (ctx.token_present and ctx.folders):
        pytest.skip('API calls were not executed due to missing prerequisites.')
    assert not ctx.unmapped, \
        'Rule sets with no NETTING_RULES entry, not written:\n' + '\n'.join(ctx.unmapped)


def test_one_rule_set_per_trade(ctx):
    """Verify no two rule sets cover the same trade"""
    if not ctx.rules:
        pytest.skip('No rule set was read.')
    conflicts = [
        f'{entry_label(entries[0])}: ' + ', '.join(entry['source'] for entry in entries)
        for entries in ctx.rules.values()
        if len({entry['ruleId'] for entry in entries}) > 1
    ]
    assert not conflicts, \
        'Several rule sets cover one trade, so neither is written:\n' + '\n'.join(conflicts)


# --- WRITE NETTING_RULES ---
def test_rule_ids_written_to_trade_scenarios(ctx):
    """Write rule ids to data/trade_scenarios.py NETTING_RULES"""
    # Trades with conflicting rule sets keep their current entry; the test above
    # names them.
    rules = {key: entries[0] for key, entries in ctx.rules.items()
             if len({entry['ruleId'] for entry in entries}) == 1}
    if not rules:
        pytest.skip('No rule set to write.')

    for rule in rules.values():
        if not rule['mode']:
            logger.warning('WARN: %s does not end in %s; its mode is left as it is.',
                           rule['source'], ' or '.join(m.upper() for m in AFFIRMATION_MODES))

    updated, added, stale = write_netting_rules(rules)

    for entry, rule in updated:
        logger.info('UPDATED %s: ruleId %s -> %s, mode %s -> %s',
                    entry_label(entry), entry.get('ruleId'), rule['ruleId'],
                    entry.get('mode'), rule['mode'] or entry.get('mode'))
    for rule in added:
        logger.info('ADDED %s: ruleId %s, mode %s', entry_label(rule), rule['ruleId'], rule['mode'])
    for entry in stale:
        logger.warning('WARN: no active rule set matches NETTING_RULES entry %s (ruleId %s); '
                       'left unchanged.', entry_label(entry), entry.get('ruleId'))
    logger.info('NETTING_RULES: %d updated, %d added, %d unchanged, %d with no rule set.',
                len(updated), len(added), len(rules) - len(updated) - len(added), len(stale))

    # Read the file back as steps 06 to 07b will: each trade must now resolve to
    # its rule set's id alone.
    written = runpy.run_path(str(SCENARIOS_FILE))['NETTING_RULES']
    wrong = []
    for key, rule in rules.items():
        rule_ids = {entry.get('ruleId') for entry in written if entry_key(entry) == key}
        if rule_ids != {rule['ruleId']}:
            wrong.append(f'{entry_label(rule)}: expected {rule["ruleId"]}, file has {sorted(rule_ids)}')
    assert not wrong, 'NETTING_RULES does not hold the rule ids written:\n' + '\n'.join(wrong)
