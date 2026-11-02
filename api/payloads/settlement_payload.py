"""Request bodies for initiating (13 / 13b / 13c) and cancelling (13cs_a / 13cs_b) a settlement."""


def get_initiate_settlement_payload(settlement_id, trading_party):
    """Return the body that initiates settlement_id on behalf of trading_party."""
    return {
        'settlementId': settlement_id,
        'tradingParty': trading_party,
    }


def get_cancel_settlement_payload():
    """Return the body that cancels a settlement: empty, as the id is in the path."""
    return {}
