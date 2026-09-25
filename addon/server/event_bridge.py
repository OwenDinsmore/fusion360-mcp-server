"""
EventBridge — dispatches work from socket threads to Fusion's main thread.

Uses adsk.core.CustomEvent + a thread-safe queue so all Fusion API calls
happen on the main thread.  A 200 ms backup timer fires the custom event
periodically in case fireCustomEvent from a daemon thread is unreliable.

Two rules keep concurrent clients from corrupting each other:

1. drain_queue is NOT re-entrant.  Fusion pumps its event loop inside some
   API calls, which means a CustomEvent can be delivered — and drain_queue
   called — while a previous command is still executing.  Without a guard
   the second client's command runs NESTED inside the first one's
   transaction.  Measured: a build that takes 10.5 s alone took 27–55 s with
   one other client's command queued, and the other command completed in
   the middle of the build.  The guard makes a nested call return at once;
   the outer loop picks the item up when the current command finishes.

2. The backup timer stays quiet while a command is executing.  With an item
   queued it used to fire the custom event five times a second for the whole
   duration of a build, and every API call in that build paid for it.
"""

import os
import queue
import threading
import time
import traceback

import adsk.core

from . import get_logger
from .hints import classify

log = get_logger("bridge")

CUSTOM_EVENT_ID = "Fusion360MCP_BridgeEvent"
TIMER_INTERVAL_MS = 200  # backup polling interval

# Most commands are interactive and should fail fast if Fusion has wedged —
# a modal dialog blocks the main thread and every call queues behind it.
# Builds are different: a live-parametric rebuild spends real seconds in the
# constraint solver, and a 47-tooth patterned rack alone can take a minute.
# Failing those at 30s turns a slow build into a broken one.
#
# In between sit the commands that trigger a full timeline recompute on a
# finished model — a parameter change re-cuts every patterned tooth, a joint
# sweep runs interference at every step.  Measured on the carriage with a
# rack: a module change took 13.6 s, and a sweep 16.9 s; under the old 30 s
# budget both were one slow recompute away from a spurious timeout.
DEFAULT_TIMEOUT = 30.0
MEDIUM_TIMEOUT = float(os.environ.get("FUSION_MCP_MEDIUM_TIMEOUT", "180"))
LONG_TIMEOUT = float(os.environ.get("FUSION_MCP_BUILD_TIMEOUT", "600"))
MEDIUM_COMMANDS = frozenset({
    "fusion_params", "set_parameter", "create_parameter", "delete_parameter",
    "fusion_drive_joint", "fusion_sweep_joint",
    "fusion_check_interference", "check_interference", "fusion_analyze",
    "suppress_feature", "unsuppress_feature",
})
LONG_COMMANDS = frozenset({
    "fusion_rebuild", "fusion_execute", "execute_code", "fusion_reset",
    "delete_all", "fusion_export", "export", "export_stl", "export_step",
    "export_f3d", "cam_generate_toolpath", "cam_post_process",
})


def timeout_for(command_type: str) -> float:
    if command_type in LONG_COMMANDS:
        return LONG_TIMEOUT
    if command_type in MEDIUM_COMMANDS:
        return MEDIUM_TIMEOUT
    return DEFAULT_TIMEOUT


class WorkItem:
    """One unit of work submitted from a socket thread."""

    __slots__ = ("command", "result", "error", "done", "client", "submitted",
                 "cancelled")

    def __init__(self, command: dict):
        self.command = command
        self.result = None
        self.error = None
        self.done = threading.Event()
        # Set by the socket server from the peer address, or by a client that
        # names itself.  Logged with every command so the log can tell two
        # sessions apart.
        self.client = command.get("_client") or "?"
        self.submitted = time.monotonic()
        # Set when the waiting socket thread gave up (timeout).  A
        # cancelled item is skipped by drain_queue so a timed-out command
        # never executes late — the client has already reported failure.
        self.cancelled = False


class _MainThreadHandler(adsk.core.CustomEventHandler):
    """Attached to the CustomEvent; runs on Fusion's main thread."""

    def __init__(self, bridge: "EventBridge"):
        super().__init__()
        self._bridge = bridge

    def notify(self, args):  # called on main thread
        self._bridge.drain_queue()


