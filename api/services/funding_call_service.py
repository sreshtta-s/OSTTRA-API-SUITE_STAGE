"""Simulator funding-call client, used by step 14 to generate MT910 messages.

Calls SIMULATOR_TASKS_URL with the simulator API key instead of a domain token,
so it does not extend BaseService; it logs requests and records them in the
report's call log the same way.
"""

import os

from utils.api_logger import log_request
from utils.reporter import log_call

BASE = '/api/cashflow/osttra/tasks/fundingCalls'


class FundingCallService:
    """Client for the simulator's funding-call task endpoints (list and pump)."""

    def __init__(self, session):
        self.session = session

    @property
    def headers(self):
        """Simulator API-key headers shared by both calls."""
        return {
            'x-api-key': os.environ['SIMULATOR_API_KEY'],
            'Accept': 'application/json',
        }

    def get_funding_tasks(self):
        """List the simulator's funding-call tasks.

        Step 14 matches each task's payload to the settlement id to find the
        message ids to pump.
        """
        log_request('GET', f'SIM {BASE}/list')
        res = self.session.get(
            f'{os.environ["SIMULATOR_TASKS_URL"]}{BASE}/list',
            headers=self.headers,
        )
        log_call(f'SIM {BASE}/list', res.status_code, 'GET')
        return res

    def generate_mt910(self, payload):
        """Ask the simulator to generate MT910s for the payload's message ids."""
        log_request('POST', f'SIM {BASE}/pump', payload)
        res = self.session.post(
            f'{os.environ["SIMULATOR_TASKS_URL"]}{BASE}/pump',
            headers={**self.headers, 'Content-Type': 'application/json'},
            json=payload,
        )
        log_call(f'SIM {BASE}/pump', res.status_code, 'POST')
        return res
