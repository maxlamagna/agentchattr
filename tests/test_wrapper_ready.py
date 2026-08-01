"""Readiness probe contract (spec D1.2; codex #3921 pt 2).

Pure function - no tmux in tests: the caller supplies capture/liveness
callables. Positive ready pattern, negative blockers that fail fast and
BEAT a simultaneously-matching ready pattern, unknown screens fail closed
by timeout, session death and capture failure are never ready.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wrapper_ready import wait_cli_ready

BLOCKERS = [("consent", r"No, exit"), ("login", r"Select login method")]


def _seq(panes):
    """Capture fn serving each pane text once, then repeating the last."""
    it = iter(panes)
    last = [panes[-1]]

    def cap():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    return cap


class WrapperReadyTests(unittest.TestCase):
    def test_ready_on_pattern_match(self):
        cap = _seq(["starting up...", "Welcome\n> ", "Welcome\n> "])
        self.assertEqual(
            wait_cli_ready(cap, r"^> ", BLOCKERS, timeout_s=5, poll_s=0.01),
            "ready")

    def test_blocker_beats_timeout_and_is_labeled(self):
        cap = _seq(["loading", "Bypass Permissions dialog\n1. No, exit"])
        self.assertEqual(
            wait_cli_ready(cap, r"^> ", BLOCKERS, timeout_s=30, poll_s=0.01),
            "blocker:consent")

    def test_blocker_wins_over_ready_on_same_pane(self):
        """A loose ready pattern must not mask a blocker screen (spec D1.2)."""
        cap = _seq(["> something\n1. No, exit"])
        self.assertEqual(
            wait_cli_ready(cap, r"^> ", BLOCKERS, timeout_s=5, poll_s=0.01),
            "blocker:consent")

    def test_unknown_screen_times_out_fail_closed(self):
        cap = _seq(["something never ready"])
        self.assertEqual(
            wait_cli_ready(cap, r"^> ", BLOCKERS, timeout_s=0.05, poll_s=0.01),
            "timeout")

    def test_session_death_detected(self):
        cap = _seq(["booting"])
        self.assertEqual(
            wait_cli_ready(cap, r"^> ", BLOCKERS, timeout_s=5, poll_s=0.01,
                           session_alive_fn=lambda: False),
            "died")

    def test_capture_failure_is_not_ready(self):
        self.assertEqual(
            wait_cli_ready(lambda: None, r".", BLOCKERS,
                           timeout_s=0.05, poll_s=0.01),
            "timeout")


if __name__ == "__main__":
    unittest.main()
