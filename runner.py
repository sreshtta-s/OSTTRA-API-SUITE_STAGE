#!/usr/bin/env python3
"""Runner: execute the full ordered pipeline and produce its reports.

Step 01 (auth) runs once, and again before each MULTIPLE_FILE, IMPORT,
IMPORT_EXCEL or multi-trade SCENARIO trade, so every injection is made on fresh
tokens; then, for each trade, steps 04 -> 15b (STEP_TOKENS) run in
order, each in its own pytest process. A process per step lets the Excel
report attribute API calls to a step through CURRENT_STEP, and keeps one step's
crash from ending the run. Each step writes a JSON blob (conftest.py); the
runner merges the blobs into one HTML report per test case (per mode for
DYNAMIC and SINGLE_FILE, per trade for MULTIPLE_FILE and IMPORT) and appends
one Excel row per trade.

IMPORT is MULTIPLE_FILE with one step in front: import_trade_runner.py first
fills multiple_unparsed.json from import_trade/trades/<IMPORT_DATE>/.

IMPORT_EXCEL runs every test case, TC001 on, on the trades the Excel in
data/osttra_test_scenarios/ names for it: trade_reader.py there finds them in
import_trade/trades/<TRADE_DATE>/, imports and parses them as IMPORT does, and
each case follows its scenario file's flow (run_excel_case). A case the Excel
names no trade for (N/A) is skipped: its scenario's own trades are never
generated in their place. A case with no scenario file runs each trade alone, staged in single_unparsed.json and injected as SINGLE_FILE
injects single_trade.json. Each case gets one HTML report and
one Excel row per trade, in a workbook of its own laid out as IMPORT's.

Run state (.state/default.json) is cleared at start-up, after every trade and
when the run ends. Each trade starts from nothing but the tokens Phase 1 auth
wrote (DYNAMIC adds its seed from .state.example.json), so a value no earlier
step of that trade wrote is missing rather than left over from another trade.

A multi-trade case that declares a nettingTotal (TC010 to TC013, TC018) then checks
its trades as one netting group, against the sum utils/sum_storer.py stored as
they were booked, and takes that group on to its runUpTo. A continuation (a
row with "continues", which books nothing; none at present) and the scenario
it continues always run together, each with its own report. Every file the run writes is owner-only:
state holds live tokens, and reports hold trade data.

Usage: python runner.py  (the mode and scenario are set in utils/flag.py)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Imported for its side effect: it loads .env (this directory's, then its
# parent's) into os.environ, which every step subprocess inherits.
import config.auth_config  # noqa: F401,E402

from api.payloads.trade_payload import (  # noqa: E402
    CONTINUATION_RESUME_STEP,
    NETTING_GROUP_START_STEP,
    NETTING_TOTAL_AFTER,
    SCENARIO_SHARED_IDS_ENV,
    SCENARIO_TRADE_INDEX_ENV,
    continuation_of,
    flatten_trades,
    load_scenario,
    scenario_continuation,
    scenario_netting_group,
    scenario_trades,
)
from config.steps import get_step, order_key  # noqa: E402
from data.osttra_test_scenarios import trade_reader  # noqa: E402
# The injection mode and scenario come from utils/flag.py; an INJECTION_MODE
# key in .env has no effect.
from utils.flag import SCENARIO_ENV, UI_MODE_ENV, resolve_injection_mode, resolve_scenario_id  # noqa: E402
from utils.helpers import TestUtils  # noqa: E402
from utils.reporter import (  # noqa: E402
    NOT_PRESENT_RESULT,
    NOT_RUN_RESULT,
    describe_exit,
    is_crash,
    local_date_time,
    merge_html_report,
    read_injected_payload,
    reset_calls,
    reset_injected_payload,
    write_run,
)
from utils.scenario_record import load_record, record_path, save_record  # noqa: E402
from utils.sum_storer import SumError, add_trade as add_to_sum, load_sum, sum_line  # noqa: E402
from utils.sum_storer import cancel_trade as cancel_in_sum  # noqa: E402
from utils.sum_storer import start as start_sum  # noqa: E402
from utils.state_helper import (  # noqa: E402
    STATE_DIR,
    StateHelper,
    resolve_affirmation_mode,
    state_file,
)

INJECTION_MODE = resolve_injection_mode()

PAYLOAD_DIR = ROOT / 'api' / 'json_payload_trade_injection'
MULTIPLE_TRADES_PATH = PAYLOAD_DIR / 'multiple_trade.json'
# Where an IMPORT_EXCEL case with no scenario file stages each trade, one at a
# time, for parse_trades.py to turn into single_trade.json.
SINGLE_UNPARSED_PATH = PAYLOAD_DIR / 'single_unparsed.json'

# IMPORT's first step: collects one day's trade files into multiple_unparsed.json.
IMPORT_SCRIPT = ROOT / 'import_trade' / 'import_trade_runner.py'

BLOB_ROOT = ROOT / 'blob-report'
HTML_ROOT = ROOT / 'reports' / 'html'

# One timestamp for the whole run, in local time to match the Excel workbook's
# sheet-per-day naming: a UTC date would file an early-morning IST run under
# the previous day.
RUN_AT = datetime.now()
RUN_DISPLAY = local_date_time(RUN_AT)                                 # 2026-09-16 10:03:41
RUN_DATE = RUN_AT.strftime('%Y-%m-%d')                                # 2026-09-16

# HTML report folders are named by day and test case (or mode, or MULTIPLE_FILE
# trade), not by run time, so a re-run on the same day replaces that report. A
# day keeps one report per case, as the Excel workbook keeps one sheet per day.

# Auth runs once per run. Its blob is copied into each row's blob directory so
# step 01 appears in every merged report ahead of steps 04 -> 15b. MULTIPLE_FILE,
# IMPORT, IMPORT_EXCEL and multi-trade SCENARIO instead run auth again per trade,
# writing that trade's own step 01 blob.
AUTH_BLOB = BLOB_ROOT / 'auth' / '01-1.json'

# The per-trade pipeline, in run order. Each step runs in its own pytest process.
STEP_TOKENS = ['4', '5', '5b', '6', '6b', '7', '7b', '8', '8b', '9', '9b',
               '10', '11', '11b', '12', '12b', '13', '13b', '13c', '14', '15', '15b']

# Every step a complete merged report covers: auth, then the pipeline. Steps 02
# and 03 are disabled in config/steps.py, so they are absent.
EXPECTED_TOKENS = ['1'] + STEP_TOKENS

# The affirmation steps, which match the netting group's proposals. They run
# for a trade whose netting rule's mode (data/trade_scenarios.py) is manual, and
# are skipped when it is auto: the group is then confirmed without a match. A
# skipped step still gets a blob saying why, so the report shows it and no step
# reads as missing.
AFFIRMATION_STEPS = ('10', '11', '11b')

# Steps switched off for every trade and case, with the reason each one's blob
# records. Like a skipped affirmation step, each still gets a blob, so the
# report shows it as not run rather than missing, and config/steps.py keeps it
# so the Excel columns (and today's sheet) stay the same. Delete an entry to
# run that step again.
SKIPPED_STEPS = {}

# The case-level check run_netting_group() makes after steps 06 to 07b: that the
# group steps 07 / 07b found nets to the row's stored sum. It is kept
# out of config/steps.py because the Excel column set is derived from that
# registry, and a changed column set starts a new sheet. Its blob name sorts
# between step 07b's (x01-08-7b) and step 08's (x01-09-8), after every trade's
# blobs.
NETTING_TOTAL_SPEC = 'tests/test_07c_validate_netting_total.py'
NETTING_TOTAL_TOKEN = 'total'
NETTING_TOTAL_BLOB = 'x01-08-7c.json'

# The steps a cancelled trade adds, all run straight after step 05b, once both
# ledgers have checked the live trade, in this order: step 04 cancel resends
# step 04's trade with metaData.status "Cancelled", and steps 05ct_a and 05ct_b
# check the Domain A and B ledgers then show it CANCELLED, 05ct_b held back
# 20s after 05ct_a. So TC032 runs 01, 04, 05, 05b, 04 cancel, 05ct_a, 05ct_b.
# They run only for a trade run_pipeline() is told to cancel:
# a scenario trade entry with "cancel": true, such as TC032's. They are kept out of
# config/steps.py for the same reason as the netting total check: the Excel
# columns come from that registry. Each blob is named after the step it follows
# (ADDED_STEP_BLOB: '04-5b' -> '04-5b_4cancel', '04-5b_5ct_a'), so it
# sorts straight after that step in the merged report.
CANCEL_STEPS = (
    # (runs after step, token, spec, label, seconds to wait first)
    ('5b', '4cancel', 'tests/test_04_cancel_trade.py', 'Inject trade (Cancelled)', None),
    ('5b', '5ct_a', 'tests/test_05ct_a_validate_trade_cancelled.py', 'Trade cancelled (A)', None),
    ('5b', '5ct_b', 'tests/test_05ct_b_validate_trade_cancelled_domain_b.py',
     'Trade cancelled (B)', 20),
)

# The steps a cancelled settlement adds, in this order: steps 13cs_a and 13cs_b
# cancel the Domain A and B settlements (settlementId, settlementIdB) straight
# after the scenario's cancelSettlement.after step (13c unless it names a later
# one: 15b in TC038, once the settlement is complete), and steps 15cs_a and
# 15cs_b check only that the settlement ledger (getAll) then shows each as
# CANCELLED, straight after its checkAfter step (the cancel's unless it names a
# later one: 15b in TC036, after steps 15 and 15b). A cancel declared refused
# has no checkAfter, so the checks are not run. Each step's outcome can be
# declared in cancelSettlement.expect, and a wait before it in
# cancelSettlement.delayBefore (TC037 holds 13cs_a 30s after step 14, once the
# settlement is initiated and funded). They run only when a scenario sets
# "cancelSettlement": on a trade entry for that trade's own settlement, or at
# the top of a netting row for its group's. Like any added step, they run after
# their step even when the row skips it (TC036 skips 13 to 13c, so the
# settlement is cancelled while still queued). Kept out of config/steps.py and
# STEP_TOKENS like CANCEL_STEPS, and named the same way.
SETTLEMENT_CANCEL_STEPS = (
    # (the cancelSettlement key naming the step it runs after, token, spec, label)
    ('after', '13cs_a', 'tests/test_13cs_a_cancel_settlement.py', 'Cancel settlement (A)'),
    ('after', '13cs_b', 'tests/test_13cs_b_cancel_settlement_domain_b.py', 'Cancel settlement (B)'),
    ('checkAfter', '15cs_a', 'tests/test_15cs_a_validate_settlement_cancelled.py',
     'Settlement cancelled (A)'),
    ('checkAfter', '15cs_b', 'tests/test_15cs_b_validate_settlement_cancelled_domain_b.py',
     'Settlement cancelled (B)'),
)

# An added step's blob is named after the step it follows: '02-4' ->
# '04-5b_4cancel', '20-13c' -> '20-13c_13cs_a'. The merge orders blobs by file
# name, so it sorts straight after that step and before the next, which a bare
# token would not do: '23-13cs_a' would sort before step 15b's '23-15b'.
ADDED_STEP_BLOB = '{prefix}{order:02d}-{after}_{token}.json'

# A netting row's cancelTrade runs the same steps, all three in a row, on one of
# its trades after the group step it names: after 07b, before the netting total
# checks the group's net without that trade (TC034), or after 08b, once the
# group is closed and the cancel is refused (TC035). Each blob is named after
# that step's, so it sorts straight after it: x01-08-7b_4cancel follows step
# 07b's x01-08-7b and comes before the total's x01-08-7c.
GROUP_CANCEL_BLOB = 'x01-{order:02d}-{after}_{token}.json'

# A blob name's numeric order prefix ('02-' in '02-4.json'). Stripping it and
# the .json suffix leaves the step token.
BLOB_TOKEN_RE = re.compile(r'^\d+-')


# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------

def join_steps(tokens):
    """Return step tokens as prose for a message: ('10', '11', '11b') -> '10, 11 and 11b'."""
    return ' and '.join([', '.join(tokens[:-1]), tokens[-1]]) if len(tokens) > 1 else tokens[0]


def report_dir(row_key):
    """Return the reports/html folder name for a non-scenario run.

    Named by day rather than run time, so a second run on the same day replaces
    the first run's report. MULTIPLE_FILE and IMPORT include the row key so each
    trade in the file gets its own report; DYNAMIC and SINGLE_FILE are named by
    mode. SCENARIO runs use scenario_report_dir() instead.

    IMPORT also names the mode: both loops key their rows trade-1, trade-2...,
    so without it a same-day MULTIPLE_FILE run would overwrite the reports the
    import workbook links to.

    IMPORT_EXCEL passes the test case id as row_key: one report per case, kept
    apart from the same case's SCENARIO report.
    """
    if INJECTION_MODE == 'IMPORT_EXCEL':
        return f'{RUN_DATE}__IMPORT_EXCEL__{row_key}'

    if INJECTION_MODE == 'IMPORT':
        return f'{RUN_DATE}__IMPORT__{row_key}'

    if INJECTION_MODE == 'MULTIPLE_FILE':
        return f'{RUN_DATE}__{row_key}'

    return f'{RUN_DATE}__{INJECTION_MODE}'


def reset_blobs():
    """Delete leftover blobs so they are not merged into this run's report.

    Earlier HTML reports are kept: like the Excel workbook, reports/html
    accumulates across runs.
    """
    shutil.rmtree(BLOB_ROOT, ignore_errors=True)
    HTML_ROOT.mkdir(parents=True, exist_ok=True)


def seed_auth_blob(row_key):
    """Copy the shared auth blob into a row's blob directory as its step 01."""
    if not AUTH_BLOB.exists():
        return
    dest = BLOB_ROOT / row_key / AUTH_BLOB.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(AUTH_BLOB, dest)


