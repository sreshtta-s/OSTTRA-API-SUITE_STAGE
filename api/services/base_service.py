"""Base class and success check shared by every domain-scoped API client.

Subclasses supply endpoint paths. BaseService adds the domain's base URL and
bearer token, logs each request before it is sent, and records its status in
the report's call log.
"""

from config.auth_config import get_domain
from utils.api_logger import log_request
from utils.reporter import log_call


def is_ok(response):
    """Return True for a 2xx status only.

    `requests.Response.ok` is true for any status below 400, which would count a
    redirect as success, so the suite checks success with this instead.
    """
    return 200 <= response.status_code < 300


class BaseService:
    """JSON client for one auth domain, authenticated with a bearer token.

    :param domain: 'A', 'B' or 'GLOBAL' (see config/auth_config.py).
    :param token: that domain's access token, saved to run state by step 01.
    """

    # Subclasses whose request bodies are filter boilerplate set this False: the
    # endpoint and the payload's ids are still logged, the body is not.
    log_payload = True

    def __init__(self, session, domain='A', token=None):
        self.session = session
        self.domain = get_domain(domain)
        self.base_url = self.domain.base_url
        self.token = token

    @property
    def headers(self):
        """JSON request headers carrying the bearer token."""
        return {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def post(self, pathname, data):
        """POST a JSON body to a path under the domain's base URL.

        The request is logged before it is sent, so it is on screen even if the
        call never returns; the status is then added to the report's call log.
        """
        log_request('POST', f'{self.domain.key} {pathname}', data,
                    ids_only=not self.log_payload)
        res = self.session.post(f'{self.base_url}{pathname}', headers=self.headers, json=data)
        log_call(f'{self.domain.key} {pathname}', res.status_code, 'POST')
        return res
