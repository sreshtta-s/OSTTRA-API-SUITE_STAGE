"""Request bodies for creating rule sets (unused) and listing them (step 03)."""


def get_rule_set_payload(party_id, counter_party_id, rule_set_name, folder_id, currency_pair,
                         asset1, asset2, netting_affirmation, netting_time, settlement_time):
    """Return a NETWORK rule set that nets and then settles PVP for one FX spot pair.

    Netting closes at netting_time and settlement runs at settlement_time, both on
    the value date ('VD'). Every party/asset combination uses venue OSTTRA and
    account type CENTRAL.
    """
    def paths(value):
        """Map each party/asset combination (2 parties x 2 assets) to the same value."""
        return {
            'paths': [
                {'when': [{'field': 'tradingParty', 'op': '=', 'value': party_id},
                          {'field': 'assetId', 'op': '=', 'value': asset1}], 'value': value},
                {'when': [{'field': 'tradingParty', 'op': '=', 'value': party_id},
                          {'field': 'assetId', 'op': '=', 'value': asset2}], 'value': value},
                {'when': [{'field': 'tradingParty', 'op': '=', 'value': counter_party_id},
                          {'field': 'assetId', 'op': '=', 'value': asset1}], 'value': value},
                {'when': [{'field': 'tradingParty', 'op': '=', 'value': counter_party_id},
                          {'field': 'assetId', 'op': '=', 'value': asset2}], 'value': value},
            ]
        }

    return {
        'partyId': party_id,
        'ruleSetType': 'NETWORK',
        'name': rule_set_name,
        'description': None,
        'priority': 10,
        'folderId': int(folder_id),
        'scopes': [
            {'scopeType': 'PRODUCT', 'scopeValue': 'FX'},
            {'scopeType': 'PRODUCT_SUBTYPE', 'scopeValue': 'SPOT'},
            {'scopeType': 'CURRENCY_PAIR', 'scopeValue': currency_pair},
        ],
        'rules': [
            {
                'ruleType': 'NETTING',
                'action': {
                    'settlementMode': 'NET',
                    'nettingAffirmation': netting_affirmation,
                    'nettingClose': {
                        'scheduleType': 'SCHEDULED',
                        'triggerDay': 'VD',
                        'triggerTime': netting_time,
                    },
                },
            },
            {
                'ruleType': 'SETTLEMENT',
                'action': {
                    'settlementMode': 'PVP',
                    'legs': [
                        {
                            'name': 'NA',
                            'venue': paths('OSTTRA'),
                            'accountType': paths('CENTRAL'),
                            'settlementTime': {
                                'scheduleType': 'SCHEDULED',
                                'triggerDay': 'VD',
                                'triggerTime': settlement_time,
                            },
                        }
                    ],
                },
            },
        ],
    }


def get_rule_set_list_payload():
    """Return an unfiltered body for the first 500 rule sets in a folder."""
    return {'filters': [], 'sortBy': [], 'paging': {'offset': 0, 'limit': 500}}
