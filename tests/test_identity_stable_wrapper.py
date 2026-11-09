"""Stable identities (ad-e5gz, proposal revision 2): the wrapper side.

  group 11  ownership fence on the queue reader (F1, I5);
  group 13  named registration only against a server that declares the contract (F3), and a
            returned name that differs from the requested one is never adopted;
  group 15  queue disposition at start: nothing is erased without a copy (F5, I6).
Plus the 409-recovery and --name building blocks (groups 3, 8 and 14 on the wrapper side).
"""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wrapper
from agents import AgentTrigger


class StopWatcher(BaseException):
    pass


class StubServer:
    """A loopback HTTP server that records every request and answers from a table.

    routes: {(method, path): (status, body_dict)}; anything else answers 404.
    """

    def __init__(self, routes):
        self.routes = dict(routes)
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append((self.command, self.path, body,
                                      self.headers.get("Authorization", "")))
                status, payload = stub.routes.get((self.command, self.path), (404, {"detail": "Not Found"}))
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = _answer
            do_POST = _answer

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def calls(self, method, path_prefix):
        return [r for r in self.requests if r[0] == method and r[1].startswith(path_prefix)]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def capability(version=2):
    return {("GET", "/api/register/capabilities"): (200, {"identity_contract": version})}


# --- Group 13 (wrapper side): capability before a named registration ---------------------------

class NamedRegistrationTests(unittest.TestCase):
    def serve(self, routes):
        stub = StubServer(routes)
        self.addCleanup(stub.close)
        return stub

    def test_contract_constant(self):
        self.assertEqual(wrapper.IDENTITY_CONTRACT, 2)

    def test_old_server_without_the_capability_gets_no_registration(self):
        stub = self.serve({})
        self.assertEqual(wrapper._server_identity_contract(stub.port), 0)
        with self.assertRaises(wrapper.RegistrationRefused) as caught:
            wrapper._register_named(stub.port, "claude", None, False, "claude")
        self.assertEqual(caught.exception.code, "server_unsupported")
        self.assertEqual(stub.calls("POST", "/api/register"), [])

    def test_unreachable_server_counts_as_no_capability(self):
        stub = self.serve({})
        port = stub.port
        stub.close()
        self.assertEqual(wrapper._server_identity_contract(port), 0)

    def test_new_server_grants_the_requested_name(self):
        stub = self.serve({**capability(),
                           ("POST", "/api/register"): (200, {"name": "claude", "token": "t1",
                                                             "identity_id": "a" * 32, "slot": 1})})
        reg = wrapper._register_named(stub.port, "claude", None, False, "claude")
        self.assertEqual(reg["name"], "claude")
        sent = json.loads(stub.calls("POST", "/api/register")[0][2])
        self.assertEqual(sent["name"], "claude")
        self.assertEqual(sent["base"], "claude")

    def test_refusal_carries_the_servers_code(self):
        stub = self.serve({**capability(),
                           ("POST", "/api/register"): (409, {"error": "name_in_use"})})
        with self.assertRaises(wrapper.RegistrationRefused) as caught:
            wrapper._register_named(stub.port, "claude", None, False, "claude")
        self.assertEqual(caught.exception.code, "name_in_use")

    def test_a_different_returned_name_is_never_adopted_and_is_released(self):
        stub = self.serve({**capability(),
                           ("POST", "/api/register"): (200, {"name": "claude-2", "token": "tok-x",
                                                             "identity_id": "b" * 32, "slot": 2}),
                           ("POST", "/api/deregister/claude-2"): (200, {"ok": True})})
        with self.assertRaises(wrapper.RegistrationRefused) as caught:
            wrapper._register_named(stub.port, "claude", None, False, "claude")
        self.assertEqual(caught.exception.code, "name_mismatch")
        released = stub.calls("POST", "/api/deregister/claude-2")
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0][3], "Bearer tok-x")

    def test_unnamed_registration_body_has_no_name(self):
        stub = self.serve({("POST", "/api/register"): (200, {"name": "claude", "token": "t",
                                                             "identity_id": "c" * 32, "slot": 1})})
        wrapper._register_instance(stub.port, "claude", None, ready_gate=False)
        sent = json.loads(stub.calls("POST", "/api/register")[0][2])
        self.assertNotIn("name", sent)


# --- Group 11 (unit): queue ownership check -----------------------------------------------------

