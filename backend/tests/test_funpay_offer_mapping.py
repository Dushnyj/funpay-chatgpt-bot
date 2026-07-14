import pytest

from app.integrations.funpay.types import (
    OfferSubscriptionOption,
    OfferSubscriptionType,
)
from app.models.catalog import SubscriptionTier
from app.services.funpay_offer_mapping import (
    UnsupportedFunPayOfferTierError,
    funpay_offer_plan_fields,
)


@pytest.mark.parametrize(
    ("code", "subscription", "subscription_type"),
    [
        ("free", OfferSubscriptionOption.WITHOUT_SUBSCRIPTION, None),
        (
            "go",
            OfferSubscriptionOption.WITH_SUBSCRIPTION,
            OfferSubscriptionType.GO,
        ),
        (
            "plus",
            OfferSubscriptionOption.WITH_SUBSCRIPTION,
            OfferSubscriptionType.PLUS,
        ),
        (
            "pro_5x",
            OfferSubscriptionOption.WITH_SUBSCRIPTION,
            OfferSubscriptionType.PRO,
        ),
        (
            "pro_20x",
            OfferSubscriptionOption.WITH_SUBSCRIPTION,
            OfferSubscriptionType.PRO,
        ),
        (
            "business",
            OfferSubscriptionOption.WITH_SUBSCRIPTION,
            OfferSubscriptionType.BUSINESS,
        ),
    ],
)
def test_local_tier_maps_to_exact_funpay_form_values(
    code: str,
    subscription: OfferSubscriptionOption,
    subscription_type: OfferSubscriptionType | None,
):
    tier = SubscriptionTier(code=code, name=code, is_sellable=True)

    result = funpay_offer_plan_fields(tier)

    assert result.subscription is subscription
    assert result.subscription_type is subscription_type


@pytest.mark.parametrize("code", [None, "pro", "enterprise", "edu", "custom"])
def test_unknown_or_unrepresentable_tier_fails_closed(code: str | None):
    tier = SubscriptionTier(code=code, name=code or "legacy", is_sellable=True)

    with pytest.raises(
        UnsupportedFunPayOfferTierError,
        match="cannot be represented",
    ):
        funpay_offer_plan_fields(tier)
