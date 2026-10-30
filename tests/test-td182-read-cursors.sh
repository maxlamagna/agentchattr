#!/usr/bin/env bash
# tests/test-td182-read-cursors.sh - TD-182: an AgentChattr read cursor must never
# skip an unread message, and store.MessageStore must never reissue a message
# id (Max #9391/#9392).
#
# Reproduction first: T01 and T02 drive the real mcp_bridge chat_send/chat_read on
# top of the real store.MessageStore and print the id of the message the cursor
# skipped. Everything after them pins the fix requirements and the neighbouring
# contracts that must not move (first read, since_id, no-sender read, channel vs
# "__all__" keys, resync, job threads, identity and channel migrations, the
# persisted cursors file, the empty-read hints, the chat_send channel fallback,
# and an own send by an agent that has no cursor yet (T28).
#
# C01-C15 cover the store's own id allocation (Max #9391/#9392): an id must never
# be reissued after a delete, a clear, a channel delete, a bulk import, a restart,
# a crash or concurrent writes; an existing store must load unchanged; and every
# consumer of a message id must keep working. They drive the real MessageStore
# through the real chat tools where one exists, and through the store's own API
# where the caller is the web app rather than a tool.
#
# The grading container has python3 and the standard library only: no mcp, no
# FastAPI. mcp_bridge imports `from mcp.server.fastmcp import Context, FastMCP`
# at module level and builds two FastMCP servers at import, so this suite puts a
# stand-in for exactly that import surface on sys.path. Nothing is started and no
# port is bound: the tools are called as plain functions. The real MessageStore
# and JobStore are used, on files inside this seed's scratch directory, and
# _CURSORS_FILE is pointed at the same scratch so nothing touches
# ~/.config/agentchattr.
set -uo pipefail

SEED_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SEED_REPO PYTHONDONTWRITEBYTECODE=1

# ---- BEGIN seed-scratch-preamble (verbatim; reference: tools/local-coder/seed-scratch-preamble.sh) ----
S=""; S_INO=""
[ -z "${SEED_SCRATCH_PATH+x}" ] && [ -z "${SEED_SCRATCH_INO+x}" ] || { printf 'INCOMPLETE: scratch: a creation record reached the preamble from outside (%s); refusing to run\n' "${SEED_SCRATCH_PATH:-}"; exit 2; }
_seed_inode(){ local o; o=$(command -p ls -di -- "$1" 2>/dev/null) || return 1
  o=${o%% *}; case "$o" in ''|*[!0-9]*) return 1 ;; esac; printf '%s' "$o"; }
seed_scratch_make(){ # seed_scratch_make <prefix>: creates the one scratch dir, records it READONLY, sets S
  local prefix="${1:-}" root real d cwd tries=0 name ino
  S=""; S_INO=""
  [ -z "${SEED_SCRATCH_PATH+x}" ] || { printf 'scratch: a creation record already exists (%s): one directory per seed, and the seed never writes the record itself\n' "$SEED_SCRATCH_PATH" >&2; return 3; }
  case "$prefix" in ''|*[!A-Za-z0-9._-]*) printf 'scratch: bad prefix %s\n' "$prefix" >&2; return 3 ;; esac
  root=$(cd -- "${TMPDIR:-/tmp}" 2>/dev/null && command -p pwd -P) || { printf 'scratch: temp root unreachable: %s\n' "${TMPDIR:-/tmp}" >&2; return 3; }
  [ -n "$root" ] && [ "$root" != / ] || { printf 'scratch: temp root resolves to / or nothing\n' >&2; return 3; }
  d=""
  while [ "$tries" -lt 20 ]; do
    tries=$((tries + 1))
    name=$(printf '%s.%06d' "$prefix" "$(( (RANDOM * 32768 + RANDOM) % 1000000 ))")
    if command -p mkdir -m 700 -- "$root/$name" 2>/dev/null; then d="$root/$name"; break; fi
    [ -e "$root/$name" ] || { printf 'scratch: cannot create a directory under %s\n' "$root" >&2; return 3; }
  done
  [ -n "$d" ] || { printf 'scratch: no free name under %s\n' "$root" >&2; return 3; }
  [ -d "$d" ] && [ ! -L "$d" ] || { command -p rmdir -- "$d" 2>/dev/null; printf 'scratch: created path is not a directory: %s\n' "$d" >&2; return 3; }
  real=$(cd -- "$d" 2>/dev/null && command -p pwd -P) || { command -p rmdir -- "$d" 2>/dev/null; printf 'scratch: cannot canonicalise %s\n' "$d" >&2; return 3; }
  cwd=$(command -p pwd -P 2>/dev/null) || cwd=""
  [ "$real" != "$cwd" ] && [ "$real" != "${HOME:-}" ] && [ "$real" != / ] || { command -p rmdir -- "$d" 2>/dev/null; printf 'scratch: refusing %s\n' "$real" >&2; return 3; }
  ino=$(_seed_inode "$real") || { command -p rmdir -- "$d" 2>/dev/null; printf 'scratch: cannot read the inode of %s\n' "$real" >&2; return 3; }
  readonly SEED_SCRATCH_PATH="$real" SEED_SCRATCH_INO="$ino" || { command -p rmdir -- "$d" 2>/dev/null; printf 'scratch: cannot record %s\n' "$real" >&2; return 3; }
  S="$real"; S_INO="$ino"
  return 0
}
seed_scratch_remove(){ # removes the RECORDED directory only if it is still the one created; 3 otherwise
  local p have cwd
  [ -n "${SEED_SCRATCH_PATH:-}" ] && [ -n "${SEED_SCRATCH_INO:-}" ] || { printf 'scratch: no creation record, nothing to remove\n' >&2; return 3; }
  ( builtin unset SEED_SCRATCH_PATH ) 2>/dev/null && { printf 'scratch: refusing %s: the path record is not the readonly one seed_scratch_make writes\n' "$SEED_SCRATCH_PATH" >&2; return 3; }
  ( builtin unset SEED_SCRATCH_INO ) 2>/dev/null && { printf 'scratch: refusing %s: the inode record is not the readonly one seed_scratch_make writes\n' "$SEED_SCRATCH_PATH" >&2; return 3; }
  p="$SEED_SCRATCH_PATH"
  case "$p" in .|..|/|"${HOME:-}") printf 'scratch: refusing %s\n' "$p" >&2; return 3 ;; esac
  [ -d "$p" ] && [ ! -L "$p" ] || { printf 'scratch: not a directory (or a symlink): %s\n' "$p" >&2; return 3; }
  have=$(_seed_inode "$p") || { printf 'scratch: cannot read the inode of %s\n' "$p" >&2; return 3; }
  [ "$have" = "$SEED_SCRATCH_INO" ] || { printf 'scratch: refusing %s: replaced since creation (inode %s is now %s)\n' "$p" "$SEED_SCRATCH_INO" "$have" >&2; return 3; }
  cwd=$(command -p pwd -P 2>/dev/null) || cwd=""
  [ "$p" != "$cwd" ] || { printf 'scratch: refusing the current directory: %s\n' "$p" >&2; return 3; }
  command -p chmod -R u+rwX -- "$p" 2>/dev/null
  command -p rm -rf -- "$p" || { printf 'scratch: rm failed: %s\n' "$p" >&2; return 3; }
  [ ! -e "$p" ] || { printf 'scratch: still present after rm: %s\n' "$p" >&2; return 3; }
  return 0
}
# ---- END seed-scratch-preamble ----

seed_scratch_make td182 || { printf 'INCOMPLETE: no scratch dir\n'; exit 2; }
SEED_SCRATCH="$S"; export SEED_SCRATCH   # the record is readonly: only the hook may clear it

_scrub() {
  if seed_scratch_remove; then
    return 0
  fi
  printf 'INCOMPLETE: the scratch root %s could not be removed and was left behind\n' "$S"
  exit 1
}
trap '_scrub' EXIT

SEED_STUB="$SEED_SCRATCH/stub"
DRIVER="$SEED_SCRATCH/driver.py"
export SEED_STUB DRIVER
mkdir -p "$SEED_STUB/mcp/server/fastmcp" "$SEED_SCRATCH/control"

cat > "$SEED_STUB/mcp/__init__.py" <<'STUB_EOF'
"""Stand-in package for the `mcp` distribution (import surface only)."""
STUB_EOF

cat > "$SEED_STUB/mcp/server/__init__.py" <<'STUB_EOF'
"""Stand-in for `mcp.server`."""
STUB_EOF

cat > "$SEED_STUB/mcp/server/fastmcp/__init__.py" <<'STUB_EOF'
"""Stand-in for mcp.server.fastmcp, with only what mcp_bridge.py touches.

mcp_bridge imports Context (annotations only) and FastMCP, then calls
_create_server(), which builds FastMCP(name, host=, port=, log_level=,
instructions=) and registers each tool with server.tool()(func). No server is
started and no port is bound, so tool() must hand the function back unchanged.
"""


class Context:
    """Annotation placeholder for the real mcp Context."""


