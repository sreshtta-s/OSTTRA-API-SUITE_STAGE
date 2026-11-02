# OSTTRA API Suite (Project A)

End-to-end API tests for the OSTTRA PvP integration. Each run sends FX trades in
through the simulator. It then checks each trade on Domain A, Domain B and the
Global Operator, from booking through netting and settlement to the ledger.
Results are written to HTML and Excel reports.

## Prerequisites

- Linux or macOS
- Python 3.10 or newer
- `venv` and `pip`. On Ubuntu or Debian: `sudo apt install python3-venv python3-pip`
- HTTPS access to Keycloak, the Domain A, Domain B and Global APIs, and the
  simulator
- Credentials for Domain A, Domain B and the Global Operator: a Keycloak client
  id and secret, plus a username and password. Also the simulator API key.
- Active netting rules for the trades you book (see [Netting rules](#netting-rules))

## Setup

Run all commands from `project A/`.

```bash
python3 -m venv .venv
source .venv/bin/activate                 # repeat in every new terminal
python -m pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
```

Fill in every value in `.env`:

| Variable | Value |
| --- | --- |
| `DOMAIN_A`, `DOMAIN_B`, `DOMAIN_GLOBAL` | API base URL, no trailing slash |
| `KEYCLOAK_URL`, `KEYCLOAK_URL_B`, `KEYCLOAK_URL_GLOBAL` | Full token URL, ending in `/protocol/openid-connect/token` |
| `CLIENT_ID`, `CLIENT_SECRET`, `API_USERNAME`, `API_PASSWORD` | Domain A login. Domain B and Global use the same names with `_B` and `_GLOBAL` added. |
| `SIMULATOR_TASKS_URL`, `SIMULATOR_API_KEY` | Simulator base URL (no trailing slash) and its API key |

If a variable is set in more than one place, the first of these wins:

1. the shell
2. `project A/.env`
3. the repository root `.env`

A `.venv` folder can't be copied or moved. Create a new one instead.

## Verify

```bash
python -m pytest --collect-only -q           # ends with "N tests collected" and no errors
python -m pytest tests/test_01_auth.py       # all tests pass: login works
UI_INJECTION_MODE=DYNAMIC python runner.py   # sends one real trade; exits with status 0
```

## Run

```bash
python runner.py
```

The runner exits with status `0` if every trade passed, or `1` if a step failed
or the run stopped early. A failed step is recorded, and the run continues
with the next step.

To choose what runs, set `INJECTION_MODE` (and `SCENARIO`) in
`utils/flag.py`. To override them for one run, use `UI_INJECTION_MODE` and
`TEST_SCENARIO`:

| Mode | Runs |
| --- | --- |
| `DYNAMIC` | One generated trade, built from `.state.example.json` |
| `SCENARIO` | One test case, `data/osttra_test_scenarios/<SCENARIO>.json` |
| `IMPORT_EXCEL` | Every test case, on the trades a results workbook lists for it (see below) |
| `IMPORT` | Every trade in `import_trade/trades/<IMPORT_DATE>/` |
| `SINGLE_FILE` | The trade in `api/json_payload_trade_injection/single_unparsed.json` |
| `MULTIPLE_FILE` | The trades in `api/json_payload_trade_injection/multiple_unparsed.json` |

For example, to run one test case:

```bash
UI_INJECTION_MODE=SCENARIO TEST_SCENARIO=TC009 python runner.py
```

You can also set these for a single run:

| Variable | Effect |
| --- | --- |
| `AUTH_DOMAINS=A,B` | Logs in to only the domains listed |
| `WAIT_TIMEOUT_SECONDS`, `WAIT_INTERVAL_SECONDS` | Poll timing in seconds. The default is 5, and setting them overrides every step's own value. |

Other ways to run:

- From the repository root, `python run_all.py a` runs project A.
- `python -m pytest tests/<file>` runs one step on its own, for debugging. The
  step needs the run state left by steps 01 and 04.

### IMPORT_EXCEL

1. Put the day's trade files in `import_trade/trades/DD_MM_YYYY/`. Each trade
   is one `.json` file, and the file name starts with the trade id.
2. Set these three values in `data/osttra_test_scenarios/trade_reader.py`:
   - `EXCEL_FILES`: the workbooks to read, one after another in list order.
     Each needs the columns `TC (Test ID traceability)` and `matchId`. In
     `matchId`, separate trade ids with `#`, or write `N/A`.
   - `TRADE_DATE`: the folder name. Leave it blank for today.
   - `CASES`: the cases to run. Leave it empty to run all of them.
3. Optional: preview which trades each case will use, without sending anything:
   `python data/osttra_test_scenarios/trade_reader.py`
4. Set `INJECTION_MODE = 'IMPORT_EXCEL'`, then run.

A case with a scenario file follows that file's flow on the workbook's trades,
including any cancel steps it declares. A case with no scenario file runs each
of its trades as a normal trade through every step, without the cancel steps.
A case the workbook names no trade for is skipped. A trade file that holds a
cancellation (status `Cancelled`) is still booked as a new trade, with status
`New`, so only a scenario's cancel steps cancel it.

A case shows `NOT PRESENT` when some trade files it lists aren't in the folder.
It shows `FAIL (not run)` when it can't run for any other reason, and the
runner's output says why.

Never send a trade id the environment has already received.

## Pipeline steps

Steps ending in `b` run on Domain B. `13c` runs on the Global Operator.

| Step | Checks |
| --- | --- |
| 01 | Login on all three domains |
| 04 | Trade sent in through the simulator |
| 05, 05b | Trade booked correctly |
| 04 cancel, 05ct_a, 05ct_b | Trade resent as Cancelled and shown CANCELLED on both ledgers. 05ct_b starts 20s after 05ct_a, and each polls its ledger every 3s for up to 15s until the trade is CANCELLED. Runs only for a scenario trade with `"cancel": true` (all three after 05b, as in TC032: 01, 04, 05, 05b, 04 cancel, 05ct_a, 05ct_b), or for the netting group trade a row's `"cancelTrade"` names, after the group step it names: after 07b in TC034, TC045 and TC050, where 07c then checks the group's balance without it (to 0 in TC045 and TC050, which cancel their only trade; TC045's group stops after 07c, with no top-level `"runUpTo"`, and TC050's runs on to 13 / 13b, `"runUpTo": "13b"`); after 08b in TC035, where the closed group refuses the cancel and 05ct_a / 05ct_b are expected to fail. |
| 06, 06b | Netting group exists |
| 07, 07b, 07c | Trade is in the group. 07c checks the amount a multi-trade group nets to, reading only the group 07 / 07b found. |
| 08, 08b | Netting group closed |
| 09, 09b | Group status |
| 10, 11, 11b | Netting proposal fetched and matched (manual rules only) |
| 12, 12b | Group confirmed |
| 13, 13b, 13c | Settlement started |
| 13cs_a, 13cs_b | Settlement cancelled on Domain A (`settlementId`) and Domain B (`settlementIdB`): `POST .../settlement/<id>/cancel` with an empty `{}` body. Runs only when a scenario sets `"cancelSettlement"`: on a trade entry for that trade's settlement, or at the top of a netting row (with a `runUpTo` of 13c or later) for its group's. `true` cancels straight after 13c, even when the row skips 13c; steps 14 to 15b still run up to the `runUpTo`. An object can move the cancel later with `"after"` and declare outcomes with `"expect"`: TC038 cancels its completed settlement after 15b and expects 13cs_a and 13cs_b to fail (the NACK). `"delayBefore"` waits before a cancel step: TC037 runs its group to 14, waits 30 seconds, then expects 13cs_a and 13cs_b to fail. |
| 15cs_a, 15cs_b | Settlement shown CANCELLED on the Domain A and B settlement ledgers (`POST .../settlement/getAll`, the list step 15 reads). Only `settlementStatus` is checked. Runs for the same scenarios, straight after 13cs_a / 13cs_b or after the later step `"cancelSettlement": {"checkAfter": "<step>"}` names, and not at all when its `"expect"` declares the cancel refused (TC038). TC036 skips 13 to 14 with a netting row's `"skip"`, so its settlement is cancelled while queued, declares 15 and 15b fail with the row's `"expect"`, and checks the cancel after 15b. |
| 14 | MT910 funding call generated |
| 15, 15b | Ledger |

## Netting rules

`NETTING_RULES` in `data/trade_scenarios.py` gives the rule id and mode for
each combination of party, counterparty, currency pair and product.

- Write the currency pair in alphabetical order, for example `CNHUSD`.
- Write the product as `FxSpot` or `FxForward`.
- Mode `manual` runs steps 10 to 11b. Any other mode skips them.
- A trade with no entry gets an invalid rule id on purpose, so it fails at step 06.
- A rule gets a new id when it is recreated in the rule center. Update the id here.

## Reports

- **HTML:** `reports/html/<date>__<mode, case or trade>/index.html`. Folders
  are named by day, so a rerun on the same day replaces the report.
- **Excel:** in `reports/excel/`, one tab per day and one row per trade.
  - `IMPORT_EXCEL` runs: `osttra-api-suite-import-excel-report.xlsx`
  - `IMPORT` runs: `osttra-api-suite-import-report.xlsx`
  - all other modes: `osttra-api-suite-report.xlsx`

Reports, `.state/` and `.env` are ignored by git and readable only by your user.

## Rules

- Run only one `runner.py` at a time. Check with `pgrep -af runner.py` first.
  Two runs at once corrupt each other's state.
- Don't edit input files while a run is in progress.
- Back up `reports/html/` before a rerun if you need the earlier report.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `ensurepip is not available` | `sudo apt install python3-venv` |
| `bad interpreter` from `.venv/bin/pip` | `rm -rf .venv`, then run the setup again |
| `ModuleNotFoundError` | `source .venv/bin/activate` |
| `missing env vars in .env` | Fill in the variables it lists |
| Step 01 returns 400 or 401 | Check the credentials and the `KEYCLOAK_URL*` values |
| Steps 06 and 07 find no netting group | Update `NETTING_RULES` |
| `FATAL: Import failed` | Check the date (`DD_MM_YYYY`) and the trade folder |
| Timeouts | `WAIT_TIMEOUT_SECONDS=60 python runner.py` |
| Report row shows the wrong trade | Two runs overlapped. Run again on its own. |

## Key files

| Path | Purpose |
| --- | --- |
| `runner.py` | Runs the pipeline and writes the reports |
| `utils/flag.py` | Injection mode and scenario |
| `data/trade_scenarios.py` | Netting rules |
| `data/osttra_test_scenarios/` | Test cases, results workbook, `trade_reader.py` |
| `import_trade/trades/` | Daily trade files |
| `tests/` | One pytest file per step |
