#!/usr/bin/env bash
# make_todays_trades.sh - get today's trades ready for an IMPORT_EXCEL run.
#
# Usage (from anywhere):
#   ./make_todays_trades.sh               copy the latest earlier day's trades (normally yesterday's)
#   ./make_todays_trades.sh 21_10_2026    copy that day's trades instead
#   ./make_todays_trades.sh --new-ids     today's ids were already sent: give today's trades new ones
#
# What it does:
#   1. Copies the day's trade files into import_trade/trades/<today>/, each with
#      a new trade id and match id, MSERV<today>.<NN>..., where NN is a number
#      no trade id of today's has used yet (trade folders, workbooks, .state,
#      reports and the injection files are all checked).
#   2. Moves the dates to today: every trade date becomes today, and each value
#      date keeps its distance from the trade date (never falling before it),
#      except TC030 (next Sunday) and TC031 (2028-02-29), as their scenario
#      files say.
#   3. Writes the new ids into the results workbooks named by EXCEL_FILES in
#      data/osttra_test_scenarios/trade_reader.py. Every test case keeps the
#      same trades, only their ids change.
#   4. Sets IMPORT_DATE in import_trade/import_trade_runner.py and TRADE_DATE in
#      trade_reader.py to today.
#
# Nothing is sent and nothing is committed. Afterwards: python runner.py

set -euo pipefail
cd "$(dirname "$0")"                          # always work from project A/

TODAY=${TODAY:-$(date +%d_%m_%Y)}             # TODAY=23_10_2026 ./make_todays_trades.sh to pretend another day
TRADES=import_trade/trades

# The newest DD_MM_YYYY folder in import_trade/trades dated before today.
latest_before_today() {
    local today_key=${TODAY:6:4}${TODAY:3:2}${TODAY:0:2} best="" best_key="" dir name key
    for dir in "$TRADES"/??_??_????/; do
        name=$(basename "$dir")
        key=${name:6:4}${name:3:2}${name:0:2}  # YYYYMMDD, so folders sort by date
        if [[ $key < $today_key && $key > $best_key ]]; then
            best=$name
            best_key=$key
        fi
    done
    echo "$best"
}

case "${1:-}" in
    -h|--help)  sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
    --new-ids)  FROM=$TODAY ;;
    "")         FROM=$(latest_before_today) ;;
    *)          FROM=$1 ;;
esac

if pgrep -f 'python[0-9.]* .*runner\.py' >/dev/null; then
    echo "ERROR runner.py is running. Wait for it to finish, then try again." >&2
    exit 1
fi
if [[ -z $FROM || ! -d $TRADES/$FROM ]]; then
    echo "ERROR no trades folder to copy from: $TRADES/${FROM:-<none before $TODAY>}" >&2
    exit 1
fi
if [[ $FROM != "$TODAY" ]] && compgen -G "$TRADES/$TODAY/*.json" >/dev/null; then
    echo "ERROR $TRADES/$TODAY already has trades." >&2
    echo "      To give them new ids instead: $0 --new-ids" >&2
    exit 1
fi

PYTHON=.venv/bin/python
[[ -x $PYTHON ]] || PYTHON=python3

# Steps 1 to 3. Each trade is built in a temporary folder first, so a failure
# leaves today's folder and the workbooks as they were.
"$PYTHON" - "$FROM" "$TODAY" <<'END_OF_PYTHON'
import json
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

sys.path.insert(0, '.')
from data.osttra_test_scenarios import trade_reader as tr

FROM, TODAY = sys.argv[1], sys.argv[2]
from_day = datetime.strptime(FROM, '%d_%m_%Y').date()
today = datetime.strptime(TODAY, '%d_%m_%Y').date()
shift = today - from_day
ymd = today.strftime('%Y%m%d')

ID = re.compile(r'MSERV(\d{8})\.(\d{2})(\d+)')     # MSERV<date>.<NN><rest of the id>
DATE = re.compile(r'(?<!\d)(\d{4})-(\d\d)-(\d\d)(?!\d)')
EPOCH_MS_KEYS = ('eventTime', 'validFrom')