class FastMCP:
    def __init__(self, name="", **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func
        return decorator
STUB_EOF

cat > "$DRIVER" <<'PYEOF'
"""One control per invocation: driver for tests/test-td182-read-cursors.sh."""
import json
import os
import sys
import threading
from pathlib import Path

sys.dont_write_bytecode = True
for _p in (os.environ["SEED_STUB"], os.environ["SEED_REPO"]):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import mcp_bridge
from jobs import JobStore
from store import MessageStore

S = Path(os.environ["SEED_SCRATCH"])
FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def finish():
    for _f in FAILS:
        print("  detail: %s" % _f)
    sys.exit(1 if FAILS else 0)


def env(tag):
    """Fresh store + empty module state for one control (or one leg of it)."""
    d = S / "control" / tag
    d.mkdir(parents=True, exist_ok=True)
    mcp_bridge.store = MessageStore(str(d / "chat.jsonl"))
    mcp_bridge.jobs = None
    mcp_bridge.rules = None
    mcp_bridge.registry = None
    mcp_bridge.config = None
    mcp_bridge._CURSORS_FILE = d / "cursors.json"
    mcp_bridge._cursors.clear()
    mcp_bridge._last_read_channel.clear()
    mcp_bridge._last_read_job_id.clear()
    mcp_bridge._empty_read_count.clear()
    return d


def posts(sender, text, channel):
    """chat_send as that sender and return the id it got."""
    out = mcp_bridge.chat_send(sender=sender, message=text, channel=channel)
    if not out.startswith("Sent (id="):
        raise AssertionError("chat_send(%s) failed: %s" % (sender, ascii(out[:80])))
    return int(out.split("id=", 1)[1].split(")")[0])


def entries(out):
    """chat_read payload -> list of entries, or None when it is not an array."""
    if not out:
        return []
    try:
        parsed = json.loads(out)
    except Exception:
        return None
    return parsed if isinstance(parsed, list) else None


def msg_ids(out):
    """Ids of the real messages a read returned (marker entry excluded)."""
    es = entries(out)
    if es is None:
        return None
    return [e["id"] for e in es if e.get("type") != "read_more"]


def quiet(out):
    # True when a read delivered nothing: an empty array or the "No new messages"
    # text (msg_ids gives None for anything that is not an array).
    ids = msg_ids(out)
    return ids == [] or ids is None


def markers(out):
    es = entries(out)
    if es is None:
        return []
    return [e for e in es if e.get("type") == "read_more"]


def cur(sender=None, key=None):
    c = mcp_bridge._cursors
    if sender is None:
        return c
    if key is None:
        return c.get(sender)
    return c.get(sender, {}).get(key)


# --------------------------------------------------------------- reproductions

def t01():
    env("t01")
    seen = [posts("bob", "bob %d" % i, "x") for i in range(3)]
    first = mcp_bridge.chat_read(sender="alice", channel="x")
    check(msg_ids(first) == seen, "T01 setup: first read returned %s" % ascii(first[:70]))
    missed = posts("bob", "posted while alice was typing", "x")
    own = posts("alice", "alice replies", "x")
    nxt = mcp_bridge.chat_read(sender="alice", channel="x")
    got = msg_ids(nxt)
    if got is None:
        check(False, "T01 SKIPPED UNREAD id=%d: alice read up to %d, bob posted %d, alice sent %d "
                     "and her cursor moved to her own message, so the next read returned %s"
              % (missed, seen[-1], missed, own, ascii(nxt[:70])))
        return
    check(missed in got, "T01 SKIPPED UNREAD id=%d: alice's read after her own send (id=%d) "
                         "returned %s" % (missed, own, got))


def t02():
    env("t02")
    seen = [posts("bob", "setup %d" % i, "y") for i in range(2)]
    first = mcp_bridge.chat_read(sender="alice", channel="y", limit=20)
    check(msg_ids(first) == seen, "T02 setup: first read returned %s" % ascii(first[:70]))
    unread = [posts("bob", "flood %d" % i, "y") for i in range(25)]
    pages = [mcp_bridge.chat_read(sender="alice", channel="y", limit=20) for _ in range(3)]
    got = []
    for p in pages:
        got.extend(msg_ids(p) or [])
    lost = sorted(set(unread) - set(got))
    check(not lost, "T02 SKIPPED %d of %d unread messages (ids %d..%d): a backlog bigger than "
                    "limit=20 keeps only the newest 20 and the cursor moves past the rest; "
                    "never returned by any read: ids %s"
          % (len(lost), len(unread), unread[0], unread[-1], lost))


# ------------------------------------------------------- fix requirement pins

def t03():
    env("t03")
    seen = [posts("bob", "setup %d" % i, "y") for i in range(2)]
    mcp_bridge.chat_read(sender="alice", channel="y", limit=20)
    unread = [posts("bob", "flood %d" % i, "y") for i in range(25)]
    p1 = mcp_bridge.chat_read(sender="alice", channel="y", limit=20)
    p2 = mcp_bridge.chat_read(sender="alice", channel="y", limit=20)
    check(msg_ids(p1) == unread[:20], "T03 an unread backlog must be paged oldest-first: page 1 "
                                      "returned %s want %s" % (msg_ids(p1), unread[:20]))
    check(msg_ids(p2) == unread[20:], "T03 page 2 must continue where page 1 stopped: got %s want %s"
          % (msg_ids(p2), unread[20:]))
    every = (msg_ids(p1) or []) + (msg_ids(p2) or [])
    check(sorted(every) == sorted(unread), "T03 the two pages must cover every unread id exactly: %s"
          % (sorted(every),))


def t04():
    env("t04")
    seen = [posts("bob", "setup %d" % i, "y") for i in range(2)]
    mcp_bridge.chat_read(sender="alice", channel="y", limit=20)
    unread = [posts("bob", "flood %d" % i, "y") for i in range(25)]
    p1 = mcp_bridge.chat_read(sender="alice", channel="y", limit=20)
    mark = markers(p1)
    check(len(mark) == 1, "T04 a read that left %d unread behind must end with exactly one "
                          "read_more entry, got %d: %s" % (len(unread) - 20, len(mark), ascii(p1[-90:])))
    if len(mark) == 1:
        e = mark[0]
        check(entries(p1)[-1].get("type") == "read_more", "T04 the read_more entry must be last")
        check(e.get("remaining") == len(unread) - 20, "T04 read_more.remaining must say how many "
                                                      "unread are left: %s" % (e.get("remaining"),))
        check(e.get("id") == msg_ids(p1)[-1], "T04 read_more.id must be the last message actually "
                                              "returned: %s vs %s" % (e.get("id"), msg_ids(p1)[-1]))
    p2 = mcp_bridge.chat_read(sender="alice", channel="y", limit=20)
    check(markers(p2) == [], "T04 the last page must not advertise a continuation: %s" % ascii(p2[-90:]))
    p3 = mcp_bridge.chat_read(sender="alice", channel="y", limit=20)
    check(markers(p3) == [], "T04 an empty read must not advertise a continuation: %s" % ascii(p3[-90:]))


def t05():
    env("t05")
    [posts("bob", "setup %d" % i, "y") for i in range(2)]
    mcp_bridge.chat_read(sender="alice", channel="y", limit=20)
    unread = [posts("bob", "flood %d" % i, "y") for i in range(45)]
    seen_once = []
    for _ in range(4):
        got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="y", limit=20)) or []
        seen_once.extend(got)
    twice = sorted({i for i in seen_once if seen_once.count(i) > 1})
    check(not twice, "T05 consecutive ordinary reads delivered the same message twice: ids %s" % (twice,))
    check(sorted(seen_once) == sorted(unread), "T05 ordinary reads must deliver every unread message: %s"
          % (sorted(set(unread) - set(seen_once)),))


def t06():
    env("t06")
    ids = [posts("bob", "m%d" % i, "z") for i in range(10)]
    hi = mcp_bridge.store.get_by_id(ids[-1])
    lo = mcp_bridge.store.get_by_id(ids[3])
    mcp_bridge._update_cursor("alice", [hi], "z")
    check(cur("alice", "z") == ids[-1], "T06 a forward cursor move was lost: %s" % (cur("alice", "z"),))
    mcp_bridge._update_cursor("alice", [lo], "z")
    check(cur("alice", "z") == ids[-1], "T06 cursor for alice/#z moved BACKWARDS from %d to %s; a read "
                                        "that starts from an older point may not drag a cursor back"
          % (ids[-1], cur("alice", "z")))
    mcp_bridge._update_cursor("carl", [lo], "z")
    mcp_bridge._update_cursor("carl", [hi], "z")
    check(cur("carl", "z") == ids[-1], "T06 a cursor that had no value yet did not advance: %s"
          % (cur("carl", "z"),))


