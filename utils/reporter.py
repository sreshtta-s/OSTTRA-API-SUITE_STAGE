"""Run reporting: Excel workbook, per-step call log and merged HTML report.

Mirrors utils/reporter.js in the Playwright suite. Every step runs in its own
pytest process, so the call log and the injected payload are handed to
runner.py through scratch files under .state, one per report row (STATE_ROW).
runner.py then merges the step blobs into one HTML report (merge_html_report)
and appends one workbook row per trade (write_run).
"""

import html
import json
import os
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.views import BookView

from config.steps import STEPS, sort_tokens
from utils.state_helper import STATE_DIR

ROOT = Path(__file__).resolve().parent.parent
WORKBOOK = ROOT / 'reports' / 'excel' / 'osttra-api-suite-report.xlsx'
# IMPORT rows go to a workbook of their own, keeping a day's imported batch
# apart from the DYNAMIC, file and scenario rows in WORKBOOK.
IMPORT_WORKBOOK = ROOT / 'reports' / 'excel' / 'osttra-api-suite-import-report.xlsx'
# IMPORT_EXCEL rows likewise, laid out as IMPORT's with each row's Test Case
# filled in.
IMPORT_EXCEL_WORKBOOK = ROOT / 'reports' / 'excel' / 'osttra-api-suite-import-excel-report.xlsx'

# The modes whose rows get a workbook of their own, opening on a Summary tab,
# and that tab's heading.
SUMMARY_WORKBOOKS = {
    'IMPORT':       (IMPORT_WORKBOOK, 'Import Run Summary'),
    'IMPORT_EXCEL': (IMPORT_EXCEL_WORKBOOK, 'Import Excel Run Summary'),
}


def local_date(d=None):
    """Return the local date as YYYY-MM-DD; it names the day's Excel sheet.

    Local time, not UTC: a UTC date would file every run before 05:30 IST
    under the previous day's sheet.
    """
    return (d or datetime.now()).strftime('%Y-%m-%d')


