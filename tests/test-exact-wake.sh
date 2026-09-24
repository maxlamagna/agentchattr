#!/usr/bin/env bash
# tests/test-exact-wake.sh - exact-instance wake through the strict identity_id mode.
#
# The grading container has python3 (Debian bookworm, 3.11) and the standard
# library only: no FastAPI/Starlette/uvicorn/pydantic/mcp, no network except
# loopback. So this suite NEVER imports app.py or mcp_bridge.py; it drives
# registry.py, agents.py and wrapper.py as plain child processes and asserts on
# their return values, their queue files and the wrapper's own inject path.
#
# K1-K9 are the exact-wake contract. P1-P3 are positive controls that are green
# on the pre-change tree, so a broken harness fails loudly instead of turning
# every K control green-by-invisibility.
set -uo pipefail

SEED_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SEED_TMP="$(mktemp -d)" || exit 1
export SEED_REPO SEED_TMP
cleanup() { rm -rf "$SEED_TMP"; }
trap cleanup EXIT

PY=python3
DRIVER="$SEED_TMP/driver.py"
cat > "$DRIVER" <<'PYEOF'
"""One control per invocation: driver for tests/test-exact-wake.sh."""
import ast
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = os.environ["SEED_REPO"]
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SEED_TMP = Path(os.environ["SEED_TMP"])

# Well-formed 32-hex identities that no test ever registers.
VALID = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def finish():
    for m in FAILS:
        print("  detail: %s" % m)
    sys.exit(1 if FAILS else 0)


def workdir(tag):
    d = SEED_TMP / tag
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_env(tag, family="codex"):
    from registry import RuntimeRegistry
    from agents import AgentTrigger
    d = workdir(tag)
    reg = RuntimeRegistry(data_dir=str(d))
    reg.seed({family: {"label": "Codex", "color": "#888888"}})
    return d, reg, AgentTrigger(reg, data_dir=str(d))


def queue_files(d):
    return {p.name: p.read_bytes() for p in Path(d).glob("*_queue.jsonl")}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def queue_line(prompt, identity=None):
    entry = {"sender": "Max", "text": "Max: @codex you are mentioned", "time": "10:00:00",
             "channel": "general", "prompt": prompt}
    if identity is not None:
        entry["identity_id"] = identity
    return json.dumps(entry) + "\n"


def watcher_harness(tag, own_identity):
    """Run wrapper._queue_watcher against a temp queue file, recording injections.

    server_port points at a port where nothing listens, so the watcher's role
    and rules fetches fail fast and harmlessly. own_identity None means "do not
    pass the identity getter at all" (today's signature)."""
    import wrapper
    d = workdir(tag)
    qf = d / "codex-1_queue.jsonl"
    qf.write_text("", "utf-8")
    calls = []
    lock = threading.Lock()

    def inject_fn(text):
        with lock:
            calls.append(text)

    kwargs = {"server_port": free_port(), "agent_name": "codex", "get_token_fn": lambda: ""}
    if own_identity is not None:
        kwargs["get_identity_id_fn"] = lambda: own_identity
    thread = threading.Thread(target=wrapper._queue_watcher,
                              args=(lambda: ("codex-1", qf), inject_fn),
                              kwargs=kwargs, daemon=True)
    thread.start()
    return qf, calls


def deliver(qf, payload, timeout=10):
    for _ in range(2):
        with open(qf, "a", encoding="utf-8") as fh:
            fh.write(payload)
        if wait_until(lambda: qf.stat().st_size == 0, timeout):
            return True
    return False


def k1():
    d, reg, trig = new_env("K1")
    target = reg.register("codex")
    decoy = reg.register("codex")
    tname = decoy.get("_renamed_slot1", {}).get("new")
    check(tname == "codex-1", "K1 unexpected slot-1 rename info %r" % (decoy.get("_renamed_slot1"),))
    res = trig.trigger_identity(target["identity_id"], "wake and check the job",
                               message="Max: @codex you are mentioned")
    check(res == {"status": "queued", "target": "codex-1"}, "K1 result %r" % (res,))
    names = sorted(queue_files(d))
    check(names == ["codex-1_queue.jsonl"],
          "K1 queue files %r: a family-wide wake happened" % (names,))
    lines = (d / "codex-1_queue.jsonl").read_text("utf-8").splitlines()
    check(len(lines) == 1, "K1 line count %d" % len(lines))


