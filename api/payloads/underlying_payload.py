"""Request body for listing a netting group's underlying legs (steps 07 / 07b, 07c)."""


def get_underlying_list_payload(netting_group_id, start_date, end_date, offset=0):
    """Return a body for one group's legs with value dates in [start_date, end_date].

    :param offset: the number of legs to skip, for reading the next page; the
        response's totalItems says how many there are in all.
    """
    return {
        'filters': [
            {
                'fieldName': 'underlyingDetails.nettingGroup.nettingGroupId',
                'value': [netting_group_id],
                'operator': 'EQUAL',
            },
            {
                'fieldName': 'valueDate',
                'value': [start_date, end_date],
                'operator': 'BETWEEN',
            },
        ],
        'paging': {
            # Steps 07 / 07b read every page; 07c reads page 0 only, so kept high
            # to avoid missing legs.
            'limit': 500,
            'offset': offset,
        },
        'sortBy': [],
    }
