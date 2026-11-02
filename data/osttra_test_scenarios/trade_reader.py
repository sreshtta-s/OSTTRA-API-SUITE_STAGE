#!/usr/bin/env python3
"""Pick each test case's trades from the Excel, then import and parse them for IMPORT_EXCEL.

Each Excel in this folder named in EXCEL_FILES has one row per test case: column
"TC (Test ID traceability)" holds the case id and "matchId" its trade ids,
separated by '#', or N/A when the case has none. Each id is looked up in
import_trade/trades/<TRADE_DATE>/, whose file names start with the trade id.

The trades of cases with a scenario file are written, case by case, to
multiple_unparsed.json the way import_trade/import_trade_runner.py writes a
day's import, and parse_trades.py turns that file into multiple_trade.json
(utils/trade_parser.py, then the party id and currency pair steps). With
INJECTION_MODE = 'IMPORT_EXCEL' in utils/flag.py, runner.py does all of this
first, then runs the cases in test sheet order, TC001 on, each injecting its
own trades by TRADE_INDEX. A case with no scenario file instead runs each of
its trades as a single trade: runner.py writes it to single_unparsed.json just
before it runs, and parse_trades.py turns that into single_trade.json for step
04. What a case runs on depends on the Excel and on whether it has a scenario
file here:

  Excel trades + scenario file   The Excel's trades take the place of the
                                 scenario's generated ones and follow its flow:
                                 runUpTo, expect and delayBefore per trade, and
                                 a nettingTotal checked against the sum stored
                                 as they are booked. The scenario says how many
                                 trades the case books (TC010 10, TC011 8,
                                 TC012 20, TC013 40, TC018 4); the first that many the
                                 Excel lists and the day's folder holds are
                                 booked.
  Excel trades, no scenario      Each trade, one at a time, through
                                 single_unparsed.json and every step, as a
                                 SINGLE_FILE trade. No trade or settlement
                                 cancel step runs: only a scenario declares
                                 them.
  N/A                            Skipped: listed, with no report or Excel row.
                                 A scenario file's own trades are not generated
                                 in place of the Excel's.

Every Excel trade is booked as a new trade. For a cancellation case the
folder holds what TradeServ last published for the trade, the cancellation
itself (BilateralCancellation, status Cancelled); as_booking() turns a copy of
it back into the trade's booking, so step 04 books it as a scenario's
generated trade is booked, and only a scenario's cancel steps cancel it.

A case is not run when the folder holds fewer of its trades than it books, or
when a trade it nets cannot join its netting group. It is reported as Not
Present, and left out of the Summary's counts, when trade ids the Excel names
for it are not in the folder; otherwise as a failure. A trade listed twice for one case is booked once. Trades in the folder
that the Excel names for no case are listed and left out.

  python data/osttra_test_scenarios/trade_reader.py   pick, import and parse; list the cases
"""

import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook  # noqa: E402

import parse_trades  # noqa: E402
from api.payloads.trade_payload import (  # noqa: E402
    SCENARIO_DIR,
    available_scenarios,
    flatten_trades,
    netting_total_differences,
    scenario_netting_group,
    scenario_trades,
)
from import_trade.import_trade_runner import DATE_FORMAT, OUT_PATH, TRADES_DIR, write_events  # noqa: E402
from utils.trade_parser import extract_wrapper, parse_trade  # noqa: E402

# The workbooks in this folder that name each case's trades, read one after
# another in this order: the first one's trades are picked, imported and
# parsed and its cases run, then the same for the next. Each takes its trades
# from TRADE_DATE's folder. Empty means the only .xlsx here.
EXCEL_FILES = [  # <-- EDIT THESE LINES to read other workbooks
    'test_results_174138.xlsx',
    'test_results_163731.xlsx',
]

# The folder under import_trade/trades the Excel's trades are found in, named
# DD_MM_YYYY like IMPORT_DATE in import_trade/import_trade_runner.py. Blank
# means today's folder.
TRADE_DATE = '22_10_2026'  # <-- EDIT THIS LINE to take the trades from another day

# The cases to run, such as ['TC010', 'TC013']. Empty runs every case.
CASES = []  # <-- EDIT THIS LINE to run only some cases

# The Excel's columns, found by header ignoring case and surrounding spaces.
CASE_HEADER = 'TC (Test ID traceability)'
TRADE_IDS_HEADER = 'matchId'
TRADE_ID_SEPARATOR = '#'

# A trade ids cell that names no trade.
NO_TRADES = ('', 'N/A', 'NA', '-')