def run_pytest(spec_file, env_overrides):
    """Run one step spec in its own pytest process.

    A step whose process ends without writing its blob (BLOB_FILE), because it
    was killed or crashed first, gets a failed one from write_unreported_blob,
    so the merged report shows it failed instead of leaving it out.

    :param env_overrides: variables added to the inherited environment.
    :returns: True when pytest exits 0 and wrote its blob, meaning no test
        failed or errored.
    """
    env = {**os.environ, **env_overrides}
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', spec_file],
        cwd=ROOT,
        env=env,
    )

    blob = env_overrides.get('BLOB_FILE')
    if blob and not Path(blob).exists():
        write_unreported_blob(blob, env_overrides.get('CURRENT_STEP', ''), spec_file,
                              result.returncode)
        return False
    return result.returncode == 0


def write_unreported_blob(blob_path, token, spec_file, exit_status):
    """Write a failed blob for a step whose process ended without writing its own.

    Unlike write_skipped_blob it accepts a token the step registry does not
    list, such as the netting total check's.
    """
    try:
        label = get_step(token)['label']
    except ValueError:
        label = ''
    reason = (f'Step {token or spec_file} left no results: its pytest process ended with '
              f'{describe_exit(exit_status)} before writing them, so its checks did not run. '
              'See the console output for the error.')
    print(reason, file=sys.stderr)

    path = Path(blob_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'step': token,
            'label': label,
            'file': spec_file,
            'exitStatus': exit_status,
            'tests': [{
                'name': f'Step {token or spec_file} reported no results',
                'outcome': 'failed',
                'duration': 0.0,
                'message': reason,
            }],
        }, indent=2), encoding='utf-8')
    except OSError as err:
        print(f'Could not record that step {token} left no results: {err}', file=sys.stderr)


def run_trade_auth(row_key, blob_path):
    """Run step 01 for one trade of a batch, so it does not run on tokens that expired.

    The fresh tokens also replace AUTH_SNAPSHOT, so state seeded later in the
    batch, such as a netting group's checks after its last trade, starts from
    them rather than from Phase 1's.

    :param blob_path: where this trade's step 01 blob goes.
    :returns: True when every auth domain returned a token.
    """
    print(f'\n--- Step 1: {get_step("1")["label"]} ---')
    ok = run_pytest('tests/test_01_auth.py', {
        'CURRENT_STEP': '1',
        'STATE_ROW': row_key,
        'BLOB_FILE': str(blob_path),
        'CI': 'true',
    })

    if ok and state_file().exists():
        shutil.copyfile(state_file(), AUTH_SNAPSHOT)
        # Holds live tokens; copyfile keeps an existing file's mode.
        AUTH_SNAPSHOT.chmod(0o600)
    return ok


def announce_trade(case_id, number, total, label, stop_after, expect, delays):
    """Print the banner a test case's trade runs under: which trade, and the flow it declares."""
    print('\n------------------------------------------------------')
    print(f'EXECUTING {case_id} TRADE {number} of {total}: {label}')
    if stop_after:
        print(f'  runs to step {stop_after}', end='')
        declared = ', '.join(f'{k} expects {v}' for k, v in expect.items())
        print(f'; {declared}' if declared else '')
    for token, seconds in delays.items():
        print(f'  holds {seconds:g}s before step {token}')
    print('------------------------------------------------------\n')


def describe_settlement_cancel(cancel_settlement):
    """Return a scenario's cancelSettlement as one banner line: where it cancels, checks and expects."""
    line = f'cancels its settlement after step {cancel_settlement["after"]}'
    holds = ', '.join(f'{seconds:g}s before step {token}'
                      for token, seconds in cancel_settlement['delayBefore'].items())
    if holds:
        line += f', holding {holds}'
    check_after = cancel_settlement['checkAfter']
    if check_after is None:
        line += ', declared refused, so it is not checked as cancelled'
    elif check_after != cancel_settlement['after']:
        line += f', and checks it is cancelled after step {check_after}'
    declared = ', '.join(f'{token} expects {outcome}'
                         for token, outcome in cancel_settlement['expect'].items())
    return f'{line}; {declared}' if declared else line


def apply_expected_outcome(blob_path, token, expected, passed, label):
    """Reconcile a step's actual outcome with the outcome the scenario declares.

    A trade built to prove the system rejects it fails its step on purpose,
    and reported as a failure it would look like a regression. So when the
    scenario declares `fail` and the step fails, the blob's failed tests are
    recorded as passed, with a note, and the step does not count against the
    exit code. When a step declared `fail` passes, the expected rejection has
    stopped happening, so the step is recorded as a failure even though pytest
    passed. Only the blob is rewritten, never the specs.

    :param expected: 'pass', 'fail', or None when the scenario declares nothing.
    :returns: the outcome the run should act on.
    """
    if expected not in ('pass', 'fail'):
        return passed

    if expected == 'pass':
        return passed

    # A crash is not the rejection the scenario expects: the step's checks never
    # ran, so it stays a failure, and the merged report shows why.
    if not passed and is_crash(blob_exit_status(blob_path)):
        print(f'Step {token} ({label}): expected to fail, but it crashed rather than '
              'failing its checks; recorded as a failure.', file=sys.stderr)
        return False

    reconciled = not passed
    note = ('EXPECTED FAILURE: the scenario declares step '
            f'{token} fails for this trade.' if reconciled else
            f'UNEXPECTED PASS: the scenario declares step {token} fails for '
            'this trade, but it passed.')

    print(f'Step {token} ({label}): '
          + ('failed as the scenario expected; recorded as passed.'
             if reconciled else
             'was expected to fail but passed; recorded as a failure.'),
          file=sys.stderr)

    path = Path(blob_path)
    if not path.exists():
        return reconciled

    try:
        blob = json.loads(path.read_text(encoding='utf-8'))

        if reconciled:
            # Rewrite only failed tests, keeping the original message under the
            # note. Skipped tests keep their outcome so the report still shows it.
            for test in blob.get('tests', []):
                if test.get('outcome') == 'failed':
                    test['outcome'] = 'passed'
                    test['message'] = f'{note}\n\n{test.get("message") or ""}'.strip()
        else:
            # The tests really passed, so their outcomes stay as they are. One
            # added failed entry marks the step red and states what went wrong.
            blob.setdefault('tests', []).append({
                'name': f'EXPECTED FAILURE at step {token} did not occur',
                'outcome': 'failed',
                'duration': 0.0,
                'message': note,
            })

        blob['expectedOutcome'] = expected
        path.write_text(json.dumps(blob, indent=2), encoding='utf-8')
    except (OSError, json.JSONDecodeError) as err:
        print(f'Could not apply the expected outcome to {path.name}: {err}',
              file=sys.stderr)

    return reconciled


def blob_exit_status(blob_path):
    """Return the pytest exit status a step's blob records, or None if it cannot be read."""
    try:
        return json.loads(Path(blob_path).read_text(encoding='utf-8')).get('exitStatus')
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def annotate_blob(blob_path, trade_label):
    """Append the trade's label to a step's heading in the merged report.

    A multi-trade scenario merges every trade into one report, so without the
    label each step heading would repeat once per trade with nothing to tell
    the trades apart.
    """
    path = Path(blob_path)
    if not trade_label or not path.exists():
        return

    try:
        blob = json.loads(path.read_text(encoding='utf-8'))
        blob['label'] = f'{blob.get("label", "")} - {trade_label}'.strip(' -')
        blob['tradeLabel'] = trade_label
        path.write_text(json.dumps(blob, indent=2), encoding='utf-8')
    except (OSError, json.JSONDecodeError) as err:
        print(f'Could not label {path.name}: {err}', file=sys.stderr)


def annotate_delay(blob_path, seconds):
    """Record in a step's heading that the scenario delayed its start.

    Otherwise a delayed step looks like any other in the merged report, and the
    delay the row exists to exercise leaves no trace.
    """
    path = Path(blob_path)
    if not path.exists():
        return

    try:
        blob = json.loads(path.read_text(encoding='utf-8'))
        blob['label'] = f'{blob.get("label", "")} (held {seconds:g}s before it started)'
        blob['delayBeforeSeconds'] = seconds
        path.write_text(json.dumps(blob, indent=2), encoding='utf-8')
    except (OSError, json.JSONDecodeError) as err:
        print(f'Could not record the delay on {path.name}: {err}', file=sys.stderr)