class QueueOwnerCheckTests(unittest.TestCase):
    def serve(self, routes):
        stub = StubServer(routes)
        self.addCleanup(stub.close)
        return stub

    def test_owner_when_the_heartbeat_names_the_queue(self):
        stub = self.serve({("POST", "/api/heartbeat/claude"): (200, {"ok": True, "name": "claude"})})
        self.assertEqual(wrapper._check_queue_owner(stub.port, "claude", "tok"), "owner")
        self.assertEqual(stub.calls("POST", "/api/heartbeat/claude")[0][3], "Bearer tok")

    def test_other_when_the_server_names_someone_else(self):
        stub = self.serve({("POST", "/api/heartbeat/claude"): (200, {"ok": True, "name": "claude-2"})})
        self.assertEqual(wrapper._check_queue_owner(stub.port, "claude", "tok"), "other")

    def test_stale_on_409(self):
        stub = self.serve({("POST", "/api/heartbeat/claude"): (409, {"error": "stale_session"})})
        self.assertEqual(wrapper._check_queue_owner(stub.port, "claude", "tok"), "stale")

    def test_unreachable_when_the_server_is_down(self):
        stub = self.serve({})
        port = stub.port
        stub.close()
        self.assertEqual(wrapper._check_queue_owner(port, "claude", "tok"), "unreachable")


# --- Group 11 (unit): the fence on the queue reader ---------------------------------------------

class OwnershipFenceWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue = self.root / "claude_queue.jsonl"
        self.trigger = AgentTrigger(SimpleNamespace(get_state=lambda name: "active"), str(self.root))

    def put(self, text="wake"):
        self.trigger.trigger_sync("claude", f"user: {text}", "general")

    def watch(self, owns, polls=4, own_identity=None):
        """Run one watcher for `polls` idle polls; returns (inject calls, owner checks)."""
        calls, checks = [], []
        ticks = 0

        def sleep(seconds):
            nonlocal ticks
            if seconds == 1:
                ticks += 1
                if ticks >= polls:
                    raise StopWatcher()

        def owner(name):
            checks.append(name)
            return owns(name) if callable(owns) else owns

        def inject(prompt):
            calls.append(prompt)
            return True

        with mock.patch.object(wrapper.time, "sleep", sleep), \
             mock.patch.object(wrapper, "_fetch_role", return_value=""), \
             mock.patch.object(wrapper, "_fetch_active_rules", return_value=None):
            with self.assertRaises(StopWatcher):
                wrapper._queue_watcher(lambda: ("claude", self.queue), inject, owns_queue_fn=owner,
                                       get_identity_id_fn=(lambda: own_identity) if own_identity else None)
        return calls, checks

    def journal(self):
        path = self.queue.with_suffix(".delivery.jsonl")
        return [json.loads(l) for l in path.read_text("utf-8").splitlines()] if path.exists() else []

    def test_a_reader_that_does_not_own_the_queue_touches_nothing(self):
        self.put("for the owner")
        before = self.queue.read_bytes()
        calls, checks = self.watch(owns=False)
        self.assertEqual(calls, [])
        self.assertTrue(checks, "the reader must ask before it reads")
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertEqual(self.journal(), [])
        self.assertFalse(self.queue.with_suffix(".attempt.json").exists())
        self.assertFalse(self.queue.with_suffix(".hold.json").exists())

    def test_the_owner_delivers_once_and_journals_it(self):
        self.put("for the owner")
        calls, _ = self.watch(owns=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual([r["outcome"] for r in self.journal()], ["transport-accepted"])
        self.assertEqual(self.queue.read_text("utf-8"), "")

    def test_the_journal_row_names_the_consuming_identity(self):
        """Section 7's check: a delivery row names the instance that consumed the wake."""
        own = "5" * 32
        self.put("for the owner")
        self.watch(owns=True, own_identity=own)
        rows = self.journal()
        self.assertEqual([r["outcome"] for r in rows], ["transport-accepted"])
        self.assertEqual(rows[0].get("identity_id"), own)

    def test_stale_then_owner_each_act_only_on_their_own_turn(self):
        """F1: A (stale) and B (owner) read the same queue; only B injects and acknowledges."""
        self.put("one wake")
        a_calls, _ = self.watch(owns=False)
        b_calls, _ = self.watch(owns=True)
        a_again, _ = self.watch(owns=False)
        self.assertEqual(a_calls, [])
        self.assertEqual(len(b_calls), 1)
        self.assertEqual(a_again, [])
        self.assertEqual(len(self.journal()), 1)

    def test_one_owner_check_per_batch_not_per_idle_poll(self):
        _, idle_checks = self.watch(owns=True, polls=5)
        self.assertEqual(idle_checks, [])
        self.put("a")
        self.put("b")
        _, checks = self.watch(owns=True, polls=5)
        self.assertEqual(checks, ["claude"])

    def test_unreachable_server_leaves_the_batch_queued(self):
        self.put("while the server is down")
        before = self.queue.read_bytes()
        calls, _ = self.watch(owns=lambda name: False)
        self.assertEqual(calls, [])
        self.assertEqual(self.queue.read_bytes(), before)


# --- Group 15: queue disposition at start -------------------------------------------------------

class QueueDispositionTests(unittest.TestCase):
    OWN = "1" * 32
    RETIRED = "9" * 32

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Path(self.tmp.name) / "claude_queue.jsonl"
        self.name_lines = [json.dumps({"sender": "user", "text": f"user: n{i}", "channel": "general"}) + "\n"
                           for i in range(2)]
        self.retired_line = json.dumps({"sender": "x", "text": "x: r", "channel": "general",
                                        "prompt": "p", "identity_id": self.RETIRED}) + "\n"
        self.queue.write_text(self.name_lines[0] + self.retired_line + self.name_lines[1], "utf-8")
        self.hold = self.queue.with_suffix(".hold.json")
        self.hold.write_text(json.dumps({"since": 1.0, "noticed": True}), "utf-8")
        self.trace = self.queue.with_suffix(".delivery.jsonl")
        self.prior = json.dumps({"time": 1.0, "outcome": "transport-accepted", "attempts": 1}) + "\n"
        self.trace.write_text(self.prior, "utf-8")

    def evidence_text(self):
        return "".join(p.read_text("utf-8") for p in self.queue.parent.glob("claude_queue.startup*"))

    def test_named_return_keeps_name_lines_and_retains_retired_ones(self):
        wrapper._dispose_queue_at_start(self.queue, self.OWN, named=True)
        self.assertEqual(self.queue.read_text("utf-8"), "".join(self.name_lines))
        failed = self.queue.with_suffix(".failed.jsonl").read_text("utf-8")
        self.assertEqual(failed, self.retired_line)

    def test_every_line_is_copied_to_evidence_first(self):
        wrapper._dispose_queue_at_start(self.queue, self.OWN, named=True)
        evidence = self.evidence_text()
        for line in self.name_lines + [self.retired_line]:
            self.assertIn(line, evidence)

    def test_hold_record_and_prior_journal_untouched(self):
        hold_before = self.hold.read_bytes()
        wrapper._dispose_queue_at_start(self.queue, self.OWN, named=True)
        self.assertEqual(self.hold.read_bytes(), hold_before)
        self.assertTrue(self.trace.read_text("utf-8").startswith(self.prior))

    def test_unnamed_launch_copies_then_clears(self):
        wrapper._dispose_queue_at_start(self.queue, self.OWN, named=False)
        self.assertEqual(self.queue.read_text("utf-8"), "")
        self.assertIn(self.name_lines[0], self.evidence_text())
        self.assertEqual(self.queue.with_suffix(".failed.jsonl").read_text("utf-8"), self.retired_line)

    def test_a_line_for_the_new_identity_itself_stays(self):
        own_line = json.dumps({"sender": "s", "text": "s: own", "channel": "general",
                               "prompt": "p", "identity_id": self.OWN}) + "\n"
        with open(self.queue, "a", encoding="utf-8") as f:
            f.write(own_line)
        wrapper._dispose_queue_at_start(self.queue, self.OWN, named=False)
        self.assertEqual(self.queue.read_text("utf-8"), own_line)

    def test_missing_queue_is_a_no_op(self):
        self.queue.unlink()
        wrapper._dispose_queue_at_start(self.queue, self.OWN, named=True)
        self.assertFalse(self.queue.exists())

    def test_a_wake_arriving_after_disposal_is_delivered_once_after_readiness(self):
        wrapper._dispose_queue_at_start(self.queue, self.OWN, named=False)
        AgentTrigger(SimpleNamespace(get_state=lambda n: "active"),
                     str(self.queue.parent)).trigger_sync("claude", "user: late", "general")
        ready = [False]
        calls = []
        ticks = 0

        def sleep(seconds):
            nonlocal ticks
            if seconds == 1:
                ticks += 1
                if ticks == 3:
                    ready[0] = True
                if ticks >= 8:
                    raise StopWatcher()

        def inject(prompt):
            calls.append((ready[0], prompt))
            return True

        with mock.patch.object(wrapper.time, "sleep", sleep), \
             mock.patch.object(wrapper, "_fetch_role", return_value=""), \
             mock.patch.object(wrapper, "_fetch_active_rules", return_value=None):
            with self.assertRaises(StopWatcher):
                wrapper._queue_watcher(lambda: ("claude", self.queue), inject, ready_flag=ready,
                                       owns_queue_fn=lambda n: True)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0], "delivered only once the CLI was ready")


if __name__ == "__main__":
    unittest.main()