# A case id: TC, its number, and an optional letter (TC003A).
CASE_ID_RE = re.compile(r'^TC(\d+)([A-Z]*)$')

PARSED_PATH = parse_trades.DIR / parse_trades.JOBS['MULTIPLE_FILE']['out']

# The fields utils/trade_parser.py carries that mark a trade event as a
# cancellation, and the value each takes in a booking, as in a scenario's
# generated trade: (where in the wrapper, key, booking value).
BOOKING_FIELDS = (
    ('metaData', 'status', 'New'),
    ('metaData', 'positionStatus', 'Active'),
    ('sourcePlatform', 'eventType', 'Trade'),
    ('fx', 'activeStatus', 'Active'),
    ('fx', 'transactionType', 'Trade'),
)


def resolve_trade_date():
    """Return TRADE_DATE, or today's folder name when it is blank.

    :raises ValueError: if TRADE_DATE is not a real DD_MM_YYYY date.
    """
    date = TRADE_DATE.strip()
    if not date:
        return datetime.now().strftime(DATE_FORMAT)
    try:
        datetime.strptime(date, DATE_FORMAT)
    except ValueError:
        raise ValueError(f"trade_reader.py: TRADE_DATE '{TRADE_DATE}' is not a DD_MM_YYYY "
                         'date such as 29_09_2026.') from None
    return date


def excel_files():
    """Return the workbooks to read, in order: EXCEL_FILES, or [None] for the only .xlsx here."""
    return [name.strip() for name in EXCEL_FILES if name.strip()] or [None]


def excel_path(excel_file=None):
    """Return the Excel to read: excel_file, or the only .xlsx in this folder when it is None.

    Excel's own lock files (~$name.xlsx), left while a workbook is open, are
    not counted.
    """
    if excel_file:
        path = SCENARIO_DIR / excel_file
        if not path.is_file():
            raise FileNotFoundError(f"trade_reader.py: EXCEL_FILES names '{excel_file}', which is "
                                    f'not in {SCENARIO_DIR}.')
        return path

    found = [p for p in sorted(SCENARIO_DIR.glob('*.xlsx')) if not p.name.startswith('~$')]
    if len(found) != 1:
        raise ValueError(f'trade_reader.py: EXCEL_FILES is empty and {SCENARIO_DIR} holds '
                         f'{len(found)} .xlsx files, not one. Name the ones to read.')
    return found[0]


def read_excel(path):
    """Return {case id: [trade ids]} from the Excel's first sheet, in row order.

    A case whose cell is N/A or blank gets []. A case listed twice raises,
    since its two rows could name different trades.
    """
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as err:  # openpyxl raises several types for a file it cannot open
        raise ValueError(f'{path.name} cannot be opened as an Excel workbook: {err}') from err

    try:
        rows = workbook.worksheets[0].iter_rows(values_only=True)
        headers = [str(value or '').strip() for value in next(rows, ())]
        lowered = [header.lower() for header in headers]
        if CASE_HEADER.lower() not in lowered or TRADE_IDS_HEADER.lower() not in lowered:
            raise ValueError(f'{path.name} needs the columns "{CASE_HEADER}" and "{TRADE_IDS_HEADER}" '
                             f'in its first row; it has {", ".join(h for h in headers if h) or "none"}.')
        case_col = lowered.index(CASE_HEADER.lower())
        ids_col = lowered.index(TRADE_IDS_HEADER.lower())

        cases = {}
        for row in rows:
            case_id = _cell(row, case_col).upper()
            if not case_id:
                continue
            if case_id in cases:
                raise ValueError(f'{path.name} lists {case_id} more than once.')
            ids = _cell(row, ids_col)
            cases[case_id] = [] if ids.upper() in NO_TRADES else \
                [i.strip() for i in ids.split(TRADE_ID_SEPARATOR) if i.strip()]
        return cases
    finally:
        workbook.close()


def _cell(row, col):
    """Return a row's cell as stripped text, '' when it is empty or past the row's end."""
    value = row[col] if col < len(row) else None
    return '' if value is None else str(value).strip()


