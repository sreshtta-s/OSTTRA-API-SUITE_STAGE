#!/usr/bin/env python3
"""Collect one day's received trade events into multiple_unparsed.json.

Reads every *.json file in import_trade/trades/<IMPORT_DATE>/, each holding one
received publishEvent, and writes them as a single list to
api/json_payload_trade_injection/multiple_unparsed.json, one event per line in
trade id order. parse_trades.py then turns that file into multiple_trade.json
for a MULTIPLE_FILE run. With INJECTION_MODE = 'IMPORT' in utils/flag.py,
runner.py runs this script first, then the parse, then each trade in turn.

The old contents are always replaced, never merged. If the chosen day cannot
be imported (bad date, missing or empty folder, or a bad file) the output is
emptied to [], so it never keeps an earlier day's trades: parse_trades.py then
fails on the empty list and runner.py aborts instead of injecting them. Exits 1
on any failure.

  python import_trade/import_trade_runner.py              import IMPORT_DATE
  python import_trade/import_trade_runner.py 30_09_2026   import that folder
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.trade_parser import extract_wrapper  # noqa: E402

# The folder under TRADES_DIR to import, named DD_MM_YYYY. Leave it blank to
# import today's folder; a date given on the command line overrides it.
IMPORT_DATE = '22_10_2026'  # <-- EDIT THIS LINE to import another day's trades

DATE_FORMAT = '%d_%m_%Y'

TRADES_DIR = ROOT / 'import_trade' / 'trades'
OUT_PATH = ROOT / 'api' / 'json_payload_trade_injection' / 'multiple_unparsed.json'


def resolve_import_date(argv):
    """Return the folder name to import: argv[1], else IMPORT_DATE, else today.

    :raises ValueError: if the chosen date is not a real DD_MM_YYYY date.
    """
    if len(argv) > 1:
        source, where = argv[1], 'command line'
    elif IMPORT_DATE.strip():
        source, where = IMPORT_DATE, 'import_trade_runner.py: IMPORT_DATE'
    else:
        return datetime.now().strftime(DATE_FORMAT)

    date = source.strip()
    try:
        datetime.strptime(date, DATE_FORMAT)
    except ValueError:
        raise ValueError(f"{where} '{source}' is not a DD_MM_YYYY date such as 29_09_2026.")
    return date


def load_events(folder):
    """Load every trade event in folder, sorted by file name.

    File names start with the source trade id, so this orders the events by
    trade id, matching the order multiple_unparsed.json has always used.

    :returns: (events, errors), where errors lists one message per bad file.
    """
    events = []
    errors = []

    for path in sorted(folder.glob('*.json')):
        try:
            event = json.loads(path.read_text(encoding='utf-8-sig'))
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            errors.append(f'{path.name} is not valid JSON: {err}')
            continue

        if extract_wrapper(event) is None:
            errors.append(f'{path.name} holds no trade wrapper (publishEvent -> '
                          'eventPayload -> current -> wrapper).')
            continue

        events.append(event)

    return events, errors


def write_output(text):
    """Write text to OUT_PATH, creating the file and its folder if missing."""
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding='utf-8')


def write_events(events):
    """Write events to OUT_PATH as a JSON list with one compact event per line."""
    lines = [json.dumps(e, separators=(',', ':'), ensure_ascii=False) for e in events]
    write_output('[\n' + ',\n'.join(lines) + '\n]')


def fail(*messages):
    """Print each message as an error, empty OUT_PATH, and return exit code 1.

    Emptying rather than keeping the file means a failed import never leaves an
    earlier day's trades in place to be injected by mistake.
    """
    for message in messages:
        print(f'ERROR {message}', file=sys.stderr)
    write_output('[]')
    print(f'\n{OUT_PATH.name} cleared; it holds no trades until an import succeeds.',
          file=sys.stderr)
    return 1


def main(argv):
    """Import the folder chosen by resolve_import_date into OUT_PATH.

    :returns: the process exit code, 0 on success and 1 on any failure.
    """
    try:
        date = resolve_import_date(argv)
    except ValueError as err:
        return fail(err)

    folder = TRADES_DIR / date
    if not folder.is_dir():
        available = sorted(p.name for p in TRADES_DIR.iterdir() if p.is_dir()) \
            if TRADES_DIR.is_dir() else []
        return fail(f'no trades folder for {date} at {folder}',
                    f'Available: {", ".join(available) or "none"}')

    events, errors = load_events(folder)

    if errors:
        return fail(*errors, f'{len(errors)} bad file(s) in {date}; none of its trades imported.')

    if not events:
        return fail(f'{folder} has no .json trade files.')

    action = 'replaced' if OUT_PATH.exists() else 'created'
    write_events(events)

    plural = 's' if len(events) > 1 else ''
    print(f'OK    trades/{date} -> {OUT_PATH.name} {action} ({len(events)} trade{plural})')
    for i, event in enumerate(events):
        wrapper = extract_wrapper(event)
        leg = wrapper['tradeData']['fx']['fxCash']['nearLeg']
        print(f'        [{i}] {wrapper["sourcePlatform"][0]["tradeId"]} | '
              f'{wrapper["tradeData"]["fx"]["productType"]} | '
              f'{wrapper["tradeData"]["fx"]["fxCash"]["currencyPair"]} | '
              f'value {leg["valueDate"]} | '
              f'{leg["baseCurrencySeller"]} -> {leg["baseCurrencyBuyer"]}')

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