def t07():
    env("t07")
    pool = {}
    for ch in ("a", "b"):
        pool[ch] = [posts("bob", "%s %d" % (ch, i), ch) for i in range(5)]
    errors = []
    gate = threading.Barrier(6)

    def work(key):
        try:
            gate.wait(15)
            for mid in pool[key]:
                mcp_bridge._update_cursor("alice", [mcp_bridge.store.get_by_id(mid)], key)
        except Exception as exc:  # noqa: BLE001 - the control reports it
            errors.append("%s: %s" % (type(exc).__name__, exc))

    threads = [threading.Thread(target=work, args=(("a" if i % 2 else "b"),)) for i in range(6)]
    for t in threads:
        t.daemon = True
        t.start()
    for t in threads:
        t.join(20)
    check(not any(t.is_alive() for t in threads),
          "T07 threads updating one sender's cursor did not finish (lock contention?)")
    check(not errors, "T07 cursor updates raised %s" % (errors[:3],))
    state = cur() or {}
    check(set(state.get("alice", {})) == {"a", "b"},
          "T07 concurrent updates lost a channel key: %s" % (state,))
    for key in ("a", "b"):
        check(state.get("alice", {}).get(key) in pool[key],
              "T07 alice/#%s holds a value no thread wrote: %s" % (key, state.get("alice")))
    saved = Path(mcp_bridge._CURSORS_FILE)
    check(saved.exists(), "T07 the cursors file was never written")
    if saved.exists():
        try:
            json.loads(saved.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001
            check(False, "T07 the cursors file is not valid JSON after concurrent updates: %s" % exc)


# -------------------------------------------------- neighbouring contracts pin

def t08():
    env("t08")
    ids = [posts("bob", "m%d" % i, "x") for i in range(25)]
    out = mcp_bridge.chat_read(sender="alice", channel="x", limit=20)
    check(msg_ids(out) == ids[-20:], "T08 the first read with a sender must return the newest "
                                     "`limit` messages: got %s want %s" % (msg_ids(out), ids[-20:]))
    check(markers(out) == [], "T08 a first read must not advertise a continuation: %s" % ascii(out[-90:]))
    check(cur("alice", "x") == ids[-1], "T08 the first read must set the cursor to what it returned: %s"
          % (cur("alice", "x"),))


def t09():
    env("t09")
    ids = [posts("bob", "m%d" % i, "x") for i in range(12)]
    mcp_bridge.chat_read(sender="alice", channel="x")
    out = mcp_bridge.chat_read(sender="alice", channel="x", since_id=ids[2])
    check(msg_ids(out) == ids[3:], "T09 since_id must override the cursor and read from that point: "
                                   "got %s want %s" % (msg_ids(out), ids[3:]))


def t10():
    env("t10")
    ids = [posts("bob", "m%d" % i, "w") for i in range(10)]
    mcp_bridge.chat_read(sender="alice", channel="w")
    before = cur("alice", "w")
    mcp_bridge.store.delete(ids[7:])
    back = mcp_bridge.chat_read(sender="alice", channel="w", since_id=1)
    check(msg_ids(back) == ids[2:7], "T10 setup: the since_id read returned %s" % (msg_ids(back),))
    check(cur("alice", "w") == before, "T10 an explicit since_id moved the cursor backwards from %d to "
                                       "%s, re-opening messages alice had already consumed"
          % (before, cur("alice", "w")))
    fresh = posts("bob", "arrived after the backwards read", "w")
    check(msg_ids(mcp_bridge.chat_read(sender="alice", channel="w")) == [fresh],
          "T10 the ordinary read after a backwards read did not return exactly what is new: %s"
          % (msg_ids(mcp_bridge.chat_read(sender="alice", channel="w")),))


def t11():
    env("t11")
    ids = [posts("bob", "m%d" % i, "x") for i in range(25)]
    before = cur()
    out = mcp_bridge.chat_read(channel="x", limit=20)
    check(msg_ids(out) == ids[-20:], "T11 a read without a sender must return the newest `limit`: %s"
          % (msg_ids(out),))
    check(markers(out) == [], "T11 a read without a sender must not page: %s" % ascii(out[-90:]))
    check(cur() == before, "T11 a read without a sender created a cursor: %s (before %s)"
          % (cur(), before))
    check("alice" not in (cur() or {}), "T11 a read without a sender created a cursor for the reader: %s"
          % (cur(),))
    # A read with no sender has no cursor to continue from, so there is nothing
    # to page through: it keeps the historical newest-window answer even when it
    # passes an explicit since_id and leaves unread messages behind it.
    env("t11b")
    ids = [posts("bob", "m%d" % i, "x") for i in range(25)]
    before = cur()
    back = mcp_bridge.chat_read(channel="x", since_id=3, limit=20)
    check(msg_ids(back) == ids[-20:], "T11 a sender-less since_id read must still return the newest "
                                      "`limit`: got %s want %s" % (msg_ids(back)[:1], ids[-19:]))
    check(markers(back) == [], "T11 a sender-less since_id read must not page: %s" % ascii(back[-110:]))
    check("alice" not in (cur() or {}), "T11 a sender-less since_id read created a cursor for the "
                                        "reader: %s" % (cur(),))
    check(cur() == before, "T11 a sender-less since_id read moved a cursor: %s (before %s)"
          % (cur(), before))
    # A non-positive `limit` never meant "a page of zero": such a read returns
    # the whole window ahead of the cursor and moves it, exactly as before.
    env("t11c")
    ids = [posts("bob", "m%d" % i, "x") for i in range(3)]
    mcp_bridge.chat_read(sender="alice", channel="x")
    late = [posts("bob", "after the cursor %d" % i, "x") for i in range(2)]
    whole = mcp_bridge.chat_read(sender="alice", channel="x", limit=0)
    check(msg_ids(whole) == late, "T11 a paged read with limit=0 must return every unread message: "
                                  "got %s want %s" % (msg_ids(whole), late))
    check(markers(whole) == [], "T11 a limit=0 read must not advertise a continuation: %s"
          % ascii(whole[-110:]))
    check(cur("alice", "x") == late[-1], "T11 a limit=0 read left the cursor at %s, want %s"
          % (cur("alice", "x"), late[-1]))


def t12():
    env("t12")
    xids = [posts("bob", "x%d" % i, "x") for i in range(3)]
    yids = [posts("bob", "y%d" % i, "y") for i in range(3)]
    a = mcp_bridge.chat_read(sender="alice", channel="x")
    check(msg_ids(a) == xids, "T12 setup: channel read returned %s" % (msg_ids(a),))
    check(set(cur("alice") or {}) == {"x"}, "T12 a channel read must only move that channel's key: %s"
          % (cur("alice"),))
    b = mcp_bridge.chat_read(sender="alice")
    check(msg_ids(b) == xids + yids, "T12 the first broad read must return everything: %s" % (msg_ids(b),))
    check(set(cur("alice") or {}) == {"x", "__all__"},
          "T12 a broad read must keep its own __all__ key, separate from the channel keys: %s"
          % (cur("alice"),))
    late = posts("bob", "late in x", "x")
    c = mcp_bridge.chat_read(sender="alice", channel="x")
    check(msg_ids(c) == [late], "T12 the channel cursor must return only what is new in #x: %s" % (msg_ids(c),))
    check(cur("alice", "__all__") == yids[-1], "T12 a channel read must not advance the __all__ cursor: %s"
          % (cur("alice"),))
    d = mcp_bridge.chat_read(sender="alice")
    check(msg_ids(d) == [late], "T12 the broad read must use its own cursor, not the channel one: %s"
          % (msg_ids(d),))


def t13():
    env("t13")
    seen = [posts("bob", "m%d" % i, "x") for i in range(3)]
    mcp_bridge.chat_read(sender="alice")
    missed = posts("bob", "reply for alice", "x")
    own = posts("alice", "alice replies in #x", "x")
    broad = msg_ids(mcp_bridge.chat_read(sender="alice"))
    check(broad is not None and missed in broad, "T13 a broad read after alice's own send skipped id=%d: %s"
          % (missed, ascii(str(broad)[:80])))
    ch = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    if ch is None:
        check(False, "T13 SKIPPED UNREAD id=%d: alice's own send to #x (id=%d) set the #x cursor before "
                     "she ever read that channel, so her first read of #x found nothing" % (missed, own))
        return
    check(missed in ch, "T13 SKIPPED UNREAD id=%d: alice's first read of #x returned %s after her own "
                        "send (id=%d)" % (missed, ch, own))


def t14():
    env("t14")
    seen = [posts("bob", "m%d" % i, "p") for i in range(2)]
    mcp_bridge.chat_read(sender="alice", channel="p")
    missed = posts("bob", "unread while alice proposes", "p")
    out = mcp_bridge.chat_propose_job(sender="alice", title="Do the thing", body="why", channel="p")
    check(out.startswith("Proposed job"), "T14 setup: chat_propose_job returned %s" % ascii(out[:70]))
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="p"))
    if got is None:
        check(False, "T14 SKIPPED UNREAD id=%d: chat_propose_job moved alice's #p cursor to the "
                     "proposal card, so the message posted before it is never returned" % (missed,))
        return
    check(missed in got, "T14 SKIPPED UNREAD id=%d: the read after chat_propose_job returned %s"
          % (missed, got))


def t15():
    env("t15")
    first = [posts("bob", "m%d" % i, "r") for i in range(3)]
    mcp_bridge.chat_read(sender="alice", channel="r")
    later = [posts("bob", "n%d" % i, "r") for i in range(2)]
    rs = mcp_bridge.chat_resync(sender="alice", channel="r")
    check(msg_ids(rs) == first + later, "T15 chat_resync must return the latest `limit` messages: %s"
          % (msg_ids(rs),))
    check(cur("alice", "r") == later[-1], "T15 chat_resync must move the cursor to the latest returned "
                                          "id: cursor is %s want %s" % (cur("alice", "r"), later[-1]))
    after = mcp_bridge.chat_read(sender="alice", channel="r")
    check(after.startswith("No new messages."), "T15 after a resync an ordinary read must find nothing "
                                                "new: %s" % ascii(after[:70]))


