"""Suite-wide pytest configuration: the shared HTTP session and result output.

runner.py runs each step in its own process with BLOB_FILE set to a unique
.json path, and this module writes the step's results there.
utils/reporter.merge_html_report merges the blobs, in file-name order, into one
HTML report. One blob per step keeps steps from overwriting each other, and
merging leaves one report to open instead of one per step. Without BLOB_FILE
(a plain `pytest` run), pytest-html writes the report directly.
"""

import json
import os
import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent

# Put the project root on sys.path so project imports (api, config, utils)
# resolve wherever pytest is started from.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.steps import STEPS  # noqa: E402  (needs the sys.path line above)

# Owner-only for every file this process creates (state holds live tokens,
# blobs and reports hold trade data). runner.py sets the same; this covers a
# plain `pytest` run.
os.umask(0o077)

# Sent on every request made through the session fixture.
DEFAULT_HEADERS = {'Accept': 'application/json'}

# Per-request timeout for the session fixture, unless a call sets its own, so a
# hung request fails its step instead of stalling the pipeline.
TIMEOUT_SECONDS = 300


def pytest_configure(config):
    """Point pytest-html at reports/html/latest/index.html for an ad-hoc run.

    Does nothing under the runner (BLOB_FILE set), where results go to a blob
    that runner.py merges into its own HTML report; nor when pytest-html is not
    installed or --html was given.
    """
    if os.environ.get('BLOB_FILE'):
        return
    if not config.pluginmanager.hasplugin('html'):
        return
    if getattr(config.option, 'htmlpath', None):
        return   # an explicit --html wins

    config.option.htmlpath = str(ROOT / 'reports' / 'html' / 'latest' / 'index.html')
    config.option.self_contained_html = True


@pytest.fixture(scope='session')
def session():
    """One requests.Session per pytest process, with default headers and timeout.

    Connection pooling helps the steps that make many calls: the paged ledger
    search in 05/05b and the polling in 15/15b.
    """
    with requests.Session() as http:
        http.headers.update(DEFAULT_HEADERS)
        http.request = _with_timeout(http.request)
        yield http


def _with_timeout(request_method):
    """Wrap a request method so every call gets TIMEOUT_SECONDS by default."""
    def wrapped(method, url, **kwargs):
        kwargs.setdefault('timeout', TIMEOUT_SECONDS)
        return request_method(method, url, **kwargs)
    return wrapped


# ----------------------------------------------------------------------------
# BLOB RESULTS (one file per step, merged by the runner)
# ----------------------------------------------------------------------------

# One entry per test node id; _order keeps the order tests were first seen.
_results = {}
_order = []

# Caps each message in the blob so one long traceback cannot bloat the report.
MAX_MESSAGE_CHARS = 4000


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Set the report's description to the test's display name.

    The name is the first line of the test's docstring, or the test item's name
    if there is none. It is the test's name in the HTML report, so that first
    line must stay stable.
    """
    outcome = yield
    report = outcome.get_result()
    doc = (item.function.__doc__ or '').strip()
    name = doc.splitlines()[0].strip() if doc else item.name

    # Append the parameter id so each case of a parametrised test (step 01 runs
    # once per auth domain) gets a distinct name.
    callspec = getattr(item, 'callspec', None)
    if callspec is not None:
        name = f'{name} [{callspec.id}]'

    report.description = name


def _message_for(report):
    """Return a report's failure or skip text, truncated, or None if it passed."""
    if report.passed:
        return None
    text = report.longreprtext or ''
    if report.skipped and isinstance(report.longrepr, tuple):
        text = report.longrepr[2]   # (path, lineno, reason)
    text = text.strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + '\n... (truncated)'
    return text or None


def pytest_runtest_logreport(report):
    """Record one blob entry per test, taking its worst phase as the outcome.

    Every phase (setup, call, teardown) counts: a module-scoped fixture that
    raises fails in setup, so recording only the call phase would drop a broken
    pre-fetch from the report. Durations are summed across phases.
    """
    if not os.environ.get('BLOB_FILE'):
        return

    node = report.nodeid
    if node not in _results:
        _order.append(node)
        _results[node] = {
            'name': getattr(report, 'description', None) or node.split('::')[-1],
            'outcome': 'passed',
            'duration': 0.0,
            'message': None,
        }

    entry = _results[node]
    entry['duration'] += report.duration

    if report.failed and entry['outcome'] != 'failed':
        entry['outcome'] = 'failed'
        entry['message'] = _message_for(report)
    elif report.skipped and entry['outcome'] == 'passed':
        entry['outcome'] = 'skipped'
        entry['message'] = _message_for(report)


def pytest_sessionfinish(session, exitstatus):
    """Write this step's blob to BLOB_FILE, if the runner set one.

    CURRENT_STEP, also set by runner.py, names the step; its label and spec
    file come from the step registry, and are blank for a token it does not
    list, such as runner.py's netting-total check.
    """
    blob = os.environ.get('BLOB_FILE')
    if not blob:
        return

    token = os.environ.get('CURRENT_STEP', '')
    step = STEPS.get(token, {})

    path = Path(blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'step': token,
        'label': step.get('label', ''),
        'file': step.get('file', ''),
        'exitStatus': exitstatus,
        'tests': [_results[node] for node in _order],
    }, indent=2), encoding='utf-8')
