# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 15cs_b: check the Domain B settlement ledger shows the settlement as cancelled.

The counterparty's view of the check step 15cs_a makes on Domain A, for
settlementIdB. The check is imported from test_15cs_a_validate_settlement_cancelled
and runs here against this module's ctx, so the two steps cannot drift apart.
Writes nothing back to state.

runner.py runs it after step 15cs_a, only for a scenario that sets
"cancelSettlement": true.
"""
import pytest

# The imported test function is collected here too, resolving ctx below.
from test_15cs_a_validate_settlement_cancelled import (  # noqa: F401
    find_cancelled_settlement,
    test_settlement_status_is_cancelled,
)


# ============================================================================
# TEST SUITE: VALIDATE SETTLEMENT CANCELLED (DOMAIN B)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Wait for the settlement to show as cancelled on Domain B, once for every test in this module."""
    return find_cancelled_settlement(session, 'B')
