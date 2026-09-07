"""Authentication for the socket bridge.

The bridge executes arbitrary Python via execute_code, so an unauthenticated
caller is a full remote-code-execution path for anything running as the user.
A loopback bind is not an access control.
"""

from __future__ import annotations

import os
from unittest import mock

from fusion360_mcp.auth import (
    DISABLE_ENV,
    SECRET_ENV,
    auth_disabled,
    load_secret,
    token_ok,
)


class TestTokenComparison:
    def test_correct_token_passes(self):
        assert token_ok("s3cret", "s3cret") is True

    def test_wrong_token_fails(self):
        assert token_ok("s3cret", "nope") is False

    def test_missing_token_fails(self):
        assert token_ok("s3cret", None) is False
        assert token_ok("s3cret", "") is False

    def test_non_string_token_fails(self):
        """A JSON payload can put anything in that field."""
        for junk in (1, True, [], {}, 0.0):
            assert token_ok("s3cret", junk) is False

    def test_no_expected_secret_means_auth_is_off(self):
        assert token_ok(None, None) is True
        assert token_ok(None, "anything") is True

    def test_near_miss_fails(self):
        """Guards against a prefix or length-only comparison."""
        assert token_ok("abcdef", "abcde") is False
        assert token_ok("abcdef", "abcdeg") is False
        assert token_ok("abcdef", "abcdefg") is False


class TestSecretResolution:
    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {SECRET_ENV: "from-env"}, clear=False):
            assert load_secret() == "from-env"

    def test_disable_flag_returns_none(self):
        with mock.patch.dict(
            os.environ, {DISABLE_ENV: "1", SECRET_ENV: "ignored"}, clear=False
        ):
            assert auth_disabled() is True
            assert load_secret() is None

    def test_disable_flag_accepts_common_spellings(self):
        for value in ("1", "true", "TRUE", "yes", "Yes"):
            with mock.patch.dict(os.environ, {DISABLE_ENV: value}, clear=False):
                assert auth_disabled() is True

    def test_unset_flag_means_auth_is_on(self):
        env = {k: v for k, v in os.environ.items() if k != DISABLE_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            assert auth_disabled() is False

    def test_client_never_creates_a_secret(self, tmp_path):
        """Only the add-in may generate one.

        A client that quietly wrote its own would produce a token mismatch
        instead of a clear 'no secret found', and the two files would then
        disagree forever.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in (SECRET_ENV, DISABLE_ENV)}
        missing = tmp_path / "nonexistent-secret"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("fusion360_mcp.auth.SECRET_FILE", missing):
            assert load_secret(create=False) is None
            assert not missing.exists()

    def test_addon_creates_a_secret_with_owner_only_permissions(self, tmp_path):
        env = {k: v for k, v in os.environ.items()
               if k not in (SECRET_ENV, DISABLE_ENV)}
        path = tmp_path / "secret"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("fusion360_mcp.auth.SECRET_FILE", path):
            value = load_secret(create=True)
            assert value and len(value) >= 32
            assert path.exists()
            assert (path.stat().st_mode & 0o077) == 0, (
                "the secret file must not be group- or world-readable"
            )
            # Stable across calls, or the two sides would never agree.
            assert load_secret(create=True) == value