def index_trades(folder):
    """Return every usable trade file in folder, keyed by each id it can be found by.

    A file is found by its name's leading trade id (the Excel's matchId) and by
    its sourcePlatform tradeId and matchId. A file that is not valid JSON,
    holds no trade wrapper or cannot be parsed is left out and named in the
    returned problems, so a case that needs it is not run rather than the whole
    batch failing at the parse.

    :returns: (index, problems).
    """
    index = {}
    problems = []

    for path in sorted(folder.glob('*.json')):
        try:
            event = json.loads(path.read_text(encoding='utf-8-sig'))
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            problems.append(f'{path.name} is not valid JSON: {err}')
            continue

        wrapper = extract_wrapper(event)
        if wrapper is None:
            problems.append(f'{path.name} holds no trade wrapper '
                            '(publishEvent -> eventPayload -> current -> wrapper).')
            continue
        try:
            parse_trade(event)
        except (ValueError, KeyError, TypeError) as err:
            problems.append(f'{path.name} cannot be parsed: {err}')
            continue

        source = (wrapper.get('sourcePlatform') or [{}])[0] or {}
        trade = {
            'file': path.name,
            'event': event,
            # The id utils/trade_parser.py keeps, used to check each trade
            # parsed into its own place in multiple_trade.json.
            'tradeId': source.get('tradeId') or (wrapper.get('metaData') or {}).get('sourcePlatformTradeId'),
        }
        for key in (path.name.split('_', 1)[0], trade['tradeId'], source.get('matchId')):
            if key:
                index.setdefault(key, trade)

    return index, problems


def find_trade(index, trade_id):
    """Return the indexed trade an Excel id names, or None.

    An exact key first; failing that, the one trade whose tradeId starts with
    the id, since a source tradeId is the Excel's matchId plus a suffix
    (MSERV20260925.0116591791 -> MSERV20260925.01165917912213). An id several
    trades start with is not guessed at.
    """
    if trade_id in index:
        return index[trade_id]
    matches = {t['file']: t for t in index.values() if str(t['tradeId'] or '').startswith(trade_id)}
    return next(iter(matches.values())) if len(matches) == 1 else None


def as_booking(event):
    """Return a copy of a trade event that books its trade, and whether it had to change.

    A cancellation case's trade file is the cancellation TradeServ published
    (BilateralCancellation, status Cancelled, Inactive). Injected as it is,
    step 04 would cancel a trade never booked, so each BOOKING_FIELDS field
    takes its booking value in the copy. The file is left as it is, and stays
    the report's raw payload.

    :returns: (the copy, True when any field was changed).
    """
    booking = copy.deepcopy(event)
    wrapper = extract_wrapper(booking)
    nodes = {
        'metaData': wrapper.get('metaData') or {},
        'sourcePlatform': (wrapper.get('sourcePlatform') or [{}])[0] or {},
        'fx': (wrapper.get('tradeData') or {}).get('fx') or {},
    }
    changed = False
    # A field the event leaves out is given its booking value by the parser.
    for node, key, value in BOOKING_FIELDS:
        if nodes[node].get(key) not in (None, value):
            nodes[node][key] = value
            changed = True
    return booking, changed


def case_order(case_id):
    """Sort key putting cases in test sheet order: TC003, TC003A, TC003B, TC004."""
    match = CASE_ID_RE.match(case_id)
    return (int(match.group(1)), match.group(2)) if match else (sys.maxsize, case_id)


