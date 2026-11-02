"""Simulator trade-injection client, used by step 04.

Calls SIMULATOR_TASKS_URL with the simulator API key instead of a domain token,
so it does not extend BaseService; it logs requests and records them in the
report's call log the same way.
"""

import os

from utils.api_logger import log_request
from utils.reporter import log_call

INJECT_PATH = '/api/cashflow/osttra/trade/previewPump'


class TradeService:
    """Client for the simulator's trade-injection endpoint."""

    def __init__(self, session):
        self.session = session

    def inject_trade(self, payload):
        """Post a trade payload to the simulator's injection endpoint.

        Steps 05 / 05b then look for the trade on each domain's ledger.
        """
        log_request('POST', f'SIM {INJECT_PATH}', payload)
        res = self.session.post(
            f'{os.environ["SIMULATOR_TASKS_URL"]}{INJECT_PATH}',
            headers={
                'x-api-key': os.environ['SIMULATOR_API_KEY'],
                'Content-Type': 'application/json',
            },
            json=payload,
        )
        log_call(f'SIM {INJECT_PATH}', res.status_code, 'POST')
        return res
