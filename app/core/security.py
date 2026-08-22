"""Secret masking and password hashing helpers.

Nothing in this project may write a credential, bearer token or signed URL
parameter to a log file, a database row or an HTTP response.  Every code path
that touches such a value routes it through the helpers below.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query-string parameters that are masked in dashboards and logs.
SENSITIVE_QUERY_KEYS = {
    "token",
    "auth",
    "auth_key",
    "authorization",
    "key",
    "hash",
    "hmac",
    "md5",
    "sig",
    "signature",
    "secret",
    "session",
    "sid",
    "password",
    "passwd",
    "pwd",
    "expires",
    "expire",
    "e",
    "st",
}

#: Header names that are never written to a log verbatim.
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
}

_MASK = "***"

# Values registered here are scrubbed from every log record - see
# app.core.logging.SecretScrubbingFilter.
_REGISTERED_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal secret so it is scrubbed from all log output."""
    if value and len(value) >= 4:
        _REGISTERED_SECRETS.add(value)


def registered_secrets() -> frozenset[str]:
    return frozenset(_REGISTERED_SECRETS)


def mask_secret(value: str | None, keep: int = 4) -> str:
    """Mask a secret, optionally keeping a short suffix for identification.

    >>> mask_secret("1234567890abcdef")
    '***cdef'
    """
    if not value:
        return ""
    if len(value) <= keep:
        return _MASK
    return f"{_MASK}{value[-keep:]}"


def mask_url_token(url: str | None) -> str:
    """Return *url* with sensitive query parameters replaced by ``***``.

    The host and path are preserved so the URL is still recognisable.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _MASK
    if not parts.query:
        return url
    masked_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_QUERY_KEYS and value:
            masked_pairs.append((key, _MASK))
        else:
            masked_pairs.append((key, value))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(masked_pairs), parts.fragment)
    )


def shorten_url(url: str | None, max_path: int = 28) -> str:
    """Compact a URL for a table cell: ``https://host/.../index.m3u8``."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _MASK
    path = parts.path or "/"
    if len(path) > max_path:
        tail = path.rsplit("/", 1)[-1] or path[-max_path:]
        path = f"/.../{tail}"
    suffix = "?..." if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}{path}{suffix}"


def mask_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Copy *headers* with sensitive values masked."""
    if not headers:
        return {}
    return {
        k: (mask_secret(v) if k.lower() in SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


def scrub(text: str) -> str:
    """Remove every registered secret literal from *text*."""
    if not text:
        return text
    for secret_value in _REGISTERED_SECRETS:
        if secret_value in text:
            text = text.replace(secret_value, _MASK)
    return text


# --------------------------------------------------------------------------- #
# password hashing (PBKDF2-HMAC-SHA256, no third-party dependency)
# --------------------------------------------------------------------------- #

_PBKDF2_ROUNDS = 240_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password into ``pbkdf2_sha256$rounds$salt$digest``."""
    if not password:
        raise ValueError("password must not be empty")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of a password against a stored hash."""
    if not password or not stored:
        return False
    try:
        algorithm, rounds_raw, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(candidate, expected)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def random_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)
