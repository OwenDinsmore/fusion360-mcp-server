"""
Shared-secret authentication for the Fusion360MCP socket bridge.

This file is duplicated between ``addon/server/auth.py`` and
``src/fusion360_mcp/auth.py`` because the add-in runs inside Fusion's
interpreter and cannot import from the installed package. The copies must stay
byte-identical; ``tests/test_addon_sync.py`` fails if they drift.

Why this exists: the bridge accepts arbitrary Python via ``execute_code``. With
no credential, any local process — any script, any npm postinstall, anything
that can open a socket — can drive or read the user's CAD session. A loopback
bind is not an access control.

Behaviour:

* ``FUSION_MCP_NO_AUTH=1`` disables authentication entirely. Explicit, logged,
  and the only way to get the old unauthenticated behaviour.
* Otherwise the secret comes from ``FUSION_MCP_SECRET`` if set, else from
  ``~/.fusion-mcp-secret``.
* If that file does not exist it is CREATED with a fresh random secret at mode
  0600. Secure by default with no setup step — both sides run as the same user
  on the same machine, so both read the same file and it simply works.

For a cross-machine (LAN) setup, copy the secret file to the client host or set
``FUSION_MCP_SECRET`` on both sides.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path

SECRET_ENV = "FUSION_MCP_SECRET"
DISABLE_ENV = "FUSION_MCP_NO_AUTH"
SECRET_FILE = Path.home() / ".fusion-mcp-secret"

UNAUTHORIZED = (
    "Unauthorized: missing or incorrect token. The add-in and the client must "
    "read the same secret. Check ~/.fusion-mcp-secret is readable by both, or "
    "set FUSION_MCP_SECRET on both sides. To run without authentication, "
    "start Fusion with FUSION_MCP_NO_AUTH=1 (not recommended — the bridge "
    "executes arbitrary Python)."
)


def auth_disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes")


def load_secret(create: bool = False) -> str | None:
    """Return the shared secret, or None when authentication is disabled.

    With *create* true, generates the secret file if it is missing. Only the
    add-in should do that; a client that silently creates its own secret would
    just produce a confusing mismatch instead of a clear error.
    """
    if auth_disabled():
        return None

    from_env = os.environ.get(SECRET_ENV, "").strip()
    if from_env:
        return from_env

    try:
        value = SECRET_FILE.read_text().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    except OSError:
        # Unreadable (wrong owner, bad permissions). Do not paper over it by
        # generating a second secret that will never match the other side.
        raise

    if not create:
        return None

    value = secrets.token_hex(32)
    SECRET_FILE.write_text(value + "\n")
    try:
        SECRET_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    return value


def token_ok(expected: str | None, presented) -> bool:
    """Constant-time token comparison.

    ``expected`` None means authentication is off and everything passes.
    """
    if expected is None:
        return True
    if not isinstance(presented, str) or not presented:
        return False
    return hmac.compare_digest(expected, presented)
