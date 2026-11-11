"""A wrapper's tmux calls reach exactly its own session (ad-e5gz launcher acceptance, Max #10212).

tmux resolves a bare `-t name` to any session whose name starts with it when no exact match exists, so with
no `agentchattr-x-codex` session, `kill-session -t agentchattr-x-codex` killed `agentchattr-x-codex-2`, and a
wake meant for codex could be pasted into codex-2's pane. Private tmux server only.
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wrapper_unix

LONG = "agentchattr-xt-codex-2"
SHORT = "agentchattr-xt-codex"


@unittest.skipIf(sys.platform == "win32", "tmux is unix-only")
@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class ExactTargetTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="xt.", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        saved = {k: os.environ.get(k) for k in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        os.environ["TMUX_TMPDIR"] = self.base
        self._new(LONG)
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], capture_output=True)
        sock = subprocess.run(["tmux", "display-message", "-p", "-t", f"={LONG}:", "#{socket_path}"],
                              capture_output=True, text=True).stdout.strip()
        self.assertTrue(os.path.realpath(sock).startswith(os.path.realpath(self.base)), sock)

    def _new(self, name):
        r = subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 120"], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _alive(self, name):
        return subprocess.run(["tmux", "has-session", "-t", f"={name}"], capture_output=True).returncode == 0

    def test_bare_name_is_a_prefix_match(self):
        """Positive control: without '=', tmux really does reach the longer session."""
        r = subprocess.run(["tmux", "has-session", "-t", SHORT], capture_output=True)
        self.assertEqual(r.returncode, 0)

    def test_absent_session_is_absent(self):
        self.assertFalse(wrapper_unix._session_exists(SHORT))
        self.assertIsNone(wrapper_unix._pane_id(SHORT))
        self.assertTrue(wrapper_unix._session_exists(LONG))

    def test_kill_reaches_only_the_exact_session(self):
        subprocess.run(["tmux", "kill-session", "-t", wrapper_unix._session_target(SHORT)], capture_output=True)
        self.assertTrue(self._alive(LONG))
        self._new(SHORT)
        subprocess.run(["tmux", "kill-session", "-t", wrapper_unix._session_target(SHORT)], capture_output=True)
        self.assertFalse(self._alive(SHORT))
        self.assertTrue(self._alive(LONG))

    def test_pane_of_each_session(self):
        self._new(SHORT)
        short = wrapper_unix._pane_id(SHORT)
        long_ = wrapper_unix._pane_id(LONG)
        self.assertTrue(short and long_ and short != long_, (short, long_))


class NoBareSessionTarget(unittest.TestCase):
    def test_every_session_name_target_goes_through_the_helpers(self):
        """No tmux call in wrapper_unix passes a session-name variable straight to -t."""
        tree = ast.parse(Path(wrapper_unix.__file__).read_text(encoding="utf-8"))
        bare = []
        for node in ast.walk(tree):
            if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                    and node.elts[0].value == "tmux":
                for i, el in enumerate(node.elts[:-1]):
                    nxt = node.elts[i + 1]
                    if isinstance(el, ast.Constant) and el.value == "-t" and isinstance(nxt, ast.Name) \
                            and nxt.id in ("session_name", "tmux_session"):
                        bare.append(node.lineno)
        self.assertEqual(bare, [])


if __name__ == "__main__":
    unittest.main()
