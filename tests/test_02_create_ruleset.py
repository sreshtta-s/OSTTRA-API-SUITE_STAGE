"""Step 02: create a netting ruleset for the first scenario. Disabled.

Disabled in config/steps.py (enabled: False) because rule creation is out of
scope: the rules under test are provisioned in the environment and referenced by
id from NETTING_RULES in data/trade_scenarios.py. The code stays commented out,
matching tests/02-create-ruleset.spec.js in the Playwright suite, so the step
can be revived without being rewritten.
"""

# # ============================================================================
# # IMPORTS & DEPENDENCIES
# # ============================================================================
# from api.payloads.folder_payload import get_folder_list_payload
# from api.payloads.ruleset_payload import get_rule_set_payload
# from api.services.base_service import is_ok
# from api.services.folder_service import FolderService
# from api.services.ruleset_service import RuleSetService
# from data.trade_scenarios import SCENARIOS
# from utils.helpers import TestUtils
# from utils.state_helper import StateHelper
#
#
# # ============================================================================
# # TEST SUITE: CREATE RULESET (DATA-DRIVEN)
# # ============================================================================
# # The rule is built from the first scenario, so the logic below holds no
# # hard-coded parties or currencies.
# data = SCENARIOS[0]
# REQ_NETTING_AFFIRMATION = 'AUTO'   # Fixed by the backend, not by the scenario.
#
#
# def test_folder_is_located_and_ruleset_is_injected(session):
#     """Verify folder is located and ruleset is injected"""
#
#     timestamps = TestUtils.generate_timestamps()
#     rule_set_name = f'API Automation-{timestamps["currentTimestamp"]}'
#
#     # ------------------------------------------------------------------
#     # STEP 1: LOAD STATE & VERIFY AUTH
#     # ------------------------------------------------------------------
#     state = StateHelper.get_state()
#     token = state.get('token')
#     assert token is not None, 'Auth token is missing. Did test_01_auth.py run?'
#
#     # ------------------------------------------------------------------
#     # STEP 2: FOLDER LOOKUP
#     # ------------------------------------------------------------------
#     folder_service = FolderService(session, token)
#     folder_res = folder_service.get_folders(get_folder_list_payload())
#     assert is_ok(folder_res), f'Folder lookup failed with status {folder_res.status_code}'
#
#     # ------------------------------------------------------------------
#     # STEP 3: PARTICIPANT MATCHING
#     # ------------------------------------------------------------------
#     folder_data = folder_res.json()
#
#     # The bilateral folder is the one that lists both the party and the
#     # counterparty as participants.
#     participants = [data['party'], data['counterParty']]
#     matches = [
#         folder for folder in (folder_data.get('items') or [])
#         if all(pid in (folder.get('participants') or []) for pid in participants)
#     ]
#     assert matches, f'No bilateral folder found for [{data["party"]}, {data["counterParty"]}]'
#
#     folder_id = matches[0]['id']
#     print(f'Found Folder ID: {folder_id} ({matches[0]["name"]})')
#
#     # ------------------------------------------------------------------
#     # STEP 4: RULESET CREATION & STATE PERSISTENCE
#     # ------------------------------------------------------------------
#     rule_set_service = RuleSetService(session, token)
#     rule_folder_id = folder_id or 0   # Folder 0 for negative scenarios.
#
#     rule_payload = get_rule_set_payload(
#         data['party'],
#         data['counterParty'],
#         rule_set_name,
#         rule_folder_id,
#         data['ruleCurrencyPair'],
#         data['asset1'],
#         data['asset2'],
#         REQ_NETTING_AFFIRMATION,
#         timestamps['nettingTriggerTime'],
#         timestamps['settlementTriggerTime'],
#     )
#
#     res = rule_set_service.create_rule(rule_payload)
#     assert res.status_code == data['expectedRuleStatus'], 'Rule Creation Status mismatch'
#
#     response_data = res.json()
#
#     # Saved so step 03 can find this rule and step 04 injects a trade with
#     # the same parties and currencies.
#     StateHelper.save_state({
#         'folderId_a': folder_id,
#         'ruleSetId_a': response_data['id'],
#         'ruleId': response_data['ruleSetLogicalId'],
#         'ruleSetName': rule_set_name,
#         'reqPartyId': data['party'],
#         'counterPartyId': data['counterParty'],
#         'tradeCurrencyPair': data['tradeCurrencyPair'],
#         'dealtCurrency': data['dealtCurrency'],
#         'referenceCurrency': data['referenceCurrency'],
#     })
#
#     print(f'RuleSet [{rule_set_name}] successfully created with ID: '
#           f'{response_data["ruleSetLogicalId"]}')
