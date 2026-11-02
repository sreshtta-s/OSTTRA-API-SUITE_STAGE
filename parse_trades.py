#!/usr/bin/env python3
"""Parse raw trade events into the injection files for the file-based modes.

Reads single_unparsed.json / multiple_unparsed.json in
api/json_payload_trade_injection and writes single_trade.json /
multiple_trade.json beside them. runner.py calls it with the active mode before
a SINGLE_FILE or MULTIPLE_FILE run, and step 04 does when run directly with
pytest. Exits 1 on any failure.

  python parse_trades.py                 parse both jobs
  python parse_trades.py SINGLE_FILE     parse only that job
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.currency_pair_order import order_currency_pairs  # noqa: E402
from utils.party_id_mapping import rename_party_ids  # noqa: E402
from utils.trade_parser import parse_trades  # noqa: E402

DIR = ROOT / 'api' / 'json_payload_trade_injection'

# Steps run in order on each job's parsed trades, after the limit and just
# before the file is written. Each takes the list of trades and returns it.
# Delete an entry (and its import) to drop that step.
POST_PARSE_STEPS = [
    rename_party_ids,  # BNKA_HK -> BNKA-HK etc.; see utils/party_id_mapping.py
    order_currency_pairs,  # USDCNH -> CNHUSD etc.; see utils/currency_pair_order.py
]

# Input and output file per injection mode, both in DIR. 'limit' caps how many
# parsed trades are written; SINGLE_FILE keeps only the first.
JOBS = {
    'SINGLE_FILE':   {'in': 'single_unparsed.json',   'out': 'single_trade.json',   'limit': 1},
    'MULTIPLE_FILE': {'in': 'multiple_unparsed.json', 'out': 'multiple_trade.json', 'limit': None},
}


def main(argv):
    """Parse the job named by argv[1], or every job if none is named.

    :returns: the process exit code, 0 on success and 1 on any failure.
    """
    only = argv[1] if len(argv) > 1 else None

    if only and only not in JOBS:
        print(f"Unknown mode '{only}'. Expected one of: {', '.join(JOBS)}.", file=sys.stderr)
        return 1

    modes = [only] if only else list(JOBS)

    processed = 0
    exit_code = 0

    for mode in modes:
        job = JOBS[mode]
        in_path = DIR / job['in']
        out_path = DIR / job['out']

        if not in_path.exists():
            # Fatal when the mode was named explicitly, as runner.py does: the
            # stale output file would silently inject the previous trade.
            if only:
                print(f'ERROR {mode} requires {job["in"]}, but it is missing at {in_path}',
                      file=sys.stderr)
                return 1
            print(f'SKIP  {job["in"]} (not present)')
            continue

        try:
            raw = json.loads(in_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as err:
            print(f'ERROR {job["in"]} is not valid JSON: {err}', file=sys.stderr)
            exit_code = 1
            continue

        try:
            trades = parse_trades(raw)
        except (ValueError, KeyError, TypeError) as err:
            print(f'ERROR {job["in"]}: {err}', file=sys.stderr)
            exit_code = 1
            continue

        if not trades:
            print(f'ERROR {job["in"]} parsed but produced no trades.', file=sys.stderr)
            exit_code = 1
            continue

        if job['limit']:
            trades = trades[:job['limit']]

        for step in POST_PARSE_STEPS:
            trades = step(trades)

        out_path.write_text(json.dumps(trades, indent=4), encoding='utf-8')
        processed += 1

        plural = 's' if len(trades) > 1 else ''
        print(f'OK    {job["in"]} -> {job["out"]} ({len(trades)} trade{plural})')
        for i, t in enumerate(trades):
            sp = t['wrapper']['sourcePlatform'][0]
            leg = t['wrapper']['tradeData']['fx']['fxCash']['nearLeg']
            print(f'        [{i}] {sp["tradeId"]} | '
                  f'{t["wrapper"]["tradeData"]["fx"]["fxCash"]["currencyPair"]} | '
                  f'value {leg["valueDate"]} | '
                  f'{leg["baseCurrencySeller"]} -> {leg["baseCurrencyBuyer"]}')

    if not processed:
        print('\nNothing parsed. Create single_unparsed.json or multiple_unparsed.json '
              'and paste the received trade into it.', file=sys.stderr)
        exit_code = 1

    return exit_code


if __name__ == '__main__':
    sys.exit(main(sys.argv))
