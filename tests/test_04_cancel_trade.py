# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 04 cancel: inject step 04's trade with metaData.status "Cancelled".

Everything else is step 04's: the payload for the active injection mode, built
under the ids step 04 injected (injectedTradeIds in state) so the ledger
cancels that trade, the state saved on a 2xx and every check. The checks are imported from
test_04_inject_trade and run here against this module's ctx, so the two steps
cannot drift apart.

runner.py runs it straight after step 05b (CANCEL_STEPS), but only for a trade
run_pipeline() is told to cancel: a scenario trade entry with "cancel": true,
such as TC032's. A netting row's cancelTrade runs it on one of its trades
after the group step it names instead (07b in TC034, 08b in TC035). It is in
neither config/steps.py nor STEP_TOKENS. It can also be run directly with pytest.
"""
import pytest

from api.payloads.trade_payload import CANCELLED_STATUS
# Imported test functions are collected here too, each resolving ctx below.
# Listed in step 04's order, which is the order they run and are reported in.
from test_04_inject_trade import (  # noqa: F401
    inject,
    test_endpoint_returns_2xx,
    test_injection_status_matches_expected,
    test_trade_id_exists_in_payload,
    test_trade_id_nomenclature,
    test_trade_id_minimum_length,
    test_product_type_matches_configuration,
    test_currency_sides_are_consistent,
)


# ============================================================================
# TEST SUITE: INJECT CANCELLED TRADE
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Build and inject the trade as Cancelled once for every test in this module."""
    return inject(session, status=CANCELLED_STATUS)
