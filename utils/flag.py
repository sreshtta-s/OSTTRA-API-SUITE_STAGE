"""Operator settings: the injection mode step 04 uses and, for SCENARIO, the row.

This file, not .env, decides the mode. To switch, edit the INJECTION_MODE line.

  DYNAMIC       -> build a fresh payload from run state and generated ids
  SINGLE_FILE   -> inject api/json_payload_trade_injection/single_trade.json
  MULTIPLE_FILE -> inject each trade in turn from
                   api/json_payload_trade_injection/multiple_trade.json
  IMPORT        -> collect import_trade/trades/<IMPORT_DATE>/ into
                   multiple_unparsed.json first, then parse it and inject
                   each trade in turn, exactly as MULTIPLE_FILE does; its rows
                   go to reports/excel/osttra-api-suite-import-report.xlsx
  SCENARIO      -> inject the test scenario in data/osttra_test_scenarios/
                   named by SCENARIO below
  IMPORT_EXCEL  -> run every test case, TC001 on, on the trades the Excel in
                   data/osttra_test_scenarios/ names for it, found in
                   import_trade/trades/<TRADE_DATE>/ and parsed as IMPORT
                   parses them; each case follows its scenario file's flow.
                   A case with no scenario file runs each of its trades
                   alone, through single_unparsed.json, as SINGLE_FILE does.
                   The Excel, the date and the cases to run are set in
                   data/osttra_test_scenarios/trade_reader.py. Its rows go to
                   reports/excel/osttra-api-suite-import-excel-report.xlsx
"""

import os

INJECTION_MODE = 'DYNAMIC'  # <-- EDIT THIS LINE to switch modes

# The day IMPORT collects is IMPORT_DATE in import_trade/import_trade_runner.py,
# a DD_MM_YYYY folder name; blank there means today. Other modes ignore it.

# The file in data/osttra_test_scenarios/ that SCENARIO mode injects, without
# the .json. Other modes ignore it, so it can stay set while you run DYNAMIC:
# only INJECTION_MODE decides whether a scenario is used.
#
# Each file holds one or more trades (none for a continuation such as TC011) in
# the wrapper shape DYNAMIC builds, with the values the test sheet fixes and a
# {{TOKEN}} wherever the sheet leaves a value open. Tokens are resolved on every
# run, so a scenario never injects a stale date or a trade id already used.
SCENARIO = 'TC036'  # <-- EDIT THIS LINE to switch scenarios

# Per-process override of INJECTION_MODE, for a caller that launches the
# pipeline as a child process and sets this variable on it. Deliberately not
# named INJECTION_MODE, so an INJECTION_MODE key left in .env has no effect.
UI_MODE_ENV = 'UI_INJECTION_MODE'

# Per-process override of SCENARIO. It only picks the row and never switches
# the mode, so a stray TEST_SCENARIO cannot turn an ordinary run into a
# scenario run.
SCENARIO_ENV = 'TEST_SCENARIO'

VALID_MODES = ['DYNAMIC', 'SINGLE_FILE', 'MULTIPLE_FILE', 'IMPORT', 'SCENARIO', 'IMPORT_EXCEL']


def resolve_injection_mode():
    """Return the active mode: UI_INJECTION_MODE if set, else INJECTION_MODE.

    An unknown mode raises ValueError naming where it was set, rather than
    silently falling back to DYNAMIC and ignoring the intended payload file.
    """
    override = os.environ.get(UI_MODE_ENV)
    source = INJECTION_MODE if override is None or str(override).strip() == '' else override
    mode = str(source).strip()

    if mode not in VALID_MODES:
        where = 'utils/flag.py: INJECTION_MODE' if source == INJECTION_MODE else UI_MODE_ENV
        raise ValueError(
            f"{where} '{source}' is not a valid mode. "
            f'Expected one of: {", ".join(VALID_MODES)}.'
        )

    return mode


def resolve_scenario_id():
    """Return the scenario id to inject, or None when this is not a SCENARIO run.

    trade_payload.py checks this first, so returning None in every other mode
    keeps the scenario route out of DYNAMIC, SINGLE_FILE, MULTIPLE_FILE and
    IMPORT runs.

    The id is upper-cased so TEST_SCENARIO=tc001 finds TC001.json. A blank id
    raises ValueError here, naming both places it can be set, rather than
    failing later as a missing file.

    IMPORT_EXCEL runs many cases, so runner.py names each case's scenario in
    TEST_SCENARIO for its step processes, and SCENARIO above is never read. A
    case with no scenario file runs without one: its trades' steps run as
    SINGLE_FILE (UI_INJECTION_MODE), so this returns None for them.
    """
    mode = resolve_injection_mode()

    if mode == 'IMPORT_EXCEL':
        return str(os.environ.get(SCENARIO_ENV) or '').strip().upper() or None

    if mode != 'SCENARIO':
        return None

    override = os.environ.get(SCENARIO_ENV)
    source = SCENARIO if override is None or str(override).strip() == '' else override
    scenario_id = str(source).strip().upper()

    if not scenario_id:
        raise ValueError(
            'INJECTION_MODE is SCENARIO but no scenario is selected. Set '
            f'utils/flag.py: SCENARIO to a row id such as TC001, or set '
            f'{SCENARIO_ENV} for a single run.'
        )

    return scenario_id


def scenario_source():
    """Return where the active scenario id was set, for use in error messages."""
    override = os.environ.get(SCENARIO_ENV)
    if override is None or str(override).strip() == '':
        return 'utils/flag.py: SCENARIO'
    return SCENARIO_ENV
