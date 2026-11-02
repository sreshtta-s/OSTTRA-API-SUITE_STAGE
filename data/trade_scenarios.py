"""Trade scenarios and the netting rule each in-scope trade is expected to use.

SCENARIOS holds the party pair, currency pair and expected statuses for the
ruleset steps (disabled) and step 04, which checks its injection status against
SCENARIOS[0]. NETTING_RULES and FALLBACK_RULE_ID are read by
utils/state_helper.resolve_rule_id for steps 06 to 07b, and each rule's mode by
utils/state_helper.resolve_affirmation_mode for runner.py.
"""

SCENARIOS = [
    # ==========================================
    # BNKA-NY INITIATED HAPPY PATHS
    # ==========================================
    # The rule's pair is CNH-USD, in alphabetical order, so the trade is CNHUSD.
    {
        'testName': 'Happy Path: BNKA-NY to BNKB-NY (CNH-USD)',
        'party': 'BNKA-NY', 'counterParty': 'BNKB-NY',
        'ruleCurrencyPair': 'CNH-USD', 'tradeCurrencyPair': 'CNHUSD',
        'asset1': 'CNH', 'asset2': 'USD', 'dealtCurrency': 'CNH', 'referenceCurrency': 'USD',
        'isNegativeCase': False, 'expectedRuleStatus': 202, 'expectedTradeStatus': 200,
    },
]

# ==========================================
# NETTING RULES
# ==========================================
# The rule each trade expected to run through step 15 nets under, one entry per
# party, counterparty, currency pair and product. Steps 06 to 07b find the entry
# from the trade held in state after step 04 (reqPartyId, counterPartyId,
# tradeCurrencyPair, productType), so the same product on two party pairs gets
# two separate rules.
#
# Use the trade payload's own spelling: party ids as in partyDetail, the pair
# unseparated in alphabetical order (CNHUSD, as the rule's CNH-USD), and
# productType as FxSpot or FxForward. Matching ignores case and order, so
# either party can be the trade's party.
#
# A trade with no entry here, or whose entry has a blank ruleId (a rule still to
# be onboarded), gets FALLBACK_RULE_ID instead, so it can only fail to find a
# netting group, never net under a real rule by accident. A warning names the
# trade.
#
# 'mode' is how the rule's netting groups are affirmed once both sides close
# them, 'auto' or 'manual'. An auto rule confirms them by itself, so runner.py
# skips steps 10, 11 and 11b and goes from step 09b to step 12. A manual rule
# leaves them PENDING_AFFIRMATION until steps 10, 11 and 11b match their
# proposals, so every step runs. A trade with no entry, or an entry without a
# valid mode, runs as FALLBACK_MODE.
#
# The comment on each entry names the injection modes and scenarios that inject
# it.
#
# Steps 02 and 03 (tests/test_02_create_ruleset.py, tests/test_03_validate_ruleset.py)
# rewrite each entry's ruleId and mode from the Domain A rule center, and add an
# entry for a rule set with none. Run them after rules are recreated there.
NETTING_RULES = [
    # DYNAMIC, TC029, TC030, TC034, TC036 (and TC031, which stops at 13c, TC045, which stops at 07c, TC050, which stops at 13b, and TC008, which stops at 05b)
    {
        'party': 'BNKA-NY', 'counterParty': 'BNKB-NY',
        'tradeCurrencyPair': 'CNHUSD', 'productType': 'FxForward',
        'ruleId': 'bilateral-bnka-ny-bnkb-ny-0e2d3a12f066',
        'mode': 'manual',
    },
    # TC038 (and TC035, which stops at 08b, TC037, which stops at 14, and TC032, which books the pair but stops at 05b)
    {
        'party': 'BNKA-NY', 'counterParty': 'BNKB-NY',
        'tradeCurrencyPair': 'CNHUSD', 'productType': 'FxSpot',
        'ruleId': 'bilateral-bnka-ny-bnkb-ny-09cd179635be',
        'mode': 'auto',
    },
    # No scenario books this pair at the moment.
    {
        'party': 'BNKA-NY', 'counterParty': 'BNKB-HK',
        'tradeCurrencyPair': 'CNHUSD', 'productType': 'FxSpot',
        'ruleId': 'bilateral-bnka-ny-bnkb-hk-533f7fdb827e',
        'mode': 'auto',
    },
    # TC026
    {
        'party': 'BNKA-NY', 'counterParty': 'BNKB-HK',
        'tradeCurrencyPair': 'CNHUSD', 'productType': 'FxForward',
        'ruleId': 'bilateral-bnka-ny-bnkb-hk-c164eedd82ed',
        'mode': 'auto',
    },
    # TC001, TC010, TC011, TC018, TC020 (and TC016, which stops at 13c)
    {
        'party': 'BNKA-HK', 'counterParty': 'BNKB-HK',
        'tradeCurrencyPair': 'CNHUSD', 'productType': 'FxSpot',
        'ruleId': 'bilateral-bnka-hk-bnkb-hk-e0aaf7d3ab86',
        'mode': 'manual',
    },
    # TC002, TC004
    {
        'party': 'BNKA-HK', 'counterParty': 'BNKB-HK',
        'tradeCurrencyPair': 'CNHUSD', 'productType': 'FxForward',
        'ruleId': 'bilateral-bnka-hk-bnkb-hk-74dc8035615b',
        'mode': 'manual',
    },
    # TC009, TC012
    {
        'party': 'BNKA-HK', 'counterParty': 'BNKB-NY',
        'tradeCurrencyPair': 'CNHUSD', 'productType': 'FxSpot',
        'ruleId': 'bilateral-bnka-hk-bnkb-ny-5178d549e1fe',
        'mode': 'manual',
    },
    # TC005, TC013
    {
        'party': 'BNKA-HK', 'counterParty': 'BNKB-NY',
        'tradeCurrencyPair': 'CNHUSD', 'productType': 'FxForward',
        'ruleId': 'bilateral-bnka-hk-bnkb-ny-b71bfcec9556',
        'mode': 'manual',
    },
]

# The rule id for every trade missing from NETTING_RULES. Invalid on purpose: no
# netting group carries it, so the netting steps find no group and fail.
FALLBACK_RULE_ID = 'invalid-fallback-rule-4b8616c20412'

# The values a rule's mode can take, and the one a trade without a valid mode
# runs as: auto, which skips steps 10, 11 and 11b. Only an entry that says
# manual runs them; this holds for every scenario.
AFFIRMATION_MODES = ('auto', 'manual')
FALLBACK_MODE = 'auto'
