# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 07c: check the balance of the netting group steps 07 / 07b found, on both sides.

For a row that checks several trades as one netting group (TC010, TC011,
TC012, TC013, TC018), once each of its trades has run to step 05b. On each
domain it reads the group step 07 (07b on Domain B) left in state,
nettingGroupIdString / nettingGroupIdStringB: the group step 06 (06b) found,
once 07 has found the trade's legs there, or the other group 07 found them in.
The cancel steps that may run between 07b and this one do not change it. It
checks only that group's balance. It does not look for the row's trades in the group, or search other
groups for them: steps 07 / 07b found the group. Group listings carry no
amounts, so the balance is summed from the group's underlying legs, listed by
the group's id alone, and saved to state for steps 12 / 12b.

The balance is taken from each domain's own trading party's side: a leg it
receives (RECEIVABLE) adds, a leg it pays (PAYABLE) subtracts, and a CANCELLED
leg adds nothing. So Domain B's balance is the exact reverse of Domain A's. It
must equal the sum utils/sum_storer.py stored from the injected payloads as the
trades were booked, from that domain's party's side.

A row that cancels one of its trades once they are netted (TC034's, TC045's
and TC050's cancelTrade, after step 07b) has it taken out of its stored sum
before this runs, so the group must balance to the trades still live: the
cancelled trade's legs CANCELLED or gone. TC045 and TC050 cancel their only
trade and state groupAmounts of 0 in each currency, so the group must net to
exactly 0 on both sides. Until the netting service applies the cancel the group is off by
the cancelled trade, so it is read again until it balances or the wait runs out.
On each domain the cancelled trade itself must also have no live leg left in
the group (test_cancelled_trade_not_in_group), so a balance that happens to
hold with it still counted does not pass.

The whole group is held to the stored sum, so another run's or case's trade
with the same parties, pair, product and value date, which nets into the same
open group, fails the balance. A row whose nettingTotal states groupAmounts has
the group checked against those figures, as the test sheet gives them, instead:
TC013's count such trades in, and TC045's and TC050's are 0.

runner.py runs it once per such row, after steps 06 to 07b. It is kept out of
config/steps.py because the Excel column set is derived from that registry and
a changed column set starts a new sheet. For any other scenario every test
skips.
"""
import types
from collections import defaultdict
from decimal import Decimal

import pytest

from api.payloads.trade_payload import NETTING_TOTAL_GROUP_AMOUNTS, scenario_netting_group
from api.payloads.underlying_payload import get_underlying_list_payload
from api.services.base_service import is_ok
from api.services.underlying_service import UnderlyingService
from utils.api_logger import log_ids
from utils.describe import amounts_line, netted_line
from utils.flag import resolve_scenario_id
from utils.scenario_record import load_record, record_path
from utils.state_helper import StateHelper
from utils.sum_storer import load_sum, net_for, sum_line, sum_path, sum_problems
from utils.trade_direction import leg_amount, net_line
from utils.wait import GIVE_UP, wait_until

# Underlyings are the slowest records to appear after netting, so this allows
# the same 30 seconds, checked every 5, as step 07.
# Polled rather than slept; override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
TOTAL_TIMEOUT_SECONDS = 30
TOTAL_POLL_SECONDS = 5

# The status of a cancelled trade's leg in its netting group: the cashflow is
# marked cancelled and leaves the group's balance.
CANCELLED_LEG_STATUS = 'CANCELLED'

# Per domain: the state key of its token, which nettingTotal party is its own
# trading party ('own'), the state key of the group step 07 (07b on Domain B)
# left in state, and the state key this step saves the group's balance under,
# for steps 12 / 12b. Domain B sees the group from the counterparty's side.
DOMAINS = {
    'A': {'token': 'token',  'own': 'party',        'groupKey': 'nettingGroupIdString',
          'groupNetKey': 'nettingGroupNet'},
    'B': {'token': 'tokenB', 'own': 'counterParty', 'groupKey': 'nettingGroupIdStringB',
          'groupNetKey': 'nettingGroupNetB'},
}


def is_live(leg):
    """Return True unless the leg's cashflow has been cancelled out of its group's balance."""
    return leg.get('status') != CANCELLED_LEG_STATUS


def legs_of(legs, trade_ids):
    """Return the legs that belong to any of the trades, matched on foId as step 07 matches them."""
    return [leg for leg in legs
            if leg.get('foId') and any(tid in leg['foId'] for tid in trade_ids)]


def net_of(legs):
    """Sum netting legs per currency from their trading party's side, paid legs negative.

    Amounts are Decimal, not float, so ten legs of 6746.74 sum to exactly
    67467.40. A leg whose amount or direction cannot be read adds nothing;
    test_group_balance names it. A CANCELLED leg adds nothing either: its
    cashflow is out of the balance.
    """
    net = defaultdict(Decimal)
    for leg in legs:
        if is_live(leg):
            net[leg.get('assetId')] += leg_amount(leg) or Decimal(0)
    return dict(net)


def moving(net):
    """Return the currencies a net moves: those whose amount is not 0."""
    return {ccy: value for ccy, value in net.items() if value}


def unsigned(net):
    """Return the currencies a net moves, each without its sign, as the test sheet states them."""
    return {ccy: abs(value) for ccy, value in moving(net).items()}


# ============================================================================
# TEST SUITE: NETTING GROUP BALANCE (DOMAIN A AND B)
# ============================================================================
@pytest.fixture(scope='module', params=['A', 'B'])
def ctx(request, session):
    """Read the netting group steps 07 / 07b found and sum its legs, once per domain.

    Parametrized over Domain A and B, so every test runs for each side. Returns
    with applicable False when the scenario checks no set of trades as one
    netting group, and without calling the API when the token, group id or
    value dates are missing.

    c.source is the scenario whose recorded trades are checked: the row itself,
    or for a continuation the row it continues.
    """
    domain = request.param
    c = types.SimpleNamespace(
        domain=domain, applicable=False, source=None, expected=None, record=None,
        trade_ids=[], token_present=False, res_status=None, server_error=None,
        group_id=None, group_total=None, leg_count=0, unreadable_legs=[],
        subject=None, window=None, own=None, stored=None, expected_net=None,
        stated=None, sum_problems=[], cancelled_ids=[], cancelled_legs=[],
    )

    scenario_id = resolve_scenario_id()
    group = scenario_netting_group(scenario_id) if scenario_id else None
    if not group:
        return c

    c.applicable = True
    c.source = group['tradesOf']
    c.expected = group['nettingTotal']
    c.record = load_record(c.source)
    recorded = (c.record or {}).get('trades') or []
    # A trade the row cancelled once it was netted (cancelTrade) is out of the
    # stored sum, so the group must balance without it.
    live = [t for t in recorded if not t.get('cancelled')]
    c.trade_ids = [t['tradeId'] for t in live if t.get('tradeId')]
    c.cancelled_ids = [t['tradeId'] for t in recorded if t.get('cancelled') and t.get('tradeId')]

    sides = DOMAINS[domain]
    # Cleared until this run establishes it, so steps 12 / 12b never expect a
    # balance this check did not read.
    StateHelper.save_state({sides['groupNetKey']: None})
    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in
    # failure messages, which end up in the HTML report.
    c.token_present = state.get(sides['token']) is not None
    # The group step 07 (07b on Domain B) found the trades' legs in: the only
    # group this check reads.
    c.group_id = state.get(sides['groupKey'])
    # Names the recorded trades and the total they must reach, in every
    # assertion message.
    c.subject = netted_line(state)
    c.own = c.expected[sides['own']]
    # The balance the group must hold: the sum stored from the injected
    # payloads as the trades were booked, from this domain's own party's side.
    # With every trade cancelled (TC045, TC050) it is 0 in each currency.
    c.stored = load_sum(c.source)
    c.expected_net = net_for(c.stored, c.own)
    expected_count = c.expected['tradeCount'] - len(c.cancelled_ids)
    c.sum_problems = sum_problems(c.stored, c.source, c.trade_ids, expected_count,
                                  {c.expected['baseCurrency'], c.expected['referenceCurrency']})
    # Stated unsigned, as the sheet gives them, so the same figures hold on both
    # sides.
    stated = c.expected.get(NETTING_TOTAL_GROUP_AMOUNTS)
    c.stated = {ccy: Decimal(str(value)) for ccy, value in stated.items()} if stated else None

    # The window spans the value dates step 04 recorded for the trades, not today,
    # cancelled ones included: their legs stay in the group, CANCELLED.
    value_dates = sorted({t['valueDate'] for t in recorded if t.get('valueDate')})
    if not (c.token_present and c.group_id and value_dates):
        return c
    start, end = value_dates[0], value_dates[-1]
    c.window = f'{start} .. {end}'

    underlying = UnderlyingService(session, domain=domain, token=state[sides['token']])

    def balanced(net):
        """Return True once the group holds its expected balance, or at once when none can be expected."""
        if c.stated:
            return unsigned(net) == moving(c.stated)
        if c.sum_problems or c.expected_net is None:
            return True
        return moving(net) == moving(c.expected_net)

    # One read of the group's legs. Returns c once the group balances with no
    # cancelled trade's leg still live in it, GIVE_UP if the listing is
    # rejected, or None so wait_until tries again: a trade not netted yet, or a
    # cancel not applied yet, leaves the group off.
    def read():
        res = underlying.get_all_underlyings(get_underlying_list_payload(c.group_id, start, end))
        c.res_status = res.status_code
        if not is_ok(res):
            c.server_error = res.text
            return GIVE_UP

        # assetId is the currency, assetValue its amount.
        items = res.json().get('items') or []
        c.group_total = net_of(items)
        c.leg_count = sum(1 for item in items if is_live(item))
        c.unreadable_legs = sorted(
            str(item.get('foId')) for item in items if is_live(item) and leg_amount(item) is None)
        # The cancelled trades' legs still in the group, CANCELLED or not.
        c.cancelled_legs = legs_of(items, c.cancelled_ids)
        cancel_applied = not any(is_live(leg) for leg in c.cancelled_legs)
        return c if balanced(c.group_total) and cancel_applied else None

    what = f'Domain {domain} netting group {c.group_id} to balance to the {c.source} sum'
    if c.cancelled_ids:
        what += f' with cancelled trade(s) {", ".join(c.cancelled_ids)} out of it'
    wait_until(read, what, timeout=TOTAL_TIMEOUT_SECONDS, interval=TOTAL_POLL_SECONDS)

    # On timeout c keeps the last read's balance, so the test shows how far off it is.
    print(f'INFO: {c.source} stored sum: {sum_line(c.stored)}.')
    if c.group_total is None:
        return c
    print(f'INFO: Domain {domain} group {c.group_id}: {c.leg_count} live leg(s), balance '
          f'{net_line(c.own, c.group_total)}.')
    if c.cancelled_ids:
        print(f'INFO: Cancelled trade(s) {", ".join(c.cancelled_ids)} in Domain {domain} group '
              f'{c.group_id}: '
              + (', '.join(f'{leg.get("foId")} ({leg.get("assetId")} {leg.get("status")})'
                           for leg in c.cancelled_legs) or 'no legs left') + '.')

    # The Excel row reads these back, and the trade fields name the totalled
    # trades rather than the one trade state holds. With runUpTo set, steps 08
    # onward close and settle this group.
    StateHelper.save_state({
        'reqPartyId': c.expected['party'],
        'counterPartyId': c.expected['counterParty'],
        'tradeCurrencyPair': c.expected['tradeCurrencyPair'],
        'productType': c.expected['productType'],
        'valueDate': start,
        'tradeDate': (live or recorded)[0].get('tradeDate') or start,
    })
    log_ids(f'NETTING TOTAL ({domain})', nettingGroupId=c.group_id,
            trades=len(c.trade_ids), totals=net_line(c.own, c.group_total))

    # The group settles everything in it, so steps 12 / 12b check its
    # settlement candidates against its balance. Saved only when every leg
    # could be read, so a partial balance is never expected.
    if not c.unreadable_legs:
        StateHelper.save_state({
            sides['groupNetKey']: {str(ccy): str(value) for ccy, value in c.group_total.items()},
        })

    return c


def skip_unless_applicable(ctx):
    """Skip the test when the selected scenario checks no set of trades as one netting group."""
    if not ctx.applicable:
        pytest.skip('The scenario checks no set of trades as one netting group - nothing to total.')


# --- PRE-FLIGHT ---
def test_continues_a_recorded_run(ctx):
    """Pre-flight: the scenario it continues has a recorded run"""
    skip_unless_applicable(ctx)
    assert ctx.record is not None, \
        (f'No recorded run of {ctx.source} at {record_path(ctx.source)}. Run {ctx.source} '
         f'first: this row checks the trades it books.\n{ctx.subject}')


def test_auth_token_present(ctx):
    """Pre-flight: auth token for this domain is present in state"""
    skip_unless_applicable(ctx)
    assert ctx.token_present, \
        (f'Domain {ctx.domain} token is missing: .state/default.json has no '
         f'"{DOMAINS[ctx.domain]["token"]}". Did step 01 run?\n{ctx.subject}')


def test_netting_group_present(ctx):
    """Pre-flight: steps 07 / 07b saved the netting group to check"""
    skip_unless_applicable(ctx)
    step = '07' if ctx.domain == 'A' else '07b'
    assert ctx.group_id, \
        (f'No Domain {ctx.domain} netting group to check: .state/default.json has no '
         f'"{DOMAINS[ctx.domain]["groupKey"]}". Did step {step} find the group?\n{ctx.subject}')


# --- BALANCE ---
def test_endpoint_returns_2xx(ctx):
    """API: the netting group's underlying leg listing returns a 2xx success status"""
    skip_unless_applicable(ctx)
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')
    assert 200 <= ctx.res_status < 300, \
        (f'Domain {ctx.domain} underlying leg listing of netting group {ctx.group_id} (value '
         f'dates [{ctx.window}]) should return a 2xx status, but returned {ctx.res_status}. '
         f'Reason: {ctx.server_error or "none given"}\n{ctx.subject}')


def test_group_balance(ctx):
    """Balance: the netting group nets to the expected balance, on this domain's party's side"""
    skip_unless_applicable(ctx)
    if ctx.group_total is None:
        pytest.skip('The netting group was not read; see the pre-flight checks.')

    assert not ctx.unreadable_legs, \
        (f'Domain {ctx.domain} netting group {ctx.group_id} has legs whose amount or direction '
         f'cannot be read, so its balance is unknown: {", ".join(ctx.unreadable_legs)}.'
         f'\n{ctx.subject}')

    if ctx.stated:
        # Stated unsigned, as the sheet gives them, so the same figures hold on
        # both sides. Compared on the currencies that move, so a stated 0
        # (TC045, TC050) needs the group to net to exactly 0 there.
        got, expected = unsigned(ctx.group_total), moving(ctx.stated)
        should = (f'the amounts {ctx.source} states for the whole group, other trades in it '
                  f'included: {amounts_line(ctx.stated)}')
        hint = ''
    else:
        assert not ctx.sum_problems and ctx.expected_net is not None, \
            (f'The {ctx.source} sum stored at {sum_path(ctx.source)}, the balance the group must '
             f'hold, cannot be relied on: {"; ".join(ctx.sum_problems) or "it is missing"}.'
             f'\n{ctx.subject}')
        got, expected = moving(ctx.group_total), moving(ctx.expected_net)
        should = f'the stored {ctx.source} sum: {net_line(ctx.own, ctx.expected_net)} ({sum_line(ctx.stored)})'
        hint = (' Another run\'s or case\'s trade with the same parties, pair, product and value '
                'date would share the group and count here.')
    if ctx.cancelled_ids:
        hint += (f' The cancelled trade(s) {", ".join(ctx.cancelled_ids)} still count until their '
                 f'legs are {CANCELLED_LEG_STATUS} or gone.')

    print(f'INFO: Domain {ctx.domain} group {ctx.group_id}: {net_line(ctx.own, ctx.group_total)}; '
          f'expected {should}.')
    assert got == expected, \
        (f'Domain {ctx.domain} netting group {ctx.group_id} balance: '
         f'{net_line(ctx.own, ctx.group_total)}, but it should be {should}.{hint}\n{ctx.subject}')


# --- CANCELLED TRADE ---
def test_cancelled_trade_not_in_group(ctx):
    """Cancel: a trade step 04 cancel cancelled has no live leg left in the netting group, on this domain"""
    skip_unless_applicable(ctx)
    if not ctx.cancelled_ids:
        pytest.skip('Step 04 cancel cancelled none of the row\'s trades, so none must be out of the group.')
    if ctx.group_total is None:
        pytest.skip('The netting group was not read; see the pre-flight checks.')

    # CANCELLED is how the netting service takes a leg's cashflow out of the
    # group, so a CANCELLED leg, or none at all, means the trade left it.
    live = [f'{leg.get("foId")} ({leg.get("assetId")} {leg.get("status")})'
            for leg in ctx.cancelled_legs if is_live(leg)]
    assert not live, \
        (f'Trade(s) {", ".join(ctx.cancelled_ids)} were cancelled by step 04 cancel, so their '
         f'legs should be {CANCELLED_LEG_STATUS} or gone from Domain {ctx.domain} netting group '
         f'{ctx.group_id}, but these were still live when the wait ended: {", ".join(live)}.'
         f'\n{ctx.subject}')
