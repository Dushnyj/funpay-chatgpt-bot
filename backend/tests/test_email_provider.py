import pytest

from app.integrations.email.provider import parse_verification_code


def test_parse_code_from_plain_text():
    text = "Your verification code is 123456. It expires in 10 minutes."
    assert parse_verification_code(text) == "123456"


def test_parse_code_from_html():
    text = "<html><body>Your code: <b>789012</b></body></html>"
    assert parse_verification_code(text) == "789012"


def test_parse_code_standalone():
    assert parse_verification_code("456789") == "456789"


def test_parse_code_none_when_no_match():
    assert parse_verification_code("No code here") is None


def test_parse_code_none_for_short_numbers():
    assert parse_verification_code("Order #123 confirmed") is None


def test_parse_code_prefers_explicit_code_context():
    text = "Reference 999333, code 111222, other 444555"
    assert parse_verification_code(text) == "111222"


def test_parse_code_rejects_ambiguous_numbers_without_context():
    assert parse_verification_code("Reference 999333, item 444555") is None


def test_parse_code_rejects_two_contextual_codes():
    assert parse_verification_code("Code 111222. Security code 333444.") is None


def test_parse_code_ignores_phone_numbers():
    # 10+ digits — не код подтверждения
    assert parse_verification_code("Call us at 1234567890") is None
