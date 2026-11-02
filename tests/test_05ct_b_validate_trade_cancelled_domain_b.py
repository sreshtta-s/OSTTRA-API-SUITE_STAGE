# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 05ct_b: check the Domain B ledger shows step 04's trade as cancelled.

The counterparty's view of the cancel step 05ct_a checks on Domain A. The
checks are imported from test_05ct_a_validate_trade_cancelled and run here
against this module's ctx, so the two steps cannot drift apart. Writes
nothing back to state.

runner.py runs it 20s after step 05ct_a (CANCEL_STEPS), only for a trade
run_pipeline() is told to cancel.
"""
import pytest

# Imported test functions are collected here too, each resolving ctx below.
# Listed in step 05ct_a's order, which is the order they run and are reported in.
from test_05ct_a_validate_trade_cancelled import (  # noqa: F401
    find_cancelled_trade,
    test_auth_token_present,
    test_injected_trade_id_present,
    test_request_returns_2xx,
    test_trade_found_on_ledger,
    test_trade_appears_exactly_once,
    test_trade_status_is_cancelled,
)


# ============================================================================
# TEST SUITE: VALIDATE TRADE CANCELLED ON LEDGER (DOMAIN B)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Wait for the trade to show as cancelled on the Domain B ledger, once for every test in this module."""
    return find_cancelled_trade(session, 'B')
