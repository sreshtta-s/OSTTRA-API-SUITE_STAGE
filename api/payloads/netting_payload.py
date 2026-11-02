"""Request bodies to list, read and close netting groups and list their
settlement candidates (steps 06 to 09, 07c)."""

# One page size for every netting group listing, on both domains. Steps 06, 06b
# and 07c read page 0 only, so a smaller limit could truncate a busy day and hide
# the trade's group; steps 07 / 07b read every page.
LIST_LIMIT = 5000


def get_netting_group_list_payload(start_date, end_date, limit=LIST_LIMIT):
    """Return a body for netting groups with value dates in [start_date, end_date]."""
    return {
        'filters': [
            {'fieldName': 'valueDate', 'value': [start_date, end_date], 'operator': 'BETWEEN'}
        ],
        'paging': {'limit': limit, 'offset': 0},
        'sortBy': [],
    }


def get_party_netting_group_list_payload(value_date, trading_party, counter_party,
                                         rule_id=None, limit=LIST_LIMIT, offset=0):
    """Return a body for one party pair's netting groups on one value date.

    The filter the netting screen sends (steps 07 / 07b). tradingParty is the
    domain's own side: the trade's party on Domain A, its counterparty on
    Domain B. With rule_id, only the groups under that netting rule; the server
    matches every value whole (BNKA-HK, not bnkahk), ignoring case.

    :param offset: the number of groups to skip, for reading the next page; the
        response's totalItems says how many there are in all.
    """
    filters = [{'fieldName': 'valueDate', 'value': [value_date, value_date], 'operator': 'BETWEEN'}]
    if rule_id:
        filters.append({'fieldName': 'configSnapshot.nettingRuleId', 'value': [rule_id],
                        'operator': 'EQUAL'})
    filters += [
        {'fieldName': 'counterParty', 'value': [counter_party], 'operator': 'EQUAL'},
        {'fieldName': 'tradingParty', 'value': [trading_party], 'operator': 'EQUAL'},
    ]
    return {
        'filters': filters,
        'sortBy': [],
        'paging': {'limit': limit, 'offset': offset},
    }


def get_netting_group_details_payload(netting_group_id, target_date):
    """Return a body selecting one netting group on one value date."""
    return {
        'filters': [
            {
                'fieldName': 'nettingGroupId',
                'operator': 'EQUAL',
                'value': [netting_group_id],
            },
            {
                'fieldName': 'valueDate',
                'operator': 'BETWEEN',
                # Same date for start and end, matching the request the UI sends.
                'value': [target_date, target_date],
            },
        ],
        'paging': {'offset': 0, 'limit': 10},
        'sortBy': [],
    }


def get_settlement_candidate_payload(netting_group_id):
    """Return a body for one netting group's settlement candidates.

    Filtered by nettingGroupId only, matching the request the netting screen
    sends. A group sits on a single value date, so no date filter is needed.
    """
    return {
        'filters': [
            {
                'fieldName': 'nettingGroupId',
                'operator': 'EQUAL',
                'value': [netting_group_id],
            },
        ],
        'paging': {'offset': 0, 'limit': 50},
        'sortBy': [],
    }


def get_close_netting_group_payload(netting_group_id, value_date, time_of_update):
    """Return the body that closes a netting group.

    :param value_date: the injected trade's value date, recorded by step 04.
    :param time_of_update: close timestamp as 'YYYY-MM-DD HH:MM:SS'.
    """
    return {
        'nettingGroupId': netting_group_id,
        'valueDate': value_date,
        'timeOfUpdate': time_of_update,
    }
