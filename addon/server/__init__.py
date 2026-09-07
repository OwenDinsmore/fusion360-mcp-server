"""Fusion360MCP Server Package — v2 (CustomEvent bridge architecture)"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_PATH = os.path.join(os.path.expanduser("~"), "fusion360mcp.log")

_logger = logging.getLogger("fusion360mcp")
_logger.setLevel(logging.DEBUG)

# The add-in can be stopped and started several times in one Fusion process,
# and each start re-imports this package.  Without clearing first, every
# start adds another pair of handlers and every line is logged N times.
for _h in list(_logger.handlers):
    _logger.removeHandler(_h)
    try:
        _h.close()
    except Exception:
        pass

# File handler — rotates at 2 MB, keeps 3 backups
_fh = RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3)
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"))
_logger.addHandler(_fh)

# Also echo to Fusion's TEXT COMMANDS (stdout/stderr)
_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.INFO)
_sh.setFormatter(logging.Formatter("[MCP] %(message)s"))
_logger.addHandler(_sh)


def get_logger(name: str = None) -> logging.Logger:
    """Return a child logger.  ``get_logger("bridge")`` → ``fusion360mcp.bridge``."""
    if name:
        return _logger.getChild(name)
    return _logger
