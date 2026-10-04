"""Exercise real child-process timeouts without hanging a real tmux server."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import wrapper_unix


@unittest.skipIf(sys.platform == 'win32', 'Unix executable fixture')
class InjectTimeoutTests(unittest.TestCase):
    def test_stuck_delivery_and_cleanup_return_without_retyping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / 'calls'
            executable = root / 'tmux'
            executable.write_text(
                '#!' + sys.executable + '\n'
                'import os, sys, time\n'
                'verb = sys.argv[1]\n'
                'with open(os.environ["TEST_TMUX_CALLS"], "a") as f: f.write(verb + "\\n")\n'
                'if verb == os.environ["TEST_TMUX_STALL"]: time.sleep(30)\n'
                'if verb == "display-message": print("%7")\n'
                'if verb == "paste-buffer" and os.environ["TEST_TMUX_STALL"] == "delete-buffer": sys.exit(1)\n'
            )
            executable.chmod(0o755)
            for stalled in ['display-message', 'load-buffer', 'paste-buffer', 'send-keys', 'delete-buffer']:
                with self.subTest(stalled=stalled):
                    calls.write_text('')
                    env = {'PATH': str(root) + os.pathsep + os.environ.get('PATH', ''),
                           'TEST_TMUX_CALLS': str(calls), 'TEST_TMUX_STALL': stalled}
                    with mock.patch.dict(os.environ, env), mock.patch.object(wrapper_unix, 'TMUX_COMMAND_TIMEOUT', .2):
                        start = time.monotonic()
                        ok = wrapper_unix.inject('short test', tmux_session='test', delay=0)
                        elapsed = time.monotonic() - start
                    verbs = calls.read_text().splitlines()
                    self.assertIs(ok, False)
                    self.assertLess(elapsed, 3, 'a child-process hang must be bounded')
                    self.assertLessEqual(verbs.count('paste-buffer'), 1, 'do not retry a possibly delivered paste')
                    self.assertLessEqual(verbs.count('send-keys'), 1, 'do not repeat Enter')
                    if stalled != 'send-keys':
                        self.assertNotIn('send-keys', verbs, 'no Enter after an earlier failure')


@unittest.skipIf(sys.platform == 'win32', 'Unix executable fixture')
class InjectCopyModeTests(unittest.TestCase):
    """A pane in a tmux mode keeps send-keys for the mode, so inject() must leave it first."""

    def _inject(self, sticky):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / 'calls'
            calls.write_text('')
            state = root / 'left-mode'
            executable = root / 'tmux'
            # The pane starts in copy mode; `send-keys -X cancel` leaves it unless sticky.
            executable.write_text(
                '#!' + sys.executable + '\n'
                'import os, sys\n'
                'a = sys.argv[1:]\n'
                'with open(os.environ["TEST_TMUX_CALLS"], "a") as f: f.write(" ".join(a) + "\\n")\n'
                'state = os.environ["TEST_TMUX_STATE"]\n'
                'if a[0] == "display-message":\n'
                '    print(("0" if os.path.exists(state) else "1") if "#{pane_in_mode}" in a else "%7")\n'
                'if a[0] == "send-keys" and a[-2:] == ["-X", "cancel"] and os.environ["TEST_STICKY"] != "1":\n'
                '    open(state, "w").close()\n'
            )
            executable.chmod(0o755)
            env = {'PATH': str(root) + os.pathsep + os.environ.get('PATH', ''),
                   'TEST_TMUX_CALLS': str(calls), 'TEST_TMUX_STATE': str(state),
                   'TEST_STICKY': '1' if sticky else '0'}
            with mock.patch.dict(os.environ, env), mock.patch('sys.stdout'):
                ok = wrapper_unix.inject('wake', tmux_session='test', delay=0)
            return ok, calls.read_text().splitlines()

    def test_mode_is_left_before_the_paste_and_enter_follows(self):
        ok, calls = self._inject(sticky=False)
        self.assertIs(ok, True)
        cancel = calls.index('send-keys -t %7 -X cancel')
        paste = next(i for i, c in enumerate(calls) if c.startswith('paste-buffer'))
        enter = calls.index('send-keys -t %7 Enter')
        self.assertLess(cancel, paste, 'leave the mode before pasting')
        self.assertLess(paste, enter)

    def test_a_mode_that_will_not_cancel_pastes_nothing_and_sends_no_enter(self):
        ok, calls = self._inject(sticky=True)
        self.assertIs(ok, False)
        self.assertFalse(any(c.startswith('paste-buffer') for c in calls),
                         'nothing pasted, so a later retry cannot double the prompt')
        self.assertNotIn('send-keys -t %7 Enter', calls)
