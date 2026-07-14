"""Strict mapping between the local ChatGPT catalog and FunPay offer fields.

FunPay's ChatGPT account form uses localized values rather than stable numeric
identifiers.  Keeping the mapping in one fail-closed module prevents a newly
discovered OpenAI plan from being advertised as the wrong FunPay product.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.integrations.funpay.types import (
    OfferSubscriptionOption,
    OfferSubscriptionType,
)
from app.models.catalog import SubscriptionTier


@dataclass(frozen=True, slots=True)
class FunPayOfferPlanFields:
    subscription: OfferSubscriptionOption
    subscription_type: OfferSubscriptionType | None


FUNPAY_PLAN_FIELDS_BY_TIER_CODE: Mapping[str, FunPayOfferPlanFields] = MappingProxyType({
    "free": FunPayOfferPlanFields(
        OfferSubscriptionOption.WITHOUT_SUBSCRIPTION,
        None,
    ),
    "go": FunPayOfferPlanFields(
        OfferSubscriptionOption.WITH_SUBSCRIPTION,
        OfferSubscriptionType.GO,
    ),
    "plus": FunPayOfferPlanFields(
        OfferSubscriptionOption.WITH_SUBSCRIPTION,
        OfferSubscriptionType.PLUS,
    ),
    # Both locally measured Pro allowance profiles are the same product type
    # in FunPay's four-option form.  Their exact 5x/20x distinction remains in
    # our title, description, provenance marker, and allocation criteria.
    "pro_5x": FunPayOfferPlanFields(
        OfferSubscriptionOption.WITH_SUBSCRIPTION,
        OfferSubscriptionType.PRO,
    ),
    "pro_20x": FunPayOfferPlanFields(
        OfferSubscriptionOption.WITH_SUBSCRIPTION,
        OfferSubscriptionType.PRO,
    ),
    "business": FunPayOfferPlanFields(
        OfferSubscriptionOption.WITH_SUBSCRIPTION,
        OfferSubscriptionType.BUSINESS,
    ),
})

SUPPORTED_FUNPAY_TIER_CODES = frozenset(FUNPAY_PLAN_FIELDS_BY_TIER_CODE)


class UnsupportedFunPayOfferTierError(ValueError):
    """The local tier cannot be represented truthfully in FunPay's form."""


def funpay_offer_plan_fields(
    tier: SubscriptionTier,
) -> FunPayOfferPlanFields:
    fields = FUNPAY_PLAN_FIELDS_BY_TIER_CODE.get(tier.code or "")
    if fields is None:
        raise UnsupportedFunPayOfferTierError(
            "Subscription tier cannot be represented by FunPay's ChatGPT "
            f"offer form: code={tier.code!r}, id={tier.id!r}"
        )
    return fields


def is_funpay_offer_tier_supported(tier: SubscriptionTier) -> bool:
    return (tier.code or "") in SUPPORTED_FUNPAY_TIER_CODES