def local_date_time(d=None):
    """Return the local time as YYYY-MM-DD HH:MM:SS.

    Used for the Date of Reporting column and runner.py's run timestamp.
    """
    return (d or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')


# ----------------------------------------------------------------------------
# CALL LOG + INJECTED PAYLOAD (cross-process scratch files)
# ----------------------------------------------------------------------------

def calls_file(row=None):
    """Return the call log path for a report row, defaulting to STATE_ROW."""
    row = row if row is not None else os.environ.get('STATE_ROW')
    return STATE_DIR / (f'calls-{row}.json' if row else 'calls-default.json')


def payload_file(row=None):
    """Return the path where step 04 records the payload it injected.

    DYNAMIC and SCENARIO runs have no source file to quote, so runner.py fills
    the Injected Payload column from this file. Step 04 runs in its own pytest
    process, which is why the payload travels through a file.
    """
    row = row if row is not None else os.environ.get('STATE_ROW')
    return STATE_DIR / (f'payload-{row}.json' if row else 'payload-default.json')


def _atomic_write(path, text):
    """Write via a temp file and rename, so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    tmp.write_text(text, encoding='utf-8')
    # Owner-only (0o600), like the state file beside it: the payload names live
    # trades, and the default umask would let other local accounts read it.
    tmp.chmod(0o600)
    tmp.replace(path)


def write_injected_payload(text, row=None):
    """Record the injected payload for the Excel report."""
    try:
        _atomic_write(payload_file(row), text)
    except OSError:
        # Best-effort: a reporting failure must never fail the injection step.
        pass


def read_injected_payload(row=None):
    """Return the recorded payload, or '' when none was recorded."""
    file = payload_file(row)
    if not file.exists():
        return ''
    try:
        return file.read_text(encoding='utf-8')
    except OSError:
        return ''


def reset_injected_payload(row=None):
    file = payload_file(row)
    if file.exists():
        file.unlink()


def read_calls(row=None):
    """Return the logged calls, or [] when the log is missing or unreadable."""
    file = calls_file(row)
    if not file.exists():
        return []
    try:
        return json.loads(file.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return []


def write_calls(row, calls):
    _atomic_write(calls_file(row), json.dumps(calls, indent=2))


def log_call(endpoint, status, method='POST'):
    """Append one call to the current row's log, attributed to CURRENT_STEP.

    Called after every request by BaseService, the simulator services and the
    auth step. A call counts as ok only for a 2xx status. Never raises: a
    reporting failure must not fail a test.
    """
    try:
        calls = read_calls()
        calls.append({
            'step': os.environ.get('CURRENT_STEP', ''),
            'endpoint': endpoint,
            'method': method,
            'status': status,
            'ok': 1 if 200 <= status < 300 else 0,
            'at': datetime.now().astimezone().isoformat(),
        })
        write_calls(os.environ.get('STATE_ROW'), calls)
    except Exception:
        # Best-effort, as above.
        pass


def reset_calls(row=None):
    file = calls_file(row)
    if file.exists():
        file.unlink()


# ----------------------------------------------------------------------------
# EXCEL WORKBOOK
# ----------------------------------------------------------------------------

def step_tokens():
    """Return the enabled step tokens in run order, one column pair each.

    The Excel column set is derived from the step registry (config/steps.py),
    so enabling, disabling or adding a step changes the columns automatically.
    """
    return sort_tokens([t for t, s in STEPS.items() if s.get('enabled') is not False])


# The columns before the per-step pairs: header, row key, width in pixels and
# horizontal alignment.
LEAD_COLUMNS = [
    ('Sl No',                 'slNo',             38, 'center'),
    ('Date of Reporting',     'reportDate',      125, 'left'),
    ('Trade ID',              'tradeId',         199, 'left'),
    # The scenario, or IMPORT_EXCEL test case, that produced this row; blank
    # for DYNAMIC and the static file modes, which have no test case to name.
    ('Test Case',             'testCase',         65, 'left'),
    ('Product Type',          'productType',      68, 'left'),
    ('Trade Date',            'tradeDate',        72, 'center'),
    ('Value Date',            'valueDate',        72, 'center'),
    # Wide enough for MULTIPLE_FILE.
    ('Mode',                  'mode',             96, 'center'),
    ('Currency Pair',         'currencyPair',     86, 'center'),
    ('Party',                 'party',            61, 'center'),
    ('Counterparty',          'counterParty',     83, 'center'),
    ('Netting Group (A)',     'nettingGroupA',   152, 'left'),
    ('Netting Grp State (A)', 'nettingStateA',   150, 'left'),
    ('Netting Group (B)',     'nettingGroupB',   155, 'left'),
    ('Netting Grp State (B)', 'nettingStateB',   150, 'left'),
    ('Settlement ID',         'settlementId',    271, 'left'),
    ('Settlement State',      'settlementState', 160, 'left'),
    # Wide enough for "FAIL (14 steps)" and "PASS (up to 15b)".
    ('Overall Result',        'result',          120, 'center'),
    ('HTML Report',           'htmlReport',      191, 'left'),
]

# The columns after the per-step pairs. Their JSON is clipped to the row.
PAYLOAD_COLUMNS = [
    ('Raw Payload (unparsed)', 'rawPayload',    160, 'left'),
    ('Injected Payload',       'parsedPayload', 160, 'left'),
]

RESULT_HEADER = 'Overall Result'

# The Overall Results of an IMPORT_EXCEL case that is not run: its Excel trade
# ids are not in the day's trades folder (NOT_PRESENT_RESULT), or it cannot run
# for another reason (NOT_RUN_RESULT). The Summary tab counts both as Not Run,
# outside the passed and failed percentages.
NOT_PRESENT_RESULT = 'NOT PRESENT'
NOT_RUN_RESULT = 'FAIL (not run)'

FIXED_LAYOUT = {h: (w, a) for h, _, w, a in LEAD_COLUMNS + PAYLOAD_COLUMNS}
PAYLOAD_HEADERS = {h for h, *_ in PAYLOAD_COLUMNS}

# Sheet style. Widths and heights are in pixels, as a spreadsheet shows them.
FONT_NAME = 'Roboto'
FONT_SIZE = 10
TEXT_COLOR = '191C1F'
MUTED_COLOR = '5B6472'
HEADER_FILL_COLOR = '2B3D4F'
HEADER_TEXT_COLOR = 'FFFFFF'
HEADER_RULE_COLOR = '1F2C39'
GRID_COLOR = 'E0E0E0'
HEADER_HEIGHT_PX = 38
ROW_HEIGHT_PX = 28

# Overall Result tints, (fill, text), for a result containing the word.
PASS_COLORS = ('E6F4EA', '137333')
FAIL_COLORS = ('FCE8E6', 'C5221F')

# The IMPORT and IMPORT_EXCEL workbooks' first tab: a block per day tab, with
# that day's trades, passed, failed, not run and not applicable each on a row of
# its own.
SUMMARY_SHEET = 'Summary'
SUMMARY_COLUMNS = [
    # header (the first is the day tab's name), width in pixels
    (None,         170),
    ('Count',       70),
    ('Percentage',  90),
]
SUMMARY_FIRST_ROW = 4


def build_columns():
    """Return the workbook columns in order, each with its header and row key."""
    cols = [(h, k) for h, k, *_ in LEAD_COLUMNS]

    for token in step_tokens():
        cols.append((f'{token} {STEPS[token]["label"]}', f'step_{token}'))
        cols.append((f'{token} err', f'step_{token}_err'))

    cols += [(h, k) for h, k, *_ in PAYLOAD_COLUMNS]

    return [{'header': h, 'key': k} for h, k in cols]


def column_layout(header):
    """Return (width in pixels, horizontal alignment) for a column, by its header.

    Looked up by header rather than position, so a sheet written under an
    earlier step set, with other step columns, is laid out correctly too.
    """
    if header in FIXED_LAYOUT:
        return FIXED_LAYOUT[header]
    if header.endswith(' err'):
        return len(header) * 7, 'center'
    # A step column: its header on one or two lines of the header row.
    return min(max(len(header) * 6 + 8, 60), 180), 'left'


def px_to_width(px):
    """Convert pixels to Excel's column width unit (characters of the default font)."""
    return round(max(px - 5, 7) / 7, 2)


def px_to_points(px):
    """Convert pixels to the points Excel measures row heights in."""
    return px * 0.75


def style_header_cell(cell):
    """Give a header cell the dark band: bold white, centred, wrapped."""
    cell.font = Font(name=FONT_NAME, size=FONT_SIZE, bold=True, color=HEADER_TEXT_COLOR)
    cell.fill = PatternFill('solid', fgColor=HEADER_FILL_COLOR)
    cell.border = Border(bottom=Side(style='thin', color=HEADER_RULE_COLOR))
    cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)


