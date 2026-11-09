"""Stable identities (ad-e5gz) over a REAL run.py server and REAL wrapper.py processes.

The wrappers' tmux is the fake-tmux shim (tests/harness), so each wrapper's injected keys
are in its own keys.log. Cases:
  named launch of a live name: exits, touches nothing (group 3);
  409 recovery returns under its own name (groups 3, 8);
  409 recovery never takes another name and holds the queue until its name is free,
    then delivers the held wake (groups 8, 11, 15);
  two real wrappers on one name: only the owner injects and acknowledges (group 11, F1);
  a sibling leaving, with a stale rename mapping planted, changes nothing for the
    survivor (groups 2, 4, 5: replays O1 and O2).
"""
import json
import signal
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from harness import Server, Wrapper, api, assert_stable, poll_until

WAKE = "use mcp to read #general"


@unittest.skipIf(sys.platform == "win32", "fake tmux is unix-only")
class StableIdentityProcessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def server(self, renames=None) -> Server:
        if renames is not None:
            (self.tmp / "data").mkdir(parents=True, exist_ok=True)
            (self.tmp / "data" / "renames.json").write_text(json.dumps(renames), "utf-8")
        server = Server(self.tmp)
        self.addCleanup(server.stop)
        return server

    def wrapper(self, server, *extra) -> Wrapper:
        w = Wrapper(self.tmp, server, gated=False, scenario=["just a normal prompt"],
                    extra_args=extra)
        self.addCleanup(w.stop)
        return w

    def active(self, server, name):
        poll_until(lambda: (server.status().get(name) or {}).get("state") == "active",
                   15, f"{name} to be active")

    def token_of(self, server, name):
        data = json.loads((server.data_dir / "registry.json").read_text("utf-8"))
        return data["instances"][name]["token"]

    def kill_token(self, server, name):
        """Make a live wrapper's token stale, as a superseding launch or wiped server would."""
        code, _ = api(server.base_url, "POST", f"/api/deregister/{name}",
                      token=self.token_of(server, name))
        self.assertEqual(code, 200)

    def journal(self, server, name="claude"):
        path = server.data_dir / f"{name}_queue.delivery.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text("utf-8").splitlines() if l.strip()]

    def family(self, server):
        return sorted(n for n, v in server.status().items()
                      if isinstance(v, dict) and n.startswith("claude"))

    # --- group 3 ---------------------------------------------------------------------------

    def test_named_launch_of_a_live_name_exits_and_touches_nothing(self):
        server = self.server()
        live = server.register("claude")
        w = self.wrapper(server, "--name", "claude")
        self.assertEqual(w.wait_exit(20), 1)
        self.assertIn("name_in_use", w.log())
        self.assertEqual(w.calls("new-session"), [], "a refused launch must not start a session")
        code, body = api(server.base_url, "POST", "/api/heartbeat/claude", token=live["token"])
        self.assertEqual((code, body.get("name")), (200, "claude"))
        self.assertEqual(self.family(server), ["claude"])

    def test_409_recovery_returns_under_its_own_name(self):
        server = self.server()
        speaker = server.register("codex")
        w = self.wrapper(server)
        self.assertEqual(w.registered_name(), "claude")
        self.active(server, "claude")
        self.kill_token(server, "claude")
        poll_until(lambda: "Recovered as @claude" in w.log(), 20, "recovery under the same name")
        self.active(server, "claude")
        self.assertEqual(self.family(server), ["claude"])
        server.send(speaker["token"], "@claude after recovery")
        poll_until(lambda: WAKE in w.keys_log(), 15, "the recovered wrapper to deliver")

    # --- groups 8, 11, 15 ----------------------------------------------------------------------

    def test_recovery_never_takes_another_name_and_holds_the_queue(self):
        server = self.server()
        speaker = server.register("codex")
        a = self.wrapper(server)
        self.assertEqual(a.registered_name(), "claude")
        self.active(server, "claude")
        self.kill_token(server, "claude")
        code, b = api(server.base_url, "POST", "/api/register",
                      body={"base": "claude", "name": "claude"})
        self.assertEqual((code, b.get("name")), (200, "claude"))
        poll_until(lambda: "cannot re-register as claude" in a.log() and "name_in_use" in a.log(),
                   20, "A's recovery to be refused, visibly")
        self.assertEqual(self.family(server), ["claude"], "A must not take another name")

        server.send(speaker["token"], "@claude held while A is fenced")
        queue = server.queue_path("claude")
        poll_until(lambda: queue.exists() and queue.stat().st_size > 0, 10, "the wake to be queued")
        held = queue.read_text("utf-8")
        assert_stable(lambda: (WAKE in a.keys_log(), queue.read_text("utf-8")), 6,
                      "A (fenced) must neither inject nor acknowledge")
        self.assertEqual(queue.read_text("utf-8"), held)

        code, _ = api(server.base_url, "POST", "/api/deregister/claude", token=b["token"])
        self.assertEqual(code, 200)
        poll_until(lambda: "Recovered as @claude" in a.log(), 20, "A to get its own name back")
        poll_until(lambda: WAKE in a.keys_log(), 15, "the held wake to be delivered after recovery")
        self.assertEqual(self.family(server), ["claude"])

    # --- group 11 (F1) with two real wrappers ----------------------------------------------------

    def test_two_real_wrappers_only_the_owner_injects(self):
        server = self.server()
        speaker = server.register("codex")
        a = self.wrapper(server)
        self.assertEqual(a.registered_name(), "claude")
        self.active(server, "claude")
        a.proc.send_signal(signal.SIGSTOP)          # A cannot react until B holds the name
        try:
            self.kill_token(server, "claude")
            b = self.wrapper(server, "--name", "claude")
            self.assertEqual(b.registered_name(), "claude")
            self.active(server, "claude")
        finally:
            a.proc.send_signal(signal.SIGCONT)
        poll_until(lambda: "cannot re-register as claude" in a.log(), 20, "A's recovery refused")

        before = len(self.journal(server))
        server.send(speaker["token"], "@claude one wake, two readers")
        poll_until(lambda: WAKE in b.keys_log(), 15, "the owner (B) to inject")
        assert_stable(lambda: WAKE in a.keys_log(), 4, "the stale reader (A) must not inject")
        self.assertFalse(WAKE in a.keys_log())
        records = self.journal(server)[before:]
        self.assertEqual([r["outcome"] for r in records], ["transport-accepted"],
                         "exactly one acknowledgement, by the owner")

    # --- groups 2, 4, 5 (O1, O2) ------------------------------------------------------------------

    def test_sibling_leaving_with_a_stale_mapping_changes_nothing_for_the_survivor(self):
        server = self.server(renames={"claude": "claude-2"})
        speaker = server.register("codex")
        a = self.wrapper(server)
        self.assertEqual(a.registered_name(), "claude")
        self.active(server, "claude")
        b = self.wrapper(server)
        self.assertEqual(b.registered_name(), "claude-2")
        self.active(server, "claude-2")
        b.stop()   # SIGTERM: no deregister call, so it leaves by the server's crash timeout (O2's path)
        poll_until(lambda: "claude-2" not in server.status(), 45, "the sibling to leave")
        assert_stable(lambda: "Identity updated" in a.log(), 6,
                      "the survivor's identity must not move")
        self.assertNotIn("Identity updated", a.log())
        self.assertEqual(self.family(server), ["claude"])
        server.send(speaker["token"], "@claude after the sibling left")
        poll_until(lambda: WAKE in a.keys_log(), 15, "the survivor to keep receiving its wakes")
        self.assertFalse(server.queue_path("claude-2").exists()
                         and server.queue_path("claude-2").stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
