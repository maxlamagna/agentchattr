"""Ordinary history browsing has exclusive bounded cursors and safe parent batches."""
import hashlib
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import app
from store import MessageStore


class BrowsingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = root / 'log.jsonl'
        self.store = MessageStore(str(self.path))
        for i in range(1207):
            self.store.add('Max', f'Message {i}', channel='general' if i % 3 else 'work',
                           timestamp=2000 - i, _bulk=True)
        self.store.flush_bulk()
        self.store.delete([200, 400, 600])

    def test_page_walks_all_retained_rows_both_directions_with_gaps(self):
        expected = [m['id'] for m in self.store.get_recent(2000, channel='general')]
        page = self.store.history_page(channel='general', limit=100)
        seen = [m['id'] for m in page['messages']]
        self.assertFalse(page['has_after'])
        while page['has_before']:
            page = self.store.history_page(channel='general', before_id=seen[0], limit=100)
            ids = [m['id'] for m in page['messages']]
            self.assertTrue(all(x < seen[0] for x in ids))
            seen = ids + seen
        self.assertEqual(seen, expected)
        first = self.store.history_page(channel='general', before_id=expected[1], limit=100)
        self.assertFalse(first['has_before'])
        seen = [m['id'] for m in first['messages']]
        page = first
        while page['has_after']:
            page = self.store.history_page(channel='general', after_id=seen[-1], limit=100)
            seen.extend(m['id'] for m in page['messages'])
        self.assertEqual(seen, expected)

    def test_page_boundaries_caps_isolation_and_unchanged_log(self):
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        latest = self.store.history_page(channel='work', limit=99999)
        self.assertLessEqual(len(latest['messages']), 500)
        self.assertTrue(all(m['channel'] == 'work' for m in latest['messages']))
        self.assertFalse(latest['has_after'])
        self.assertEqual(self.store.history_page(channel='empty')['messages'], [])
        for page in [self.store.history_page(channel='general', before_id=0),
                     self.store.history_page(channel='general', after_id=99999)]:
            self.assertEqual(page['messages'], [])
        page = self.store.history_page(channel='general', before_id=500)
        self.store.add('codex', 'concurrent live', channel='general', _bulk=True)
        self.assertEqual(self.store.history_page(channel='general', before_id=500), page)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)
        latest['messages'][0]['text'] = 'client edit'
        self.assertNotEqual(self.store.get_by_id(latest['messages'][0]['id'])['text'], 'client edit')

    def test_parent_batch_returns_only_bounded_preview_fields(self):
        self.store.update_message(1, {'text': '<img src=x onerror=sentinel()>' + 'x' * 500})
        parents = self.store.reply_previews([1, 1, 200, 999999, 0])
        self.assertEqual([m['id'] for m in parents], [1, 0])
        self.assertEqual(set(parents[0]), {'id', 'sender', 'text', 'channel'})
        self.assertLessEqual(len(parents[0]['text']), 81)
        self.assertTrue(parents[0]['text'].startswith('<img'))

    def test_page_snapshot_cannot_mix_channel_names_during_rename(self):
        inspecting, release, renamed = threading.Event(), threading.Event(), threading.Event()
        target = next(m for m in self.store._messages if m.get('channel') == 'general')
        class PausingMessage(dict):
            def get(msg, key, default=None):
                value = super().get(key, default)
                if key == 'channel' and threading.current_thread().name == 'page-reader' and not inspecting.is_set():
                    inspecting.set(); release.wait(3)
                return value
        index = self.store._messages.index(target)
        self.store._messages[index] = PausingMessage(target)
        result = []
        reader = threading.Thread(name='page-reader', target=lambda: result.append(self.store.history_page('general', limit=500)))
        def rename():
            self.store.rename_channel('general', 'renamed'); renamed.set()
        reader.start(); self.assertTrue(inspecting.wait(2))
        writer = threading.Thread(target=rename); writer.start()
        try:
            renamed.wait(.2)
        finally:
            release.set(); reader.join(3); writer.join(3)
        self.assertFalse(reader.is_alive()); self.assertFalse(writer.is_alive())
        self.assertTrue(result[0]['messages'])
        self.assertTrue(all(m['channel'] == 'general' for m in result[0]['messages']))


class BrowsingHTTPTests(BrowsingTests):
    def setUp(self):
        super().setUp()
        api = FastAPI()
        api.add_api_route('/api/history/page', app.history_page, methods=['POST'])
        api.add_api_route('/api/history/parents', app.history_parents, methods=['POST'])
        registry = Mock()
        registry.resolve_token.return_value = {'name': 'codex'}
        for name, value in {'app': api, 'store': self.store, 'registry': registry}.items():
            self.stack.enter_context(patch.object(app, name, value))
        app._install_security_middleware('browse-test', {'server': {'port': 8300}})
        self.client = self.stack.enter_context(TestClient(api))
        self.headers = {'X-Session-Token': 'browse-test'}

    def test_actual_security_middleware_on_both_routes(self):
        for route, body in [('page', {'channel': 'general'}), ('parents', {'ids': [1]})]:
            for headers in [{}, {'X-Session-Token': 'wrong'}, {'Authorization': 'Bearer agent'},
                            {**self.headers, 'Origin': 'https://evil.invalid'}]:
                self.assertEqual(self.client.post('/api/history/' + route, json=body, headers=headers).status_code, 403)
            self.assertEqual(self.client.post('/api/history/' + route, json=body, headers=self.headers).status_code, 200)

    def test_generic_validation_and_server_caps(self):
        for body in [[], None, {}, {'channel': []}, {'channel': ''},
                     {'channel': 'general', 'before_id': 3, 'after_id': 4},
                     {'channel': 'general', 'before_id': True},
                     {'channel': 'general', 'after_id': -1},
                     {'channel': 'general', 'limit': None},
                     {'channel': 'general', 'limit': 0}]:
            response = self.client.post('/api/history/page', json=body, headers=self.headers)
            self.assertEqual(response.status_code, 400, str(body))
            self.assertEqual(response.json(), {'error': 'Invalid history page'})
        for body in [{}, [], {'ids': None}, {'ids': [True]}, {'ids': [-1]},
                     {'ids': [1.5]}, {'ids': list(range(51))}]:
            self.assertEqual(self.client.post('/api/history/parents', json=body, headers=self.headers).status_code, 400)
        response = self.client.post('/api/history/page', json={'channel': 'general', 'limit': 99999}, headers=self.headers)
        self.assertEqual(len(response.json()['messages']), 500)
        self.assertEqual(self.client.post('/api/history/parents', json={'ids': []}, headers=self.headers).json(), {'messages': []})


if __name__ == '__main__':
    unittest.main()
