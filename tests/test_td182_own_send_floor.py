"""TD-182: a sender's own message must not hide another channel's unread messages.

_advance_over_own_send reads every message past the LOWEST of the sender's
floors (its broad __all__ cursor and the cursor of the channel it posted in).
Reading only past the highest one misses unread messages of another channel
that sit between the two, and the broad cursor then jumps over them: the skip
TD-182 fixed. (G2 r3 finding G1, probe P7a.)
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_bridge
from store import MessageStore

_GLOBALS = ("store", "jobs", "rules", "registry", "config", "_CURSORS_FILE")
_STATE = ("_cursors", "_last_read_channel", "_last_read_job_id", "_empty_read_count")


class OwnSendFloorTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._saved = {n: getattr(mcp_bridge, n) for n in _GLOBALS}
        self._saved_state = {n: dict(getattr(mcp_bridge, n)) for n in _STATE}
        mcp_bridge.store = MessageStore(str(d / "chat.jsonl"))
        mcp_bridge.jobs = mcp_bridge.rules = mcp_bridge.registry = mcp_bridge.config = None
        mcp_bridge._CURSORS_FILE = d / "cursors.json"
        for n in _STATE:
            getattr(mcp_bridge, n).clear()

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(mcp_bridge, n, v)
        for n, v in self._saved_state.items():
            getattr(mcp_bridge, n).clear()
            getattr(mcp_bridge, n).update(v)
        self._tmp.cleanup()

    def _post(self, sender, text, channel):
        out = mcp_bridge.chat_send(sender=sender, message=text, channel=channel)
        return int(out.split("id=", 1)[1].split(")")[0])

    def _read_ids(self, **kwargs):
        out = mcp_bridge.chat_read(**kwargs)
        self.assertTrue(out.startswith("["), out[:80])
        return [m["id"] for m in json.loads(out) if m.get("type") != "read_more"]

    def test_own_send_keeps_other_channel_unread_for_broad_reader(self):
        self._post("bob", "m0", "x")
        self._post("bob", "m1", "x")
        mcp_bridge.chat_read(sender="alice")  # broad cursor at 1
        unread_y = [self._post("bob", "y%d" % i, "y") for i in range(2)]
        x2 = self._post("bob", "x2", "x")
        mcp_bridge.chat_read(sender="alice", channel="x")  # #x cursor at x2
        own = self._post("alice", "alice in x", "x")
        self.assertEqual(mcp_bridge._cursors["alice"]["__all__"], 1)
        self.assertEqual(self._read_ids(sender="alice"), unread_y + [x2, own])


if __name__ == "__main__":
    unittest.main()