def pick_case(case_id, listed, index, date):
    """Decide what one case injects; see the module docstring.

    :param listed: the trade ids the Excel names for the case; [] for N/A or a
        case the Excel does not list.
    :returns: {'id', 'scenario', 'source', 'listed', 'trades', 'notes',
        'problem', 'notPresent'}. scenario is the case's scenario id, or None
        when it has no file whose flow it follows. source is 'excel', or None
        when the Excel names no trade for the case, which is then skipped.
        problem, when set, says why the case cannot run; notPresent is True
        when that is because ids the Excel names are not in the day's folder.
        Each trade is {'label', 'entry',
        'entryIndex', 'excelId', 'tradeId', 'file', 'event', 'booking',
        'tradeIndex', 'single'}: entry is the scenario flow it follows ({} for
        none) and entryIndex its place in the scenario (SCENARIO_TRADE_INDEX).
        event is the file's own event and booking what is injected
        (as_booking). single is
        True for an Excel trade of a case with no scenario file, which runs
        through single_unparsed.json. prepare() sets tradeIndex, any other
        Excel trade's place in multiple_trade.json (TRADE_INDEX).
    """
    case = {'id': case_id, 'scenario': None, 'source': None, 'listed': listed,
            'trades': [], 'notes': [], 'problem': None, 'notPresent': False}

    # IMPORT_EXCEL books only the Excel's trades: a case it names none for is
    # skipped, never run on trades its scenario would generate.
    ids = list(dict.fromkeys(listed))
    if not ids:
        return case

    entries = []
    if (SCENARIO_DIR / f'{case_id}.json').exists():
        try:
            entries = scenario_trades(case_id)
        except (OSError, ValueError) as err:
            case['problem'] = f'its scenario file cannot be used: {err}'
            return case
        if entries:
            case['scenario'] = case_id
        else:
            case['notes'].append(f'{case_id}.json books no trade of its own (it continues '
                                 'another case), so its flow is not used.')

    repeated = [i for i in ids if listed.count(i) > 1]
    if repeated:
        case['notes'].append(f'listed more than once, booked once: {", ".join(repeated)}')

    found = [(trade_id, find_trade(index, trade_id)) for trade_id in ids]
    missing = [trade_id for trade_id, trade in found if trade is None]
    found = [(trade_id, trade) for trade_id, trade in found if trade is not None]

    # A scenario books the trades its entries declare; a case with none books
    # every trade the Excel lists.
    books = len(entries) or len(ids)
    if len(found) < books:
        case['problem'] = (f'it books {books} trade{"s" if books != 1 else ""}, but only '
                           f'{len(found)} of the {len(ids)} the Excel lists for it are in '
                           f'import_trade/trades/{date}'
                           + (f'; missing: {", ".join(missing)}' if missing else ''))
        # Short because ids the Excel names are not in the folder: reported as
        # Not Present, outside the Summary's counts, rather than as a failure.
        case['notPresent'] = bool(missing)
        return case
    if missing:
        case['notes'].append(f'not in import_trade/trades/{date}, so not booked: {", ".join(missing)}')
    if len(found) > books:
        case['notes'].append(f'{case_id}.json books {books}, so these are not booked: '
                             + ', '.join(trade_id for trade_id, _ in found[books:]))
        found = found[:books]

    case['source'] = 'excel'
    rebooked = []
    for n, (trade_id, trade) in enumerate(found):
        booking, changed = as_booking(trade['event'])
        if changed:
            rebooked.append(trade_id)
        case['trades'].append({
            'label': trade_id if books == 1 else f'{trade_id} ({n + 1} of {books})',
            'entry': entries[n] if entries else {},
            'entryIndex': n if entries else None,
            'excelId': trade_id,
            'tradeId': trade['tradeId'],
            'file': trade['file'],
            'event': trade['event'],
            'booking': booking,
            'tradeIndex': None,
            'single': not entries,
        })
    if rebooked:
        case['notes'].append('each booked as a new trade (status New), though its file holds a '
                             'cancellation: ' + ', '.join(rebooked))
    return case


def check_parsed(cases, excel_trades):
    """Check the parsed Excel trades against the cases that book them.

    Each must have parsed into its own place in multiple_trade.json, or its
    TRADE_INDEX would inject another trade, so a mismatch raises. A case that
    nets its trades into one group (a nettingTotal) must find every one of them
    in that group's parties, pair, product and currencies after the party id
    and currency pair steps; otherwise it is not run, rather than booked and
    failed at step 07c.
    """
    parsed = flatten_trades(json.loads(PARSED_PATH.read_text(encoding='utf-8')))
    if len(parsed) != len(excel_trades):
        raise ValueError(f'{PARSED_PATH.name} holds {len(parsed)} trades, but {len(excel_trades)} '
                         f'were written to {OUT_PATH.name}.')

    for trade in excel_trades:
        wrapper = parsed[trade['tradeIndex']].get('wrapper') or {}
        got = ((wrapper.get('sourcePlatform') or [{}])[0] or {}).get('tradeId')
        if got != trade['tradeId']:
            raise ValueError(f'{PARSED_PATH.name}[{trade["tradeIndex"]}] is trade {got}, not '
                             f'{trade["tradeId"]} from {trade["file"]}.')

    for case in cases:
        group = scenario_netting_group(case['scenario']) \
            if case['source'] == 'excel' and case['scenario'] else None
        if not group:
            continue
        refused = []
        for trade in case['trades']:
            differs = netting_total_differences(group['nettingTotal'],
                                                parsed[trade['tradeIndex']]['wrapper'])
            if differs:
                refused.append(f'{trade["excelId"]} has {", ".join(differs)}')
        if refused:
            case['problem'] = (f'its {len(case["trades"])} trades must net into one group, but '
                               f'{len(refused)} cannot: ' + '; '.join(refused))