def write_skipped_blob(blob_path, token, reason, outcome='skipped'):
    """Write a blob for a step that is not run (SKIPPED_STEPS or affirmation), saying why.

    It uses conftest.py's blob format with one skipped entry, so the merged
    report lists the step in its place and report_missing_steps() finds it.

    :param outcome: 'failed' for a step that should have run and could not,
        such as an IMPORT_EXCEL case's injection when its trades are missing.
    """
    step = get_step(token)
    path = Path(blob_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'step': token,
            'label': step['label'],
            'file': step['file'],
            'exitStatus': 0,
            'tests': [{
                'name': f'Step {token} not run',
                'outcome': outcome,
                'duration': 0.0,
                'message': reason,
            }],
        }, indent=2), encoding='utf-8')
    except OSError as err:
        print(f'Could not record why step {token} was not run: {err}', file=sys.stderr)


def run_pipeline(row_key, extra_env=None, stop_after=None, expect=None,
                 blob_key=None, blob_prefix='', trade_label=None, delays=None,
                 start_at=None, cancel=False, cancel_settlement=None, skip=None):
    """Run the pipeline steps in order for one trade.

    Each step writes a blob for the merged report. A failed step is counted and
    the run moves on to the next step.

    Steps 10, 11 and 11b follow the mode of the trade's netting rule
    (resolve_affirmation_mode): manual runs them and auto skips them.

    :param row_key: the trade's row key, passed to each step as STATE_ROW, which
        names the row's call log and injected-payload files.
    :param start_at: first step token to run. A continuation books no trade, so
        it starts at 06, and again at 08 after its netting total.
    :param stop_after: last step token to run. A trade that only proves a
        rejection declares where it stops, instead of reporting failures for
        steps that cannot succeed.
    :param expect: {token: 'pass'|'fail'} from the scenario, reconciled by
        apply_expected_outcome().
    :param blob_key: directory under blob-report for this trade's blobs;
        defaults to row_key. A multi-trade scenario points every trade at one
        directory so they merge into one report.
    :param blob_prefix: prefix for this trade's blob names in a shared
        directory. The merge orders blobs by file name, so it also groups the
        report trade by trade.
    :param trade_label: appended to each step's heading in the merged report.
    :param delays: {token: seconds} from the scenario's delayBefore. The runner
        waits that long before starting the step, so a row that models a late
        message (such as a funding confirmation held back after settlement is
        initiated) gets the gap it declares.
    :param cancel: True to run CANCEL_STEPS, straight after step 05b: the
        scenario entry's "cancel".
    :param cancel_settlement: the scenario's "cancelSettlement", as
        {'after': step token, 'checkAfter': step token or None, 'expect':
        {token: 'pass'|'fail'}, 'delayBefore': {token: seconds}}, to run
        SETTLEMENT_CANCEL_STEPS: the cancel straight after `after`, its checks
        straight after checkAfter (none when it is None), each held back the
        seconds delayBefore names and reconciled with its expected outcome.
        Falsy runs none of them.
    :param skip: {token: reason} from a netting row's "skip": steps not run,
        each reported as such with its reason, as SKIPPED_STEPS are.
    :returns: the number of failed steps, after expected outcomes are applied.
    """
    extra_env = extra_env or {}
    expect = expect or {}
    delays = delays or {}
    skip = {**SKIPPED_STEPS, **(skip or {})}
    blob_key = blob_key or row_key
    failed = 0
    started = start_at is None
    # The netting rule's mode, 'auto' or 'manual'. Read when the run reaches the
    # affirmation steps, from the trade step 04 recorded to state.
    mode = None

    # (runs after step, token, spec, label, expected outcome, seconds to wait
    # first). A settlement check with no checkAfter (a refused cancel) follows
    # None, so never runs.
    added_steps = [(after, token, spec, label, None, delay)
                   for after, token, spec, label, delay in (CANCEL_STEPS if cancel else ())]
    if cancel_settlement:
        added_steps += [(cancel_settlement[key], token, spec, label,
                         cancel_settlement['expect'].get(token),
                         cancel_settlement['delayBefore'].get(token))
                        for key, token, spec, label in SETTLEMENT_CANCEL_STEPS]

    def run_added_steps(token, index):
        """Run the added steps that follow `token`, returning how many failed."""
        added_failed = 0
        for after, added_token, spec, label, expected, delay in added_steps:
            if token == after:
                added_blob = BLOB_ROOT / blob_key / ADDED_STEP_BLOB.format(
                    prefix=blob_prefix, order=index + 2, after=after, token=added_token)
                added_failed += run_cancel_step(row_key, extra_env, added_blob, trade_label,
                                                added_token, spec, label, expected=expected,
                                                delay=delay)
        return added_failed

    for index, token in enumerate(STEP_TOKENS):
        started = started or token == start_at
        if not started:
            continue

        try:
            step = get_step(token)
        except ValueError as err:
            print(f'Skipping unknown step token "{token}": {err}', file=sys.stderr)
            continue

        # Numbered from 02 (01 is auth) because the merge orders blobs by file name.
        blob_path = BLOB_ROOT / blob_key / f'{blob_prefix}{index + 2:02d}-{token}.json'

        if not (ROOT / step['file']).exists():
            # A step that should run and cannot is a failure, recorded so the
            # merged report shows it rather than silently leaving it out.
            reason = f'Step {token} could not run: its spec {step["file"]} does not exist.'
            print(reason, file=sys.stderr)
            write_skipped_blob(blob_path, token, reason, outcome='failed')
            annotate_blob(blob_path, trade_label)
            failed += 1
            if stop_after and token == stop_after:
                break
            continue

        if token in skip:
            print(f'\n--- Step {token}: {step["label"]} --- not run. {skip[token]}')
            write_skipped_blob(blob_path, token, skip[token])
            annotate_blob(blob_path, trade_label)
            # A step added after this one still runs: TC036 cancels its
            # settlement after step 13c, which it skips.
            failed += run_added_steps(token, index)
            if stop_after and token == stop_after:
                print(f'\nStopping after step {token}: this trade declares it goes no further.')
                break
            continue

        if token in AFFIRMATION_STEPS and mode is None:
            mode = resolve_affirmation_mode(StateHelper.get_state())
            print(f'\nThe netting rule is {mode}: steps {join_steps(AFFIRMATION_STEPS)} '
                  + ('are skipped.' if mode == 'auto' else 'match its proposals.'))

        if token in AFFIRMATION_STEPS and mode == 'auto':
            reason = ('The netting rule is auto-affirmed (mode auto in '
                      'data/trade_scenarios.py), so its netting group is confirmed without '
                      'its proposals being matched.')
            print(f'\n--- Step {token}: {step["label"]} --- not run. {reason}')
            write_skipped_blob(blob_path, token, reason)
            annotate_blob(blob_path, trade_label)
            if stop_after and token == stop_after:
                print(f'\nStopping after step {token}: this trade declares it goes no further.')
                break
            continue

        print(f'\n--- Step {token}: {step["label"]} ---')

        delay = delays.get(token)
        if delay:
            print(f'Holding {delay:g}s before step {token}: the scenario declares a delay here.')
            # A deliberate fixed wait: the gap itself is what the scenario tests.
            time.sleep(delay)

        ok = run_pytest(step['file'], {
            **extra_env,
            'CURRENT_STEP': token,
            'STATE_ROW': row_key,
            'BLOB_FILE': str(blob_path),
            'CI': 'true',
        })

        ok = apply_expected_outcome(blob_path, token, expect.get(token), ok, step['label'])
        if delay:
            annotate_delay(blob_path, delay)
        annotate_blob(blob_path, trade_label)

        if not ok:
            print(f'Step {token} ({step["label"]}) failed. Continuing.', file=sys.stderr)
            failed += 1

        # Run before the stop check, so a trade that stops at the step one
        # follows still runs it.
        failed += run_added_steps(token, index)

        if stop_after and token == stop_after:
            print(f'\nStopping after step {token}: this trade declares it goes no further.')
            break

    return failed


def run_cancel_step(row_key, extra_env, blob_path, trade_label, token, spec, label,
                    expected=None, delay=None):
    """Run one of CANCEL_STEPS or SETTLEMENT_CANCEL_STEPS, in its own pytest process like any step.

    :param extra_env: the trade's environment, as the pipeline's steps ran with,
        so step 04 cancel builds the same trade as step 04.
    :param expected: 'pass', 'fail', or None, reconciled by apply_expected_outcome().
    :param delay: seconds to wait before starting it, from CANCEL_STEPS or
        cancelSettlement.delayBefore.
    :returns: 1 if it failed, after its expected outcome is applied, else 0.
    """
    print(f'\n--- Step {token}: {label} ---')
    if delay:
        print(f'Holding {delay:g}s before step {token}: a delay is declared here.')
        # A deliberate fixed wait: the gap itself is what the scenario tests.
        time.sleep(delay)
    ok = run_pytest(spec, {
        **extra_env,
        'CURRENT_STEP': token,
        'STATE_ROW': row_key,
        'BLOB_FILE': str(blob_path),
        'CI': 'true',
    })

    # The step is not in the step registry, so conftest.py writes its blob with
    # an empty label; set the heading here.
    if blob_path.exists():
        try:
            blob = json.loads(blob_path.read_text(encoding='utf-8'))
            blob['label'] = label
            blob_path.write_text(json.dumps(blob, indent=2), encoding='utf-8')
        except (OSError, json.JSONDecodeError) as err:
            print(f'Could not label {blob_path.name}: {err}', file=sys.stderr)
    ok = apply_expected_outcome(blob_path, token, expected, ok, label)
    if delay:
        annotate_delay(blob_path, delay)
    annotate_blob(blob_path, trade_label)

    if not ok:
        print(f'Step {token} ({label}) failed. Continuing.', file=sys.stderr)
    return 0 if ok else 1


def report_title():
    """Return the merged report's heading: the trade id and the run time.

    Step 04 writes the trade id to state once the simulator accepts the trade,
    so it is available at merge time; without it the heading reads "No trade ID".
    """
    trade_id = (StateHelper.get_state() or {}).get('injectedTradeId')
    return f'{trade_id or "No trade ID"} — {RUN_DISPLAY}'


def expected_tokens_for(stop_after=None):
    """Return the step tokens a trade's report should cover.

    A trade that declares where it stops is not missing the later steps, so
    they are left out; otherwise a deliberately short run would be reported as
    if it had crashed.
    """
    if not stop_after:
        return EXPECTED_TOKENS

    covered = ['1']
    for token in STEP_TOKENS:
        covered.append(token)
        if token == stop_after:
            break
    return covered


def report_missing_steps(row_key, blobs, stop_after=None):
    """Warn when a step left no blob, since the merged report would silently omit it.

    :returns: the missing step tokens.
    """
    present = {BLOB_TOKEN_RE.sub('', name).removesuffix('.json') for name in blobs}
    missing = [token for token in expected_tokens_for(stop_after) if token not in present]

    if missing:
        print(
            f'WARNING: {row_key} produced {len(blobs)}/{len(expected_tokens_for(stop_after))} '
            f'step reports. Missing step(s): {", ".join(missing)}. '
            'The HTML report will not show them.',
            file=sys.stderr,
        )
    return missing