def t16():
    d = env("t16")
    chan = [posts("bob", "c%d" % i, "x") for i in range(3)]
    mcp_bridge.jobs = JobStore(str(d / "jobs.json"))
    job = mcp_bridge.jobs.create("Files", "task", "general", "bob")
    mcp_bridge.jobs.add_message(job["id"], "bob", "in the thread")
    out = mcp_bridge.chat_read(sender="alice", job_id=job["id"])
    es = entries(out)
    check(es is not None and es and es[0].get("type") == "job_header",
          "T16 a job read must still return the job header first: %s" % ascii(str(out)[:80]))
    check(cur("alice") in (None, {}), "T16 a job read must not create a read cursor: %s" % (cur(),))
    sent = mcp_bridge.chat_send(sender="alice", message="thread reply", job_id=job["id"])
    check(sent.startswith("Sent to job"), "T16 sending into a job failed: %s" % ascii(sent[:70]))
    check(cur("alice") in (None, {}), "T16 a job send must not create a read cursor: %s" % (cur(),))
    broad = mcp_bridge.chat_read(sender="alice")
    check(msg_ids(broad) == chan, "T16 after job traffic an ordinary read must still be a first read "
                                  "of the timeline: %s" % (msg_ids(broad),))


def t17():
    env("t17")
    seen = [posts("bob", "m%d" % i, "x") for i in range(3)]
    mcp_bridge.chat_read(sender="alice", channel="x")
    mcp_bridge.migrate_identity("alice", "alice-2")
    check("alice" not in (cur() or {}), "T17 renaming an agent left the old name's cursor: %s" % (cur(),))
    check(cur("alice-2", "x") == seen[-1], "T17 renaming an agent lost its read cursor: %s" % (cur(),))
    late = posts("bob", "after the rename", "x")
    check(msg_ids(mcp_bridge.chat_read(sender="alice-2", channel="x")) == [late],
          "T17 the renamed identity re-read a backlog it had already consumed")
    check(msg_ids(mcp_bridge.chat_read(sender="alice", channel="x")) == seen + [late],
          "T17 the released name must read as a fresh identity")


def t18():
    env("t18")
    seen = [posts("bob", "m%d" % i, "x") for i in range(3)]
    mcp_bridge.chat_read(sender="alice", channel="x")
    mcp_bridge.purge_identity("alice")
    check("alice" not in (cur() or {}), "T18 deregistering an agent left its cursor behind: %s" % (cur(),))
    check(msg_ids(mcp_bridge.chat_read(sender="alice", channel="x")) == seen,
          "T18 a re-registered agent must read as new, not resume a purged cursor")


def t19():
    env("t19")
    seen = [posts("bob", "m%d" % i, "x") for i in range(3)]
    mcp_bridge.chat_read(sender="alice", channel="x")
    mcp_bridge.store.rename_channel("x", "x2")
    mcp_bridge.migrate_cursors_rename("x", "x2")
    check(set((cur("alice") or {})) == {"x2"}, "T19 renaming a channel must move its cursor key: %s"
          % (cur("alice"),))
    late = posts("bob", "after the rename", "x2")
    check(msg_ids(mcp_bridge.chat_read(sender="alice", channel="x2")) == [late],
          "T19 after a channel rename the reader got the whole backlog again: %s"
          % (msg_ids(mcp_bridge.chat_read(sender="alice", channel="x2")),))


def t20():
    env("t20")
    [posts("bob", "m%d" % i, "x") for i in range(3)]
    yids = [posts("bob", "y%d" % i, "y") for i in range(2)]
    mcp_bridge.chat_read(sender="alice", channel="x")
    mcp_bridge.chat_read(sender="alice", channel="y")
    mcp_bridge.store.delete_channel("x")
    mcp_bridge.migrate_cursors_delete("x")
    check(set((cur("alice") or {})) == {"y"}, "T20 deleting a channel must drop its cursor key: %s"
          % (cur("alice"),))
    check(cur("alice", "y") == yids[-1], "T20 deleting another channel must leave this cursor alone: %s"
          % (cur("alice"),))
    after = mcp_bridge.chat_read(sender="alice", channel="y")
    check(after.startswith("No new messages."), "T20 the surviving channel must stay caught up: %s"
          % ascii(after[:70]))


def t21():
    env("t21")
    seen = [posts("bob", "m%d" % i, "x") for i in range(3)]
    mcp_bridge.chat_read(sender="alice", channel="x")
    saved = json.loads(Path(mcp_bridge._CURSORS_FILE).read_text("utf-8"))
    check(saved.get("alice", {}).get("x") == seen[-1],
          "T21 _save_cursors did not persist the cursor: %s" % (saved,))
    mcp_bridge._cursors.clear()
    mcp_bridge._load_cursors()
    check(cur("alice", "x") == seen[-1], "T21 _load_cursors did not restore the cursor: %s" % (cur(),))
    late = posts("bob", "after the restart", "x")
    check(msg_ids(mcp_bridge.chat_read(sender="alice", channel="x")) == [late],
          "T21 after a reload the reader re-read messages it had already consumed")


def t22():
    env("t22")
    [posts("bob", "m%d" % i, "x") for i in range(2)]
    mcp_bridge.chat_read(sender="alice", channel="x")
    one = mcp_bridge.chat_read(sender="alice", channel="x")
    two = mcp_bridge.chat_read(sender="alice", channel="x")
    three = mcp_bridge.chat_read(sender="alice", channel="x")
    check(one.startswith("No new messages.") and "Do not poll" in one,
          "T22 first empty read hint changed: %s" % ascii(one[:70]))
    check(two.startswith("No new messages.") and "twice" in two,
          "T22 second empty read hint changed: %s" % ascii(two[:70]))
    check(three.startswith("No new messages.") and "STOP" in three,
          "T22 third empty read hint changed: %s" % ascii(three[:70]))
    posts("bob", "something new", "x")
    check(len(msg_ids(mcp_bridge.chat_read(sender="alice", channel="x")) or []) == 1,
          "T22 setup: the read after a new message must return it")
    reset = mcp_bridge.chat_read(sender="alice", channel="x")
    check(reset.startswith("No new messages.") and "Do not poll" in reset,
          "T22 a non-empty read must reset the empty-read counter: %s" % ascii(reset[:70]))


def t23():
    env("t23")
    mcp_bridge.chat_read(sender="alice", channel="x")
    out = mcp_bridge.chat_send(sender="alice", message="no channel given")
    check(out.startswith("Sent (id="), "T23 chat_send failed: %s" % ascii(out[:70]))
    newest = mcp_bridge.store.get_recent(1)
    check(newest and newest[0]["channel"] == "x",
          "T23 chat_send without a channel must land in the last read channel: %s"
          % (newest and newest[0].get("channel"),))
    check(mcp_bridge._last_read_channel.get("alice") == "x",
          "T23 chat_read must still record the last read channel: %s" % (mcp_bridge._last_read_channel,))


def t24():
    # Paging is driven by the position a read starts from, not by whether a
    # cursor already existed: a sender that never read and passes an explicit
    # since_id can have a backlog bigger than `limit` too.
    env("t24")
    ids = [posts("bob", "m%d" % i, "x") for i in range(25)]
    seen = []
    marker = []
    # ids[0] is deliberately not the anchor: chat_read treats since_id=0 as
    # "not given" (pre-existing), so the anchor is a plain non-zero id.
    since = ids[1]
    for page_no in range(6):
        kwargs = {"sender": "alice", "channel": "x"}
        if since is not None:
            kwargs["since_id"] = since
            since = None
        page = mcp_bridge.chat_read(**kwargs)
        if not page:
            break
        es = entries(page)
        check(es is not None, "T24 page %d was not a JSON array: %s" % (page_no + 1, ascii(page[:80])))
        if es is None:
            break
        nmsg = len([e for e in es if e.get("type") != "read_more"])
        check(nmsg <= 20, "T24 page %d returned %d messages for limit=20" % (page_no + 1, nmsg))
        check(len(es) <= nmsg + 1, "T24 page %d carried %d read_more entries"
              % (page_no + 1, len(es) - nmsg))
        seen += [e["id"] for e in es if e.get("type") != "read_more"]
        marker = [e for e in es if e.get("type") == "read_more"]
        if not marker:
            break
    missing = [i for i in ids[2:] if i not in seen]
    check(seen == ids[2:], "T24 SKIPPED UNREAD ids %s: alice read from since_id=%d with %d unread and "
                           "limit=20, so the oldest unread never reached her; first page starts at id %s, "
                           "cursor now %s" % (missing, ids[1], len(ids) - 2,
                                              seen[0] if seen else None, cur("alice", "x")))
    if seen == ids[2:]:
        check(cur("alice", "x") == ids[-1],
              "T24 cursor after the last page is %s, want %s" % (cur("alice", "x"), ids[-1]))
        check(marker == [], "T24 the last page must not carry a read_more marker: %s" % ascii(marker))


IDS = "message_ids.json"   # the file store.py keeps the next message id in


def ids_state(d):
    # The persisted next id, or a description of why there is none.
    p = Path(d) / IDS
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text("utf-8"))
    except Exception as exc:
        return "unparseable: %s" % exc
    return raw.get("next_id") if isinstance(raw, dict) else "not a json object"


def file_ids(d):
    # The ids physically present in the message file, in file order.
    out = []
    for line in (Path(d) / "chat.jsonl").read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line)["id"])
        except Exception:
            continue
    return out


def restart(d):
    # Re-create the real MessageStore from the same files and reload the
    # persisted cursors: what a server restart does.
    mcp_bridge.store = MessageStore(str(Path(d) / "chat.jsonl"))
    mcp_bridge._cursors.clear()
    mcp_bridge._load_cursors()


