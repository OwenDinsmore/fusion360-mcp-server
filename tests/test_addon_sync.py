"""Drift guards: assert addon-side and package-side mirrors stay in sync.

The Fusion add-in is installed into Fusion's AddIns folder and cannot
import from this package, so a few tables are duplicated:

* ``addon/server/hints.py:_RULES``   ↔  ``src/fusion360_mcp/hints.py:_RULES``
* ``addon/server/auth.py``          ↔  ``src/fusion360_mcp/auth.py``
* ``CommandHandler._MUTATION_COMMANDS``  ↔  ``mock.py:_MUTATION_MOCKS``

If they drift, agents see different error envelopes / delta payloads
depending on whether they're running against Fusion or mock mode.  These
tests fail loudly the moment a maintainer updates one side without the
other.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module_by_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _extract_class_attr_set(path: Path, class_name: str, attr: str) -> set[str]:
    """Pull ``ClassName.attr`` (a set/frozenset literal) out of *path* via AST.

    Avoids importing the addon module, which depends on Fusion's ``adsk``
    runtime and isn't installable in unit-test environments.
    """
    tree = ast.parse(path.read_text())
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if cls.name != class_name:
            continue
        for stmt in cls.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == attr for t in stmt.targets):
                continue
            value = stmt.value
            # frozenset({...})  →  unwrap the set literal arg
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "frozenset"
                and value.args
            ):
                return set(ast.literal_eval(value.args[0]))
            return set(ast.literal_eval(value))
    raise AssertionError(f"{class_name}.{attr} not found in {path}")


def test_hints_rules_in_sync():
    """addon and src copies of hints._RULES must be identical."""
    addon_hints = _load_module_by_path(
        REPO_ROOT / "addon" / "server" / "hints.py", "_addon_hints"
    )
    src_hints = _load_module_by_path(
        REPO_ROOT / "src" / "fusion360_mcp" / "hints.py", "_src_hints"
    )
    assert addon_hints._RULES == src_hints._RULES, (
        "addon/server/hints.py and src/fusion360_mcp/hints.py have drifted. "
        "Update both files in lockstep."
    )


def test_mutation_sets_in_sync():
    """Addon mutation set and mock mutation set must be identical."""
    from fusion360_mcp.mock import _MUTATION_MOCKS

    addon_set = _extract_class_attr_set(
        REPO_ROOT / "addon" / "server" / "command_handler.py",
        "CommandHandler",
        "_MUTATION_COMMANDS",
    )
    mock_set = set(_MUTATION_MOCKS)
    assert addon_set == mock_set, (
        f"_MUTATION_COMMANDS (addon) and _MUTATION_MOCKS (mock.py) have drifted.\n"
        f"  only in addon: {sorted(addon_set - mock_set)}\n"
        f"  only in mock:  {sorted(mock_set - addon_set)}"
    )


def _extract_dispatch_keys(path: Path) -> set[str]:
    """Pull the command names out of ``CommandHandler._COMMANDS``.

    The table is built inside ``execute_command`` as a dict literal assigned
    to ``self.__class__._COMMANDS``, so it has to come out via AST rather than
    by importing (the addon needs Fusion's ``adsk`` runtime).
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "_COMMANDS"
                and isinstance(node.value, ast.Dict)
            ):
                return {
                    k.value
                    for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    raise AssertionError(f"CommandHandler._COMMANDS dict not found in {path}")


def test_every_tool_has_an_addon_handler():
    """Every MCP tool must map to a command the add-in can actually dispatch.

    Without this, adding a tool definition and forgetting the handler passes
    every test and only fails against live Fusion, as 'Unknown command'.
    """
    from fusion360_mcp.tools import TOOLS

    dispatch = _extract_dispatch_keys(
        REPO_ROOT / "addon" / "server" / "command_handler.py"
    )
    # ping is answered by the EventBridge fast path, never reaching dispatch.
    tool_names = {t["name"] for t in TOOLS} - {"ping"}
    missing = tool_names - dispatch
    assert not missing, (
        f"Tools declared in tools.py with no handler in the add-in: "
        f"{sorted(missing)}"
    )


def test_every_tool_has_a_real_mock():
    """--mode mock must not fall through to the placeholder for any tool."""
    from fusion360_mcp.mock import _DISPATCH
    from fusion360_mcp.tools import TOOLS

    tool_names = {t["name"] for t in TOOLS}
    missing = sorted(n for n in tool_names if n not in _DISPATCH)
    assert not missing, (
        f"Tools with no mock handler (mock mode would return the "
        f"'no mock handler' placeholder): {missing}"
    )


def test_auth_modules_are_identical():
    """The two copies of auth.py must not drift.

    They implement both halves of one handshake. If the add-in and the client
    disagree about where the secret comes from, every call fails with
    Unauthorized and the cause is invisible from either side alone.
    """
    addon = (REPO_ROOT / "addon" / "server" / "auth.py").read_text()
    src = (REPO_ROOT / "src" / "fusion360_mcp" / "auth.py").read_text()
    assert addon == src, (
        "addon/server/auth.py and src/fusion360_mcp/auth.py have drifted. "
        "They must stay byte-identical."
    )
