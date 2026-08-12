"""Phone normalisation and the SMS send-guard."""

from __future__ import annotations

import pytest

from app.phone import is_swedish_mobile, may_send_sms, normalize_e164, same_number

FOUNDER = "+46701234567"


@pytest.mark.parametrize(
    "raw",
    [
        "+46701234567",
        "+46 70 123 45 67",
        "0046701234567",
        "0046 70-123 45 67",
        "070-123 45 67",
        "0701234567",
        "(070) 123 45 67",
        "070/123.45.67",
        "  070 123 45 67  ",
    ],
)
def test_swedish_formats_all_normalise_to_the_same_number(raw: str) -> None:
    assert normalize_e164(raw) == FOUNDER


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "070-ABC 45 67", "12345", "+" + "9" * 20])
def test_junk_returns_none_rather_than_guessing(raw: str | None) -> None:
    assert normalize_e164(raw) is None


def test_same_number_compares_across_formats() -> None:
    assert same_number("070-123 45 67", "+46701234567")
    assert not same_number("070-123 45 67", "+46709999999")
    assert not same_number(None, FOUNDER)


def test_is_swedish_mobile() -> None:
    assert is_swedish_mobile("070-123 45 67")
    assert not is_swedish_mobile("+46812345678")  # Stockholm landline
    assert not is_swedish_mobile(None)


# --- the guard ------------------------------------------------------------------------------


def test_fake_provider_allows_everything_because_nothing_leaves() -> None:
    assert may_send_sms("+46709999999", sms_provider="fake", founder_mobile=FOUNDER)


def test_real_provider_allows_only_the_founder_mobile() -> None:
    assert may_send_sms(FOUNDER, sms_provider="46elks", founder_mobile=FOUNDER)
    assert may_send_sms("070-123 45 67", sms_provider="46elks", founder_mobile=FOUNDER)


def test_real_provider_blocks_every_fabricated_demo_number() -> None:
    """The whole point: seeded patients carry plausible +4670 numbers that very likely belong
    to real people, since Sweden has no reserved test range."""
    for fabricated in ("+46705550001", "+46701111111", "+46709876543"):
        assert not may_send_sms(fabricated, sms_provider="46elks", founder_mobile=FOUNDER)


def test_real_provider_blocks_when_no_founder_mobile_is_configured() -> None:
    assert not may_send_sms("+46701234567", sms_provider="46elks", founder_mobile=None)


def test_real_provider_blocks_missing_numbers() -> None:
    assert not may_send_sms(None, sms_provider="46elks", founder_mobile=FOUNDER)
    assert not may_send_sms("", sms_provider="46elks", founder_mobile=FOUNDER)