def scenario_report_dir(scenario_id):
    """Return the reports/html folder name for a test case's single report.

    Day and case id only: not a trade id, because a multi-trade case has
    several trades and one report, and not the run time, so a same-day re-run
    replaces the case's report.
    """
    return f'{RUN_DATE}__{scenario_id}'


def scenario_report_title(scenario_id, trade_id=None):
    """Return the heading for a test case's report.

    The case id leads because readers look reports up by it. The traceability
    id is the test sheet's name for the row, so the report can be matched to
    the test pack without opening the scenario file. A single-trade case also
    names its trade; a multi-trade case passes none, because each step heading
    already names its trade.
    """
    trace = (load_scenario(scenario_id).get('scenario') or {}).get('traceabilityId')

    parts = [f'{scenario_id} ({trace})' if trace else scenario_id]
    if trade_id:
        parts.append(trade_id)
    parts.append(RUN_DISPLAY)

    return ' — '.join(parts)


def merge_scenario_report(scenario_id, blob_key, out_name=None, title=None):
    """Merge every trade's step blobs into one HTML report for the test case.

    All of the case's trades write to one blob directory, ordered trade by
    trade by their file-name prefix, so a single report shows whether the case
    as a whole passed. The blobs are deleted after a successful merge.

    :param out_name: the reports/html folder; defaults to scenario_report_dir().
    :param title: the heading; defaults to scenario_report_title(). An
        IMPORT_EXCEL case passes both, since it may have no scenario file to
        name them from.
    """
    blob_dir = BLOB_ROOT / blob_key
    out_name = out_name or scenario_report_dir(scenario_id)
    out_dir = HTML_ROOT / out_name

    if not blob_dir.exists():
        print(f'No blobs found for {scenario_id}; skipping report merge.', file=sys.stderr)
        return

    title = title or scenario_report_title(scenario_id)

    try:
        if merge_html_report(blob_dir, out_dir, title) is None:
            print(f'No step results to merge for {scenario_id}.', file=sys.stderr)
            return
        shutil.rmtree(blob_dir, ignore_errors=True)
        print(f'\nHTML report [{title}]: reports/html/{out_name}/index.html')
    except OSError as err:
        print(f'Report merge failed for {scenario_id}: {err}', file=sys.stderr)


# The state keys that describe a booked trade: recorded for a netting group
# check, and written back to state when it runs. The base currency and its buyer
# and seller say which way each currency moves, for the direction checks.
# injectedTradeIds lets a cancelTrade resend the trade under its own ids.
RECORDED_TRADE_KEYS = ('uniqueTransactionId', 'reqPartyId', 'counterPartyId', 'tradeCurrencyPair',
                       'productType', 'dealtCurrency', 'baseCurrency', 'referenceCurrency',
                       'baseCurrencyAmount', 'referenceCurrencyAmount', 'baseCurrencyBuyer',
                       'baseCurrencySeller', 'tradeDate', 'valueDate', 'injectedTradeIds')


def recorded_trade():
    """Return what the trade just run booked, read from state.

    These are the values step 04 recorded from the injected payload.
    """
    s = StateHelper.get_state() or {}
    return {'tradeId': s.get('injectedTradeId'), **{key: s.get(key) for key in RECORDED_TRADE_KEYS}}


def record_scenario_run(scenario_id, blob_key, trades):
    """Save this run's trades and step blobs for a scenario that continues it.

    Step 07c reads the trades back, and a continuation also carries the blobs
    into its own report. Must run before the report merge, which deletes the
    blobs. The shared auth blob is left out because a continuation
    authenticates and reports its own.
    """
    blobs = {}
    for path in sorted((BLOB_ROOT / blob_key).glob('*.json')):
        if path.name == AUTH_BLOB.name:
            continue
        try:
            blobs[path.name] = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue

    save_record(scenario_id, {
        'scenario': scenario_id,
        'recordedAt': RUN_DISPLAY,
        'trades': trades,
        'blobs': blobs,
    })
    print(f'Recorded {len(trades)} {scenario_id} trade(s) and {len(blobs)} step result(s) '
          f'to .state/{record_path(scenario_id).name}, for any row that continues it.')


def run_netting_group(row_key, blob_key, source, trades, run_up_to, cancel_trade=None,
                      **options):
    """Check that a set of recorded trades nets into one group, then take that group on.

    Steps 06 to 07b find the netting group from the first trade, then the
    netting total check (07c) verifies that the group 07 / 07b found nets to
    the expected amounts. A run_up_to of NETTING_TOTAL_AFTER (07b) stops
    there, without the total: steps 07 / 07b are then the only check of its
    net, as after a trade cancelled after step 06b. With a later run_up_to, steps 08
    onward then run on that group, but only if every trade passed its own
    steps. With
    cancel_trade set, one trade is cancelled straight after the group step it
    names (cancel_group_trade): after 07b, the total is of the trades still
    live. Blobs are prefixed x01-, so they follow every trade's in the merged
    report.

    :param source: the scenario whose trades these are, for messages and step
        headings.
    :param trades: the trades as record_scenario_run() saved them.
    :param cancel_trade: the scenario's cancelTrade, as scenario_netting_group()
        returns it, or None.
    :param options: run_pipeline() keywords for every group step, from the
        netting row: cancel_settlement, expect and skip.
    :returns: the number of failed steps and checks.
    """
    source_failures = sum(1 for trade in trades if trade.get('failedSteps'))
    failed = 0

    # Steps 06 to 07b look the group up from one trade in state. The first is
    # enough because all of them net into one group, which the total checks.
    # Skipped without recorded trades, or step 06 would search with whatever
    # party pair the seeded state holds.
    if trades:
        first = trades[0]
        StateHelper.save_state({'injectedTradeId': first.get('tradeId'),
                                **{key: first.get(key) for key in RECORDED_TRADE_KEYS}})
        failed += run_group_steps(row_key, blob_key, source, trades,
                                  NETTING_GROUP_START_STEP, '7b', cancel_trade, **options)

    if run_up_to == NETTING_TOTAL_AFTER:
        print(f'\nNot running the netting group total ({source}): runUpTo is step '
              f'{NETTING_TOTAL_AFTER}, so the group stops there.')
        return failed

    print(f'\n--- Netting group total ({source}) ---')
    blob_path = BLOB_ROOT / blob_key / NETTING_TOTAL_BLOB
    ok = run_pytest(NETTING_TOTAL_SPEC, {
        'CURRENT_STEP': NETTING_TOTAL_TOKEN,
        'STATE_ROW': row_key,
        'BLOB_FILE': str(blob_path),
        'CI': 'true',
    })
    if not ok:
        print(f'Netting group total ({source}) failed.', file=sys.stderr)

    # The check is not in the step registry, so conftest.py writes its blob with
    # an empty label; set the heading here.
    if blob_path.exists():
        try:
            blob = json.loads(blob_path.read_text(encoding='utf-8'))
            blob['label'] = f'Netting group total - {source}'
            blob_path.write_text(json.dumps(blob, indent=2), encoding='utf-8')
        except (OSError, json.JSONDecodeError) as err:
            print(f'Could not label {blob_path.name}: {err}', file=sys.stderr)

    failed += 0 if ok else 1

    if run_up_to and trades and source_failures:
        # Closing and settling a group built from a failed run would act on
        # unverified trades, so the group is left open and the row fails.
        print(f'\nNot running steps {CONTINUATION_RESUME_STEP} to {run_up_to}: {source_failures} '
              f'{source} trade(s) failed a step, so their netting group is left open.',
              file=sys.stderr)
        failed += 1
    elif run_up_to and trades:
        # Steps 06 to 07b and the total have saved the netting group ids, value
        # date and parties to state, which is everything steps 08 onward need.
        print(f'\nRunning steps {CONTINUATION_RESUME_STEP} to {run_up_to} on the {source} netting group.')
        failed += run_group_steps(row_key, blob_key, source, trades,
                                  CONTINUATION_RESUME_STEP, run_up_to, cancel_trade, **options)

    return failed


def run_group_steps(row_key, blob_key, source, trades, start, stop, cancel_trade, **options):
    """Run a netting group's steps from `start` to `stop`, cancelling a trade on the way.

    When cancel_trade names a step in that range, the steps run up to it, then
    cancel_group_trade(), then on to `stop`, so the cancel lands straight after
    that step whether or not a step in between is skipped. With
    cancel_settlement in `options`, the group's settlement is cancelled
    straight after step 13c, if that is in the range.

    :param options: run_pipeline() keywords for every step in the range.
    :returns: the number of failed steps.
    """
    label = f'{source} netting group'
    span = STEP_TOKENS[STEP_TOKENS.index(start):STEP_TOKENS.index(stop) + 1]
    after = (cancel_trade or {}).get('after')
    if after not in span:
        return run_pipeline(row_key, blob_key=blob_key, blob_prefix='x01-', trade_label=label,
                            start_at=start, stop_after=stop, **options)

    failed = run_pipeline(row_key, blob_key=blob_key, blob_prefix='x01-', trade_label=label,
                          start_at=start, stop_after=after, **options)
    failed += cancel_group_trade(row_key, blob_key, source, trades, cancel_trade)
    if after != stop:
        failed += run_pipeline(row_key, blob_key=blob_key, blob_prefix='x01-', trade_label=label,
                               start_at=span[span.index(after) + 1], stop_after=stop,
                               **options)
    return failed


