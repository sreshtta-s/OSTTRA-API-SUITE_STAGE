"""Request body for listing one settlement on its value date (steps 15 / 15b / 15cs_a / 15cs_b)."""


def get_ledger_payload(value_date, settlement_id):
    """Return a body for settlement_id's entries on value_date (first 50).

    :param value_date: the injected trade's value date, recorded by step 04.
    :param settlement_id: the settlement's id on the domain being queried,
        settlementId (step 09) for Domain A or settlementIdB (step 09b) for
        Domain B; the ledger calls it settlementInstanceId.
    """
    return {
        'filters': [
            {
                'fieldName': 'valueDate',
                'value': [value_date, value_date],
                'operator': 'BETWEEN',
                'inputMode': 'default',
            },
            {
                'fieldName': 'settlementInstanceId',
                'value': [settlement_id],
                'operator': 'EQUAL',
            },
        ],
        'sortBy': [],
        'paging': {
            'offset': 0,
            'limit': 50,
        },
    }
