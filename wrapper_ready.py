"""Readiness probe for the opt-in ready gate (TD-006).

Pure logic: the caller supplies capture/liveness callables so tests need no
tmux. Patterns are multiline-searched over the visible pane text. Blockers
are checked BEFORE the ready pattern so a blocker screen that also happens
to match a loose ready pattern still fails closed (spec D1.2).
"""
import re
import time


def wait_cli_ready(capture_fn, ready_pattern, blocker_patterns, timeout_s,
                   poll_s=1.0, session_alive_fn=None):
    """Poll a CLI pane until it is provably ready, blocked, dead, or timed out.

    capture_fn() -> str | None   (None = capture failed; never counts as ready)
    ready_pattern: regex for the known-ready screen
    blocker_patterns: list of (label, regex) for known blocker screens
    session_alive_fn() -> bool   (optional; False = session died)

    Returns "ready" | "blocker:<label>" | "timeout" | "died".
    """
    ready_re = re.compile(ready_pattern, re.MULTILINE)
    blockers = [(label, re.compile(rx, re.MULTILINE))
                for label, rx in blocker_patterns]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if session_alive_fn is not None and not session_alive_fn():
            return "died"
        pane = capture_fn()
        if pane is not None:
            for label, rx in blockers:
                if rx.search(pane):
                    return f"blocker:{label}"
            if ready_re.search(pane):
                return "ready"
        time.sleep(poll_s)
    return "timeout"