def cancel_group_trade(row_key, blob_key, source, trades, cancel_trade):
    """Cancel one of a netting group's trades and check whether both ledgers show it cancelled.

    Runs CANCEL_STEPS on the trade, one after another: step 04 cancel resends
    it as Cancelled under its own ids (recorded as injectedTradeIds, with the
    environment its steps ran in), then steps 05ct_a and 05ct_b wait for both
    ledgers to show it CANCELLED, each reconciled with the outcome the
    scenario's expect declares for it.

    A cancel the scenario expects to go through (no cancel step declared to
    fail) then takes the trade out of the stored sum and marks it cancelled in
    the run's record whatever the steps found: step 07c then expects the group
    to net to the trades still live, and fails if the cancelled one still
    counts. A cancel declared refused (a NACK) changes nothing, so the trade
    stays in both. State is put back to the first live trade, as the group's
    steps ran on; with none left, it stays on the cancelled trade, marked
    tradeCancelled for steps 07 / 07b.

    :param cancel_trade: {'trade': number from 1, in the order the trades ran,
        'after': step token, 'expect': {cancel step token: 'pass'|'fail'}}.
    :returns: the number of failed steps, after expected outcomes are applied.
    """
    number, after = cancel_trade['trade'], cancel_trade['after']
    expect = cancel_trade.get('expect') or {}
    refused = 'fail' in expect.values()

    if not 1 <= number <= len(trades):
        print(f'cancelTrade {number} is not one of the {len(trades)} {source} trades; '
              'nothing is cancelled.', file=sys.stderr)
        return 1

    trade = trades[number - 1]
    trade_id = trade.get('tradeId')
    label = f'{trade.get("label") or f"trade {number}"} (cancelled)'

    print('\n------------------------------------------------------')
    print(f'{source} NETTING GROUP: cancelling trade {number} of {len(trades)} '
          f'[{trade_id or "not booked"}] after step {after}')
    declared = ', '.join(f'{token} expects {outcome}' for token, outcome in expect.items())
    if declared:
        print(f'  {declared}')
    print('------------------------------------------------------')

    StateHelper.save_state({'injectedTradeId': trade_id,
                            **{key: trade.get(key) for key in RECORDED_TRADE_KEYS}})

    failed = 0
    order = STEP_TOKENS.index(after) + 2
    for _, token, spec, step_label, delay in CANCEL_STEPS:
        blob_path = BLOB_ROOT / blob_key / GROUP_CANCEL_BLOB.format(order=order, after=after,
                                                                   token=token)
        failed += run_cancel_step(row_key, trade.get('env') or {}, blob_path, label,
                                  token, spec, step_label, expected=expect.get(token),
                                  delay=delay)

    if refused:
        print(f'The scenario expects the cancel to be refused, so trade {number} stays in the '
              f'{source} sum and record.')
        StateHelper.save_state({'injectedTradeId': trades[0].get('tradeId'),
                                **{key: trades[0].get(key) for key in RECORDED_TRADE_KEYS}})
        return failed

    trade['cancelled'] = True
    record = load_record(source)
    if record and len(record.get('trades') or []) >= number:
        record['trades'][number - 1]['cancelled'] = True
        save_record(source, record)
    else:
        print(f'{source} left no record to mark trade {number} cancelled in; step 07c '
              'will fail its pre-flight.', file=sys.stderr)

    if trade_id:
        try:
            cancel_in_sum(source, trade_id)
            print(f'Took trade {number} out of the {source} sum: {sum_line(load_sum(source))}')
        except SumError as err:
            print(f'Trade {number} could not be taken out of the {source} sum: {err}',
                  file=sys.stderr)
            failed += 1

    live = next((t for t in trades if not t.get('cancelled')), None)
    if live:
        StateHelper.save_state({'injectedTradeId': live.get('tradeId'),
                                **{key: live.get(key) for key in RECORDED_TRADE_KEYS}})
    else:
        # Every trade is cancelled, so state stays on this one, marked so that
        # steps 07 / 07b, if they follow (a cancelTrade after step 06b), check
        # its legs left the net rather than look for them live.
        StateHelper.save_state({'tradeCancelled': True})
    return failed


def run_own_netting_group(scenario_id, group, trades, report_name=None):
    """Check that a scenario's own trades net into one group, then settle it.

    TC010 to TC013 and TC018 book their trades, each to step 05b, then declare a
    nettingTotal: their trades must share one netting group holding the sum
    utils/sum_storer.py stored as they were booked, and runUpTo takes the group
    on from there (15b: close, confirm, settle, fund and check the ledger;
    TC018, which nets to zero, 09b: close and check it closed). Runs after every trade, once their record is saved, and writes to
    the case's blob directory, so the group's steps follow the trades' in the
    one report. The group gets its own Excel row, since no trade's row covers
    the netting group or its settlement.

    :param report_name: the report folder the group's row links to; defaults
        to scenario_report_dir(). IMPORT_EXCEL passes its case's own.
    :returns: 1 if any check failed, else 0.
    """
    row_key = f'{scenario_id}-group'

    print('\n------------------------------------------------------')
    print(f'{scenario_id} NETTING GROUP: checking that its {len(trades)} trades net together'
          + (f', then running it to step {group["runUpTo"]}' if group['runUpTo'] else ''))
    print(f'  stored sum: {sum_line(load_sum(scenario_id))}')
    if group['cancelTrade']:
        print(f'  cancels trade {group["cancelTrade"]["trade"]} after step '
              f'{group["cancelTrade"]["after"]}')
    if group['cancelSettlement']:
        print(f'  {describe_settlement_cancel(group["cancelSettlement"])}')
    if group['skip']:
        print(f'  skips step {join_steps(list(group["skip"]))}')
    declared = ', '.join(f'{token} expects {outcome}' for token, outcome in group['expect'].items())
    if declared:
        print(f'  {declared}')
    print('------------------------------------------------------\n')

    seed_trade_state()
    reset_calls(row_key)
    reset_injected_payload(row_key)

    failed = run_netting_group(row_key, scenario_id, scenario_id, trades, group['runUpTo'],
                               cancel_trade=group['cancelTrade'],
                               cancel_settlement=group['cancelSettlement'],
                               expect=group['expect'], skip=group['skip'])

    os.environ['STATE_ROW'] = row_key
    row = build_run_record(
        row_key, failed, {'raw': '', 'parsed': ''},
        test_case=scenario_id, report_name=report_name or scenario_report_dir(scenario_id),
        ran_to=group['runUpTo'],
    )
    os.environ.pop('STATE_ROW', None)
    # The group nets every trade, so the Trade ID column names how many.
    cancelled = sum(1 for trade in trades if trade.get('cancelled'))
    row['tradeId'] = (f'Netting group of {len(trades)} {scenario_id} trades'
                      + (f', {cancelled} cancelled' if cancelled else ''))

    out = write_run(row, row_key)
    print(f'Excel row {out["slNo"]} written to sheet [{out["sheet"]}] ({scenario_id}, netting group)')

    reset_calls(row_key)
    reset_injected_payload(row_key)
    clear_state()
    return 0 if failed == 0 else 1


def run_continuation(scenario_id, continuation):
    """Run a scenario that books nothing and checks what an earlier one booked.

    A row with "continues" (none at present) runs straight after the row it
    continues, so the record it reads is the one that row has just written. It
    injects no trade and carries that row's step results into its own report,
    then runs run_netting_group() on its trades. The case produces one HTML
    report and one Excel row.

    :returns: 1 if any check failed, else 0.
    """
    source = continuation['continues']
    run_up_to = continuation.get('runUpTo')
    record = load_record(source)
    trades = (record or {}).get('trades') or []
    row_key = scenario_id
    blob_key = scenario_id

    print(f'\nPHASE 2: Scenario {scenario_id} continues {source}. It books nothing and '
          f'checks the netting total of the trades {source} last recorded.\n')

    seed_trade_state()
    reset_calls(row_key)
    reset_injected_payload(row_key)
    seed_auth_blob(blob_key)

    if record:
        for name, blob in (record.get('blobs') or {}).items():
            path = BLOB_ROOT / blob_key / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {**blob, 'label': f'{source} · {blob.get("label", "")}'}, indent=2), encoding='utf-8')
        print(f'Carried {len(record.get("blobs") or {})} step result(s) from {source}\'s run at '
              f'{record.get("recordedAt")} into this report.')
    else:
        print(f'WARNING: {source} left no record at .state/{record_path(source).name}; '
              'the check below will fail its pre-flight.', file=sys.stderr)

    failed = run_netting_group(row_key, blob_key, source, trades, run_up_to)

    merge_scenario_report(scenario_id, blob_key)

    os.environ['STATE_ROW'] = row_key
    row = build_run_record(
        row_key, failed, {'raw': '', 'parsed': ''},
        test_case=scenario_id, report_name=scenario_report_dir(scenario_id),
    )
    os.environ.pop('STATE_ROW', None)
    # No trade of its own, so the Trade ID column names whose trades were totalled.
    row['tradeId'] = f'{len((record or {}).get("trades") or [])} trades from {source}'

    out = write_run(row, row_key)
    print(f'Excel row {out["slNo"]} written to sheet [{out["sheet"]}] ({scenario_id})')

    reset_calls(row_key)
    reset_injected_payload(row_key)
    clear_state()
    return 0 if failed == 0 else 1


def excel_case_title(case, plan):
    """Return the heading for an IMPORT_EXCEL case's report: the case, what it ran on, the run time.

    A case with a scenario file is led by its id and the sheet's traceability
    id, as its SCENARIO report is.
    """
    count = len(case['trades'])
    plural = 's' if count != 1 else ''
    if case['problem']:
        source = 'not present' if case['notPresent'] else 'not run'
    else:
        source = f'{count} Excel trade{plural} of {plan["date"]}'

    if case['scenario']:
        return scenario_report_title(case['scenario'], source)
    return ' — '.join([case['id'], source, RUN_DISPLAY])


def report_unrun_case(case, plan):
    """Record an IMPORT_EXCEL case that cannot run on its Excel trades.

    Nothing is injected: a case short of trades, or with a trade its netting
    group cannot hold, would only fail later, after booking them. The case
    still gets its report and Excel row, so the gap shows in the run's results
    and not only in the console. A case whose Excel trade ids are not in the
    day's folder is Not Present, which the Summary tab leaves out of its
    counts; any other is a failure.

    :returns: 1 for a failure, 0 for a Not Present case.
    """
    case_id = case['id']
    report_name = report_dir(case_id)
    not_present = case['notPresent']

    # Filed as step 04's, the step that would have injected the trades.
    write_skipped_blob(
        BLOB_ROOT / case_id / '02-4.json', '4',
        f'{case_id} is not run on the trades {plan["excel"]} names for it: {case["problem"]}',
        outcome='skipped' if not_present else 'failed',
    )
    merge_scenario_report(case_id, case_id, out_name=report_name, title=excel_case_title(case, plan))

    row = build_run_record(case_id, 1, {'raw': '', 'parsed': ''},
                           test_case=case_id, report_name=report_name)
    row.update({
        'tradeId': trade_reader.TRADE_ID_SEPARATOR.join(case['listed']),
        'productType': '',
        'result': NOT_PRESENT_RESULT if not_present else NOT_RUN_RESULT,
    })

    out = write_run(row, case_id)
    print(f'Excel row {out["slNo"]} written to sheet [{out["sheet"]}] '
          f'({case_id}, {"not present" if not_present else "not run"})')
    return 0 if not_present else 1


def stage_single_trade(event):
    """Write one trade event to single_unparsed.json and parse it into single_trade.json.

    An IMPORT_EXCEL case with no scenario file runs each of its trades this
    way, just before the trade's steps, so step 04 injects it as SINGLE_FILE
    does. Both files are overwritten, as IMPORT overwrites multiple_unparsed.json.

    :returns: True when parse_trades.py parsed it.
    """
    SINGLE_UNPARSED_PATH.write_text(json.dumps(event, separators=(',', ':'), ensure_ascii=False),
                                    encoding='utf-8')
    parsed = subprocess.run([sys.executable, 'parse_trades.py', 'SINGLE_FILE'], cwd=ROOT)
    return parsed.returncode == 0


