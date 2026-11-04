"""ad-519 job-end continuity: a real tmux pane, the real watcher, a queue line written the way launch-notify's is.

Private tmux server only: the test refuses to type unless the socket lives under its own temporary directory.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wrapper
import wrapper_unix
from agents import AgentTrigger
from harness import Server, Wrapper, poll_until

STANDIN = Path(__file__).resolve().parent / "harness" / "standin_cli.py"
IDENTITY = "f" * 32
SESSION = "ad519test"


@unittest.skipIf(sys.platform == "win32", "tmux is unix-only")
@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class JobEndContinuityTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ad519t.", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.saved = {k: os.environ.get(k) for k in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}
        self.addCleanup(self._restore_env)
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        os.environ["TMUX_TMPDIR"] = self.base
        self.state = Path(self.base) / "state"
        self.events = Path(self.base) / "events"
        self.state.write_text("dialog")
        self.queue = Path(self.base) / "tester_queue.jsonl"
        started = subprocess.run(
            ["tmux", "new-session", "-d", "-s", SESSION, "-x", "120", "-y", "30",
             f"{sys.executable} -I {STANDIN} {self.state} {self.events}"], capture_output=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], capture_output=True)
        socket = subprocess.run(["tmux", "display-message", "-p", "-t", SESSION, "#{socket_path}"],
                                capture_output=True, text=True).stdout.strip()
        self.assertTrue(os.path.realpath(socket).startswith(os.path.realpath(self.base)), socket)
        self._wait(lambda: self.events.exists() and "ready" in self.events.read_text(), 10, "stand-in up")

    def _restore_env(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _wait(self, predicate, seconds, what):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for " + what)

    def _events(self, kind=None):
        rows = [json.loads(line) for line in self.events.read_text().splitlines()]
        return [row for row in rows if kind is None or row["kind"] == kind]

    def _start_watcher(self):
        def inject_fn(text):
            return wrapper_unix.inject(text, tmux_session=SESSION, delay=0.1, provider="claude")
        inject_fn.hold_check = wrapper_unix.settled_hold_check(SESSION, "claude")
        stop = threading.Event()

        def identity():
            if stop.is_set():
                raise SystemExit  # the watcher catches Exception only: this ends the thread
            return "tester", self.queue
        thread = threading.Thread(
            target=wrapper._queue_watcher,
            args=(identity, inject_fn),
            kwargs={"server_port": 9, "agent_name": "tester", "get_identity_id_fn": lambda: IDENTITY},
            daemon=True)
        thread.start()
        # A watcher left running would call time.sleep inside later tests that patch it.
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)

    def test_a_job_end_wake_waits_through_dialog_work_and_typing_then_arrives_once(self):
        registry = SimpleNamespace(resolve_identity=lambda identity: {"name": "tester", "state": "active"})
        trigger = AgentTrigger(registry, self.base)
        answer = trigger.trigger_identity(IDENTITY, "local-coder job ended: JOB-END-WAKE", message="local-coder: job ended")
        self.assertEqual(answer, {"status": "queued", "target": "tester"})
        queued = self.queue.read_bytes()
        self._start_watcher()

        for state in ("dialog", "working", "typed"):
            self.state.write_text(state)
            time.sleep(3)
            self.assertEqual(self._events("bytes"), [], f"input reached the pane during {state}")
            self.assertEqual(self.queue.read_bytes(), queued, f"queue changed during {state}")
            hold = json.loads(self.queue.with_suffix(".hold.json").read_text())
            self.assertIn(hold["reason"], (state, "dialog", "working", "typed", "no-prompt"))
        self.assertEqual(self._events("approved"), [])

        self.state.write_text("idle")
        self._wait(lambda: self._events("submitted"), 10, "the wake after the pane went idle")
        submitted = self._events("submitted")
        self.assertIn("JOB-END-WAKE", submitted[0]["text"])
        self._wait(lambda: self.queue.read_text() == "", 5, "the queue to be acknowledged")
        self.assertFalse(self.queue.with_suffix(".hold.json").exists())
        self.assertFalse(self.queue.with_suffix(".failed.jsonl").exists())
        self.assertIn("transport-accepted", self.queue.with_suffix(".delivery.jsonl").read_text())
        time.sleep(3)
        self.assertEqual(len(self._events("submitted")), 1, "exactly one delivery")
        self.assertEqual(self._events("approved"), [])


@unittest.skipIf(sys.platform == "win32", "tmux is unix-only")
class RealServerAndWrapperTests(unittest.TestCase):
    """The real run.py server and the real wrapper.py process, with the fake tmux the ready-gate suite uses."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.server = Server(self.tmp)
        self.addCleanup(self.server.stop)

    def test_the_notice_posts_through_the_existing_route_and_wakes_nobody(self):
        other = self.server.register("codex")
        mine = self.server.register("claude")
        ok = wrapper._post_hold_notice(self.server.port, mine["token"], "general", "[wrapper for claude] a test notice")
        self.assertTrue(ok)
        texts = [m["text"] for m in self.server.messages(mine["token"])]
        self.assertIn("[wrapper for claude] a test notice", texts)
        time.sleep(1)
        self.assertFalse(self.server.read_queue(other["name"]), "the notice must not wake another agent")
        self.assertFalse(wrapper._post_hold_notice(self.server.port, "not-a-token", "general", "refused"))

    def test_a_real_wrapper_holds_at_a_dialog_then_delivers_once(self):
        """wrapper.py names the provider itself: a wrong or absent name would hold for ever or type at once."""
        agent = Wrapper(self.tmp, self.server, scenario=["READY> yes"], gated=True)
        self.addCleanup(agent.stop)
        name = agent.registered_name()
        (agent.fake_dir / "wake-screen").write_text("Do you want to proceed?\n❯ 1. Yes\n  2. No\n", "utf-8")
        poll_until(lambda: self.server.status().get(name, {}).get("state") == "active", 15, "ready gate to release")
        entry = self.server.write_queue_entry(name, text="must wait")
        hold_file = self.server.queue_path(name).with_suffix(".hold.json")
        poll_until(hold_file.exists, 10, "the hold record")
        time.sleep(3)
        self.assertEqual(agent.keys_log(), "", "nothing typed at the dialog")
        self.assertEqual(self.server.read_queue(name), entry, "queue untouched while held")
        self.assertEqual(json.loads(hold_file.read_text())["reason"], "dialog")
        (agent.fake_dir / "wake-screen").unlink()
        poll_until(lambda: "Enter" in agent.keys_log(), 15, "delivery once idle")
        poll_until(lambda: not self.server.read_queue(name), 10, "the acknowledgement")
        self.assertEqual(agent.keys_log().count("Enter"), 1)
        trace = self.server.queue_path(name).with_suffix(".delivery.jsonl").read_text()
        self.assertIn("transport-accepted", trace)
        self.assertFalse(hold_file.exists())


if __name__ == "__main__":
    unittest.main()
