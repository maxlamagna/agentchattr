"""Real queue producers/consumer; no live server, queue or tmux pane."""
import builtins
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wrapper
from agents import AgentTrigger
import wrapper_unix


class StopWatcher(BaseException):
    pass


class QueueDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue = self.root / 'tester_queue.jsonl'
        self.trigger = AgentTrigger(SimpleNamespace(get_state=lambda name: 'ready'), str(self.root))

    def put(self, prompt='', channel='general', job_id=None):
        self.trigger.trigger_sync('tester', 'test: wake', channel, job_id, prompt=prompt)

    def watch(self, outcomes=(True,), polls=5, injector=None, ready=None):
        calls = []
        ticks = 0
        steps = 0
        exceeded = False
        real_finish = wrapper.finish_batch

        def guard():
            nonlocal steps, exceeded
            steps += 1
            if steps > max(20, polls * 20):
                exceeded = True
                raise StopWatcher()

        def identity():
            guard()
            return 'tester', self.queue

        def finish(*args, **kwargs):
            guard()
            return real_finish(*args, **kwargs)

        def sleep(seconds):
            nonlocal ticks
            guard()
            if seconds == 1:
                ticks += 1
                if ticks >= polls:
                    raise StopWatcher()

        def inject(prompt):
            calls.append(prompt)
            if injector:
                return injector(prompt)
            outcome = outcomes[min(len(calls)-1, len(outcomes)-1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(wrapper.time, 'sleep', sleep), \
             mock.patch.object(wrapper, '_fetch_role', return_value=''), \
             mock.patch.object(wrapper, '_fetch_active_rules', return_value=None), \
             mock.patch.object(wrapper, 'finish_batch', finish):
            with self.assertRaises(StopWatcher):
                wrapper._queue_watcher(identity, inject, ready_flag=ready)
        self.assertFalse(exceeded, 'watcher exceeded its bound without reaching the poll sleep')
        return calls

    def test_completion_and_idle_both_survive(self):
        self.put('COMPLETION-A')
        self.put('IDLE-NOTICE')
        calls = self.watch()
        self.assertEqual(sum(p.count('COMPLETION-A') for p in calls), 1)
        self.assertEqual(sum(p.count('IDLE-NOTICE') for p in calls), 1)

    def test_two_completions_both_survive(self):
        self.put('COMPLETION-A')
        self.put('COMPLETION-B')
        calls = self.watch()
        for marker in ('COMPLETION-A', 'COMPLETION-B'):
            self.assertEqual(sum(p.count(marker) for p in calls), 1)

    def test_ready_gate_preserves_queue_until_release(self):
        self.put('COMPLETION-A')
        before = self.queue.read_bytes()
        ready = [False]
        self.assertEqual(self.watch(ready=ready), [])
        self.assertEqual(self.queue.read_bytes(), before)
        ready[0] = True
        self.assertEqual(len(self.watch(ready=ready)), 1)
        self.assertEqual(self.queue.read_text(), '')

    def test_changed_prefix_is_traced_and_later_wakes_are_delivered(self):
        for result in (True, False, None):
            with self.subTest(first_result=result):
                self.put('ENTRY-ONE')
                real_begin = wrapper.begin_batch
                consumed = []

                def begin(path, *args, **kwargs):
                    if not consumed:
                        consumed.append(True)
                        path.write_text('')  # external consumer, after our snapshot
                    return real_begin(path, *args, **kwargs)

                def inject(prompt):
                    if 'ENTRY-ONE' in prompt:
                        self.put('ENTRY-TWO')
                        return result
                    if 'ENTRY-TWO' in prompt:
                        self.put('ENTRY-THREE')
                    return True

                with mock.patch.object(wrapper, 'begin_batch', begin):
                    calls = self.watch(injector=inject, polls=8)
                for marker in ('ENTRY-ONE', 'ENTRY-TWO', 'ENTRY-THREE'):
                    self.assertEqual(sum(p.count(marker) for p in calls), 1)
                self.assertEqual(self.queue.read_text(), '')
                self.assertIn('prefix-changed', (self.root/'tester_queue.delivery.jsonl').read_text())

    def test_mixed_channel_and_job_read_tasks_survive(self):
        self.put(channel='first')
        self.put(channel='second', job_id=17)
        self.put('CUSTOM-COMPLETION')
        calls = ' '.join(self.watch())
        for marker in ('#first', 'job_id=17', 'CUSTOM-COMPLETION'):
            self.assertIn(marker, calls)

    def test_proven_pre_paste_failure_retries_once_then_stops(self):
        self.put('COMPLETION-A')
        calls = self.watch((False, True))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(self.queue.read_text(), '')
        trace = (self.root / 'tester_queue.delivery.jsonl').read_text()
        self.assertIn('not-pasted', trace)
        self.assertIn('delivered', trace)

    def test_persistent_pre_paste_failure_is_bounded_and_preserved(self):
        self.put('COMPLETION-A')
        calls = self.watch((False,), polls=8)
        self.assertGreater(len(calls), 1)
        self.assertLessEqual(len(calls), 3)
        self.assertIn('COMPLETION-A', (self.root / 'tester_queue.failed.jsonl').read_text())

    def test_ambiguous_or_post_paste_failure_never_retries(self):
        for outcome in (None, RuntimeError('ambiguous')):
            with self.subTest(outcome=outcome):
                self.put('COMPLETION-A')
                calls = self.watch((outcome,))
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.queue.read_text(), '')
                trace_file = self.root / 'tester_queue.delivery.jsonl'
                self.assertTrue(trace_file.exists(), 'ambiguous injection needs a durable trace')
                trace = trace_file.read_text()
                self.assertIn('uncertain', trace)

    def test_real_inject_retries_only_before_any_paste_attempt(self):
        fake = self.root/'tmux'
        fake.write_text('#!'+sys.executable+'\n'+'''
import os, sys
from pathlib import Path
root = Path(os.environ['TEST_DELIVERY_ROOT'])
verb = sys.argv[1]
with (root/'calls').open('a') as log: log.write(verb+'\\n')
count = root/'attempt'
if verb == 'display-message':
    if '#{pane_id}' in sys.argv:
        count.write_text(str(int(count.read_text() if count.exists() else '0')+1))
        print('%7')
    elif os.environ['TEST_DELIVERY_FAILURE'] == 'before' and count.read_text() == '1':
        print('unreadable')
    else: print('0')
if verb == os.environ['TEST_DELIVERY_FAILURE']: sys.exit(1)
''')
        fake.chmod(0o755)
        for failure in ('before', 'paste-buffer', 'send-keys'):
            with self.subTest(failure=failure):
                for name in ('calls', 'attempt'):
                    (self.root/name).unlink(missing_ok=True)
                self.put('COMPLETION-A')
                env = {'PATH':str(self.root)+os.pathsep+os.environ.get('PATH',''),
                       'TEST_DELIVERY_ROOT':str(self.root), 'TEST_DELIVERY_FAILURE':failure}
                with mock.patch.dict(os.environ, env), mock.patch('sys.stdout'):
                    calls = self.watch(injector=lambda text: wrapper_unix.inject(
                        text, tmux_session='fixture', delay=0))
                verbs = (self.root/'calls').read_text().splitlines()
                self.assertEqual(len(calls), 2 if failure == 'before' else 1)
                self.assertEqual(verbs.count('paste-buffer'), 1)
                self.assertEqual(verbs.count('send-keys'), 0 if failure == 'paste-buffer' else 1)

    def test_real_append_between_read_and_ack_survives(self):
        self.put('COMPLETION-A')
        real_open = builtins.open
        fired = threading.Event()
        appended = threading.Event()
        writers = []

        def append():
            self.put('COMPLETION-B')
            appended.set()

        class Reader:
            def __init__(self, file):
                self.file = file
            def __enter__(self):
                self.file.__enter__()
                return self
            def __exit__(self, *args):
                return self.file.__exit__(*args)
            def __getattr__(self, attr):
                return getattr(self.file, attr)
            def readlines(inner):
                lines = inner.file.readlines()
                if not fired.is_set():
                    fired.set()
                    writer = threading.Thread(target=append)
                    writers.append(writer)
                    writer.start()
                    appended.wait(.1)
                return lines

        def open_file(path, mode='r', *args, **kwargs):
            file = real_open(path, mode, *args, **kwargs)
            return Reader(file) if Path(path) == self.queue and mode.startswith('r') else file

        with mock.patch('builtins.open', open_file):
            calls = self.watch()
        for writer in writers:
            writer.join(2)
            self.assertFalse(writer.is_alive())
        self.assertTrue(fired.is_set(), 'race must actually run')
        self.assertTrue(appended.is_set())
        self.assertEqual(sum(p.count('COMPLETION-B') for p in calls), 1)

    def test_wrapper_death_during_paste_does_not_replay_on_restart(self):
        self.put('COMPLETION-A')
        def dies(prompt):
            raise StopWatcher()
        self.assertEqual(len(self.watch(injector=dies)), 1)
        self.assertEqual(self.watch(), [])
        self.assertEqual(self.queue.read_text(), '')
        self.assertIn('COMPLETION-A', (self.root/'tester_queue.failed.jsonl').read_text())

    def test_acknowledgement_error_after_delivery_does_not_paste_twice(self):
        self.put('COMPLETION-A')
        original = wrapper.finish_batch
        tries = []
        def finish(*args, **kwargs):
            tries.append(1)
            if len(tries) == 1:
                raise OSError('temporary acknowledgement failure')
            return original(*args, **kwargs)
        with mock.patch.object(wrapper, 'finish_batch', finish):
            self.assertEqual(len(self.watch()), 1)
        self.assertEqual(self.queue.read_text(), '')


if __name__ == '__main__':
    unittest.main()
