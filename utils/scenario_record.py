"""Saved record of a multi-trade scenario's last run, for its netting group check.

A row that checks its trades as one netting group (TC010 to TC013, TC018) books them
one at a time; runner.py saves them here, with their economics, and step 07c
reads them back to find their group. A continuation (a row that books nothing
and checks an earlier row's trades; none at present) also carries the earlier
row's step results into its own report.

Stored under .state with the other run scratch files, since it names live trade
ids and is output, not source. Each run replaces its scenario's record, so a
continuation always checks the most recent run.
"""

import json
import os

from utils.state_helper import STATE_DIR


def record_path(scenario_id):
    """Return the record file path for a scenario id."""
    return STATE_DIR / f'last-run-{scenario_id}.json'


def save_record(scenario_id, record):
    """Write a scenario's record, replacing any earlier one, and return its path."""
    path = record_path(scenario_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a crash cannot leave a truncated record, and
    # owner-only (0o600) like the state file beside it: it names live trades.
    tmp = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(record, indent=2), encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def load_record(scenario_id):
    """Return a scenario's last recorded run, or None if missing or unreadable.

    Returns None rather than raising so the continuation's pre-flight test can
    fail with a clear "run the earlier scenario first" message.
    """
    path = record_path(scenario_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
