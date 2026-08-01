"""Ready-gate delivery contract (spec D1.1/D1.4; codex #3925.1, #3925.3).

The starting notice must be reachable in the REAL routing path (the extracted
_route_targets coroutine app.py uses), not only in AgentTrigger: routing must
short-circuit BEFORE the "appears offline - message queued" branch, post
exactly ONE honest system notice, and write zero queue bytes. AgentTrigger
keeps a defense-in-depth guard of its own, and get_status exposes the state
field the UI renders.
"""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents import AgentTrigger
from registry import RuntimeRegistry
import app as app_mod
import mcp_bridge


class FakeStore:
    """Matches store.MessageStore.add(sender, text, msg_type=, channel=, ...)."""

    def __init__(self):
        self.messages = []

    def add(self, sender, text, msg_type="chat", channel="general", **kw):
        self.messages.append({"sender": sender, "text": text,
                              "type": msg_type, "channel": channel})
        return {"id": len(self.messages)}


class ReadyGateDeliveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reg = RuntimeRegistry(data_dir=self._tmp.name)
        self.reg.seed({"claude": {"label": "Claude", "color": "#da7756"}})
        self.store = FakeStore()
        self.trig = AgentTrigger(self.reg, data_dir=self._tmp.name,
                                 store=self.store)

    def tearDown(self):
        self._tmp.cleanup()
        with mcp_bridge._presence_lock:
            mcp_bridge._presence.pop("claude", None)

    def _queue_lines(self):
        qf = Path(self._tmp.name) / "claude_queue.jsonl"
        return qf.read_text().splitlines() if qf.exists() else []

    def _route(self, chat_msg="Max: @claude hi"):
        with mock.patch.object(app_mod, "registry", self.reg), \
             mock.patch.object(app_mod, "agents", self.trig), \
             mock.patch.object(app_mod, "store", self.store):
            asyncio.run(app_mod._route_targets(["claude"], chat_msg, "general"))

    # --- AgentTrigger defense-in-depth guard ---

    def test_trigger_guard_blocks_starting(self):
        self.reg.register("claude", ready_gate=True)
        self.assertFalse(self.trig.trigger_sync("claude", "Max: @claude hi"))
        self.assertEqual(self._queue_lines(), [])

    def test_ready_delivery_unchanged(self):
        self.reg.register("claude", ready_gate=True)
        self.reg.mark_ready("claude")
        self.assertTrue(self.trig.trigger_sync("claude", "Max: @claude hi"))
        self.assertEqual(len(self._queue_lines()), 1)
        self.assertEqual(self.store.messages, [])

    def test_ungated_delivery_unchanged(self):
        self.reg.register("claude")
        self.assertTrue(self.trig.trigger_sync("claude", "Max: @claude hi"))
        self.assertEqual(len(self._queue_lines()), 1)
        self.assertEqual(self.store.messages, [])

    def test_no_store_still_guards_without_crashing(self):
        bare = AgentTrigger(self.reg, data_dir=self._tmp.name)
        self.reg.register("claude", ready_gate=True)
        self.assertFalse(bare.trigger_sync("claude", "Max: @claude hi"))
        self.assertEqual(self._queue_lines(), [])

    def test_is_available_false_while_starting(self):
        self.reg.register("claude", ready_gate=True)
        self.assertFalse(self.trig.is_available("claude"))
        self.reg.mark_ready("claude")
        self.assertTrue(self.trig.is_available("claude"))

    def test_get_status_exposes_state(self):
        self.reg.register("claude", ready_gate=True)
        self.assertEqual(self.trig.get_status()["claude"]["state"], "starting")
        self.reg.mark_ready("claude")
        self.assertEqual(self.trig.get_status()["claude"]["state"], "active")

    # --- Real routing path (#3925.1) ---

    def test_route_targets_starting_gets_one_notice_zero_queue(self):
        self.reg.register("claude", ready_gate=True)
        self._route()
        notices = [m for m in self.store.messages if m["type"] == "system"]
        self.assertEqual(len(notices), 1)
        self.assertIn("starting", notices[0]["text"])
        self.assertIn("not delivered", notices[0]["text"])
        self.assertEqual(notices[0]["channel"], "general")
        self.assertTrue(
            all("message queued" not in m["text"] for m in self.store.messages),
            "the misleading offline/queued notice must NOT fire for starting")
        self.assertEqual(self._queue_lines(), [])

    def test_route_targets_ready_delivers_without_notices(self):
        self.reg.register("claude", ready_gate=True)
        self.reg.mark_ready("claude")
        with mcp_bridge._presence_lock:
            mcp_bridge._presence["claude"] = time.time()
        self._route()
        self.assertEqual(len(self._queue_lines()), 1)
        self.assertEqual(self.store.messages, [])


if __name__ == "__main__":
    unittest.main()
