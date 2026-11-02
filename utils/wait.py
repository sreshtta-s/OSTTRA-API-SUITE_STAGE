"""Polling helper for steps that wait on the eventually consistent ledger or simulator.

wait_until() calls a probe at once, then on a fixed cadence, until it returns
something truthy or the time budget runs out, so a wait is as short as the
environment allows and only as long as it needs. The defaults can be
overridden per run without editing a spec:

    WAIT_TIMEOUT_SECONDS=30 WAIT_INTERVAL_SECONDS=2 python runner.py
"""

import logging
import os
import time

logger = logging.getLogger('osttra.api')


DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_INTERVAL_SECONDS = 5


def _seconds(name, given, fallback):
    """Resolve a wait setting: the env var, else the caller's value, else the default.

    The env var takes precedence so an operator can allow for a slow environment
    without a spec's own value overriding it. It is read on every call, so a
    value set mid-process applies. A non-numeric or non-positive env value
    raises ValueError.
    """
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return fallback if given is None else given
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f'{name} must be a number of seconds, got {raw!r}.') from None
    if value <= 0:
        raise ValueError(f'{name} must be greater than 0, got {raw!r}.')
    return value

# Returned by a probe when the result can never arrive (the call was rejected,
# the token is invalid), so the wait stops at once instead of repeating a
# request that has already given its answer.
GIVE_UP = object()


def wait_until(probe, what, timeout=None, interval=None, initial_delay=None):
    """Call probe() until it returns a truthy value or the timeout expires.

    :param probe: callable run once immediately, then every `interval` seconds.
        Its return value is the result: falsy means "not ready yet", GIVE_UP
        means stop now.
    :param what: description of what is awaited, for the log lines.
    :param timeout: seconds to keep trying; WAIT_TIMEOUT_SECONDS overrides it.
    :param interval: seconds between attempts; WAIT_INTERVAL_SECONDS overrides it.
    :param initial_delay: seconds to pause before the first probe, for a write
        the ledger applies shortly after acknowledging it. The timeout starts
        after this pause, so it does not reduce the number of checks.
    :returns: the probe's truthy result, or None on timeout or GIVE_UP.

    Exceptions from the probe propagate, so a broken call fails the step with
    its own error instead of silently using up the timeout.
    """
    timeout = _seconds('WAIT_TIMEOUT_SECONDS', timeout, DEFAULT_TIMEOUT_SECONDS)
    interval = _seconds('WAIT_INTERVAL_SECONDS', interval, DEFAULT_INTERVAL_SECONDS)

    if initial_delay:
        logger.info('WAIT: holding %.1fs before the first check for %s',
                    initial_delay, what)
        time.sleep(initial_delay)

    started = time.monotonic()
    deadline = started + timeout
    attempts = 0

    while True:
        attempts += 1
        result = probe()
        elapsed = time.monotonic() - started

        if result is GIVE_UP:
            logger.warning('WAIT: stopped waiting for %s after %.1fs - the call '
                           'itself failed, so retrying would not help', what, elapsed)
            return None

        if result:
            logger.info('WAIT: %s after %.1fs (%d check%s)',
                        what, elapsed, attempts, '' if attempts == 1 else 's')
            return result

        # Checks run on a fixed schedule from the start (0, interval, 2*interval
        # ...), so a slow call does not delay later checks and the last one can
        # land exactly on the deadline: a 5s budget at 5s intervals checks at 0s
        # and 5s. A check due after the deadline is not made, so no overrun.
        next_at = started + attempts * interval
        if next_at > deadline + 1e-9:
            logger.warning('WAIT: gave up on %s after %.1fs (%d check%s)',
                           what, elapsed, attempts, '' if attempts == 1 else 's')
            return None

        time.sleep(max(0.0, next_at - time.monotonic()))
