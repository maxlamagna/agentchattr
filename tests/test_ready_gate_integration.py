"""Ready-gate integration suite (TD-006 T5, codex #3925.5).

End-to-end seeded controls over a REAL run.py server subprocess and REAL
wrapper.py subprocesses whose tmux is the fake-tmux PATH-shim (see
tests/harness/). Coverage contract:

  Auth matrix: for each of starting/ready/cancel: no bearer -> 403; wrong
    token -> 403; OTHER instance's valid token -> 403; correct token -> 2xx
    and the state actually changes. Plus: a cancelled instance's token is
    dead (403).
  Lifecycle:
    gated wrapper + scripted ready pane -> registry starting until pane
      ready (mention during starting => visible notice + zero queue bytes;
      a direct queue entry stays untouched >2 watcher cycles), then active
      + presence; watcher only then consumes the queue.
    scripted blocker pane -> wrapper exit 3, instance gone, immediate
      re-register reacquires the bare name (no -2), keys.log empty.
    never-ready pane + small --ready-timeout -> exit 3 "timeout", single
      new-session even with restart enabled (no restart-loop re-entry).
    session death mid-probe -> exit 3 "died".
    ready-POST failure (server killed pre-ready) -> exit 3, no local
      release (keys.log empty, queue untouched).
    later CLI restart re-gates server-side; a queue entry written between
      death and re-gate is neither consumed nor erased (also the
      watcher-monitor resurrection control: any resurrected watcher obeys
      the same gate or the entry would vanish).
    heartbeat-409: a server restart with wiped data (the box-respin event)
      makes the wrapper's token resolve nowhere -> heartbeat 409 -> the
      replacement identity re-registers GATED (observably starting), ends
      active only via the authenticated ready transition, no claude-2
      survives, and routing delivers to the replacement.
    ungated end-to-end preservation: no gate flags -> active immediately,
      watcher consumes pre-"ready", zero /api/agent/* calls in the server
      log.
"""
import json
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

from harness import (Server, Wrapper, api, assert_stable, poll_until)


def _family(status: dict, base: str = "claude") -> dict:
    """claude-family entries of an /api/status payload (drops 'paused')."""
    return {name: info for name, info in status.items()
            if isinstance(info, dict) and (name == base or name.startswith(f"{base}-"))}