def k2():
    d, reg, trig = new_env("K2")
    target = reg.register("codex")
    decoy = reg.register("codex")
    tid = target["identity_id"]
    r1 = trig.trigger_identity(tid, "first wake prompt", message="Max: hi")
    check(r1 == {"status": "queued", "target": "codex-1"}, "K2 after the slot-1 rename: %r" % (r1,))
    check(len((d / "codex-1_queue.jsonl").read_text("utf-8").splitlines()) == 1,
          "K2 wake did not land in codex-1_queue.jsonl")
    renamed = reg.rename("codex-1", "codex-alpha")
    check(isinstance(renamed, dict), "K2 rename failed: %r" % (renamed,))
    r2 = trig.trigger_identity(tid, "second wake prompt", message="Max: hi again")
    check(r2 == {"status": "queued", "target": "codex-alpha"}, "K2 after a human rename: %r" % (r2,))
    check(len((d / "codex-alpha_queue.jsonl").read_text("utf-8").splitlines()) == 1,
          "K2 wake did not follow the identity into codex-alpha_queue.jsonl")
    check(len((d / "codex-1_queue.jsonl").read_text("utf-8").splitlines()) == 1,
          "K2 the abandoned name was written to again")


def k3():
    d, reg, trig = new_env("K3")
    target = reg.register("codex")
    decoy = reg.register("codex")
    tid = target["identity_id"]
    trig.trigger_identity(tid, "wake before leaving", message="Max: hi")
    reg.deregister("codex-1")
    check(sorted(reg.get_all_names()) == ["codex"],
          "K3 live names after the rename-back: %r" % (sorted(reg.get_all_names()),))
    check(reg.get_instance("codex")["identity_id"] == decoy["identity_id"],
          "K3 the recycled name does not hold the decoy")
    check(reg.resolve_to_instances("codex") == ["codex"],
          "K3 legacy name resolution: %r" % (reg.resolve_to_instances("codex"),))
    before = queue_files(d)
    check(reg.resolve_identity(tid) is None, "K3 resolve_identity returned a dead identity")
    res = trig.trigger_identity(tid, "wake after leaving", message="Max: hi")
    check(res == {"status": "not_live", "target": None}, "K3 result %r" % (res,))
    check(queue_files(d) == before,
          "K3 queue files changed: %r -> %r" % (sorted(before), sorted(queue_files(d))))


def k4():
    d, reg, trig = new_env("K4")
    live = reg.register("codex")
    gated = reg.register("codex", ready_gate=True)
    check(reg.get_state("codex-2") == "starting", "K4 ready-gate precondition missing")
    check(queue_files(d) == {}, "K4 queue files existed before any wake: %r" % (sorted(queue_files(d)),))
    res = trig.trigger_identity(VALID, "some prompt", message="Max: hi")
    check(res == {"status": "not_live", "target": None}, "K4 unknown identity: %r" % (res,))
    res = trig.trigger_identity(gated["identity_id"], "some prompt", message="Max: hi")
    check(res == {"status": "not_ready", "target": "codex-2"}, "K4 starting instance: %r" % (res,))
    for bad in ["codex", "codex-1", "CODEX", VALID[:31], VALID + "0",
                VALID[:-1] + "Z", VALID.upper(), ""]:
        res = trig.trigger_identity(bad, "some prompt", message="Max: hi")
        check(res == {"status": "invalid", "target": None},
              "K4 malformed identity %r -> %r" % (bad, res))
    for blank in ["", "   ", "\t\n "]:
        res = trig.trigger_identity(live["identity_id"], blank, message="Max: hi")
        check(res == {"status": "invalid", "target": None},
              "K4 blank prompt %r -> %r" % (blank, res))
    check(queue_files(d) == {}, "K4 a refusal wrote a queue file: %r" % (sorted(queue_files(d)),))
    d2, reg2, trig2 = new_env("K4b", family="claude")
    solo = reg2.register("claude")
    reg2.deregister("claude", reclaimable=True)
    reclaimable = getattr(reg2, "_reclaimable", {})
    check(any(i.identity_id == solo["identity_id"] for i in reclaimable.values()),
          "K4 reclaimable precondition not met: %r" % (sorted(reclaimable),))
    check(reg2.resolve_identity(solo["identity_id"]) is None,
          "K4 a reclaimable identity was read as live")
    res = trig2.trigger_identity(solo["identity_id"], "some prompt", message="Max: hi")
    check(res == {"status": "not_live", "target": None}, "K4 reclaimable identity: %r" % (res,))
    check(queue_files(d2) == {}, "K4 reclaimable refusal wrote: %r" % (sorted(queue_files(d2)),))


def k5():
    import agents
    fn = getattr(agents, "strict_wake_response", None)
    check(callable(fn), "K5 agents.strict_wake_response is missing")
    if not callable(fn):
        return
    got = fn({"status": "queued", "target": "codex-1"}, VALID)
    check(got == (200, {"ok": True, "queued": True, "target": "codex-1",
                        "identity_id": VALID}), "K5 queued -> %r" % (got,))
    got = fn({"status": "not_live", "target": None}, VALID)
    check(got == (404, {"ok": False, "queued": False, "error": "identity_not_live",
                        "identity_id": VALID}), "K5 not_live -> %r" % (got,))
    got = fn({"status": "not_ready", "target": "codex-1"}, VALID)
    check(got == (409, {"ok": False, "queued": False, "error": "not_ready", "target": "codex-1",
                        "identity_id": VALID}), "K5 not_ready -> %r" % (got,))
    got = fn({"status": "invalid", "target": None}, VALID)
    check(got == (400, {"ok": False, "queued": False, "error": "invalid_request"}),
          "K5 invalid -> %r" % (got,))


