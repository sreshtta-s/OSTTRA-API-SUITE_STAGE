"""Request body for listing rule-center folders (disabled step 02)."""


def get_folder_list_payload():
    """Return a body for the first 50 folders, sorted by name."""
    return {
        'filters': [],
        'sortBy': [{'fieldName': 'name', 'order': 'ASC'}],
        'paging': {'offset': 0, 'limit': 50},
    }
