"""Canonical parsing of immutable bot ownership markers in FunPay text."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable


_HEX_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_LEGACY_MARKER_RE = re.compile(r"\[FPBOT:([0-9a-f]{32})\]")
_PUBLIC_CODE_PATTERN = (
    r"(?<![A-Za-z0-9])FPR-([A-Z2-7]{26})(?![A-Za-z0-9])"
)
_PUBLIC_CODE_RE = re.compile(_PUBLIC_CODE_PATTERN)
_PUBLIC_CODE_NORMALIZER_PATTERN = (
    r"(?<![A-Za-z0-9])FPR-[A-Z2-7]{26}(?![A-Za-z0-9])"
)
_READABLE_MARKER_PATTERN = (
    r"(?:🔖[ \t]*)?"
    r"(?:Код автодоставки|Automatic delivery code):[ \t]*"
    rf"{_PUBLIC_CODE_NORMALIZER_PATTERN}"
)

# Matches the whole buyer-visible marker so a legacy or already-rendered
# description can be normalized before the authoritative marker is appended.
# The standalone public-code alternative also removes a code whose label was
# edited by an operator, while exact token extraction below remains strict.
PROVENANCE_MARKER_RE = re.compile(
    rf"(?:\[FPBOT:[0-9a-f]{{32}}\]|{_READABLE_MARKER_PATTERN}|"
    rf"{_PUBLIC_CODE_NORMALIZER_PATTERN})"
)


def public_provenance_code(token: str) -> str:
    """Encode the complete 128-bit lot token as reversible RFC 4648 Base32."""

    if not _HEX_TOKEN_RE.fullmatch(token):
        raise ValueError("Lot provenance token must be 32 lowercase hex characters")
    encoded = base64.b32encode(bytes.fromhex(token)).decode("ascii").rstrip("=")
    return f"FPR-{encoded}"


def _decode_public_code(encoded: str) -> str | None:
    padded = encoded + "=" * (-len(encoded) % 8)
    try:
        raw = base64.b32decode(padded, casefold=False)
    except (ValueError, binascii.Error):
        return None
    if len(raw) != 16:
        return None
    token = raw.hex()
    # Reject non-canonical encodings with non-zero unused padding bits.
    if public_provenance_code(token) != f"FPR-{encoded}":
        return None
    return token


def _description_tokens(description: str | None) -> list[str]:
    value = description or ""
    tokens = [match.group(1) for match in _LEGACY_MARKER_RE.finditer(value)]
    for match in _PUBLIC_CODE_RE.finditer(value):
        token = _decode_public_code(match.group(1))
        if token is not None:
            tokens.append(token)
    return tokens


def exact_provenance_token(
    descriptions: Iterable[str | None],
) -> str | None:
    """Return one unambiguous token repeated at most once per description.

    The same marker is normally present in both localized descriptions.  Any
    second marker in one locale or any disagreement between locales fails
    closed, because such text cannot prove ownership of one exact local lot.
    """

    markers_by_description = [
        _description_tokens(description) for description in descriptions
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