def t25():
    # F3: chat_resync keeps its established reset contract, and that reset is
    # what saves a reader when message ids come back from below a persisted
    # cursor. The rewind is produced the only way this code can produce it: the
    # newest messages are deleted from a log in HEAD's format (no id state) and
    # the store is re-created, which is what a restart does.
    d = env("t25")
    ids = [posts("bob", "m%d" % i, "w") for i in range(10)]      # ids 0..9
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="w"))
    check(got == ids, "T25 setup: alice's first read returned %s" % (got,))
    check(cur("alice", "w") == ids[-1], "T25 setup: alice's cursor is %s" % (cur("alice", "w"),))
    mcp_bridge.store.delete(ids[7:])                # the newest three, as the UI does
    (Path(d) / IDS).unlink(missing_ok=True)         # the log is in HEAD's format now
    restart(d)
    check(mcp_bridge.store.get_by_id(ids[-1]) is None, "T25 setup: a deleted message came back")
    check(cur("alice", "w") == ids[-1], "T25 setup: alice's cursor did not survive the restart: %s"
          % (cur("alice", "w"),))
    back = posts("bob", "posted after the restart with a rewound id", "w")
    check(back < ids[-1], "T25 setup: no id rewind was produced (new id %d, cursor %d)" % (back, ids[-1]))
    deaf = mcp_bridge.chat_read(sender="alice", channel="w")
    check(quiet(deaf), "T25 setup: an ordinary read should not reach the rewound id, got %s"
          % (ascii(str(deaf)[:60]),))
    rs = msg_ids(mcp_bridge.chat_resync(sender="alice", channel="w"))
    check(rs is not None and back in rs, "T25 chat_resync must return the latest messages, the rewound one "
          "included: got %s" % (ascii(str(rs)[:80]),))
    check(cur("alice", "w") == back, "T25 F3: chat_resync left alice's cursor at %s instead of resetting it to "
          "the latest id it returned (%d), which is its established contract" % (cur("alice", "w"), back))
    late = [posts("bob", "later %d" % i, "w") for i in (1, 2)]
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="w"))
    check(got == late, "T25 F3 DEAF: after a resync alice's ordinary read returned %s, want %s - ids %s were "
          "posted above the rewound id %d and nothing can deliver them any more: her cursor is %d and the one "
          "write that could reach it, the reset, already happened"
          % (ascii(str(got)[:60]), late, late, back, cur("alice", "w")))


def t26():
    # F4: the read_more continuation must actually continue the read, and what it
    # says about the messages it left behind must be true.
    env("t26")
    ids = [posts("bob", "m%d" % i, "x") for i in range(30)]      # ids 0..29
    mcp_bridge.chat_read(sender="alice", channel="x")             # she read it all: cursor 29
    out = mcp_bridge.chat_read(sender="alice", channel="x", since_id=1, limit=5)
    page, first, rest = msg_ids(out), ids[2:7], ids[7:]
    check(page == first, "T26 setup: since_id=1 limit=5 returned %s, want %s" % (page, first))
    mark = markers(out)
    check(len(mark) == 1, "T26 a since_id page that stopped with %d messages still ahead must advertise a "
          "continuation; the response was %s" % (len(rest), ascii(str(out)[-130:])))
    if not mark:
        return
    e, text = mark[0], str(mark[0].get("text", ""))
    check(e.get("remaining") == len(rest), "T26 the marker counts %s messages while %d were left behind "
          "(ids %d..%d)" % (e.get("remaining"), len(rest), rest[0], rest[-1]))
    check("unread" not in text, "T26 F4: the %d messages past the page were read long ago (alice's cursor is "
          "%d), so the marker may not call them unread: %s" % (len(rest), ids[-1], ascii(text)))
    check(("since_id=%d" % first[-1]) in text, "T26 F4: an ordinary chat_read uses alice's cursor %d and returns "
          "nothing, so the marker must name the call that continues this page (since_id=%d): %s"
          % (ids[-1], first[-1], ascii(text)))
    follow = mcp_bridge.chat_read(sender="alice", channel="x", since_id=e.get("since_id"),
                                 limit=e.get("limit", 0))
    check(msg_ids(follow) == ids[7:12], "T26 F4: doing exactly what the marker says must deliver the next page, "
          "got %s want %s" % (msg_ids(follow), ids[7:12]))
    check(cur("alice", "x") == ids[-1], "T26 paging below the cursor must not drag alice's cursor back from %d "
          "to %s" % (ids[-1], cur("alice", "x")))


def t27():
    # F6: what a sender's own message does to that sender's cursor. Pinned
    # behaviour: an own send moves a cursor over its own message and only when
    # nothing unread sits in front of it, each cursor key on its own. T01, T13
    # and T14 pin the missed messages coming back.
    env("t27")
    posts("bob", "bob 1", "x")
    mcp_bridge.chat_read(sender="alice", channel="x")              # alice is caught up in #x
    own = posts("alice", "reply", "x")
    out = mcp_bridge.chat_read(sender="alice", channel="x")
    check(quiet(out), "T27 F6: alice was caught up and posted herself, so her next read must not hand her own "
          "message back to her: %s" % (ascii(str(out)[:90]),))
    check(cur("alice", "x") == own, "T27 F6: an own send with nothing unread in front of it must still move the "
          "#x cursor over that message (own id %d): the cursor is %s" % (own, cur("alice", "x")))
    missed = posts("bob", "bob 2", "x")
    own2 = posts("alice", "reply again", "x")
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(got == [missed, own2], "T27 F6: a message posted since alice's last read must come back together with "
          "her own reply, got %s want %s" % (got, [missed, own2]))
    # the broad key is decided on its own
    b_y = posts("bob", "bob in another channel", "y")
    broad = msg_ids(mcp_bridge.chat_read(sender="alice"))
    check(b_y in (broad or []), "T27 setup: alice's broad read missed %d: %s" % (b_y, broad))
    own3 = posts("alice", "reply in x", "x")
    out = mcp_bridge.chat_read(sender="alice")
    check(quiet(out), "T27 F6: alice was caught up everywhere, so a broad read after her own send must not echo "
          "it back: %s" % (ascii(str(out)[:90]),))
    check(cur("alice", "__all__") == own3, "T27 F6: the broad cursor must move over the own message too: it is "
          "%s, want %d" % (cur("alice", "__all__"), own3))
    # unread in another channel blocks the broad key only
    b_y2 = posts("bob", "bob again elsewhere", "y")
    own4 = posts("alice", "another reply in x", "x")
    got = msg_ids(mcp_bridge.chat_read(sender="alice"))
    check(got == [b_y2, own4], "T27 F6: unread in another channel sits in front of alice's own message in the "
          "broad key, so a broad read must return both, got %s want %s" % (got, [b_y2, own4]))
    out = mcp_bridge.chat_read(sender="alice", channel="x")
    check(quiet(out), "T27 F6: nothing unread sat in front of alice's own message in #x, so that cursor moved "
          "over it and reading #x must not echo it back: %s" % (ascii(str(out)[:90]),))


def t28():
    # F5 (r3): an own send by an agent that has no cursor yet, in a store whose
    # first message is someone else's. A cursor key with no value counted as
    # floor 0 and get_since(0) excludes id 0, so a cursor invented by the own
    # send hides the first message in that channel and, when the broad key is
    # invented too, in every channel.
    env("t28")
    first = posts("bob", "the very first message", "x")                 # id 0
    check(first == 0, "T28 setup: the store's first message took id %d, want 0" % first)
    own = posts("alice", "alice replies, having read nothing", "x")     # id 1
    ch = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(ch is not None and first in ch, "T28 SKIPPED UNREAD id=%d: alice had never read #x, posted there "
          "(id=%d), and her next read of #x returned %s" % (first, own, ascii(str(ch)[:60])))
    broad = msg_ids(mcp_bridge.chat_read(sender="alice"))
    check(broad is not None and first in broad, "T28 SKIPPED UNREAD id=%d: alice's first broad read returned %s; "
          "her own send put a position in the broad key as well, so the first message of the store never "
          "reaches her" % (first, ascii(str(broad)[:60])))
    check(ch is not None and own in ch and own in (broad or []), "T28 a first read is the recent window from that "
          "point, so alice's own message comes with what she missed: #x %s broad %s"
          % (ascii(str(ch)[:60]), ascii(str(broad)[:60])))
    # what r2's F6 chose to keep: the echo stays gone for a sender that is caught up
    own2 = posts("alice", "alice again, now caught up", "x")
    out = mcp_bridge.chat_read(sender="alice", channel="x")
    check(quiet(out), "T28 F5: alice had just read #x and posted again with nothing unread in front of it, so "
          "her read must not echo her own message back: %s" % (ascii(str(out)[:90]),))
    check(cur("alice", "x") == own2, "T28 F5: the caught-up own-send advance must stay: the #x cursor is %s, "
          "want %d" % (cur("alice", "x"), own2))
    # a position that legitimately sits on id 0 is a position, not an absent key
    env("t28-zero")
    zero = posts("bob", "the very first message", "x")                  # id 0
    seen = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(seen == [zero], "T28 setup: alice's first read of #x returned %s, want [%d]"
          % (ascii(str(seen)[:60]), zero))
    check(cur("alice", "x") == zero, "T28 F5: a cursor that sits on id 0 must be recorded, not treated as no "
          "position: the #x cursor is %s, want %d" % (cur("alice", "x"), zero))
    nxt = posts("bob", "the second message", "x")                       # id 1
    again = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(again == [nxt], "T28 F5: reading #x again from a recorded position of 0 must return only the new "
          "message, not the recent window again: got %s want [%d]" % (ascii(str(again)[:60]), nxt))