def next_sunday(d):
    return d + timedelta(days=(6 - d.weekday()) % 7 or 7)


# Value dates that do not keep their distance from the trade date.
FIXED_VALUE_DATES = {
    'TC030': next_sunday(today).isoformat(),        # {{NEXT_SUNDAY}} in TC030.json
    'TC031': '2028-02-29',                          # fixed in TC031.json
}


def used_numbers():
    """Every NN already used in an MSERV<today>.<NN> id anywhere in the project."""
    found = set()
    pattern = re.compile(rf'MSERV{ymd}\.(\d\d)')
    for place in ('import_trade/trades', 'data/osttra_test_scenarios', '.state', 'reports',
                  'api/json_payload_trade_injection'):
        for path in Path(place).rglob('*'):
            try:
                if path.suffix == '.xlsx' and not path.name.startswith('~$'):
                    with zipfile.ZipFile(path) as z:
                        texts = [z.read(n).decode('utf-8', 'ignore') for n in z.namelist() if n.endswith('.xml')]
                elif path.suffix in ('.json', '.html', '.txt') and path.is_file():
                    texts = [path.read_text(encoding='utf-8', errors='ignore')]
                else:
                    continue
            except (OSError, zipfile.BadZipFile):
                continue
            for text in texts:
                found.update(int(n) for n in pattern.findall(text))
    return found


