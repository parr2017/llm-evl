"""Unit tests for the auth primitives: passwords, client keys, sessions.

These are the tests that matter most in the project, because a bug here is
either a locked-out owner or an open door. They are deliberately blunt: wrong
password, tampered cookie, expired cookie, empty input, malformed hash.
"""

import time

from llm_evl.core.auth import (
    find_client,
    generate_key,
    hash_key,
    hash_password,
    key_prefix,
    make_client,
    new_password,
    sign_session,
    verify_password,
    verify_session,
)


# --------------------------------------------------------------- passwords

class TestPassword:
    def test_round_trip(self):
        h = hash_password("s3cret-密码")
        assert verify_password("s3cret-密码", h) is True
        assert verify_password("wrong", h) is False

    def test_hash_is_salted(self):
        # Two identical passwords must not produce the same hash, or a leaked
        # config would reveal which providers share a key.
        assert hash_password("same") != hash_password("same")

    def test_hash_is_self_describing(self):
        h = hash_password("x", iterations=1000)
        assert h.startswith("pbkdf2_sha256$1000$")
        # A hash made with a lower work factor still verifies.
        assert verify_password("x", h) is True

    def test_empty_password_rejected(self):
        for bad in ("", None):
            try:
                hash_password(bad)
            except ValueError:
                continue
            raise AssertionError("empty password must not be hashable")

    def test_malformed_hash_returns_false(self):
        for bad in ("", "garbage", "pbkdf2_sha256$notanint$a$b",
                    "md5$1$a$b", "pbkdf2_sha256$$$"):
            assert verify_password("x", bad) is False, bad

    def test_generated_password_verifies(self):
        pw = new_password()
        assert len(pw) >= 8
        assert verify_password(pw, hash_password(pw)) is True

    def test_generated_passwords_differ(self):
        assert new_password() != new_password()


# ------------------------------------------------------------- client keys

class TestClientKeys:
    def test_generated_keys_are_unique_and_prefixed(self):
        keys = {generate_key() for _ in range(50)}
        assert len(keys) == 50
        assert all(k.startswith("sk-r_") for k in keys)
        assert all(len(k) > 30 for k in keys)

    def test_hash_is_stable_and_prefixed(self):
        k = generate_key()
        assert hash_key(k) == hash_key(k)
        assert hash_key(k).startswith("sha256$")

    def test_stored_record_has_no_plaintext(self):
        key = generate_key()
        record = make_client("alice", key)
        assert key not in str(record)
        assert record["name"] == "alice"
        assert record["enabled"] is True
        assert record["groups"] == []
        assert record["last_used_at"] is None

    def test_find_client_matches_and_misses(self):
        key = generate_key()
        record = make_client("alice", key)
        assert find_client([record], key) is not None
        assert find_client([record], generate_key()) is None
        assert find_client([record], "") is None
        assert find_client([], key) is None

    def test_find_client_ignores_disabled_flag(self):
        # Whether a key is enabled is a policy decision made by the caller;
        # the lookup itself should still find it.
        key = generate_key()
        record = make_client("bob", key, groups=["A"])
        record["enabled"] = False
        found = find_client([record], key)
        assert found is not None and found["groups"] == ["A"]

    def test_prefix_is_short_enough_to_be_safe(self):
        key = generate_key()
        assert len(key_prefix(key)) == 12
        assert not key.startswith(key_prefix(key)[8:])


# ---------------------------------------------------------------- sessions

class TestSession:
    def test_round_trip(self):
        token = sign_session("admin", "s3cret-secret", ttl_seconds=3600)
        assert verify_session(token, "s3cret-secret") == "admin"

    def test_wrong_secret_rejected(self):
        token = sign_session("admin", "secret-a", ttl_seconds=3600)
        assert verify_session(token, "secret-b") is None

    def test_tampered_payload_rejected(self):
        token = sign_session("admin", "s3cret", ttl_seconds=3600)
        expires, user, sig = token.split(".")
        # Impersonating another user by editing the payload must invalidate it.
        forged = f"{expires}.root.{sig}"
        assert verify_session(forged, "s3cret") is None

    def test_extended_expiry_rejected(self):
        token = sign_session("admin", "s3cret", ttl_seconds=60, now=1000.0)
        # Push the expiry out without touching the signature.
        expires, user, sig = token.split(".")
        forged = f"{99999999999}.{user}.{sig}"
        assert verify_session(forged, "s3cret", now=1001.0) is None

    def test_expired_rejected(self):
        token = sign_session("admin", "s3cret", ttl_seconds=10, now=1000.0)
        assert verify_session(token, "s3cret", now=1005.0) == "admin"
        assert verify_session(token, "s3cret", now=1011.0) is None

    def test_garbage_rejected(self):
        for bad in ("", "x", "a.b", "a.b.c.d", "notanumber.user.sig"):
            assert verify_session(bad, "s3cret") is None

    def test_empty_secret_cannot_sign(self):
        try:
            sign_session("admin", "")
        except ValueError:
            return
        raise AssertionError("signing with an empty secret must fail loudly")