def run_excel_case(case, plan):
    """Run one IMPORT_EXCEL test case: its trades, then any netting group, into one report.

    Each trade gets its own step 01, so a long batch never runs on expired
    tokens, then the steps its scenario entry declares (runUpTo, expect,
    delayBefore) and an Excel row of its own. A case that nets its trades (a
    nettingTotal) adds each booked trade to its sum, and checks the group only
    once every trade has run, through run_own_netting_group() as SCENARIO
    does. Every step process reads the case's scenario through TEST_SCENARIO.
    A case with no scenario file runs without one: each trade is staged in
    single_unparsed.json (stage_single_trade) and its steps run as
    SINGLE_FILE (UI_INJECTION_MODE), so step 04 injects single_trade.json.

    :param case: one of trade_reader.prepare()'s cases.
    :param plan: what trade_reader.prepare() returned, for the Excel and date.
    :returns: 1 if any step or check failed, else 0.
    """
    case_id = case['id']
    scenario_id = case['scenario']
    trades = case['trades']
    report_name = report_dir(case_id)
    blob_key = case_id

    print('\n======================================================')
    print(f'TEST CASE {case_id}: {trade_reader.describe(case)}')
    for note in case['notes']:
        print(f'  note: {note}')
    print('======================================================')

    if case['problem']:
        return report_unrun_case(case, plan)

    if scenario_id:
        os.environ[SCENARIO_ENV] = scenario_id
    else:
        os.environ.pop(SCENARIO_ENV, None)

    try:
        own_group = scenario_netting_group(scenario_id) if scenario_id else None
        if own_group:
            start_sum(scenario_id, RUN_DISPLAY)

        recorded = []
        failures = 0

        for i, trade in enumerate(trades):
            entry = trade['entry']
            label = trade['label']
            stop_after = entry.get('runUpTo')
            expect = entry.get('expect') or {}
            delays = entry.get('delayBefore') or {}

            announce_trade(case_id, i + 1, len(trades), label, stop_after, expect, delays)

            row_key = f'{case_id}-{i + 1}'
            blob_prefix = f't{i + 1:02d}-'

            seed_trade_state()
            reset_calls(row_key)
            reset_injected_payload(row_key)

            auth_blob = BLOB_ROOT / blob_key / f'{blob_prefix}{AUTH_BLOB.name}'
            auth_ok = run_trade_auth(row_key, auth_blob)
            annotate_blob(auth_blob, label)

            # A trade of a case with no scenario file is staged in
            # single_unparsed.json first; one that does not parse is not injected.
            parse_failed = auth_ok and trade['single'] and not stage_single_trade(trade['booking'])
            # Set below for a trade that runs; recorded either way, so a
            # cancelTrade never resends a trade under another trade's env.
            trade_env = {}

            if parse_failed:
                # Filed as step 04's, the step that would have injected it.
                reason = (f'{label} could not be parsed from single_unparsed.json into '
                          'single_trade.json, so it is not injected.')
                print(f'{reason} Skipping its pipeline.', file=sys.stderr)
                inject_blob = BLOB_ROOT / blob_key / f'{blob_prefix}02-4.json'
                write_skipped_blob(inject_blob, '4', reason, outcome='failed')
                annotate_blob(inject_blob, label)
                failed = 1
            elif auth_ok:
                # SCENARIO_TRADE_INDEX picks the scenario entry the trade
                # follows; TRADE_INDEX, for an Excel trade, the trade itself.
                # A trade of a case with no scenario file was just staged in
                # single_unparsed.json, so its steps run as SINGLE_FILE.
                if trade['entryIndex'] is not None:
                    trade_env[SCENARIO_TRADE_INDEX_ENV] = str(trade['entryIndex'])
                if trade['tradeIndex'] is not None:
                    trade_env['TRADE_INDEX'] = str(trade['tradeIndex'])
                if trade['single']:
                    trade_env[UI_MODE_ENV] = 'SINGLE_FILE'

                failed = run_pipeline(
                    row_key,
                    trade_env,
                    stop_after=stop_after,
                    expect=expect,
                    blob_key=blob_key,
                    blob_prefix=blob_prefix,
                    trade_label=label,
                    delays=delays,
                    cancel=entry.get('cancel', False),
                    cancel_settlement=entry.get('cancelSettlement', False),
                )
            else:
                # Every step would 401, so this trade stops at auth.
                print(f'Auth failed for {case_id} trade {i + 1}. Skipping its pipeline.', file=sys.stderr)
                failed = 1

            # As in SCENARIO: only a trade step 04 accepted joins the sum, and a
            # trade the sum refuses fails, so the group is not settled on it.
            booked_id = recorded_trade()['tradeId']
            if own_group and booked_id:
                try:
                    add_to_sum(scenario_id, read_injected_payload(row_key), booked_id)
                except SumError as err:
                    print(f'{label}: not added to the {scenario_id} netting sum: {err}', file=sys.stderr)
                    failed += 1

            failures += failed
            # env: what its steps ran with, for a cancelTrade to resend it.
            recorded.append({'label': label, **recorded_trade(), 'failedSteps': failed,
                             'env': trade_env})

            os.environ['STATE_ROW'] = row_key
            row = build_run_record(
                row_key, failed,
                {'raw': json.dumps(trade['event'], indent=2) if trade['event'] else '',
                 'parsed': read_injected_payload(row_key)},
                test_case=case_id,
                report_name=report_name,
                # A netting case's trade stops where its group's steps take
                # over, so its row says how far it ran.
                ran_to=stop_after if own_group else None,
            )
            os.environ.pop('STATE_ROW', None)
            # A trade the simulator refused leaves no id in state; the row still
            # names the Excel's.
            row['tradeId'] = row['tradeId'] or trade['tradeId'] or ''

            out = write_run(row, row_key)
            print(f'Excel row {out["slNo"]} written to sheet [{out["sheet"]}] ({case_id}, {label})')

            reset_calls(row_key)
            reset_injected_payload(row_key)
            clear_state()

        # Saved before the merge, which deletes the blobs, for step 07c to read
        # the trades back; then the group runs once every trade has.
        if own_group:
            record_scenario_run(scenario_id, blob_key, recorded)
            if run_own_netting_group(scenario_id, own_group, recorded, report_name=report_name):
                failures += 1

        merge_scenario_report(case_id, blob_key, out_name=report_name, title=excel_case_title(case, plan))
    finally:
        os.environ.pop(SCENARIO_ENV, None)

    return 1 if failures else 0


def merge_reports(row_key, stop_after=None, scenario_id=None):
    """Merge a row's step blobs into one HTML report.

    :param stop_after: the last step the trade declares, so later steps are not
        reported as missing.
    :param scenario_id: the test case a single-trade SCENARIO run belongs to.
        The report folder and heading are then named after the case, as for a
        multi-trade case, so each case keeps its own report instead of every
        case run that day overwriting one shared SCENARIO folder.
    """
    blob_dir = BLOB_ROOT / row_key
    out_name = scenario_report_dir(scenario_id) if scenario_id else report_dir(row_key)
    out_dir = HTML_ROOT / out_name

    if not blob_dir.exists():
        print(f'No blobs found for {row_key}; skipping report merge.', file=sys.stderr)
        return

    blobs = sorted(p.name for p in blob_dir.glob('*.json'))
    report_missing_steps(row_key, blobs, stop_after)

    if scenario_id:
        trade_id = (StateHelper.get_state() or {}).get('injectedTradeId')
        title = scenario_report_title(scenario_id, trade_id or 'No trade ID')
    else:
        title = report_title()

    try:
        summary = merge_html_report(blob_dir, out_dir, title)
        if summary is None:
            print(f'No step results to merge for {row_key}.', file=sys.stderr)
            return

        shutil.rmtree(blob_dir, ignore_errors=True)
        print(f'HTML report [{title}]: reports/html/{out_name}/index.html')
    except OSError as err:
        print(f'Report merge failed for {row_key}: {err}', file=sys.stderr)


# Sorts after every step blob ('01-', 't01-', 'x01-'), so the crash shows last.
CRASH_BLOB = 'zz-crash.json'


def merge_crashed_report(blob_key, out_name, title, what):
    """Merge the step blobs a crashed case or run left behind into an HTML report.

    Without this they are deleted unmerged and the case has no report at all.
    A failed entry carrying the traceback being handled is added last, so the
    report shows where it stopped. Call it from inside the except block.

    :param what: what crashed, for the entry's heading, e.g. 'TC012'.
    """
    blob_dir = BLOB_ROOT / blob_key
    try:
        blob_dir.mkdir(parents=True, exist_ok=True)
        (blob_dir / CRASH_BLOB).write_text(json.dumps({
            'step': '',
            'label': f'{what} stopped before it finished',
            'exitStatus': None,
            'tests': [{
                'name': f'{what} crashed',
                'outcome': 'failed',
                'duration': 0.0,
                'message': traceback.format_exc()[-4000:],
            }],
        }, indent=2), encoding='utf-8')
        if merge_html_report(blob_dir, HTML_ROOT / out_name, title) is not None:
            shutil.rmtree(blob_dir, ignore_errors=True)
            print(f'HTML report of what {what} ran before it stopped: '
                  f'reports/html/{out_name}/index.html', file=sys.stderr)
    except Exception as err:   # the report is a by-product and must not mask the crash
        print(f'Could not write a report for {what}: {err}', file=sys.stderr)


# The tokens step 01 wrote in Phase 1: the only values a trade's state starts
# with, apart from DYNAMIC's seed. Holds live tokens, so it is owner-only and
# deleted when the run ends.
AUTH_SNAPSHOT = STATE_DIR / 'auth-snapshot.json'

# Left by an earlier runner, which snapshotted the whole post-auth state and
# restored it at the end. Deleted unread at start-up.
LEGACY_SNAPSHOT = STATE_DIR / 'default.backup.json'

# DYNAMIC builds its trade from these run-state keys, so each DYNAMIC trade is
# seeded with them from .state.example.json. Every other mode takes them from
# the trade it injects. Nothing stands in for a key the file does not set, so
# step 04 fails on it.
DYNAMIC_SEED_FILE = ROOT / '.state.example.json'
DYNAMIC_SEED_KEYS = ('reqPartyId', 'counterPartyId', 'tradeCurrencyPair',
                     'productType', 'dealtCurrency', 'referenceCurrency')


def clear_state():
    """Empty the run state, so nothing an earlier trade or run wrote can be read.

    Called at start-up, after every trade and when the run ends. A value a step
    needs that no earlier step of the same trade wrote is then missing, and the
    step's pre-flight fails instead of reading a leftover.
    """
    try:
        StateHelper.replace_state({})
    except OSError as err:
        print(f'WARNING: could not clear run state: {err}. The next trade may read '
              'this one\'s values.', file=sys.stderr)