def grid_border():
    """Return the thin light border that keeps the grid visible on styled cells."""
    side = Side(style='thin', color=GRID_COLOR)
    return Border(left=side, right=side, top=side, bottom=side)


def is_styled(sheet):
    """Return True if the sheet already has the dark header band."""
    color = sheet.cell(row=1, column=1).fill.fgColor.rgb
    return isinstance(color, str) and color.upper().endswith(HEADER_FILL_COLOR)


def style_sheet(sheet):
    """Lay out a day sheet: header band, widths, row heights, fonts, result tints.

    Restyles every row, so it is safe to run again after each append.
    """
    headers = headers_of(sheet)
    data_font = Font(name=FONT_NAME, size=FONT_SIZE, color=TEXT_COLOR)
    border = grid_border()

    for index, header in enumerate(headers, start=1):
        width, horizontal = column_layout(header)
        sheet.column_dimensions[get_column_letter(index)].width = px_to_width(width)
        style_header_cell(sheet.cell(row=1, column=index))

        # Payload JSON wraps inside the fixed row height, which clips it to the
        # cell instead of letting it spill across the sheet; every other value
        # stays on one line.
        alignment = Alignment(horizontal=horizontal, vertical='center',
                              wrap_text=header in PAYLOAD_HEADERS)
        for (cell,) in sheet.iter_rows(min_row=2, min_col=index, max_col=index):
            cell.font = data_font
            cell.border = border
            cell.alignment = alignment

    sheet.row_dimensions[1].height = px_to_points(HEADER_HEIGHT_PX)
    for r in range(2, sheet.max_row + 1):
        sheet.row_dimensions[r].height = px_to_points(ROW_HEIGHT_PX)

    # Keep the header row and the first three identity columns in view while
    # scrolling.
    sheet.freeze_panes = 'D2'
    sheet.sheet_view.showGridLines = True

    # Replaced rather than added to, so restyling never stacks duplicate rules.
    sheet.conditional_formatting = ConditionalFormattingList()
    if RESULT_HEADER in headers:
        letter = get_column_letter(headers.index(RESULT_HEADER) + 1)
        for word, (fill, text) in (('PASS', PASS_COLORS), ('FAIL', FAIL_COLORS)):
            sheet.conditional_formatting.add(f'{letter}2:{letter}1048576', FormulaRule(
                formula=[f'ISNUMBER(SEARCH("{word}",{letter}2))'],
                font=Font(color=text, bold=True),
                fill=PatternFill(start_color=fill, end_color=fill, fill_type='solid'),
            ))


