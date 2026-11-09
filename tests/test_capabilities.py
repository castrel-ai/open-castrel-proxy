"""Capability list, locale detection and error code parity with the platform."""

from castrel_proxy.protocol.capabilities import (
    DEFAULT_LOCALE,
    EGRESS_CAPABILITY,
    ErrorCode,
    build_capabilities,
    detect_locale,
)


def test_build_capabilities_starts_with_egress_and_has_no_duplicates():
    capabilities = build_capabilities()
    assert capabilities[0] == EGRESS_CAPABILITY
    assert len(capabilities) == len(set(capabilities))


def test_detect_locale_defaults_to_en_us(monkeypatch):
    for name in ("CASTREL_LOCALE", "LC_ALL", "LANG"):
        monkeypatch.delenv(name, raising=False)
    assert detect_locale() == DEFAULT_LOCALE


def test_detect_locale_normalizes_posix_values(monkeypatch):
    monkeypatch.delenv("CASTREL_LOCALE", raising=False)
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    assert detect_locale() == "zh-CN"


def test_detect_locale_prefers_the_explicit_override(monkeypatch):
    monkeypatch.setenv("CASTREL_LOCALE", "zh-CN")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert detect_locale() == "zh-CN"


def test_detect_locale_falls_back_for_unsupported_languages(monkeypatch):
    monkeypatch.delenv("CASTREL_LOCALE", raising=False)
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    assert detect_locale() == DEFAULT_LOCALE


def test_error_codes_mirror_the_platform_list():
    values = {value for name, value in vars(ErrorCode).items() if name.isupper()}
    assert "sandbox_image_missing" in values
    assert "unsupported_capability" in values
    assert len(values) == 19
