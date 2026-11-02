"""Request body for listing rule-center folders (step 02)."""


def get_folder_list_payload():
    """Return an unfiltered body for the first 50 folders."""
    return {'filters': [], 'page_no': 0, 'page_size': 50}