def k6():
    d, reg, trig = new_env("K6")
    target = reg.register("codex")
    reg.register("codex")
    res = trig.trigger_identity(target["identity_id"], "wake and check job 42",
                               message="Max: @codex you are mentioned", channel="jobs")
    check(res == {"status": "queued", "target": "codex-1"}, "K6 result %r" % (res,))
    names = sorted(queue_files(d))
    check(names == ["codex-1_queue.jsonl"], "K6 queue files %r" % (names,))
    lines = (d / "codex-1_queue.jsonl").read_text("utf-8").splitlines()
    check(len(lines) == 1, "K6 line count %d" % len(lines))
    if not lines:
        return
    entry = json.loads(lines[0])
    check(entry.get("identity_id") == target["identity_id"],
          "K6 identity_id %r" % (entry.get("identity_id"),))
    check(entry.get("prompt") == "wake and check job 42", "K6 prompt %r" % (entry.get("prompt"),))
    check(entry.get("text") == "Max: @codex you are mentioned", "K6 text %r" % (entry.get("text"),))
    check(entry.get("channel") == "jobs", "K6 channel %r" % (entry.get("channel"),))
    check(entry.get("sender") == "Max", "K6 sender %r" % (entry.get("sender"),))
    check(isinstance(entry.get("time"), str) and bool(entry.get("time")), "K6 time %r" % (entry.get("time"),))
    blob = "".join(p.read_text("utf-8") for p in Path(d).glob("*_queue.jsonl"))
    check("codex-2" not in blob, "K6 another instance's name appears in the queue files")


def k7():
    own = "7" * 32
    other = "3" * 32
    qf, calls = watcher_harness("K7", own)
    check(deliver(qf, queue_line("foreign wake prompt", other)), "K7 foreign line was never consumed")
    time.sleep(2.5)
    check(list(calls) == [], "K7 a wake for another identity was injected: %r" % (list(calls),))
    check(deliver(qf, queue_line("own wake prompt", own)), "K7 own line was never consumed")
    check(wait_until(lambda: len(calls) >= 1, 10), "K7 own wake was not injected")
    time.sleep(2.0)
    check(len(calls) == 1, "K7 own wake injected %d times" % len(calls))
    check(bool(calls) and calls[0].startswith("own wake prompt"),
          "K7 injected text %r" % (calls[:1],))
    check(deliver(qf, queue_line("legacy wake prompt")), "K7 legacy line was never consumed")
    check(wait_until(lambda: len(calls) >= 2, 10), "K7 legacy wake was not injected")
    check(len(calls) >= 2 and calls[1].startswith("legacy wake prompt"),
          "K7 legacy injected text %r" % (calls[:2],))


def k8():
    import wrapper
    fn = getattr(wrapper, "instance_wake_env", None)
    check(callable(fn), "K8 wrapper.instance_wake_env is missing")
    if not callable(fn):
        return
    got = fn(VALID, 8321, "codex-alpha")
    check(got == {"AGENTCHATTR_INSTANCE_IDENTITY": VALID,
                  "AGENTCHATTR_INSTANCE_SERVER": "http://127.0.0.1:8321",
                  "AGENTCHATTR_INSTANCE_NAME": "codex-alpha"}, "K8 env -> %r" % (got,))
    got = fn(OTHER, 9999, "codex")
    check(got == {"AGENTCHATTR_INSTANCE_IDENTITY": OTHER,
                  "AGENTCHATTR_INSTANCE_SERVER": "http://127.0.0.1:9999",
                  "AGENTCHATTR_INSTANCE_NAME": "codex"}, "K8 second env -> %r" % (got,))