def c01():
    # The store's own allocation: the newest messages are deleted and the server
    # restarts on the file they were written to.
    d = env("c01")
    ids = [posts("bob", "m%d" % i, "x") for i in range(10)]         # ids 0..9
    mcp_bridge.chat_read(sender="alice", channel="x")                # cursor 9, persisted
    mcp_bridge.store.delete(ids[7:])                                 # newest three deleted
    restart(d)
    fresh = mcp_bridge.store.add("bob", "posted after the restart", channel="x")
    check(fresh["id"] > ids[-1], "C01 REISSUED id=%d: ten messages were stored (highest id %d), the newest three "
          "were deleted and the store was re-created from its own file, so the next send started again above the "
          "highest surviving id and handed out %d - an id that had already reached the message file"
          % (fresh["id"], ids[-1], fresh["id"]))
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(got == [fresh["id"]], "C01 alice's cursor (%d) was persisted before the restart and the new message "
          "took id %d, so an ordinary chat_read returned %s: only a chat_resync would ever show it to her"
          % (ids[-1], fresh["id"], ascii(str(got)[:60])))
    st = ids_state(d)
    check(isinstance(st, int) and st > ids[-1], "C01 %s holds %s after the newest messages were deleted and the "
          "store restarted; it must hold a next id above every id ever issued (%d), because deleting a message "
          "may not free its id" % (IDS, st, ids[-1]))


def c02():
    d = env("c02")
    ids = [posts("bob", "m%d" % i, "x") for i in range(5)]           # 0..4
    mcp_bridge.chat_read(sender="alice", channel="x")                 # cursor 4
    mcp_bridge.store.clear()                                          # the UI's /clear
    check(mcp_bridge.store.get_recent(999) == [], "C02 setup: clear() left messages in the store")
    restart(d)
    fresh = mcp_bridge.store.add("bob", "first after a full clear", channel="x")
    check(fresh["id"] > ids[-1], "C02 REISSUED id=%d: clear() emptied the message file, so after the restart the "
          "next id came back from the surviving messages (none) and started again at %d; ids 0-%d had already "
          "reached the file and alice's cursor was at %d" % (fresh["id"], fresh["id"], ids[-1], ids[-1]))
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(got == [fresh["id"]], "C02 an ordinary chat_read by alice (cursor %d) returned %s instead of the new "
          "message id=%d" % (ids[-1], ascii(str(got)[:60]), fresh["id"]))


def c03():
    d = env("c03")
    g = [posts("bob", "g%d" % i, "general") for i in range(3)]         # 0..2
    x = [posts("bob", "x%d" % i, "x") for i in range(3)]               # 3..5, newest in #x
    mcp_bridge.chat_read(sender="alice")                                # broad cursor 5
    mcp_bridge.store.clear(channel="x")
    check(mcp_bridge.store.get_recent(999, channel="x") == [], "C03 setup: clear(channel) left #x messages")
    check(len(mcp_bridge.store.get_recent(999)) == len(g), "C03 setup: clear(channel) took other channels too")
    restart(d)
    fresh = mcp_bridge.store.add("bob", "after the channel clear", channel="general")
    check(fresh["id"] > x[-1], "C03 REISSUED id=%d: clear(channel='x') removed the newest ids %d-%d and the "
          "restart put the next id back to %d, inside the range that had reached the file and alice's cursor"
          % (fresh["id"], x[0], x[-1], fresh["id"]))
    got = msg_ids(mcp_bridge.chat_read(sender="alice"))
    check(got == [fresh["id"]], "C03 alice's broad read (cursor %d) returned %s, not the new message id=%d"
          % (x[-1], ascii(str(got)[:60]), fresh["id"]))


def c04():
    d = env("c04")
    keep = [posts("bob", "k%d" % i, "keep") for i in range(2)]         # 0..1
    drop = [posts("bob", "d%d" % i, "drop") for i in range(3)]         # 2..4
    mcp_bridge.chat_read(sender="alice", channel="keep")                # #keep cursor 1
    mcp_bridge.chat_read(sender="alice")                                 # broad cursor 4
    mcp_bridge.store.delete_channel("drop")
    check(mcp_bridge.store.get_recent(999, channel="drop") == [], "C04 setup: delete_channel left messages")
    restart(d)
    fresh = mcp_bridge.store.add("bob", "after the channel delete", channel="keep")
    check(fresh["id"] > drop[-1], "C04 REISSUED id=%d: delete_channel('drop') removed the newest ids %d-%d, so "
          "after the restart the next id was %d again - those ids had reached the file and two of alice's cursors"
          % (fresh["id"], drop[0], drop[-1], fresh["id"]))
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="keep"))
    check(got == [fresh["id"]], "C04 an ordinary chat_read by alice in #keep (cursor %d) returned %s instead of "
          "the new message id=%d" % (keep[-1], ascii(str(got)[:60]), fresh["id"]))


def c05():
    d = env("c05")
    ids = [posts("bob", "m%d" % i, "x") for i in range(6)]              # 0..5
    mcp_bridge.chat_read(sender="alice", channel="x")                    # cursor 5
    mcp_bridge.store.delete(list(ids))                                    # one batch: every message gone
    restart(d)
    check(mcp_bridge.store.get_recent(999) == [], "C05 setup: the restarted store is not empty")
    imported = [mcp_bridge.store.add("carl", "imported %d" % i, channel="x", _bulk=True) for i in range(4)]
    mcp_bridge.store.flush_bulk()
    got_ids = [m["id"] for m in imported]
    check(min(got_ids) > ids[-1], "C05 REISSUED ids %s: this store had issued ids up to %d, every message was "
          "then deleted and the store re-created from its file, so a bulk import through add(_bulk=True) and "
          "flush_bulk() started again at %d" % (got_ids, ids[-1], got_ids[0]))
    restart(d)
    fresh = mcp_bridge.store.add("carl", "after the import", channel="x")
    check(fresh["id"] > max(got_ids), "C05 after the bulk import and a restart the next id went back to %d; the "
          "import had just written ids %s" % (fresh["id"], got_ids))
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(bool(got) and got[0] > ids[-1], "C05 alice's cursor was %d before all of this and her ordinary read "
          "returned %s: nothing above her cursor reached her" % (ids[-1], ascii(str(got)[:80])))


def c06():
    # An existing store must load unchanged, including a store this code never
    # wrote (HEAD's format: a log and no id state beside it).
    d = env("c06")
    ids = [posts("bob", "m%d" % i, "x") for i in range(8)]               # 0..7
    before = file_ids(d)
    check(before == ids, "C06 setup: the message file holds %s, want %s" % (before, ids))
    raw = (Path(d) / "chat.jsonl").read_bytes()
    (Path(d) / IDS).unlink(missing_ok=True)       # a store this code never wrote
    check(not (Path(d) / IDS).exists(), "C06 setup: the id state file would not go away")
    restart(d)
    check((Path(d) / "chat.jsonl").read_bytes() == raw, "C06 the first load of an existing store rewrote the "
          "message file; message lines keep their format and content")
    check(not (Path(d) / IDS).exists(), "C06 the first load of an existing store created %s: loading must write "
          "nothing, because scripts/agentchattr-export-history.sh:167 builds a store inside the live data dir and "
          "a stale id state written there is the one way a second process could move ids backwards" % IDS)
    check([m["id"] for m in mcp_bridge.store.get_recent(999)] == before, "C06 the first load changed what the "
          "store holds: %s -> %s" % (before, [m["id"] for m in mcp_bridge.store.get_recent(999)]))
    fresh = mcp_bridge.store.add("bob", "first send after the first load", channel="x")
    check(fresh["id"] == max(before) + 1, "C06 with no id state to trust, the next id must be one above the "
          "highest id in the file (%d): got %d" % (max(before), fresh["id"]))
    st = ids_state(d)
    check(st is None or st > fresh["id"], "C06 the first load of an existing store recorded id state %s, not "
          "above the id it just handed out (%d)" % (st, fresh["id"]))


def c07():
    # An id state that is missing, empty or unusable must not change what loads,
    # and an id state ahead of the file must win.
    d = env("c07")
    ids = [posts("bob", "m%d" % i, "x") for i in range(6)]                # 0..5
    want = [m["id"] for m in mcp_bridge.store.get_recent(999)]
    p = Path(d) / IDS
    log = (Path(d) / "chat.jsonl").read_bytes()
    for label, body in (("missing", None), ("empty", ""), ("not json", "next id = 42"),
                        ("not a json object", "[7, 8]"), ("not an integer", '{"next_id": "six"}'),
                        ("negative", '{"next_id": -3}')):
        (Path(d) / "chat.jsonl").write_bytes(log)   # every leg starts from the same message file
        if body is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(body, "utf-8")
        restart(d)
        got = [m["id"] for m in mcp_bridge.store.get_recent(999)]
        check(got == want, "C07 an id state file that is %s changed what the store loads: %s vs %s"
              % (label, got, want))
        fresh = mcp_bridge.store.add("bob", "after a %s state" % label, channel="x")
        check(fresh["id"] > max(want), "C07 with the id state %s the next id went back to %d, reissuing an id "
              "that had already reached the message file (highest id there: %d)" % (label, fresh["id"], max(want)))
    hi = max(want) + 40
    p.write_text('{"next_id": %d}' % hi, "utf-8")
    restart(d)
    fresh = mcp_bridge.store.add("bob", "ahead of the file", channel="x")
    check(fresh["id"] == hi, "C07 the id state (%d) sits above the highest id in the message file (%d): the state "
          "must win, or an id whose message was deleted is handed out again - the load gave out %d"
          % (hi, max(want), fresh["id"]))