class EventBridge:
    """
    Bridge between daemon socket threads and Fusion's main thread.

    Socket thread calls ``submit(command)`` which blocks (up to *timeout*
    seconds) until the main thread has executed the command and stored the
    result.
    """

    def __init__(self, app: adsk.core.Application, command_handler):
        self._app = app
        self._handler = command_handler
        self._queue: queue.Queue = queue.Queue()

        # Re-entrancy guard and counters, main-thread only.
        self._busy = False
        self._current = None          # (cmd_type, client, started) while busy
        self.stats = {"executed": 0, "nested_rejected": 0, "failed": 0,
                      "cancelled": 0}

        # Register a custom event on the main thread
        self._event = app.registerCustomEvent(CUSTOM_EVENT_ID)
        self._event_handler = _MainThreadHandler(self)
        self._event.add(self._event_handler)

        # Backup timer thread — fires the custom event every 200 ms
        self._timer_running = True
        self._timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
        self._timer_thread.start()

        log.info("EventBridge initialised (custom event: %s)", CUSTOM_EVENT_ID)

    # ------------------------------------------------------------------
    # Called from socket (daemon) threads
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Cheap, lock-free view of what the main thread is doing."""
        cur = self._current
        return {
            "busy": self._busy,
            "queued": self._queue.qsize(),
            "current": (
                {"command": cur[0], "client": cur[1],
                 "elapsed_s": round(time.monotonic() - cur[2], 3)}
                if cur else None
            ),
            **self.stats,
        }

    def submit(self, command: dict, timeout: float = None) -> dict:
        """Queue *command* for main-thread execution; block until done."""
        cmd_type = command.get("type", "?")

        # Fast-path: ping never touches Fusion API — answer immediately.
        # It reports whether the main thread is busy, so a client can tell
        # "another session is mid-build" from "Fusion is wedged".
        if cmd_type == "ping":
            log.debug("ping (fast path)")
            return {"status": "success",
                    "result": {"ok": True, "status": "pong", "pong": True,
                               "bridge": self.status()}}

        if timeout is None:
            timeout = timeout_for(cmd_type)
        item = WorkItem(command)
        log.debug("submit cmd=%s client=%s (timeout %ss, queued=%d, busy=%s)",
                  cmd_type, item.client, timeout, self._queue.qsize(),
                  self._busy)
        self._queue.put(item)

        # Poke the main thread
        try:
            self._app.fireCustomEvent(CUSTOM_EVENT_ID)
        except Exception:
            pass  # timer will pick it up

        if not item.done.wait(timeout=timeout):
            # The client has given up.  Cancel the item so drain_queue
            # skips it instead of executing it late — a late execution
            # plus a client retry would apply mutations twice.
            item.cancelled = True
            log.warning("Command %s from %s timed out after %ss, cancelled "
                        "(bridge %s)", cmd_type, item.client, timeout,
                        self.status())
            return {"status": "error", "error_kind": "TIMEOUT",
                    "message": f"Command timed out after {timeout}s"}

        if item.error is not None:
            log.error("Command %s from %s failed: %s",
                      cmd_type, item.client, item.error)
            kind, hints = classify(item.error)
            return {"status": "error", "error_kind": kind, "hints": hints,
                    "message": item.error}

        log.debug("Command %s from %s completed", cmd_type, item.client)
        return item.result

    # ------------------------------------------------------------------
    # Called on Fusion's main thread (from CustomEventHandler.notify)
    # ------------------------------------------------------------------

    def drain_queue(self):
        """Execute every queued work item (main thread only).

        Not re-entrant: if a command is already executing — Fusion delivered
        the custom event from inside an API call — return immediately and let
        the outer loop pick the queued item up afterwards.
        """
        if self._busy:
            self.stats["nested_rejected"] += 1
            log.debug("drain_queue re-entered while %s is running; deferred",
                      self._current[0] if self._current else "?")
            return

        self._busy = True
        try:
            while True:
                try:
                    item: WorkItem = self._queue.get_nowait()
                except queue.Empty:
                    break

                cmd_type = item.command.get("type", "?")
                if item.cancelled:
                    # Its client already reported the timeout; running it
                    # now would be a mutation nobody is waiting for.
                    log.info("Skipping cancelled %s from %s", cmd_type,
                             item.client)
                    self.stats["cancelled"] += 1
                    continue
                self._current = (cmd_type, item.client, time.monotonic())
                waited = time.monotonic() - item.submitted
                if waited > 1.0:
                    log.info("%s from %s waited %.1fs in queue",
                             cmd_type, item.client, waited)
                try:
                    if cmd_type == "reload_handler":
                        # Must run on the main thread — CommandHandler's
                        # constructor touches the Fusion API.
                        self.reload_handler()
                        item.result = {"status": "success",
                                       "result": {"ok": True, "reloaded": True}}
                    else:
                        item.result = self._handler.execute_command(item.command)
                    self.stats["executed"] += 1
                except Exception as exc:
                    item.error = f"{exc}\n{traceback.format_exc()}"
                    self.stats["failed"] += 1
                    log.error("Main-thread exec of %s raised: %s", cmd_type, exc)
                finally:
                    self._current = None
                    item.done.set()
        finally:
            self._busy = False

    # ------------------------------------------------------------------
    # Backup timer
    # ------------------------------------------------------------------

    def _timer_loop(self):
        interval = TIMER_INTERVAL_MS / 1000.0
        while self._timer_running:
            time.sleep(interval)
            # Only poke when there is something to do AND nothing running:
            # firing into a busy main thread is what taxed every API call.
            if not self._queue.empty() and not self._busy:
                try:
                    self._app.fireCustomEvent(CUSTOM_EVENT_ID)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Hot reload
    # ------------------------------------------------------------------

    def reload_handler(self):
        """Reimport handler modules and replace the active handler instance.

        Called on the main thread (from drain_queue).  Sibling modules
        are reloaded first, in dependency order, so command_handler picks
        up their changes too.
        """
        import importlib

        from . import command_handler as ch_mod
        from . import hints as hints_mod
        from . import hole_geometry as hole_mod
        from . import parameter_units as units_mod

        for mod in (hints_mod, hole_mod, units_mod, ch_mod):
            importlib.reload(mod)
        self._handler = ch_mod.CommandHandler()
        # Reset lazy dispatch table so it picks up new commands
        self._handler.__class__._COMMANDS = None
        log.info("CommandHandler reloaded")

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def stop(self):
        self._timer_running = False
        # Release any queued items so blocked socket threads wake up
        # immediately instead of waiting out their full timeout against
        # a dead bridge.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            item.cancelled = True
            item.error = "Add-in stopped before the command executed"
            item.done.set()
        try:
            self._event.remove(self._event_handler)
        except Exception:
            pass
        try:
            self._app.unregisterCustomEvent(CUSTOM_EVENT_ID)
        except Exception:
            pass
        log.info("EventBridge stopped")