@unittest.skipIf(sys.platform == "win32", "ready gate is unix-only")
class ReadyGateTransitionAuthMatrixTests(unittest.TestCase):
    """#3925.5 auth matrix over real HTTP, one shared server."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.server = Server(Path(cls.tmp.name))
        cls.addClassCleanup(cls.server.stop)
        cls.other = cls.server.register("codex")   # OTHER instance's valid token

    def setUp(self):
        self.inst = self.server.register("claude", ready_gate=True)
        self.name = self.inst["name"]
        self.token = self.inst["token"]

    def tearDown(self):
        # cancel_starting has no reservation, so cleanup never pollutes the
        # next test's register(). Active instances fall back to deregister.
        code, _ = api(self.server.base_url, "POST",
                      f"/api/agent/cancel/{self.name}", token=self.token)
        if code != 200:
            api(self.server.base_url, "POST",
                f"/api/deregister/{self.name}", token=self.token)

    def _state(self, name):
        status = self.server.status()
        info = status.get(name)
        return info.get("state") if info else None

    def _post(self, kind, name, token=None):
        return api(self.server.base_url, "POST",
                   f"/api/agent/{kind}/{name}", token=token)

    def test_starting_transition_auth_matrix(self):
        for label, token in [("no bearer", None), ("wrong token", "deadbeef"),
                             ("other instance's token", self.other["token"])]:
            code, _ = self._post("starting", self.name, token)
            self.assertEqual(code, 403, f"starting with {label} must 403")
        self.assertEqual(self._state(self.name), "starting")

        code, payload = self._post("starting", self.name, self.token)
        self.assertEqual(code, 200)
        self.assertEqual(payload.get("state"), "starting")
        self.assertEqual(self._state(self.name), "starting")

    def test_ready_transition_auth_matrix(self):
        for label, token in [("no bearer", None), ("wrong token", "deadbeef"),
                             ("other instance's token", self.other["token"])]:
            code, _ = self._post("ready", self.name, token)
            self.assertEqual(code, 403, f"ready with {label} must 403")
            self.assertEqual(self._state(self.name), "starting",
                             f"refused ready ({label}) must not change state")

        code, payload = self._post("ready", self.name, self.token)
        self.assertEqual(code, 200)
        self.assertEqual(payload.get("state"), "active")
        status = self.server.status()
        self.assertEqual(status[self.name]["state"], "active")
        self.assertTrue(status[self.name]["available"],
                        "ready must establish presence (available)")

    def test_cancel_transition_auth_matrix_and_dead_token(self):
        for label, token in [("no bearer", None), ("wrong token", "deadbeef"),
                             ("other instance's token", self.other["token"])]:
            code, _ = self._post("cancel", self.name, token)
            self.assertEqual(code, 403, f"cancel with {label} must 403")
            self.assertEqual(self._state(self.name), "starting")

        code, _ = self._post("cancel", self.name, self.token)
        self.assertEqual(code, 200)
        self.assertIsNone(self._state(self.name), "cancelled instance must be gone")

        code, _ = self._post("starting", self.name, self.token)
        self.assertEqual(code, 403, "a cancelled instance's token must be dead")


@unittest.skipIf(sys.platform == "win32", "ready gate is unix-only")
class GatedWrapperLifecycleTests(unittest.TestCase):
    """Full wrapper lifecycle against scripted panes. Server per test."""

    def _server(self) -> Server:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = Server(Path(tmp.name))
        self.addCleanup(server.stop)
        self._tmp = Path(tmp.name)
        return server

    def test_gate_holds_mentions_and_queue_until_ready_pane(self):
        server = self._server()
        speaker = server.register("codex")
        wrapper = Wrapper(self._tmp, server,
                          scenario=["booting 1", "booting 2", "booting 3",
                                    "booting 4", "booting 5", "READY> ok"])
        self.addCleanup(wrapper.stop)

        name = wrapper.registered_name()
        self.assertEqual(name, "claude")
        poll_until(lambda: name in server.status(), 10,
                   "instance visible in status")
        status = server.status()
        self.assertEqual(status[name]["state"], "starting")
        self.assertFalse(status[name]["available"],
                         "a starting instance must never show available")

        # Mention while starting: visible notice, zero queue bytes (D1.4).
        server.send(speaker["token"], "@claude are you up?")
        poll_until(
            lambda: any("your mention was not delivered" in m.get("text", "")
                        and m.get("sender") == "system"
                        for m in server.messages(speaker["token"])),
            5, "visible undelivered-notice for a starting agent")
        queue = server.read_queue(name)
        self.assertIn(queue, (None, ""),
                      "routing must write nothing for a starting agent")

        # A pre-existing queue entry stays untouched while the gate is held
        # (>2 watcher poll cycles; the watcher polls every 1s).
        server.write_queue_entry(name, text="held entry")
        assert_stable(lambda: server.read_queue(name), 2.2,
                      "queue file while the gate is held")

        # Pane goes ready -> active + presence, watcher consumes the queue.
        poll_until(lambda: server.status()[name]["state"] == "active",
                   10, "ready pane to activate the instance")
        self.assertTrue(server.status()[name]["available"])
        poll_until(lambda: server.read_queue(name) in (None, ""),
                   10, "watcher to consume the queue after release")
        poll_until(lambda: "use mcp to read #general" in wrapper.keys_log(),
                   10, "held entry to be injected after release")

    def test_blocker_screen_fails_closed_and_name_is_reacquirable(self):
        server = self._server()
        wrapper = Wrapper(self._tmp, server,
                          scenario=["Log in to continue please", "idle"],
                          blockers=("login=Log in",), ready_timeout=10,
                          no_restart=False)
        self.addCleanup(wrapper.stop)

        rc = wrapper.wait_exit(15)
        self.assertEqual(rc, 3)
        self.assertIn("READY-GATE FAILED (blocker:login)", wrapper.log())
        self.assertEqual(wrapper.keys_log(), "", "nothing may be injected")
        self.assertEqual(len(wrapper.calls("new-session")), 1,
                         "gate failure must not re-enter the restart loop")
        poll_until(lambda: "claude" not in server.status(), 5,
                   "failed instance to be cancelled server-side")

        # Immediate same-base relaunch reacquires the bare name: no -2.
        again = server.register("claude", ready_gate=True)
        self.assertEqual(again["name"], "claude")
        code, _ = api(server.base_url, "POST", "/api/agent/cancel/claude",
                      token=again["token"])
        self.assertEqual(code, 200)

    def test_unknown_screen_times_out_and_exits_restart_loop(self):
        server = self._server()
        wrapper = Wrapper(self._tmp, server, scenario=["mystery meat screen"],
                          ready_timeout=1, no_restart=False)
        self.addCleanup(wrapper.stop)

        rc = wrapper.wait_exit(15)
        self.assertEqual(rc, 3)
        self.assertIn("READY-GATE FAILED (timeout)", wrapper.log())
        self.assertEqual(wrapper.keys_log(), "")
        self.assertEqual(len(wrapper.calls("new-session")), 1,
                         "timeout must exit, never the restart loop")
        poll_until(lambda: "claude" not in server.status(), 5,
                   "timed-out instance to be cancelled server-side")

        again = server.register("claude", ready_gate=True)
        self.assertEqual(again["name"], "claude")
        api(server.base_url, "POST", "/api/agent/cancel/claude",
            token=again["token"])

    def test_session_death_mid_probe_fails_closed(self):
        server = self._server()
        wrapper = Wrapper(self._tmp, server,
                          scenario=["booting", "@DIE", "dead"],
                          ready_timeout=10, no_restart=False)
        self.addCleanup(wrapper.stop)

        rc = wrapper.wait_exit(15)
        self.assertEqual(rc, 3)
        self.assertIn("READY-GATE FAILED (died)", wrapper.log())
        self.assertEqual(wrapper.keys_log(), "")
        self.assertEqual(len(wrapper.calls("new-session")), 1)
        poll_until(lambda: "claude" not in server.status(), 5,
                   "dead-session instance to be cancelled server-side")

        again = server.register("claude", ready_gate=True)
        self.assertEqual(again["name"], "claude")
        api(server.base_url, "POST", "/api/agent/cancel/claude",
            token=again["token"])

    def test_ready_post_failure_never_releases_locally(self):
        server = self._server()
        wrapper = Wrapper(self._tmp, server,
                          scenario=["booting 1", "booting 2", "booting 3",
                                    "booting 4", "READY> yes"],
                          ready_timeout=30)
        self.addCleanup(wrapper.stop)

        name = wrapper.registered_name()
        poll_until(lambda: server.status().get(name, {}).get("state") == "starting",
                   10, "instance to be starting before the server hangs")
        entry = server.write_queue_entry(name, text="must survive")
        # Hung server: the ready POST fails only after its client timeout,
        # so a wrongly ordered release would have seconds to inject.
        server.suspend()
        self.addCleanup(server.resume)
        poll_until(lambda: "READY-GATE FAILED (ready-post" in wrapper.log(),
                   20, "ready POST to fail against the hung server")
        # Resume so the follow-up cancel POST completes (and quickly).
        server.resume()

        rc = wrapper.wait_exit(10)
        self.assertEqual(rc, 3)
        self.assertEqual(wrapper.keys_log(), "",
                         "a failed ready POST must never release injection")
        self.assertEqual(server.read_queue(name), entry,
                         "queue must be untouched when the gate never released")
        poll_until(lambda: name not in server.status(), 5,
                   "fail path to cancel the registration once reachable")

    def test_cli_restart_regates_and_preserves_interim_queue(self):
        server = self._server()
        speaker = server.register("codex")
        # ready_timeout must exceed one full heartbeat period (5s): the
        # second launch's starting window then provably overlaps a heartbeat
        # iteration, so a proven-ready latch wrongly surviving the CLI death
        # WOULD re-assert ready mid-probe and eat the interim entry.
        wrapper = Wrapper(self._tmp, server,
                          scenario=["READY> first", "second boot hangs"],
                          ready_timeout=7, no_restart=False)
        self.addCleanup(wrapper.stop)

        name = wrapper.registered_name()
        poll_until(lambda: server.status().get(name, {}).get("state") == "active",
                   10, "first launch to gate through to active")

        # Prove routing+watcher deliver while ready (real routed mention).
        server.send(speaker["token"], "@claude ping before restart")
        poll_until(lambda: "use mcp to read #general" in wrapper.keys_log(),
                   10, "routed mention to be injected while ready")
        # inject() is two send-keys calls (text, then Enter after a delay);
        # snapshot only once the injection is complete.
        keys_before = poll_until(
            lambda: (log := wrapper.keys_log()).endswith("Enter\n") and log,
            5, "injection to complete (trailing Enter)")

        # CLI dies -> restart loop -> the second launch must re-gate; a queue
        # entry written between death and re-gate must survive untouched.
        wrapper.kill_session(name)
        poll_until(lambda: "Restarting in" in wrapper.log(), 10,
                   "wrapper to notice the CLI death")
        victim = server.write_queue_entry(name, text="between death and re-gate")

        poll_until(lambda: server.status().get(name, {}).get("state") == "starting",
                   10, "second launch to re-enter starting server-side")
        rc = wrapper.wait_exit(20)   # second pane never matches -> timeout
        self.assertEqual(rc, 3)
        self.assertIn("READY-GATE FAILED (timeout)", wrapper.log())

        self.assertEqual(server.read_queue(name), victim,
                         "entry written between death and re-gate must be "
                         "neither consumed nor erased")
        self.assertEqual(wrapper.keys_log(), keys_before,
                         "no injection may happen after the CLI died")
        self.assertEqual(len(wrapper.calls("new-session")), 2)
        poll_until(lambda: name not in server.status(), 5,
                   "failed second launch to cancel the registration")

    def test_heartbeat_409_replacement_stays_gated_no_minus_two(self):
        server = self._server()
        wrapper = Wrapper(self._tmp, server, scenario=["READY> go"],
                          ready_timeout=30)
        self.addCleanup(wrapper.stop)

        name = wrapper.registered_name()
        poll_until(lambda: server.status().get(name, {}).get("state") == "active",
                   10, "gated wrapper to reach active")

        # Box-respin event: same port, wiped data. The wrapper's token now
        # resolves nowhere -> its next heartbeat (<=5s) 409s -> it must
        # re-register as a REPLACEMENT identity, gated.
        server.restart_wiped()

        # The replacement must be observably starting first: only the
        # heartbeat loop's authenticated ready re-assertion may activate it.
        poll_until(lambda: _family(server.status())
                   and all(i["state"] == "starting"
                           for i in _family(server.status()).values()),
                   15, "replacement identity to re-register as starting")

        def replaced_and_active():
            fam = _family(server.status())
            return (len(fam) == 1
                    and next(iter(fam.values()))["state"] == "active"
                    and fam)
        fam = poll_until(replaced_and_active, 15,
                         "replacement identity to re-activate via the gate")
        self.assertNotIn("claude-2", fam,
                         "replacement must not mint a surviving claude-2")
        replacement = next(iter(fam))
        self.assertIn(f"/api/agent/ready/{replacement}", server.log(),
                      "replacement must activate via the authenticated ready "
                      "transition, not skip the gate")

        # Routing still delivers to the replacement identity.
        speaker = server.register("codex")
        server.send(speaker["token"], "@claude ping after replacement")
        poll_until(lambda: "use mcp to read #general" in wrapper.keys_log(),
                   10, "routed mention to reach the replacement identity")

    def test_ungated_wrapper_preserves_current_flow(self):
        server = self._server()
        speaker = server.register("codex")
        wrapper = Wrapper(self._tmp, server, gated=False,
                          scenario=["just a normal prompt"])
        self.addCleanup(wrapper.stop)

        name = wrapper.registered_name()
        status = poll_until(lambda: name in server.status() and server.status(),
                            10, "ungated wrapper to register")
        self.assertEqual(status[name]["state"], "active",
                         "ungated registration must be active immediately")

        server.send(speaker["token"], "@claude ungated ping")
        poll_until(lambda: "use mcp to read #general" in wrapper.keys_log(),
                   10, "ungated watcher to consume and inject pre-'ready'")
        self.assertNotIn("/api/agent/", server.log(),
                         "ungated runs must never call the gate endpoints")


if __name__ == "__main__":
    unittest.main()