def c08():
    # Crash recovery at every write this design makes. The rule: the file wins
    # when it is ahead, the recorded state wins when it is ahead, so a crash can
    # only ever skip an id - never reissue one.
    d = env("c08")
    ids = [posts("bob", "m%d" % i, "x") for i in range(12)]               # 0..11
    p = Path(d) / IDS
    log = Path(d) / "chat.jsonl"
    lines = log.read_text("utf-8").splitlines(True)
    check(len(lines) == len(ids), "C08 setup: %d lines for %d messages" % (len(lines), len(ids)))

    def probe(want, why):
        hi = max(file_ids(d) or [-1])
        restart(d)
        fresh = mcp_bridge.store.add("bob", "probe after a crash", channel="x")
        check(fresh["id"] == want, "%s: the next id must be %d, the restart gave out %d (highest id in the "
              "message file: %d, recorded id state: %s)" % (why, want, fresh["id"], hi, ids_state(d)))

    p.write_text('{"next_id": %d}' % ids[-1], "utf-8")
    probe(ids[-1] + 1, "C08a a crash after a message append left the id state behind the file")
    p.write_text('{"next_id": %d}' % (ids[-1] + 1), "utf-8")
    log.write_text("".join(lines[:-3]), "utf-8")
    probe(ids[-1] + 1, "C08b a removal crashed after the id state was written and before the file came back "
          "without the newest ids")
    p.write_text('{"next_id": %d}' % (ids[-1] + 1), "utf-8")
    log.write_text("", "utf-8")
    probe(ids[-1] + 1, "C08c clear() crashed between the id-state write and emptying the file")
    log.write_text("".join(lines), "utf-8")
    p.write_text('{"next_id": 1}', "utf-8")
    probe(ids[-1] + 1, "C08d a crash inside flush_bulk() left the messages written and the id state stale")
    log.write_text("".join(lines) + '{"id": 99, "sender": "bob", "text": "torn', "utf-8")
    restart(d)
    check([m["id"] for m in mcp_bridge.store.get_recent(999)] == ids, "C08e a torn trailing line changed what the "
          "store loads: %s, want %s" % ([m["id"] for m in mcp_bridge.store.get_recent(999)], ids))
    p.write_text('{"next_id": %d}' % (ids[-1] + 1), "utf-8")
    probe(ids[-1] + 1, "C08e a torn trailing line is skipped exactly as HEAD skips it")


def c09():
    d = env("c09")
    out = []
    gate = threading.Lock()

    def work(n):
        for i in range(25):
            m = mcp_bridge.store.add("w%d" % n, "thread %d msg %d" % (n, i), channel="x")
            with gate:
                out.append(m["id"])

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.daemon = True
        t.start()
    for t in threads:
        t.join(60)
    check(not any(t.is_alive() for t in threads), "C09 the writer threads did not finish")
    check(len(out) == 200, "C09 only %d of 200 adds came back" % len(out))
    dup = sorted({i for i in out if out.count(i) > 1})
    check(not dup, "C09 concurrent adds issued the same id more than once: %s" % (dup[:10],))
    check(sorted(set(out)) == list(range(200)), "C09 the ids are not one clean run up to 199: %d distinct ids, "
          "highest %d" % (len(set(out)), max(out)))
    check(ids_state(d) == 200, "C09 %s holds %s while 200 ids (0..199) have been issued" % (IDS, ids_state(d)))
    hi = mcp_bridge.store.get_recent(1)[0]["id"]
    mcp_bridge.store.delete([hi])
    check(ids_state(d) == 200, "C09 deleting the newest message (id %d) moved the recorded id state backwards: %s"
          % (hi, ids_state(d)))
    restart(d)
    fresh = mcp_bridge.store.add("bob", "after the threads and a delete", channel="x")
    check(fresh["id"] == 200, "C09 after concurrent writes and deleting the newest message (id %d), the restart "
          "gave out %d; 200 ids up to 199 had already been issued" % (hi, fresh["id"]))


def c10():
    # scripts/agentchattr-export-history.sh:167 runs in the live data dir and :225
    # builds a MessageStore on the same files. That process may not move an id,
    # and it must still see every message the server wrote.
    d = env("c10")
    ids = [posts("bob", "m%d" % i, "x") for i in range(5)]                 # 0..4
    server = mcp_bridge.store
    st = ids_state(d)
    check(isinstance(st, int), "C10 %s does not exist beside the message file, so nothing records the ids this "
          "store already handed out" % IDS)
    def snapshot():     # every byte of the data dir, keyed by name
        return {q.name: q.read_bytes() for q in Path(d).iterdir()}
    before = snapshot()
    reader = MessageStore(str(Path(d) / "chat.jsonl"))
    seen = [m["id"] for m in reader.get_recent(999)]
    check(seen == ids, "C10 a second MessageStore on the same files read %s; the server had written %s"
          % (seen, ids))
    after = snapshot()
    check(after == before, "C10 constructing a second MessageStore changed files in the data dir: %s vs %s"
          % (sorted((k, ascii(v[:20])) for k, v in after.items()), sorted((k, ascii(v[:20])) for k, v in before.items())))
    check(ids_state(d) == st, "C10 the second store moved the recorded id state from %s to %s" % (st, ids_state(d)))
    more = [server.add("bob", "written while the export ran", channel="x")["id"] for _ in range(3)]
    check(min(more) > max(ids), "C10 the server reissued ids %s after a second store had read the same files"
          % (more,))
    st2 = ids_state(d)
    check(isinstance(st2, int) and st2 >= st, "C10 the recorded id state moved backwards while the server kept "
          "writing: %s -> %s" % (st, st2))
    restart(d)
    fresh = mcp_bridge.store.add("bob", "after the export", channel="x")
    check(fresh["id"] > max(more), "C10 after the second process and a restart the next id went back to %d; the "
          "server had issued ids up to %d" % (fresh["id"], max(more)))


def c11():
    # Everything that consumes a message id keeps working: store.add, a thread's
    # reply_to (app.py:1664, :2202), todos, the on_delete callback (app.py:1377),
    # update_reply_to (archive.py:291) and ordinary reads.
    d = env("c11")
    ids = [posts("bob", "m%d" % i, "x") for i in range(10)]                 # 0..9
    keep, target = ids[4], ids[9]
    uid9 = mcp_bridge.store.get_by_id(target)["uid"]
    mcp_bridge.store.update_reply_to(keep, target)
    mcp_bridge.store.add_todo(ids[3])
    seen_deletes = []
    mcp_bridge.store.on_delete(lambda gone: seen_deletes.append(list(gone)))
    mcp_bridge.chat_read(sender="alice", channel="x")                        # cursor 9
    gone = mcp_bridge.store.delete(ids[7:])
    check(gone == ids[7:], "C11 delete() returned %s, want %s" % (gone, ids[7:]))
    check(bool(seen_deletes) and seen_deletes[-1] == ids[7:], "C11 the on_delete callback got %s, want %s"
          % (seen_deletes, ids[7:]))
    restart(d)
    fresh = [mcp_bridge.store.add("bob", "new %d" % i, channel="x") for i in range(3)]
    got_ids = [m["id"] for m in fresh]
    check(min(got_ids) > target, "C11 REISSUED ids %s: three sends after a restart started again below id %d, "
          "which had already reached the message file (its uid %s)" % (got_ids, target, uid9[:8]))
    moved = mcp_bridge.store.get_by_id(target)
    check(moved is None, "C11 id %d belongs to %r after the restart, and message %d's reply_to still points at "
          "%d: a thread that silently changed target (that id's original uid was %s)"
          % (target, str((moved or {}).get("text", ""))[:40], keep, target, uid9[:8]))
    mcp_bridge.store.update_reply_to(keep, got_ids[-1])
    restart(d)
    check((mcp_bridge.store.get_by_id(keep) or {}).get("reply_to") == got_ids[-1], "C11 update_reply_to did not "
          "survive a restart: %s" % (mcp_bridge.store.get_by_id(keep),))
    check(mcp_bridge.store.get_todos().get(ids[3]) == "todo", "C11 the todo on message %d did not survive: %s"
          % (ids[3], mcp_bridge.store.get_todos()))
    todo_msgs = mcp_bridge.store.get_todo_messages("todo")
    check([m["id"] for m in todo_msgs] == [ids[3]], "C11 get_todo_messages no longer returns the todo message: %s"
          % ([m["id"] for m in todo_msgs],))
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(got == got_ids, "C11 alice's cursor was %d before the restart and her ordinary read returned %s "
          "instead of the three new messages %s" % (target, ascii(str(got)[:80]), got_ids))


