"""Password hashing, client keys and signed session cookies.

Everything here is standard library on purpose: this project has a six-package
dependency list and adding passlib/bcrypt for two hashes is not worth it. The
rounding errors you would make by hand-rolling crypto come from using the wrong
primitive, not from using ``hashlib`` — so the rules are:

- Passwords: PBKDF2-HMAC-SHA256 with a random salt and a high iteration count
  (passwords are low entropy, so the work factor has to do the defending).
- Client keys: plain SHA-256. They are 256-bit random strings, so there is
  nothing to brute force; the work factor would only add latency to every
  relayed request.
- Comparisons: always ``hmac.compare_digest`` — a byte-by-byte ``==`` leaks the
  answer length one character at a time.
- Sessions: a stateless signed cookie, not a server-side session table. Nothing
  to store, nothing to leak, and a restart does not log everyone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time


# Iterations for PBKDF2. 200k is ~100ms on a laptop — imperceptible once at
# login, painful enough to make an offline cracking of a leaked relay.yaml slow.
PBKDF2_ITERATIONS = 200_000

# Client keys are read from the "sk-" prefix. OpenAI SDKs validate that a key
# looks like a key before sending it, so a bare random string gets rejected by
# some clients for reasons that have nothing to do with the relay.
KEY_PREFIX = "sk-r_"
KEY_RANDOM_BYTES = 24          # 24 bytes -> 32 base62-ish chars
KEY_PREFIX_VISIBLE = 12        # characters kept in the UI list


# ---------------------------------------------------------------- passwords

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS,
                  salt: bytes | None = None) -> str:
    """Return ``pbkdf2_sha256$iterations$salt$digest``.

    Self-describing on purpose: the iteration count lives in the hash, so
    raising the default later does not invalidate existing passwords.
    """
    if not password:
        raise ValueError("password cannot be empty")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash.

    Returns False (rather than raising) for any malformed stored value, so a
    corrupted config cannot turn into a 500 on the login page.
    """
    if not password or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = _unb64(parts[2])
        expected = _unb64(parts[3])
    except (ValueError, TypeError):
        return False
    if iterations <= 0:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def new_password(n_bytes: int = 9) -> str:
    """A human-typable random password for the first-run bootstrap.

    Base32 without padding: no characters that need quoting in a shell, curl or
    a YAML file, and no 0/O or 1/I confusion.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = secrets.token_bytes(n_bytes)
    return "-".join(
        "".join(alphabet[b % len(alphabet)] for b in raw[i:i + 5])
        for i in range(0, len(raw), 5)
    )


# -------------------------------------------------------------- client keys

def generate_key() -> str:
    """A fresh client key, e.g. ``sk-r_3f9a...`` (256 bits of entropy)."""
    return KEY_PREFIX + secrets.token_urlsafe(KEY_RANDOM_BYTES).replace("-", "_")


def hash_key(key: str) -> str:
    """Hash a client key for storage. One-way by design.

    A leaked relay.yaml must not hand an attacker a working key — otherwise
    "stored hashed" is security theatre.
    """
    return "sha256$" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """The leading characters shown in the UI so people can tell keys apart."""
    return key[:KEY_PREFIX_VISIBLE]


def make_client(name: str, key: str, *, groups: list[str] | None = None) -> dict:
    """Build a stored client record. The key itself is not kept."""
    return {
        "name": name,
        "key_hash": hash_key(key),
        "key_prefix": key_prefix(key),
        "enabled": True,
        # Empty list = may call every group. Non-empty = only these.
        "groups": list(groups or []),
        "created_at": time.time(),
        "last_used_at": None,
        "last_used_ip": "",
    }


def find_client(clients: list[dict], presented: str) -> dict | None:
    """Match a presented Bearer key against stored hashes, in constant time.

    Every client is checked even after a match, so the response time does not
    reveal which prefix hit or how many clients exist.
    """
    if not presented:
        return None
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    found: dict | None = None
    for c in clients or []:
        stored = str(c.get("key_hash") or "")
        if stored.startswith("sha256$"):
            stored = stored[len("sha256$"):]
        if hmac.compare_digest(stored, digest):
            found = c
    return found


# ---------------------------------------------------------------- sessions

COOKIE_NAME = "llm_evl_session"


def sign_session(username: str, secret: str, *, ttl_seconds: int = 12 * 3600,
                 now: float | None = None) -> str:
    """Build a cookie value: ``expires.username.hmac``.

    The HMAC covers both the expiry and the username, so neither can be edited
    without invalidating the signature.
    """
    if not secret:
        raise ValueError("session secret is empty")
    expires = int((now if now is not None else time.time()) + ttl_seconds)
    payload = f"{expires}.{username}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session(token: str, secret: str, *, now: float | None = None) -> str | None:
    """Return the username if the cookie is valid and unexpired, else None."""
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    expires_s, username, sig = parts
    payload = f"{expires_s}.{username}"
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        expires = int(expires_s)
    except ValueError:
        return None
    if (now if now is not None else time.time()) > expires:
        return None
    return username or None
