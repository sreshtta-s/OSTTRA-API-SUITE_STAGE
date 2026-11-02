"""Live log of each request a step sends and the ids it carries or captures.

pytest captures print() and shows it only for failing tests, so these records
go through logging: pytest.ini enables log_cli at INFO, which streams them live
whether a test passes or fails.

Every function here is best-effort: a logging failure must never fail a test,
the same rule utils.reporter.log_call follows.
"""

import json
import logging

logger = logging.getLogger('osttra.api')

# Fields logged on their own line above the payload: the ids of the trade, its
# netting group, its settlement and the simulator messages, plus the party.
ID_FIELDS = (
    'tradeId',
    'uniqueTransactionId',
    'affirmationId',
    'sourceTradeId',
    'nettingGroupId',
    'nettingRuleId',
    'settlementId',
    'commonGroupId',
    'message_id',
    'message_ids',
    'tradingParty',
)

# Payloads up to this many characters are logged on one line, longer ones
# indented. The ledger paging loop posts a short body up to 40 times, and forty
# indented blocks would bury the calls that matter.
COMPACT_LIMIT = 200


def _record(found, field, value):
    """Add a field's value(s) to found, de-duplicated in first-seen order.

    Filter endpoints wrap ids in a list, so a list is flattened. None, dict and
    list items are skipped.
    """
    values = value if isinstance(value, list) else [value]
    seen = found.setdefault(field, [])
    for item in values:
        if item is None or isinstance(item, (dict, list)):
            continue
        text = str(item)
        if text and text not in seen:
            seen.append(text)


def _walk(node, found):
    """Recursively collect ID_FIELDS values from node into found."""
    if isinstance(node, dict):
        # Filter payloads name an id as {'fieldName': 'nettingGroupId',
        # 'value': [...]} rather than as a key, so read that shape as its field.
        field = node.get('fieldName')
        if isinstance(field, str) and 'value' in node:
            # Dotted paths such as 'underlyingDetails.nettingGroup.nettingGroupId'
            # are named by their last segment.
            leaf = field.rsplit('.', 1)[-1]
            if leaf in ID_FIELDS:
                _record(found, leaf, node['value'])

        for key, value in node.items():
            if key in ID_FIELDS:
                _record(found, key, value)
            _walk(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk(item, found)


def extract_ids(payload):
    """Return every ID_FIELDS value in the payload, at any depth, by field.

    No fixed depth is assumed: the trade injection payload, for example, holds
    its tradeId under [0].wrapper.sourcePlatform[0].
    """
    found = {}
    _walk(payload, found)
    return found


def _format_ids(found):
    return '  '.join(f'{field}=[{", ".join(values)}]' for field, values in found.items())


def _render(payload):
    """Format a payload for the log: one line if short, indented JSON if long."""
    try:
        compact = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return repr(payload)
    if len(compact) <= COMPACT_LIMIT:
        return compact
    return '\n' + json.dumps(payload, indent=2, default=str)


def log_request(method, endpoint, payload=None, ids_only=False):
    """Log a request, its ids and its payload before the request is sent.

    Logging first means the body is on screen even if the call never returns.

    :param ids_only: log the ids but not the body. Used for the netting and
        underlying services, which are polled with near-identical filter
        bodies where only the ids change between calls.
    """
    try:
        logger.info('--> %s %s', method, endpoint)
        if payload is None:
            return
        found = extract_ids(payload)
        if found:
            logger.info('    IDS: %s', _format_ids(found))
        if not ids_only:
            logger.info('    PAYLOAD: %s', _render(payload))
    except Exception:
        pass


def log_ids(label, **ids):
    """Log ids read from a response at the point the step captures them.

    Netting group, settlement and message ids are discovered, not sent, so
    without this they would first appear in the next step's payload. Empty
    values are left out.
    """
    try:
        shown = '  '.join(
            f'{name}=[{", ".join(str(v) for v in value)}]' if isinstance(value, (list, tuple))
            else f'{name}=[{value}]'
            for name, value in ids.items()
            if value not in (None, '', [], ())
        )
        logger.info('*** %s: %s', label, shown or 'nothing captured')
    except Exception:
        pass
