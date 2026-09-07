"""Make the src-layout package importable without relying on the editable .pth.

On macOS, uv installs `__editable__.*.pth` (and `_virtualenv.pth`) with the
BSD `UF_HIDDEN` flag set. Python 3.11+ `site.addpackage()` deliberately skips
hidden .pth files, so the editable install silently does nothing and every
`import fusion360_mcp` fails with ModuleNotFoundError — even though the file is
present and its contents are correct. `chflags nohidden` fixes it until the
next `uv sync`/`uv run`, which re-applies the flag.

Putting src on sys.path here makes the test suite immune. The MCP server gets
the same guarantee via PYTHONPATH in the project's .mcp.json.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