def result_counts(sheet):
    """Return (total, passed, failed, not run, not applicable) for a day sheet's rows.

    A row is Not Run when its case was never run (NOT_PRESENT_RESULT or
    NOT_RUN_RESULT), so passed and failed count only the cases that ran. A row
    is Not Applicable when its Overall Result is none of these, blank for
    instance: the trade was recorded but has no verdict.
    """
    headers = headers_of(sheet)
    if RESULT_HEADER not in headers:
        return 0, 0, 0, 0, 0

    col = headers.index(RESULT_HEADER)
    not_run_results = (NOT_PRESENT_RESULT.upper(), NOT_RUN_RESULT.upper())
    passed = failed = not_run = other = 0
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if all(v is None or v == '' for v in row):
            continue
        result = str(row[col] or '').strip().upper()
        if result in not_run_results:
            not_run += 1
        elif result.startswith('PASS'):
            passed += 1
        elif result.startswith('FAIL'):
            failed += 1
        else:
            other += 1

    return passed + failed + not_run + other, passed, failed, not_run, other


def write_summary(workbook, heading):
    """Rebuild the Summary tab at the front of the workbook and return it.

    One block per day sheet, newest first, headed by the sheet's name: its
    trades, then how many passed, failed, were not run or are not applicable,
    one per row. Passed and failed also show their share of the cases that
    ran (passed + failed); Not Run and Not Applicable are counts alone.
    Rebuilt from the day sheets on every write, so it always matches them.

    :param heading: the tab's title, from SUMMARY_WORKBOOKS.
    """
    if SUMMARY_SHEET in workbook.sheetnames:
        workbook.remove(workbook[SUMMARY_SHEET])
    days = list(reversed(workbook.worksheets))
    sheet = workbook.create_sheet(SUMMARY_SHEET, 0)

    title = sheet.cell(row=1, column=1,
                       value=f'OSTTRA API Suite - {heading} ({local_date()})')
    title.font = Font(name=FONT_NAME, size=14, bold=True, color=TEXT_COLOR)
    note = sheet.cell(row=2, column=1, value=(
        f'Last updated {local_date_time()}. One block per day tab, newest first; each '
        'percentage is a share of the cases that ran (Passed + Failed). Not Run cases '
        "(trade ids not in the day's trades folder, or not runnable) are not in the "
        'percentages.'))
    note.font = Font(name=FONT_NAME, size=FONT_SIZE, color=MUTED_COLOR)
    sheet.row_dimensions[1].height = px_to_points(32)

    for index, (_, width) in enumerate(SUMMARY_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = px_to_width(width)

    border = grid_border()
    r = SUMMARY_FIRST_ROW
    for day in days:
        total, passed, failed, not_run, other = result_counts(day)
        ran = passed + failed

        for c, (header, _) in enumerate(SUMMARY_COLUMNS, start=1):
            style_header_cell(sheet.cell(row=r, column=c, value=header or day.title))
        sheet.row_dimensions[r].height = px_to_points(HEADER_HEIGHT_PX)

        # label, count, text color, bold, has a share of the cases that ran
        rows = [
            ('Total Test Cases (Trades)', total,   TEXT_COLOR,     True,  False),
            ('Passed',                    passed,  PASS_COLORS[1], False, True),
            ('Failed',                    failed,  FAIL_COLORS[1], False, True),
            ('Not Run',                   not_run, MUTED_COLOR,    False, False),
            ('Not Applicable',            other,   TEXT_COLOR,     False, False),
        ]
        for label, count, color, bold, has_share in rows:
            r += 1
            font = Font(name=FONT_NAME, size=FONT_SIZE, bold=bold, color=color)
            share = (count / ran if ran else 0) if has_share else ''
            for c, value in enumerate([label, count, share], start=1):
                cell = sheet.cell(row=r, column=c, value=value)
                cell.font = font
                cell.border = border
                cell.alignment = Alignment(horizontal='left' if c == 1 else 'center',
                                           vertical='center')
            cell.number_format = '0.0%'   # the last cell is the percentage
            sheet.row_dimensions[r].height = px_to_points(ROW_HEIGHT_PX)

        # One empty row between days.
        r += 2

    return sheet


def activate(workbook, sheet):
    """Open the workbook on this sheet, with no other tab left selected.

    openpyxl leaves the previously active tab selected, and Excel opens two
    selected tabs as a group, where an edit lands on both. A workbook saved
    without a workbook view has nowhere to record the active tab and opens on
    the first one, so a view is added if it is missing.
    """
    if not workbook.views:
        workbook.views.append(BookView())
    for ws in workbook.worksheets:
        ws.sheet_view.tabSelected = ws is sheet
    workbook.active = workbook.index(sheet)


def headers_of(sheet):
    """Return a sheet's header row as strings, with '' for empty cells."""
    return [
        '' if cell.value is None else str(cell.value)
        for cell in next(sheet.iter_rows(min_row=1, max_row=1))
    ]


def same_headers(a, b):
    return len(a) == len(b) and all(x == y for x, y in zip(a, b))


def apply_columns(sheet, columns):
    """Write the header row, leaving data rows untouched; style_sheet lays it out."""
    for index, column in enumerate(columns, start=1):
        sheet.cell(row=1, column=index, value=column['header'])


def get_sheet(workbook, name):
    """Return the sheet to append to, creating it if it does not exist.

    The column set follows the step registry, so it changes when a step is
    enabled or disabled. Appending under a different header row would put
    values under the wrong headers, so an existing sheet with other headers is
    left as it is and the row goes to a new sheet named "<name> (2)", "(3)"...
    """
    columns = build_columns()
    sheet = workbook[name] if name in workbook.sheetnames else None

    if sheet is not None and not same_headers(headers_of(sheet), [c['header'] for c in columns]):
        n = 2
        while f'{name} ({n})' in workbook.sheetnames:
            n += 1
        sheet = None
        name = f'{name} ({n})'

    if sheet is None:
        sheet = workbook.create_sheet(title=name)

    apply_columns(sheet, columns)
    return sheet


def workbook_for(run):
    """Return the workbook a run's row belongs in: its mode's in SUMMARY_WORKBOOKS, else WORKBOOK."""
    return SUMMARY_WORKBOOKS.get(run.get('mode'), (WORKBOOK, None))[0]


def write_run(run, row=None):
    """Append one row for a finished run to today's sheet of its workbook.

    :param run: identity and result fields, keyed as in build_columns. Its
        'mode' picks the workbook (workbook_for).
    :param row: STATE_ROW key whose call log fills the per-step columns.
    :returns: the workbook path, the sheet title and the row's Sl No.
    """
    path = workbook_for(run)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        workbook = load_workbook(path)
    else:
        workbook = Workbook()
        # Drop openpyxl's default empty "Sheet" so the first tab is a real day.
        workbook.remove(workbook.active)

    sheet_name = local_date()   # e.g. 2026-09-08
    sheet = get_sheet(workbook, sheet_name)

    calls = read_calls(row)

    # Per step: "<passed>/<total> ok" (or FAIL) and the first non-2xx status.
    step_cells = {}
    for token in step_tokens():
        step_calls = [c for c in calls if c.get('step') == token]
        if not step_calls:
            step_cells[f'step_{token}'] = ''
            step_cells[f'step_{token}_err'] = ''
            continue
        passed = len([c for c in step_calls if c.get('ok') == 1])
        failed = next((c for c in step_calls if c.get('ok') == 0), None)
        step_cells[f'step_{token}'] = f'{passed}/{len(step_calls)} ' + ('ok' if passed == len(step_calls) else 'FAIL')
        step_cells[f'step_{token}_err'] = failed['status'] if failed else ''

    sl_no = sheet.max_row   # row 1 is the header, so this is the next serial number

    values = {'slNo': sl_no, 'reportDate': local_date_time(), **run, **step_cells}
    sheet.append([values.get(column['key'], '') for column in build_columns()])
    style_sheet(sheet)

    # Day sheets written before this layout existed get it once.
    for other in workbook.worksheets:
        if other is not sheet and other.title != SUMMARY_SHEET and not is_styled(other):
            style_sheet(other)

    # Excel opens on the active tab. New days are added at the end, so mark
    # today's sheet active or the workbook opens on the oldest day. Tab order
    # stays chronological. The IMPORT and IMPORT_EXCEL workbooks open on their
    # Summary tab, first in the tab order.
    summary = SUMMARY_WORKBOOKS.get(run.get('mode'))
    front = write_summary(workbook, summary[1]) if summary else sheet
    activate(workbook, front)

    workbook.save(path)
    return {'workbook': str(path), 'sheet': sheet.title, 'slNo': sl_no}


# ----------------------------------------------------------------------------
# HTML REPORT
# ----------------------------------------------------------------------------
#
# runner.py gives each step's pytest process its own BLOB_FILE, and conftest.py
# writes the step's results there as JSON. merge_html_report folds a run's
# blobs into one HTML report covering steps 01 to 15b.
#
# One blob per step: steps sharing a file would overwrite each other, and an
# HTML file per step would scatter one run across many reports.
#
# Without BLOB_FILE (a plain `pytest` run), pytest-html writes the report.

_REPORT_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1c1f; --muted: #5b6472; --line: #e3e6eb;
  --card: #f7f8fa; --pass: #197a4b; --fail: #b3261e; --skip: #8a6d1f;
  --pass-bg: #e7f5ed; --fail-bg: #fdeceb; --skip-bg: #fdf4e0;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14161a; --fg: #e8eaed; --muted: #9aa3b0; --line: #2b3038;
    --card: #1b1e24; --pass: #6ede9f; --fail: #ff8a80; --skip: #f3cf6b;
    --pass-bg: #16301f; --fail-bg: #35191a; --skip-bg: #322a12;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.wrap { max-width: 1040px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
.totals { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 28px; }
.pill { border: 1px solid var(--line); border-radius: 999px; padding: 4px 12px; font-size: 13px; }
.pill b { font-variant-numeric: tabular-nums; }
.pill.pass { color: var(--pass); background: var(--pass-bg); border-color: transparent; }
.pill.fail { color: var(--fail); background: var(--fail-bg); border-color: transparent; }
.pill.skip { color: var(--skip); background: var(--skip-bg); border-color: transparent; }
.step { border: 1px solid var(--line); border-radius: 10px; margin-bottom: 12px; overflow: hidden; background: var(--card); }
.step > summary { cursor: pointer; padding: 12px 16px; display: flex; gap: 10px;
  align-items: baseline; flex-wrap: wrap; list-style: none; }
.step > summary::-webkit-details-marker { display: none; }
.step > summary::before { content: "\\25B8"; color: var(--muted); font-size: 11px; }
.step[open] > summary::before { content: "\\25BE"; }
.token { font-weight: 600; font-variant-numeric: tabular-nums; min-width: 34px; }
.label { flex: 1; min-width: 160px; }
.count { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.tests { list-style: none; margin: 0; padding: 0 0 6px; border-top: 1px solid var(--line); background: var(--bg); }
.tests li { padding: 8px 16px 8px 40px; position: relative; border-bottom: 1px solid var(--line); }
.tests li:last-child { border-bottom: 0; }
.tests li::before { position: absolute; left: 16px; top: 8px; font-size: 13px; }
.t-passed::before { content: "\\2713"; color: var(--pass); }
.t-failed::before { content: "\\2717"; color: var(--fail); }
.t-skipped::before { content: "\\2013"; color: var(--skip); }
.t-failed { background: var(--fail-bg); }
.name { font-size: 14px; }
.dur { color: var(--muted); font-size: 12px; margin-left: 6px; font-variant-numeric: tabular-nums; }
pre.msg { margin: 8px 0 2px; padding: 10px 12px; background: var(--bg); color: var(--fail);
  border: 1px solid var(--line); border-radius: 6px; font-size: 12.5px;
  white-space: pre-wrap; word-break: break-word; overflow-x: auto; }
.missing { color: var(--muted); font-style: italic; padding: 10px 16px; font-size: 13px; }
@media (max-width: 600px) { .wrap { padding: 20px 16px 48px; } h1 { font-size: 19px; } }
"""


def _esc(value):
    """HTML-escape a value, rendering None as an empty string."""
    return html.escape('' if value is None else str(value))


def _read_blob(path):
    """Return a parsed blob, or a failed step naming the file if it cannot be read.

    An unreadable blob is a step whose results are lost, so the merge shows it
    failed rather than leaving it out.
    """
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as err:
        return {
            'step': '', 'label': f'Unreadable step results ({Path(path).name})', 'exitStatus': None,
            'tests': [{'name': 'Step results could not be read', 'outcome': 'failed',
                       'duration': 0.0, 'message': f'{Path(path).name}: {err}'}],
        }


# What each pytest exit status other than 0 means, for a step that failed
# without any test recording the failure.
PYTEST_EXIT_MEANINGS = {
    1: 'tests failed',
    2: 'interrupted, or its tests could not be collected: an import or syntax error',
    3: 'internal pytest error',
    4: 'pytest usage error',
    5: 'no tests collected',
}


def describe_exit(status):
    """Describe a pytest process's exit status: 'exit code 2 (interrupted, ...)'."""
    if isinstance(status, int) and status < 0:
        return f'signal {-status} (the process was killed)'
    meaning = PYTEST_EXIT_MEANINGS.get(status)
    return f'exit code {status}' + (f' ({meaning})' if meaning else '')


def is_crash(status):
    """Return True if a step's exit status means its checks did not all run.

    0 is a pass and 1 a failure its tests record. Anything else (an import
    error, an internal error, no tests collected, a killed process) stopped the
    step short, whatever its tests show.
    """
    return status not in (0, 1, None)


def _unrecorded_failure(step, tests):
    """Return why a step failed although none of its tests did, or None.

    A step whose pytest process could not import its spec, collected nothing or
    was killed has no failed test, and would read as passed. A step the
    scenario expects to fail exits 1 with its failed tests recorded as passed
    (runner.apply_expected_outcome), so that is not flagged.
    """
    status = step.get('exitStatus')
    if any(test.get('outcome') == 'failed' for test in tests) or status in (0, None):
        return None
    if status == 1 and step.get('expectedOutcome') == 'fail':
        return None
    return (f'The step\'s pytest process ended with {describe_exit(status)}, but no test '
            'recorded a failure, so its checks did not all run. See the console output '
            'for the error.')


def merge_html_report(blob_dir, out_dir, title):
    """Merge every step blob in blob_dir into out_dir/index.html.

    Blobs are merged in file-name order, and runner.py names them so that this
    is run order. Each test is listed under the name conftest.py recorded (the
    first line of its docstring); a step with a failure is shown expanded.

    :returns: the step count and passed/failed/skipped totals, or None when
        there is nothing to merge.
    """
    blob_dir = Path(blob_dir)
    out_dir = Path(out_dir)

    if not blob_dir.exists():
        return None

    blobs = sorted(blob_dir.glob('*.json'))
    steps = [_read_blob(path) for path in blobs]
    if not steps:
        return None

    totals = {'passed': 0, 'failed': 0, 'skipped': 0}
    sections = []

    for step in steps:
        tests = list(step.get('tests', []))
        # A step that crashed before any test failed gets one failed entry, so
        # it reads as failed and counts in the totals.
        problem = _unrecorded_failure(step, tests)
        if problem:
            tests.append({'name': 'Step did not run to completion', 'outcome': 'failed',
                          'duration': 0.0, 'message': problem})
        counts = {'passed': 0, 'failed': 0, 'skipped': 0}
        for test in tests:
            outcome = test.get('outcome', 'skipped')
            counts[outcome] = counts.get(outcome, 0) + 1
            totals[outcome] = totals.get(outcome, 0) + 1

        rows = []
        for test in tests:
            outcome = test.get('outcome', 'skipped')
            message = test.get('message')
            rows.append(
                f'<li class="t-{_esc(outcome)}"><span class="name">{_esc(test.get("name"))}</span>'
                f'<span class="dur">{test.get("duration", 0):.2f}s</span>'
                + (f'<pre class="msg">{_esc(message)}</pre>' if message else '')
                + '</li>'
            )

        summary_bits = []
        if counts['passed']:
            summary_bits.append(f'{counts["passed"]} passed')
        if counts['failed']:
            summary_bits.append(f'{counts["failed"]} failed')
        if counts['skipped']:
            summary_bits.append(f'{counts["skipped"]} skipped')

        sections.append(
            f'<details class="step"{" open" if counts["failed"] else ""}>'
            f'<summary><span class="token">{_esc(step.get("step"))}</span>'
            f'<span class="label">{_esc(step.get("label"))}</span>'
            f'<span class="count">{_esc(" · ".join(summary_bits) or "no tests")}</span></summary>'
            + (f'<ul class="tests">{"".join(rows)}</ul>' if rows
               else '<p class="missing">This step produced no test results.</p>')
            + '</details>'
        )

    total = sum(totals.values())
    pills = [f'<span class="pill"><b>{total}</b> tests</span>']
    for outcome in ('passed', 'failed', 'skipped'):
        if totals[outcome]:
            pills.append(f'<span class="pill {outcome[:4]}"><b>{totals[outcome]}</b> {outcome}</span>')

    document = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{_esc(title)}</title>\n<style>{_REPORT_CSS}</style>\n</head>\n<body>\n'
        f'<div class="wrap">\n<h1>{_esc(title)}</h1>\n'
        f'<p class="sub">OSTTRA API Suite &middot; {len(steps)} step(s)</p>\n'
        f'<div class="totals">{"".join(pills)}</div>\n'
        f'{"".join(sections)}\n</div>\n</body>\n</html>\n'
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'index.html').write_text(document, encoding='utf-8')

    return {'steps': len(steps), **totals}