def move_dates(node, key=None):
    """Move every date and timestamp in a trade by `shift`; dates over a year old (masterAgreementDate) stay."""
    if isinstance(node, dict):
        return {k: move_dates(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [move_dates(v, key) for v in node]
    if isinstance(node, str):
        def one(m):
            d = date(int(m[1]), int(m[2]), int(m[3]))
            return (d + shift).isoformat() if d >= from_day - timedelta(days=365) else m[0]
        return DATE.sub(one, node)
    if key in EPOCH_MS_KEYS and isinstance(node, int):
        return node + shift.days * 86_400_000
    return node


# The workbooks, and which workbook and case names each trade (by the id after MSERV<date>.<NN>).
workbooks = [tr.excel_path(name) for name in tr.excel_files()]
named_by = {}
for n, path in enumerate(workbooks):
    for case, ids in tr.read_excel(path).items():
        for trade_id in ids:
            m = ID.fullmatch(trade_id)
            if not m:
                sys.exit(f'ERROR {path.name} {case}: {trade_id} is not an MSERV<date>.<NN>... id')
            named_by.setdefault(m[3], (n, case))

# A new NN for each workbook's trades, above every number used today.
first = max(used_numbers(), default=0) + 1
new_nn = [first + n for n in range(len(workbooks))]
if new_nn[-1] > 99:
    sys.exit(f'ERROR no two-digit id numbers left for {ymd}.')


def new_prefix(rest):
    return f'MSERV{ymd}.{new_nn[named_by[rest][0]]:02d}'


# The trades to copy: one file per trade a workbook names. When the folder holds
# the same trade under several ids (an earlier attempt), the newest id wins.
source = Path(tr.TRADES_DIR) / FROM
picked, copied, skipped = {}, [], []
for path in sorted(source.glob('*.json')):
    raw = path.read_text(encoding='utf-8')
    wrapper = json.loads(raw)['publishEvent']['eventPayload']['current']['wrapper']
    trade_id = ID.fullmatch(wrapper['sourcePlatform'][0].get('tradeId') or '')
    file_id = ID.match(path.name) or trade_id
    rest = next((r for r in (trade_id and trade_id[3], file_id and file_id[3]) if r in named_by), None)
    if rest is None:
        skipped.append(path.name.split('_', 1)[0])
        continue
    copied.append(path)
    age = (trade_id[1], trade_id[2])
    if rest not in picked or age > picked[rest][0]:
        picked[rest] = (age, path, raw, trade_id, file_id)
if not picked:
    sys.exit(f'ERROR {source} holds no trade the workbooks name.')

with tempfile.TemporaryDirectory(dir=tr.TRADES_DIR, prefix='.new_') as tmp:
    for rest, (_, path, raw, trade_id, file_id) in picked.items():
        prefix = new_prefix(rest)
        text = raw
        for old in {f'MSERV{trade_id[1]}.{trade_id[2]}', f'MSERV{file_id[1]}.{file_id[2]}'}:
            text = text.replace(old, prefix)
        text = text.replace(f'FXMSG{trade_id[1]}{trade_id[2]}', f'FXMSG{ymd}{prefix[-2:]}')

        trade = move_dates(json.loads(text))
        wrapper = trade['publishEvent']['eventPayload']['current']['wrapper']
        meta, fx = wrapper['metaData'], wrapper['tradeData']['fx']
        leg = fx['fxCash']['nearLeg']
        ends_on_value_date = meta.get('tradeLifecycleEndDate') == leg['valueDate']
        meta['tradeDate'] = fx['tradeDate'] = today.isoformat()
        leg['valueDate'] = max(leg['valueDate'], today.isoformat())      # never before the trade date
        leg['valueDate'] = FIXED_VALUE_DATES.get(named_by[rest][1], leg['valueDate'])
        if ends_on_value_date:
            meta['tradeLifecycleEndDate'] = leg['valueDate']

        out = json.dumps(trade, separators=(',', ':'), ensure_ascii=False) + ('\n' if raw.endswith('\n') else '')
        name = path.name.replace(f'MSERV{file_id[1]}.{file_id[2]}', prefix)
        (Path(tmp) / name).write_text(out, encoding='utf-8')

    # Swap the new trades in. With --new-ids the old copies go, but only once
    # every new one is written; a file no workbook names is left alone.
    target = Path(tr.TRADES_DIR) / TODAY
    target.mkdir(exist_ok=True)
    if FROM == TODAY:
        for old in copied:
            old.unlink()
    for new in Path(tmp).iterdir():
        shutil.move(str(new), target / new.name)

# Step 3: the same new ids in the workbooks.
for n, path in enumerate(workbooks):
    book = load_workbook(path)
    sheet = book.worksheets[0]
    headers = [str(c.value or '').strip().lower() for c in sheet[1]]
    col = headers.index(tr.TRADE_IDS_HEADER.lower())
    for row in sheet.iter_rows(min_row=2):
        cell = row[col]
        if isinstance(cell.value, str):
            cell.value = ID.sub(lambda m: new_prefix(m[3]) + m[3], cell.value)
    book.save(path)

# Report what was done, and which listed trades have no file in today's folder.
print(f'OK    {FROM} -> {TODAY}: {len(picked)} trades in {tr.TRADES_DIR.name}/{TODAY}/')
print(f'      trade date {today} on every trade; value dates moved {shift.days} day(s); '
      + ', '.join(f'{case} value date {d}' for case, d in FIXED_VALUE_DATES.items()))
if skipped:
    print(f'      not copied, no workbook names them: {", ".join(skipped)}')
index, _ = tr.index_trades(Path(tr.TRADES_DIR) / TODAY)
for n, path in enumerate(workbooks):
    listed = tr.read_excel(path)
    missing = [case for case, ids in listed.items() if any(tr.find_trade(index, i) is None for i in ids)]
    print(f'OK    {path.name}: ids now MSERV{ymd}.{new_nn[n]:02d}... '
          f'({sum(len(ids) for ids in listed.values())} ids, {len(listed)} cases)')
    if missing:
        print(f'      no trade file for (shown NOT PRESENT in the run): {", ".join(missing)}')
END_OF_PYTHON

# Step 4: point both date settings at today's folder.
sed -i "s/^IMPORT_DATE = '[^']*'/IMPORT_DATE = '$TODAY'/" import_trade/import_trade_runner.py
sed -i "s/^TRADE_DATE = '[^']*'/TRADE_DATE = '$TODAY'/" data/osttra_test_scenarios/trade_reader.py
grep -q "^IMPORT_DATE = '$TODAY'" import_trade/import_trade_runner.py &&
grep -q "^TRADE_DATE = '$TODAY'" data/osttra_test_scenarios/trade_reader.py &&
echo "OK    IMPORT_DATE and TRADE_DATE set to $TODAY"
