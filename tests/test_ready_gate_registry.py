"""Ready-gate registry contract (spec D1.1/D1.5; codex #3921 pts 1,5; #3925.4).

The gate's registry invariants: gated registration enters `starting`;
`mark_ready` is the ONLY path that activates a starting instance; claim
refuses starting instances; cancel_starting removes with NO reservation and
NO reclaimable entry via the same removal primitive as deregister, so family
rename-back bookkeeping runs on both paths.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from registry import RuntimeRegistry


class ReadyGateRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reg = RuntimeRegistry(data_dir=self._tmp.name)
        self.reg.seed({"claude": {"label": "Claude", "color": "#da7756"},
                       "codex": {"label": "Codex", "color": "#10a37f"}})

    def tearDown(self):
        self._tmp.cleanup()

    def test_gated_register_starts_in_starting(self):
        result = self.reg.register("claude", ready_gate=True)
        self.assertEqual(result["state"], "starting")
        self.assertEqual(self.reg.get_state("claude"), "starting")

    def test_ungated_register_still_active(self):
        result = self.reg.register("claude")
        self.assertEqual(result["state"], "active")
        self.assertEqual(self.reg.get_state("claude"), "active")

    def test_mark_ready_only_from_starting(self):
        self.reg.register("claude", ready_gate=True)
        self.assertTrue(self.reg.mark_ready("claude"))
        self.assertEqual(self.reg.get_state("claude"), "active")
        # Illegal transitions refuse (#3925.4)
        self.assertFalse(self.reg.mark_ready("claude"))   # active -> ready: no
        self.assertFalse(self.reg.mark_ready("gemini"))   # unknown: no
        self.reg.register("codex")                        # ungated, active
        self.assertFalse(self.reg.mark_ready("codex"))    # never entered gate: no

    def test_mark_starting_regates_active_but_not_pending(self):
        self.reg.register("claude", ready_gate=True)
        self.reg.mark_ready("claude")
        self.assertTrue(self.reg.mark_starting("claude"))
        self.assertEqual(self.reg.get_state("claude"), "starting")
        # A pending instance (unclaimed placeholder) must NOT be re-gateable.
        self.reg.register("codex")
        with self.reg._lock:
            self.reg._instances["codex"].state = "pending"
        self.assertFalse(self.reg.mark_starting("codex"))

    def test_claim_refuses_starting_instance(self):
        self.reg.register("claude", ready_gate=True)
        res = self.reg.claim("claude")
        self.assertIsInstance(
            res, str,
            "claim must return an error string, not activate a starting instance")
        self.assertIn("starting", res)
        self.assertEqual(self.reg.get_state("claude"), "starting")

    def test_cancel_starting_no_reservation_no_reclaim_dead_token(self):
        first = self.reg.register("claude", ready_gate=True)
        self.assertTrue(self.reg.cancel_starting("claude"))
        self.assertIsNone(self.reg.resolve_token(first["token"]))
        relaunch = self.reg.register("claude", ready_gate=True)
        self.assertEqual(relaunch["name"], "claude")      # no claude-2 (spec D1.5)
        self.assertNotIn("_renamed_slot1", relaunch)

    def test_cancel_starting_second_instance_renames_back(self):
        """#3925.4: cancel shares deregister's family bookkeeping. claude active
        + gated claude-2; cancelling claude-2 must rename claude-1 -> claude."""
        self.reg.register("claude")                            # slot 1, active
        second = self.reg.register("claude", ready_gate=True)  # slot1 -> claude-1
        self.assertEqual(second["name"], "claude-2")
        self.assertTrue(self.reg.cancel_starting("claude-2"))
        names = set(self.reg.get_all().keys())
        self.assertIn("claude", names,
                      "rename-back bookkeeping must run on cancel")
        self.assertNotIn("claude-1", names)

    def test_cancel_starting_refuses_non_starting(self):
        self.reg.register("claude")
        self.assertFalse(self.reg.cancel_starting("claude"))   # active: deregister only

    def test_normal_deregister_still_reserves(self):
        """Pinned current behavior: the 30 s reservation SURVIVES normal deregister."""
        self.reg.register("claude")
        self.reg.deregister("claude")
        self.assertEqual(self.reg.register("claude")["name"], "claude-2")


if __name__ == "__main__":
    unittest.main()
