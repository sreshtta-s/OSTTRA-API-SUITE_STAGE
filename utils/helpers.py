"""Timestamp, id and number-check helpers used across the suite."""

import random
import uuid
from datetime import datetime, timedelta, timezone


class TestUtils:
    """Timestamps and unique identifiers for building trade payloads."""

    @staticmethod
    def generate_timestamps():
        """Return the current UTC time in each format the payloads use.

        nettingTriggerTime and settlementTriggerTime are HH:MM, 3 and 6 minutes
        from now.
        """
        now = datetime.now(timezone.utc)
        return {
            'dateStr': now.strftime('%Y-%m-%d'),
            'shortDate': now.strftime('%d%m%y'),
            'compactDate': now.strftime('%Y%m%d'),
            # Matches the Playwright suite, where moment's 'SS' token is hundredths
            # of a second, not seconds.
            'currentTimestamp': now.strftime('%y%m%d%H%M%S') + f'{now.microsecond // 10000:02d}',
            'nettingTriggerTime': (now + timedelta(minutes=3)).strftime('%H:%M'),
            'settlementTriggerTime': (now + timedelta(minutes=6)).strftime('%H:%M'),
        }

    @staticmethod
    def generate_uuid():
        """Return a random UUID4 as a string."""
        return str(uuid.uuid4())

    @staticmethod
    def generate_trade_identifiers():
        """Return a fresh, random set of trade ids, dated today in UTC where needed."""
        t = TestUtils.generate_timestamps()
        random10 = random.randint(1000000000, 9999999999)
        random8 = random.randint(10000000, 99999999)
        random20 = f'{random.randrange(10 ** 20):020d}'

        return {
            'tradeId': f'MSERV-{random10}',
            'tradeSideId': f'{random8}',
            'uniqueTransactionId': f'549300CWK6F65W566G32MSFX{t["compactDate"]}{random20}',
            'partyTradeId': f'FXFPVP{t["shortDate"]}B-{random8}',
            'counterpartyTradeId': f'FXFPVP{t["shortDate"]}BCP-{random8}',
        }


def is_number(value):
    """Return True for an int or float, but not a bool.

    Matches JavaScript's `typeof value === 'number'`. bool is a subclass of int,
    so a plain isinstance check would let True pass a numeric-id assertion.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)
