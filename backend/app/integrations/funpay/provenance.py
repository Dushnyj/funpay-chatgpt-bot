"""Canonical parsing of immutable bot ownership markers in FunPay text."""

from __future__ import annotations

import re
from collections.abc import Iterable


PROVENANCE_MARKER_RE = re.compile(r"\[FPBOT:([0-9a-f]{32})\]")


def exact_provenance_token(
    descriptions: Iterable[str | None],
) -> str | None:
    """Return one unambiguous token repeated at most once per description.

    The same marker is normally present in both localized descriptions.  Any
    second marker in one locale or any disagreement between locales fails
    closed, because such text cannot prove ownership of one exact local lot.
    """

    markers_by_description = [
        PROVENANCE_MARKER_RE.findall(description or "")
        for description in descriptions
    ]
    if any(len(markers) > 1 for markers in markers_by_description):
        return None
    markers = [
        marker
        for description_markers in markers_by_description
        for marker in description_markers
    ]
    if not markers or len(set(markers)) != 1:
        return None
    return markers[0]


def descriptions_have_exact_provenance(
    descriptions: Iterable[str | None],
    token: str,
) -> bool:
    return exact_provenance_token(descriptions) == token