def c12():
    # The archive path writes through the same store: archive.py:261 store.add
    # (_bulk) + flush_bulk, :291 update_reply_to, :355 store._rewrite.
    d = env("c12")
    ids = [posts("bob", "m%d" % i, "x") for i in range(8)]                  # 0..7
    import archive
    blob = archive.build_export(mcp_bridge.store, None, None, None)
    mcp_bridge.store.delete(ids[4:])                                         # newest four gone
    restart(d)
    rep = archive.import_archive(blob, mcp_bridge.store, None, None, None, ["general", "x"])
    check(rep.get("ok"), "C12 import_archive reported %s" % (ascii(str(rep)[:140]),))
    got = [m["id"] for m in mcp_bridge.store.get_recent(999)]
    dup = sorted({i for i in got if got.count(i) > 1})
    check(not dup, "C12 the message file holds ids %s twice: the re-import started again below the ids this "
          "store had already issued (highest %d)" % (dup, ids[-1]))
    news = [i for i in got if i not in ids[:4]]
    check(all(i > ids[-1] for i in news), "C12 the four re-imported messages took ids %s, inside the range that "
          "was already issued (highest id before the import: %d)" % (news, ids[-1]))
    check(sorted(i for i in got if i <= ids[3]) == ids[:4], "C12 the messages that survived the delete changed "
          "identity: %s" % (got,))
    restart(d)
    again = [m["id"] for m in mcp_bridge.store.get_recent(999)]
    check(len(set(again)) == len(again), "C12 after the import and a restart the file holds duplicate ids: %s"
          % (again,))
    fresh = mcp_bridge.store.add("bob", "after the import", channel="x")
    check(fresh["id"] > max(again), "C12 the next id went back to %d, below the ids the import had just written "
          "(highest %d)" % (fresh["id"], max(again)))


def c13():
    # An id is owed to the log as soon as any write makes it durable, not only
    # when chat_send appends it: archive.py:261 adds with _bulk=True and the
    # rewrite paths behind it (archive.py:291 update_reply_to, :355 _rewrite)
    # are what actually put those ids into the message file.
    d = env("c13")
    bulk = [mcp_bridge.store.add("carl", "bulk %d" % i, channel="x", _bulk=True)
            for i in range(4)]                                        # ids 0..3, in memory only
    mcp_bridge.store.update_message(bulk[0]["id"], {"text": "persisted by a rewrite path"})
    check(file_ids(d) == [0, 1, 2, 3], "C13 setup: the rewrite path wrote %s to the message file, want [0, 1, 2, 3]"
          % (file_ids(d),))
    mcp_bridge.chat_read(sender="alice", channel="x")                   # alice is caught up at 3
    check(cur("alice", "x") == 3, "C13 setup: alice's cursor is %s, want 3" % (cur("alice", "x"),))
    mcp_bridge.store.delete([3])
    check(file_ids(d) == [0, 1, 2], "C13 setup: after deleting the newest id the file holds %s" % (file_ids(d),))
    restart(d)
    fresh = mcp_bridge.store.add("carl", "after a rewrite path and a delete", channel="x")
    check(fresh["id"] > 3, "C13 REISSUED id=%d: id 3 reached the message file through a rewrite path and alice's "
          "cursor, was then deleted, and the restart handed the same id out again" % fresh["id"])
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(got == [fresh["id"]], "C13 alice's cursor was 3 and after the restart her ordinary read returned %s; the "
          "new message (id %d) must reach her without a resync" % (ascii(str(got)[:60]), fresh["id"]))


def c14():
    # A store whose only durable write was a bulk flush - the archive import shape,
    # archive.py:261 add(_bulk=True) then archive.py:355 flush_bulk/_rewrite - still
    # owes every flushed id to the id state, because a rollback to HEAD leaves the
    # old code free to delete messages and rewrite the log shorter before this code
    # runs again, and the shortened file is then all the load has to go on.
    d = env("c14")
    for i in range(4):
        mcp_bridge.store.add("carl", "imported %d" % i, channel="x", _bulk=True)   # ids 0..3
    mcp_bridge.store.flush_bulk()
    check(file_ids(d) == [0, 1, 2, 3], "C14 setup: flush_bulk wrote %s to the message file, want [0, 1, 2, 3]"
          % (file_ids(d),))
    mcp_bridge.chat_read(sender="alice", channel="x")                                # caught up at 3
    check(cur("alice", "x") == 3, "C14 setup: alice's cursor is %s, want 3" % (cur("alice", "x"),))
    rows = [json.loads(ln) for ln in (Path(d) / "chat.jsonl").read_text("utf-8").splitlines() if ln.strip()]
    (Path(d) / "chat.jsonl").write_text("".join(json.dumps(m, ensure_ascii=False) + "\n"
                                                for m in rows if m["id"] < 2), "utf-8")
    restart(d)
    fresh = mcp_bridge.store.add("carl", "after a rollback dropped ids", channel="x")
    check(fresh["id"] > 3, "C14 REISSUED id=%d: ids 2 and 3 were made durable by flush_bulk, a HEAD-era process "
          "rewrote the log without them, and the restart handed 0..3 out again" % fresh["id"])
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(got == [fresh["id"]], "C14 alice's cursor was 3 and after the restart her ordinary read returned %s; the "
          "new message (id %d) must reach her without a resync" % (ascii(str(got)[:60]), fresh["id"]))


def c15():
    # F4 (r3): clear() with no channel is the one removal path that does not go
    # through a rewrite - it truncates the log with a plain write - so it is the
    # path that can still free every id of a store this code never wrote.
    d = env("c15")
    ids = [posts("bob", "m%d" % i, "x") for i in range(10)]              # ids 0..9
    mcp_bridge.chat_read(sender="alice", channel="x")                     # cursor 9, persisted
    (Path(d) / IDS).unlink(missing_ok=True)                               # a store in HEAD's format now
    restart(d)                                                            # first load of that store
    check([m["id"] for m in mcp_bridge.store.get_recent(999)] == ids, "C15 setup: the first load changed what "
          "the store holds: %s" % ([m["id"] for m in mcp_bridge.store.get_recent(999)],))
    check(cur("alice", "x") == ids[-1], "C15 setup: alice's cursor did not survive the restart: %s"
          % (cur("alice", "x"),))
    mcp_bridge.store.clear()                                              # no channel: the UI's /clear
    check(mcp_bridge.store.get_recent(999) == [], "C15 setup: clear() left messages in the store")
    check(not (Path(d) / IDS).exists() or isinstance(ids_state(d), int),
          "C15 setup: clear() wrote an id state that cannot be read back: %s" % (ids_state(d),))
    restart(d)
    fresh = mcp_bridge.store.add("bob", "first send after a full clear", channel="x")
    check(fresh["id"] > ids[-1], "C15 REISSUED id=%d: a store in HEAD's format held ids 0-%d, this code loaded "
          "it, clear() with no channel emptied the message file without recording the next id, and the restart "
          "handed %d out again - an id that had already reached the message file and alice's cursor"
          % (fresh["id"], ids[-1], fresh["id"]))
    st = ids_state(d)
    check(isinstance(st, int) and st > ids[-1], "C15 %s holds %s after clear() with no channel emptied the log "
          "and a restart; it must hold a next id above every id ever issued (%d), because emptying the log may "
          "not free the ids that were in it" % (IDS, st, ids[-1]))
    got = msg_ids(mcp_bridge.chat_read(sender="alice", channel="x"))
    check(got == [fresh["id"]], "C15 alice's cursor was persisted at %d before the restart and the new message "
          "took id %d, so her ordinary chat_read returned %s: only a chat_resync would ever show it to her"
          % (ids[-1], fresh["id"], ascii(str(got)[:60])))


CONTROLS = {"T01": t01, "T02": t02, "T03": t03, "T04": t04, "T05": t05, "T06": t06, "T07": t07,
            "T08": t08, "T09": t09, "T10": t10, "T11": t11, "T12": t12, "T13": t13, "T14": t14,
            "T15": t15, "T16": t16, "T17": t17, "T18": t18, "T19": t19, "T20": t20, "T21": t21,
         "T22": t22, "T23": t23, "T24": t24,
            "T25": t25, "T26": t26, "T27": t27, "T28": t28,
            "C01": c01, "C02": c02, "C03": c03, "C04": c04, "C05": c05,
            "C06": c06, "C07": c07, "C08": c08, "C09": c09, "C10": c10,
            "C11": c11, "C12": c12, "C13": c13,
            "C14": c14, "C15": c15}

if len(sys.argv) != 2 or sys.argv[1] not in CONTROLS:
    print("  detail: unknown control %s" % (sys.argv[1:],))
    sys.exit(1)
try:
    CONTROLS[sys.argv[1]]()
except Exception as exc:  # noqa: BLE001 - a driver crash is a failed control
    print("  detail: %s: %s" % (type(exc).__name__, exc))
    sys.exit(1)
finish()
PYEOF

PASS_N=0
FAIL_N=0
for c in T01 T02 T03 T04 T05 T06 T07 T08 T09 T10 T11 T12 T13 T14 T15 T16 T17 T18 T19 T20 T21 T22 T23 T24 \
         T25 T26 T27 T28 C01 C02 C03 C04 C05 C06 C07 C08 C09 C10 C11 C12 C13 C14 C15; do
  python3 "$DRIVER" "$c"
  if [ $? -eq 0 ]; then
    PASS_N=$((PASS_N + 1))
    echo "PASS $c"
  else
    FAIL_N=$((FAIL_N + 1))
    echo "FAIL $c"
  fi
done

echo "TOTAL $((PASS_N + FAIL_N)) PASS $PASS_N FAIL $FAIL_N"
[ "$FAIL_N" -eq 0 ] || exit 1
