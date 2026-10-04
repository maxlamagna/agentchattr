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
                'if verb == "display-message": print("0" if "#{pane_in_mode}" in sys.argv else "%7")\n'
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

    def _inject(self, mode_answer, sticky=False, mode_after_paste=False):
        """mode_answer: what #{pane_in_mode} reads until `copy-mode -q` runs ("0" afterwards,
        unless sticky). mode_after_paste: the pane enters a mode once the paste has landed
        (a picker opened during the pause before Enter), until a second `copy-mode -q`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / 'calls'
            calls.write_text('')
            state = root / 'left-mode'
            executable = root / 'tmux'
            executable.write_text(
                '#!' + sys.executable + '\n'
                'import os, sys\n'
                'a = sys.argv[1:]\n'
                'with open(os.environ["TEST_TMUX_CALLS"], "a") as f: f.write(" ".join(a) + "\\n")\n'
                'state = os.environ["TEST_TMUX_STATE"]\n'
                'pasted = state + ".pasted"\n'
                'if a[0] == "display-message":\n'
                '    if "#{pane_in_mode}" in a:\n'
                '        if os.environ["TEST_MODE_AFTER_PASTE"] == "1" and os.path.exists(pasted):\n'
                '            print("0" if os.path.exists(state + ".2") else "1")\n'
                '        else:\n'
                '            print("0" if os.path.exists(state) else os.environ["TEST_MODE_ANSWER"])\n'
                '    else:\n'
                '        print("%7")\n'
                'if a[0] == "paste-buffer":\n'
                '    open(pasted, "w").close()\n'
                'if a[0] == "copy-mode" and "-q" in a and os.environ["TEST_STICKY"] != "1":\n'
                '    open(state + (".2" if os.path.exists(pasted) else ""), "w").close()\n'
            )
            executable.chmod(0o755)
            env = {'PATH': str(root) + os.pathsep + os.environ.get('PATH', ''),
                   'TEST_TMUX_CALLS': str(calls), 'TEST_TMUX_STATE': str(state),
                   'TEST_MODE_ANSWER': mode_answer, 'TEST_STICKY': '1' if sticky else '0',
                   'TEST_MODE_AFTER_PASTE': '1' if mode_after_paste else '0'}
            with mock.patch.dict(os.environ, env), mock.patch('sys.stdout'):
                ok = wrapper_unix.inject('wake', tmux_session='test', delay=0)
            return ok, calls.read_text().splitlines()

    def _assert_left_before_paste(self, ok, calls):
        self.assertIs(ok, True)
        leave = calls.index('copy-mode -q -t %7')
        paste = next(i for i, c in enumerate(calls) if c.startswith('paste-buffer'))
        enter = calls.index('send-keys -t %7 Enter')
        self.assertLess(leave, paste, 'leave the mode before pasting')
        self.assertLess(paste, enter)

    def test_mode_is_left_before_the_paste_and_enter_follows(self):
        self._assert_left_before_paste(*self._inject('1'))

    def test_stacked_modes_count_as_in_mode(self):
        """pane_in_mode reads 2 for stacked modes (Codex #7980): any count above 0 is a mode."""
        self._assert_left_before_paste(*self._inject('2'))

    def test_a_mode_entered_after_the_paste_is_left_before_enter(self):
        """A tree picker opened during the pause before Enter (Codex #7980) is left too,
        so the pasted prompt is still submitted."""
        ok, calls = self._inject('0', mode_after_paste=True)
        self.assertIs(ok, True)
        paste = next(i for i, c in enumerate(calls) if c.startswith('paste-buffer'))
        leave = calls.index('copy-mode -q -t %7')
        enter = calls.index('send-keys -t %7 Enter')
        self.assertLess(paste, leave, 'the mode appeared after the paste')
        self.assertLess(leave, enter, 'leave it before pressing Enter')

    def test_a_mode_that_will_not_cancel_pastes_nothing_and_sends_no_enter(self):
        ok, calls = self._inject('1', sticky=True)
        self.assertIs(ok, False)
        self.assertFalse(any(c.startswith('paste-buffer') for c in calls),
                         'nothing pasted, so a later retry cannot double the prompt')
        self.assertNotIn('send-keys -t %7 Enter', calls)

    def test_an_unreadable_mode_answer_is_not_taken_as_safe(self):
        """A mode count that is not a number is unknown, never 'no mode' (Codex #7980)."""
        for answer in ['', 'garbage']:
            with self.subTest(answer=answer):
                ok, calls = self._inject(answer, sticky=True)
                self.assertIs(ok, False)
                self.assertFalse(any(c.startswith('paste-buffer') for c in calls))
                self.assertNotIn('send-keys -t %7 Enter', calls)