def _call_names(node):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def k9():
    tree = ast.parse((Path(ROOT) / "app.py").read_text("utf-8"))
    route = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "trigger_agent_silent":
            route = node
    check(route is not None, "K9 /api/trigger-agent handler not found in app.py")
    if route is not None:
        names = _call_names(route)
        check("trigger_identity" in names, "K9 the route never calls trigger_identity")
        check("strict_wake_response" in names, "K9 the route never calls strict_wake_response")
        strict_line = None
        guard_line = None
        for sub in ast.walk(route):
            if isinstance(sub, ast.Call):
                f = sub.func
                nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
                if nm in ("trigger_identity", "strict_wake_response"):
                    strict_line = sub.lineno if strict_line is None else min(strict_line, sub.lineno)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if "agent and message required" in sub.value:
                    guard_line = sub.lineno if guard_line is None else min(guard_line, sub.lineno)
        check(strict_line is not None and guard_line is not None and strict_line < guard_line,
              "K9 the identity_id branch is not ahead of the agent/message check (strict %r vs guard %r)"
              % (strict_line, guard_line))
    wtree = ast.parse((Path(ROOT) / "wrapper.py").read_text("utf-8"))
    main_fn = None
    for node in ast.walk(wtree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_fn = node
    check(main_fn is not None, "K9 wrapper.py main() not found")
    if main_fn is None:
        return
    check("instance_wake_env" in _call_names(main_fn), "K9 main() never calls instance_wake_env")
    starts = 0
    wired = 0
    for sub in ast.walk(main_fn):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        if nm != "Thread":
            continue
        kw = {k.arg: k.value for k in sub.keywords}
        target = kw.get("target")
        if not (isinstance(target, ast.Name) and target.id == "_queue_watcher"):
            continue
        starts += 1
        keys = []
        if isinstance(kw.get("kwargs"), ast.Dict):
            keys = [k.value for k in kw["kwargs"].keys if isinstance(k, ast.Constant)]
        if any("identity" in k.lower() and "id" in k.lower() for k in keys):
            wired += 1
    check(starts >= 2, "K9 found %d _queue_watcher thread starts in main()" % starts)
    check(wired >= 2, "K9 only %d of %d watcher starts pass an identity-id getter" % (wired, starts))


def p1():
    d, reg, trig = new_env("P1")
    reg.register("codex")
    ok = trig.trigger_sync("codex", "Max: @codex hello", channel="general", prompt="legacy prompt")
    check(ok is True, "P1 trigger_sync returned %r" % (ok,))
    lines = (d / "codex_queue.jsonl").read_text("utf-8").splitlines()
    check(len(lines) == 1, "P1 line count %d" % len(lines))
    if not lines:
        return
    entry = json.loads(lines[0])
    check("identity_id" not in entry, "P1 the legacy line gained an identity_id")
    check(entry.get("prompt") == "legacy prompt", "P1 prompt %r" % (entry.get("prompt"),))
    check(entry.get("text") == "Max: @codex hello", "P1 text %r" % (entry.get("text"),))
    check(entry.get("sender") == "Max", "P1 sender %r" % (entry.get("sender"),))
    check(entry.get("channel") == "general", "P1 channel %r" % (entry.get("channel"),))


def p2():
    d, reg, trig = new_env("P2")
    reg.register("codex")
    reg.register("codex")
    check(sorted(reg.resolve_to_instances("codex")) == ["codex-1", "codex-2"],
          "P2 family expansion changed: %r" % (reg.resolve_to_instances("codex"),))
    check(reg.resolve_to_instances("codex-2") == ["codex-2"],
          "P2 exact instance name changed: %r" % (reg.resolve_to_instances("codex-2"),))


def p3():
    qf, calls = watcher_harness("P3", None)
    check(deliver(qf, queue_line("harness sanity prompt")), "P3 the harness never consumed the line")
    check(wait_until(lambda: len(calls) >= 1, 10), "P3 the harness saw no injection at all")
    check(bool(calls) and calls[0].startswith("harness sanity prompt"),
          "P3 injected text %r" % (calls[:1],))


CONTROLS = {"K1": k1, "K2": k2, "K3": k3, "K4": k4, "K5": k5, "K6": k6, "K7": k7,
            "K8": k8, "K9": k9, "P1": p1, "P2": p2, "P3": p3}

if len(sys.argv) != 2 or sys.argv[1] not in CONTROLS:
    print("  detail: unknown control %r" % (sys.argv[1:],))
    sys.exit(1)
try:
    CONTROLS[sys.argv[1]]()
except Exception as exc:
    print("  detail: %s: %s" % (type(exc).__name__, exc))
    sys.exit(1)
finish()
PYEOF

PASS_N=0
FAIL_N=0
run_control() {
  local name="$1"
  "$PY" "$DRIVER" "$name"
  local code=$?
  if [ "$code" -eq 0 ]; then
    PASS_N=$((PASS_N + 1))
    echo "PASS $name"
  else
    FAIL_N=$((FAIL_N + 1))
    echo "FAIL $name"
  fi
}

for c in K1 K2 K3 K4 K5 K6 K7 K8 K9 P1 P2 P3; do
  run_control "$c"
done

echo "TOTAL $((PASS_N + FAIL_N)) PASS $PASS_N FAIL $FAIL_N"
if [ "$FAIL_N" -ne 0 ]; then
  exit 1
fi
exit 0
# === END OF SEED SUITE ===