def dynamic_seed():
    """Return the DYNAMIC trade's parties, currencies and product from DYNAMIC_SEED_FILE.

    Only the keys the file sets are returned. A missing one is named in a
    warning and left out, so step 04 fails on it.
    """
    try:
        config = json.loads(DYNAMIC_SEED_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as err:
        print(f'WARNING: could not read {DYNAMIC_SEED_FILE.name}: {err}. DYNAMIC has no '
              'parties or currencies to build its trade from.', file=sys.stderr)
        return {}

    seed = {key: config[key] for key in DYNAMIC_SEED_KEYS if config.get(key) not in (None, '')}
    missing = [key for key in DYNAMIC_SEED_KEYS if key not in seed]
    if missing:
        print(f'WARNING: {DYNAMIC_SEED_FILE.name} does not set {", ".join(missing)}, so the '
              'DYNAMIC trade is built without them.', file=sys.stderr)
    return seed


def seed_trade_state():
    """Start a trade's state from this run's tokens only, plus DYNAMIC's seed.

    Replaces the whole file, so no key from an earlier trade survives.
    """
    seed = {}
    if AUTH_SNAPSHOT.exists():
        seed.update(json.loads(AUTH_SNAPSHOT.read_text(encoding='utf-8')))
    if INJECTION_MODE == 'DYNAMIC':
        seed.update(dynamic_seed())
    StateHelper.replace_state(seed)


def read_payload_file(file_name):
    """Return a payload file's text, or '' if it is missing or unreadable."""
    file = PAYLOAD_DIR / file_name
    if not file.exists():
        return ''
    try:
        return file.read_text(encoding='utf-8')
    except OSError:
        return ''


def result_text(failed, ran_to=None):
    """Return the Overall Result cell: PASS, or FAIL and how many steps failed.

    :param ran_to: the last step of a row that by design covers only part of
        the pipeline. A netting case's trades (TC010 to TC013, TC018) each stop
        at 05b and its netting group row runs on to its runUpTo, so a pass reads
        PASS (up to 05b) or PASS (up to 15b) rather than as a run of every step.
    """
    if failed:
        return f'FAIL ({failed} step{"s" if failed > 1 else ""})'
    if ran_to:
        number, suffix = order_key(ran_to)
        return f'PASS (up to {number:02d}{suffix})'
    return 'PASS'


def build_run_record(row_key, failed, payloads, test_case=None, report_name=None, ran_to=None):
    """Build one Excel row from run state and the step failure count.

    :param payloads: {'raw': ..., 'parsed': ...} text for the payload columns.
    :param test_case: the scenario id for the Test Case column; blank for
        DYNAMIC and the file modes, which have no test case.
    :param report_name: the report folder this row links to. A multi-trade
        case writes one row per trade but one report, so every row names that
        report.
    :param ran_to: the last step the row's flow runs to, named in a passed
        row's result (result_text); None for a row that runs every step.
    """
    s = StateHelper.get_state() or {}
    return {
        'htmlReport':      report_name or report_dir(row_key),
        'tradeId':         s.get('injectedTradeId') or '',
        'testCase':        test_case or '',
        'productType':     s.get('productType') or 'FxSpot',
        'tradeDate':       s.get('tradeDate') or s.get('valueDate') or '',
        'valueDate':       s.get('valueDate') or '',
        'mode':            INJECTION_MODE,
        'currencyPair':    s.get('tradeCurrencyPair') or '',
        'party':           s.get('reqPartyId') or '',
        'counterParty':    s.get('counterPartyId') or '',
        'nettingGroupA':   s.get('nettingGroupIdString') or '',
        'nettingStateA':   s.get('nettingStatusA') or '',
        'nettingGroupB':   s.get('nettingGroupIdStringB') or '',
        'nettingStateB':   s.get('nettingStatusB') or '',
        'settlementId':    s.get('settlementId') or '',
        'settlementState': s.get('settlementState') or '',
        'result':          result_text(failed, ran_to),
        'rawPayload':      payloads['raw'],
        'parsedPayload':   payloads['parsed'],
    }


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    """Run the pipeline for the configured mode and return the process exit code."""
    exit_code = 0

    # Owner-only for everything this run and its step processes create: state
    # and its snapshot hold live tokens; blobs and reports hold trade data.
    os.umask(0o077)

    # Step subprocesses write straight to this terminal, so the runner's own
    # output is line-buffered to keep it in order with theirs.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    print('\n======================================================')
    print(f'OSTTRA API SUITE RUNNER | MODE: [{INJECTION_MODE}]')
    print('======================================================\n')

    reset_blobs()

    # Nothing from an earlier run is read: its state is cleared, tokens
    # included, and a snapshot a killed run left behind is deleted unread.
    for leftover in (AUTH_SNAPSHOT, LEGACY_SNAPSHOT):
        leftover.unlink(missing_ok=True)
    clear_state()

    # ------------------------------------------------------------------------
    # PHASE 0: PARSE RAW TRADE EVENTS (file modes only)
    # Aborts on failure: injecting a stale file would silently re-test the
    # previous trade.
    # ------------------------------------------------------------------------
    payloads = {'raw': '', 'parsed': ''}

    # IMPORT_EXCEL's own Phase 0 picks each case's trades from the Excel, then
    # imports and parses them as IMPORT's does. Without the Excel, the day's
    # folder or a clean parse, no case can run on the trades it names.
    # trade_reader.py's EXCEL_FILES are read one after another: the first
    # here, each next one in Phase 2 once the one before it has run.
    excel_files = trade_reader.excel_files()
    excel_plan = None
    if INJECTION_MODE == 'IMPORT_EXCEL':
        print("PHASE 0: Picking each test case's trades from the Excel, then importing "
              'and parsing them...\n')
        try:
            excel_plan = trade_reader.prepare(excel_files[0])
        except (OSError, ValueError) as err:
            print(f'\nFATAL: {err}\nAborting - no test case can run on the trades the '
                  'Excel names.\n', file=sys.stderr)
            return 1
        print('')

    if INJECTION_MODE == 'IMPORT':
        # A failed import has already emptied multiple_unparsed.json, so parsing
        # would fail too; stopping here names the real cause instead.
        print('PHASE 0: Importing the day\'s trade files into multiple_unparsed.json...\n')
        imported = subprocess.run([sys.executable, str(IMPORT_SCRIPT)], cwd=ROOT)
        if imported.returncode != 0:
            print('\nFATAL: Import failed. Aborting - there are no trades for this day '
                  'to inject.\n', file=sys.stderr)
            return 1
        print('')

    if INJECTION_MODE in ('SINGLE_FILE', 'MULTIPLE_FILE', 'IMPORT'):
        # IMPORT parses what it just imported with MULTIPLE_FILE's job.
        parse_job = 'MULTIPLE_FILE' if INJECTION_MODE == 'IMPORT' else INJECTION_MODE
        print(f'PHASE 0: Parsing raw trade events for {INJECTION_MODE}...\n')
        parsed = subprocess.run(
            [sys.executable, 'parse_trades.py', parse_job],
            cwd=ROOT,
        )
        if parsed.returncode != 0:
            print(f'\nFATAL: Parsing failed for {INJECTION_MODE}. Aborting - the injection '
                  'file is stale or invalid.\n', file=sys.stderr)
            return 1
        print('')

        suffix = 'single' if INJECTION_MODE == 'SINGLE_FILE' else 'multiple'
        payloads = {
            'raw':    read_payload_file(f'{suffix}_unparsed.json'),
            'parsed': read_payload_file(f'{suffix}_trade.json'),
        }
    # DYNAMIC and SCENARIO have no file to parse; step 04 records the payload it
    # injects.
    # IMPORT falls through to the MULTIPLE_FILE branch below from here on.

    try:
        # --------------------------------------------------------------------
        # PHASE 1: GLOBAL SETUP (Auth)
        # --------------------------------------------------------------------
        print('PHASE 1: Executing Global Setup (Step 01)...')

        auth_ok = run_pytest('tests/test_01_auth.py', {
            'CURRENT_STEP': '1',
            'BLOB_FILE': str(AUTH_BLOB),
            'CI': 'true',
        })
        if not auth_ok:
            print('\nFATAL: Authentication failed. Aborting - every downstream step '
                  'would 401.\n', file=sys.stderr)
            return 1

        # State was cleared at start-up, so it now holds only the tokens step 01
        # wrote. Every trade's state starts from a copy of them.
        state_file_path = state_file()

        if state_file_path.exists():
            shutil.copyfile(state_file_path, AUTH_SNAPSHOT)
            # Holds live tokens; copyfile keeps an existing file's mode.
            AUTH_SNAPSHOT.chmod(0o600)
        else:
            print(f'WARNING: {state_file_path} was not found. Downstream steps may have '
                  'missing variables.', file=sys.stderr)

        # --------------------------------------------------------------------
        # PHASE 2: PIPELINE ROUTING
        # --------------------------------------------------------------------
        # IMPORT_EXCEL names a scenario per case itself (run_excel_case), so a
        # TEST_SCENARIO left in the environment must not pick one here.
        scenario_id = None if INJECTION_MODE == 'IMPORT_EXCEL' else resolve_scenario_id()

        # A continuation and the row it continues run as a pair: selecting
        # either runs the row's trades, then the continuation against the
        # record the row has just written. Each keeps its own report and Excel
        # rows. TEST_SCENARIO tells every step process which of the two it
        # belongs to. No scenario continues another at present.
        continuation = scenario_continuation(scenario_id) if scenario_id else None
        follow_up = None
        if continuation:
            follow_up, scenario_id = scenario_id, continuation['continues']
        elif scenario_id:
            follow_up = continuation_of(scenario_id)
        if follow_up:
            print(f'\nPHASE 2: {scenario_id} and {follow_up} run as a pair, '
                  f'{scenario_id} first.')
            os.environ[SCENARIO_ENV] = scenario_id

        scenario_entries = scenario_trades(scenario_id) if scenario_id else []

        if INJECTION_MODE == 'IMPORT_EXCEL':
            for n, excel_file in enumerate(excel_files):
                # Each workbook rewrites multiple_trade.json, so the next is
                # prepared only once the one before it has run. One that
                # cannot be prepared is skipped; the ones after it still run.
                if n:
                    print(f"\nPHASE 0: Picking each test case's trades from {excel_file}, then "
                          'importing and parsing them...\n')
                    try:
                        excel_plan = trade_reader.prepare(excel_file)
                    except (OSError, ValueError) as err:
                        print(f'\n{excel_file} skipped: {err}', file=sys.stderr)
                        exit_code = 1
                        continue

                cases = excel_plan['cases']
                print(f'\nPHASE 2: {len(cases)} test case(s) from {excel_plan["excel"]}, on the '
                      f'trades of {excel_plan["date"]}. Commencing Batch Execution...')

                for case in cases:
                    if not case['source'] and not case['problem']:
                        print(f'\n{case["id"]}: {trade_reader.describe(case)}')
                        continue
                    try:
                        if run_excel_case(case, excel_plan):
                            exit_code = 1
                    except Exception as error:
                        # One case's crash must not end a batch of thirty: the
                        # cases after it still run. Its report still shows the
                        # steps it ran and where it stopped.
                        print(f'\n{case["id"]} stopped: {error}', file=sys.stderr)
                        traceback.print_exc()
                        merge_crashed_report(
                            case['id'], report_dir(case['id']),
                            f'{case["id"]} — stopped: {type(error).__name__} — {RUN_DISPLAY}',
                            case['id'])
                        exit_code = 1
                        clear_state()

        elif len(scenario_entries) > 1:
            # A multi-trade scenario. Each trade gets its own pipeline run (one
            # simulator call per trade) and its own Excel row; the case gets one
            # HTML report.
            print(f'\nPHASE 2: Scenario {scenario_id} has {len(scenario_entries)} '
                  'trades. Commencing Batch Execution...\n')

            # Every trade writes to one blob directory so the case merges into a
            # single report. Trade blobs are prefixed t01-, t02-, ..., which also
            # orders the report trade by trade. Each trade writes its own step 01
            # there, so Phase 1's auth blob is not copied in.
            blob_key = scenario_id

            # One identity for the whole duplicate set. Every trade that declares
            # identifiers "shared" injects it, so the copies are true duplicates
            # of one trade. Generated per run so the set never collides with the
            # previous run's.
            shared_ids = (json.dumps(TestUtils.generate_trade_identifiers())
                          if any(e.get('identifiers') == 'shared'
                                 for e in scenario_entries) else None)
            if shared_ids:
                print(f'Shared identity for the duplicate set: '
                      f'{json.loads(shared_ids)["tradeId"]}\n')

            # What each trade booked, saved for the netting group check.
            recorded = []

            # A case that nets its trades together (TC010 to TC013, TC018) keeps their
            # sum as they are booked, for steps 07c, 12 and 12b to check the
            # group against (utils/sum_storer.py).
            own_group = scenario_netting_group(scenario_id)
            if own_group:
                start_sum(scenario_id, RUN_DISPLAY)

            for i, entry in enumerate(scenario_entries):
                label = entry.get('label') or f'trade {i + 1}'
                stop_after = entry.get('runUpTo')
                expect = entry.get('expect') or {}
                delays = entry.get('delayBefore') or {}

                announce_trade(scenario_id, i + 1, len(scenario_entries), label,
                               stop_after, expect, delays)

                row_key = f'{scenario_id}-{i + 1}'
                blob_prefix = f't{i + 1:02d}-'

                # Starts from this run's tokens only, so a trade cannot read
                # anything the previous trade wrote.
                seed_trade_state()
                reset_calls(row_key)
                reset_injected_payload(row_key)

                # Fresh tokens before every injection, as IMPORT_EXCEL does, so a
                # later trade of a long case never runs on tokens that expired.
                # This is the trade's step 01.
                auth_blob = BLOB_ROOT / blob_key / f'{blob_prefix}{AUTH_BLOB.name}'
                auth_ok = run_trade_auth(row_key, auth_blob)
                annotate_blob(auth_blob, label)

                trade_env = {SCENARIO_TRADE_INDEX_ENV: str(i)}
                if entry.get('identifiers') == 'shared' and shared_ids:
                    trade_env[SCENARIO_SHARED_IDS_ENV] = shared_ids

                if auth_ok:
                    failed = run_pipeline(
                        row_key,
                        trade_env,
                        stop_after=stop_after,
                        expect=expect,
                        blob_key=blob_key,
                        blob_prefix=blob_prefix,
                        trade_label=label,
                        delays=delays,
                        cancel=entry.get('cancel', False),
                        cancel_settlement=entry.get('cancelSettlement', False),
                    )
                else:
                    # Every step would 401, so this trade stops at auth.
                    print(f'Auth failed for {scenario_id} trade {i + 1}. Skipping its pipeline.',
                          file=sys.stderr)
                    failed = 1

                # Added from the payload step 04 injected, and only for a trade it
                # had accepted: a rejected trade never reaches the group. A trade
                # the sum refuses counts as a failed step, so its group is not
                # settled on a sum that cannot be relied on.
                booked_id = recorded_trade()['tradeId']
                if own_group and booked_id:
                    try:
                        add_to_sum(scenario_id, read_injected_payload(row_key), booked_id)
                    except SumError as err:
                        print(f'{label}: not added to the {scenario_id} netting sum: {err}',
                              file=sys.stderr)
                        failed += 1

                if failed:
                    exit_code = 1

                # env: what its steps ran with, for a cancelTrade to resend it.
                recorded.append({'label': label, **recorded_trade(), 'failedSteps': failed,
                                 'env': trade_env})

                # One Excel row per trade, each naming the case and its single
                # report.
                os.environ['STATE_ROW'] = row_key
                record = build_run_record(
                    row_key, failed,
                    {'raw': '', 'parsed': read_injected_payload(row_key)},
                    test_case=scenario_id,
                    report_name=scenario_report_dir(scenario_id),
                    ran_to=stop_after if own_group else None,
                )
                os.environ.pop('STATE_ROW', None)

                out = write_run(record, row_key)
                print(f'Excel row {out["slNo"]} written to sheet [{out["sheet"]}] '
                      f'({scenario_id}, {label})')

                reset_calls(row_key)
                reset_injected_payload(row_key)
                # Its row and record are written, so nothing of this trade is
                # needed any more.
                clear_state()

            # Recorded before the merge, which deletes the blobs the record keeps,
            # and before the netting group check, which reads the trades back
            # from it. A continuation therefore carries only the trades' steps.
            record_scenario_run(scenario_id, blob_key, recorded)

            # A case that nets its trades together checks their netting group
            # once all of them have run, and takes it on to its runUpTo.
            if own_group and run_own_netting_group(scenario_id, own_group, recorded):
                exit_code = 1

            # One report for the whole case, merged once every trade has run.
            merge_scenario_report(scenario_id, blob_key)

        elif INJECTION_MODE in ('MULTIPLE_FILE', 'IMPORT'):
            if not MULTIPLE_TRADES_PATH.exists():
                raise FileNotFoundError(
                    f'{INJECTION_MODE} mode requires multiple_trade.json at {MULTIPLE_TRADES_PATH}'
                )

            parsed_data = json.loads(MULTIPLE_TRADES_PATH.read_text(encoding='utf-8'))

            # Same flattening as trade_payload.py, so the trade TRADE_INDEX
            # selects is the one the runner announces.
            trades = flatten_trades(parsed_data)

            print(f'\nPHASE 2: Found {len(trades)} trades. Commencing Batch Execution...\n')

            for i, trade in enumerate(trades):
                print('\n------------------------------------------------------')
                print(f'EXECUTING TRADE INDEX: {i} (Trade {i + 1} of {len(trades)})')
                print('------------------------------------------------------\n')

                row_key = f'trade-{i + 1}'

                # Starts from this run's tokens only, as in the scenario branch.
                seed_trade_state()
                reset_calls(row_key)
                reset_injected_payload(row_key)

                # Fresh tokens for every trade, so a later trade does not run on
                # tokens that expired during the batch. This is the trade's step 01.
                auth_ok = run_trade_auth(row_key, BLOB_ROOT / row_key / AUTH_BLOB.name)

                if auth_ok:
                    failed = run_pipeline(row_key, {'TRADE_INDEX': str(i)})
                else:
                    # Every step would 401, so this trade stops at auth.
                    print(f'Auth failed for trade {i + 1}. Skipping its pipeline.', file=sys.stderr)
                    failed = 1
                if failed:
                    exit_code = 1

                merge_reports(row_key)

                os.environ['STATE_ROW'] = row_key
                record = build_run_record(row_key, failed, {
                    'raw':    payloads['raw'],
                    'parsed': json.dumps(trade, indent=2),
                })
                os.environ.pop('STATE_ROW', None)

                out = write_run(record, row_key)
                # Names the workbook, since IMPORT rows go to their own.
                print(f'Excel row {out["slNo"]} written to sheet [{out["sheet"]}] '
                      f'of {Path(out["workbook"]).name}')

                # The call log and injected payload exist only to build the row
                # above, so drop them rather than leave a pair per trade in
                # .state. Run state is cleared for the same reason.
                reset_calls(row_key)
                reset_injected_payload(row_key)
                clear_state()
        else:
            # ----------------------------------------------------------------
            # DYNAMIC, SINGLE_FILE OR SINGLE-TRADE SCENARIO
            # ----------------------------------------------------------------
            print('\nPHASE 2: Executing Single Pass Pipeline...\n')

            row_key = 'run'

            # A single-trade scenario can still declare runUpTo, expect,
            # delayBefore, cancel and cancelSettlement, read as in the
            # multi-trade branch. Other modes leave them unset and run every step.
            only = scenario_entries[0] if scenario_entries else {}
            stop_after = only.get('runUpTo')
            expect = only.get('expect') or {}
            delays = only.get('delayBefore') or {}
            cancel = only.get('cancel', False)
            cancel_settlement = only.get('cancelSettlement', False)
            if cancel_settlement:
                print(f'The trade {describe_settlement_cancel(cancel_settlement)}.\n')

            seed_trade_state()
            reset_calls(row_key)
            reset_injected_payload(row_key)
            seed_auth_blob(row_key)

            failed = run_pipeline(row_key, stop_after=stop_after, expect=expect, delays=delays,
                                  cancel=cancel, cancel_settlement=cancel_settlement)
            if failed:
                exit_code = 1

            merge_reports(row_key, stop_after, scenario_id=scenario_id)

            os.environ['STATE_ROW'] = row_key
            # DYNAMIC and SCENARIO have no source file, so the Injected Payload
            # column shows what step 04 injected. A generated trade has no
            # unparsed form, so the raw column stays blank.
            record = build_run_record(
                row_key, failed,
                {'raw': '', 'parsed': read_injected_payload(row_key)}
                if INJECTION_MODE in ('DYNAMIC', 'SCENARIO') else payloads,
                # Set only for a single-trade scenario; blank for other modes.
                test_case=scenario_id,
                # The case's own report folder rather than the shared per-mode one.
                report_name=scenario_report_dir(scenario_id) if scenario_id else None,
            )
            os.environ.pop('STATE_ROW', None)

            out = write_run(record, row_key)
            print(f'Excel row {out["slNo"]} written to sheet [{out["sheet"]}]')

            # Spent once the row is written; see the MULTIPLE_FILE branch above.
            reset_calls(row_key)
            reset_injected_payload(row_key)
            clear_state()

        if follow_up:
            os.environ[SCENARIO_ENV] = follow_up
            if run_continuation(follow_up, scenario_continuation(follow_up)):
                exit_code = 1

        shutil.rmtree(BLOB_ROOT, ignore_errors=True)

        if exit_code == 1:
            print('\nPIPELINE COMPLETE WITH FAILURES.\n')
        else:
            print('\nPIPELINE EXECUTION COMPLETE!\n')

        return exit_code

    except Exception as error:
        print(f'\nFATAL PIPELINE ERROR: {error}', file=sys.stderr)
        traceback.print_exc()
        # The steps each unfinished row ran are still reported, in a folder
        # marked CRASHED so it is never mistaken for a complete run.
        for blob_dir in sorted(p for p in BLOB_ROOT.glob('*')
                               if p.is_dir() and p != AUTH_BLOB.parent):
            merge_crashed_report(
                blob_dir.name, f'{RUN_DATE}__{INJECTION_MODE}__{blob_dir.name}__CRASHED',
                f'{blob_dir.name} — run stopped: {type(error).__name__} — {RUN_DISPLAY}',
                blob_dir.name)
        return 1

    finally:
        # However the run ends, it leaves no state behind for the next one.
        clear_state()
        AUTH_SNAPSHOT.unlink(missing_ok=True)


if __name__ == '__main__':
    sys.exit(main())