def describe(case):
    """Return one line saying what a case runs on, or why it does not run."""
    if case['problem']:
        return f'{"NOT PRESENT" if case["notPresent"] else "NOT RUN"}: {case["problem"]}'
    count = len(case['trades'])
    plural = 's' if count != 1 else ''
    if case['source'] == 'excel':
        flow = (f'following {case["scenario"]}.json' if case['scenario']
                else 'no scenario file, so each runs alone through single_unparsed.json '
                     'and every step, as a SINGLE_FILE trade, with no cancel steps')
        return f'{count} Excel trade{plural}, {flow}: ' + ', '.join(t['excelId'] for t in case['trades'])
    return 'N/A: the Excel names no trade for it, so it is not run and not reported'


def prepare(excel_file=None):
    """Pick every case's trades, import them into multiple_unparsed.json and parse them.

    Prints what each case runs on.

    :param excel_file: one of excel_files(), the workbook in this folder to read.
    :returns: {'excel': the Excel's file name, 'date': the trades' folder name,
        'cases': every case (CASES only, when set) in test sheet order}.
    :raises OSError, ValueError: when the Excel, the day's folder or the parse
        cannot be used, so no case can run.
    """
    date = resolve_trade_date()
    path = excel_path(excel_file)
    listed = read_excel(path)

    folder = TRADES_DIR / date
    if not folder.is_dir():
        available = sorted(p.name for p in TRADES_DIR.iterdir() if p.is_dir()) if TRADES_DIR.is_dir() else []
        raise FileNotFoundError(f'no trades folder for {date} at {folder}. '
                                f'Available: {", ".join(available) or "none"}.')
    index, bad_files = index_trades(folder)

    case_ids = set(listed) | {s for s in available_scenarios() if CASE_ID_RE.match(s)}
    if CASES:
        wanted = {str(c).strip().upper() for c in CASES}
        unknown = sorted(wanted - case_ids, key=case_order)
        if unknown:
            raise ValueError(f'trade_reader.py: CASES names {", ".join(unknown)}, which neither '
                             f'{path.name} nor a scenario file has.')
        case_ids = wanted

    cases = [pick_case(case_id, listed.get(case_id, []), index, date)
             for case_id in sorted(case_ids, key=case_order)]

    # Every Excel trade of a case with a scenario file, case by case, imported
    # as a day's batch is; its place in the file is the TRADE_INDEX that
    # injects it. A case with none stages each trade in single_unparsed.json
    # as it runs (runner.py), so its trades are left out of the batch.
    excel_trades = [trade for case in cases if case['source'] == 'excel'
                    for trade in case['trades'] if not trade['single']]
    single_count = sum(trade['single'] for case in cases for trade in case['trades'])
    for n, trade in enumerate(excel_trades):
        trade['tradeIndex'] = n
    write_events([trade['booking'] for trade in excel_trades])
    print(f'OK    {path.name}: {len(excel_trades)} trade(s) from trades/{date} -> {OUT_PATH.name}; '
          f'{single_count} more run one at a time through single_unparsed.json')

    # Parsed with IMPORT's own job; nothing to parse when no case books its
    # Excel trades through multiple_trade.json.
    if excel_trades:
        if parse_trades.main(['parse_trades.py', 'MULTIPLE_FILE']) != 0:
            raise ValueError(f'parse_trades.py could not parse the trades in {OUT_PATH.name}.')
        check_parsed(cases, excel_trades)

    print(f'\nTest cases from {path.name}, trades from import_trade/trades/{date}:')
    for case in cases:
        print(f'  {case["id"]:<7} {describe(case)}')
        for note in case['notes']:
            print(f'          note: {note}')

    named = {find_trade(index, i)['file'] for ids in listed.values() for i in ids if find_trade(index, i)}
    unnamed = sorted({t['file'] for t in index.values()} - named)
    if unnamed:
        print(f'\nIn trades/{date} but named for no case, so not booked: '
              + ', '.join(name.split('_', 1)[0] for name in unnamed))
    for problem in bad_files:
        print(f'WARN  {problem}', file=sys.stderr)

    return {'excel': path.name, 'date': date, 'cases': cases}


def main():
    """Run prepare() on each workbook in turn, to see what an IMPORT_EXCEL run would run.

    multiple_unparsed.json and multiple_trade.json are left holding the last
    workbook's trades.

    :returns: the process exit code, 0 on success and 1 when a workbook's cases cannot run.
    """
    code = 0
    for excel_file in excel_files():
        try:
            prepare(excel_file)
        except (OSError, ValueError) as err:
            print(f'ERROR {err}', file=sys.stderr)
            code = 1
    return code


if __name__ == '__main__':
    sys.exit(main())
