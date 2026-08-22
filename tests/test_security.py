"""Secret masking, hashing and log scrubbing."""

from __future__ import annotations

from app.core.security import (
    hash_password,
    mask_headers,
    mask_secret,
    mask_url_token,
    register_secret,
    scrub,
    shorten_url,
    verify_password,
)


def test_mask_secret_keeps_a_short_suffix():
    assert mask_secret("1234567890abcdef") == "***cdef"
    assert mask_secret("abc") == "***"
    assert mask_secret("") == ""
    assert mask_secret(None) == ""


def test_mask_url_token_hides_signed_parameters():
    url = "https://cdn.example/live/a.m3u8?token=abc123&expires=1924992000&quality=hd"
    masked = mask_url_token(url)
    assert "abc123" not in masked
    assert "1924992000" not in masked
    assert "quality=hd" in masked, "harmless parameters stay readable"
    assert masked.startswith("https://cdn.example/live/a.m3u8")


def test_mask_url_token_without_query():
    assert mask_url_token("https://cdn.example/a.m3u8") == "https://cdn.example/a.m3u8"
    assert mask_url_token("") == ""


def test_shorten_url():
    short = shorten_url("https://cdn.example/very/long/path/segments/here/index.m3u8?token=x")
    assert short.startswith("https://cdn.example/")
    assert "index.m3u8" in short
    assert "token=x" not in short


def test_mask_headers():
    masked = mask_headers(
        {"Authorization": "Bearer abcdef123456", "Cookie": "sid=xyz", "Accept": "*/*"}
    )
    assert "abcdef123456" not in masked["Authorization"]
    assert masked["Accept"] == "*/*"


def test_scrub_removes_registered_secrets():
    register_secret("hunter2-very-secret")
    assert "hunter2-very-secret" not in scrub("logging in with hunter2-very-secret now")
    assert scrub("nothing to hide") == "nothing to hide"


def test_password_hashing_roundtrip():
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("pbkdf2_sha256$")
    assert "correct horse" not in stored
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)


def test_password_verification_is_defensive():
    assert not verify_password("x", "")
    assert not verify_password("", hash_password("x"))
    assert not verify_password("x", "garbage")
    assert not verify_password("x", "md5$1$2$3")


def test_two_hashes_of_the_same_password_differ():
    assert hash_password("same") != hash_password("same"), "each hash uses a fresh salt"
