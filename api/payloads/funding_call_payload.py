"""Request body for the simulator's MT910 funding-call pump (step 14)."""


def get_funding_call_payload(message_ids, value_date):
    """Return the pump body for the given simulator message ids.

    :param value_date: the injected trade's value date, recorded by step 04.
    """
    return {
        'message_ids': message_ids,
        'value_date': value_date,
    }
