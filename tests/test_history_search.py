"""Retained history stays searchable without exposing or rewriting the log."""
import hashlib
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import app
from store import MessageStore


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'log.jsonl'
        self.store = MessageStore(str(self.path))
        for i in range(120):
            self.store.add('Max' if i % 2 else 'codex', f'Straße needle {i}',
                           channel='general' if i % 3 else 'work', timestamp=1000 + i,
                           _bulk=True)
        self.store.flush_bulk()

    def test_literal_casefold_filters_and_half_open_dates(self):
        page = self.store.search(text='STRASSE', sender='MAX', channel='general',
                                 from_ts=1020, to_ts=1030)
        self.assertEqual([m['id'] for m in page['messages']], [29, 25, 23])
        self.assertFalse(self.store.search(text='.*')['messages'])
        self.assertEqual([m['id'] for m in self.store.search(from_ts=1000, to_ts=1001)['messages']], [0])

    def test_pagination_is_exclusive_stable_bounded_and_read_only(self):
        before = self.path.read_bytes()
        ids = [m['id'] for m in self.store.get_recent(1000)]
        first = self.store.search(limit=10000)
        self.assertEqual(len(first['messages']), 50)
        self.assertEqual(first['next_before_id'], 70)
        self.assertEqual(first['messages'][0]['id'], 119)
        second = self.store.search(before_id=first['next_before_id'], limit=50)
        self.assertTrue(set(m['id'] for m in first['messages']).isdisjoint(m['id'] for m in second['messages']))
        self.assertEqual(second['messages'][0]['id'], 69)
        self.assertEqual(self.store.search(before_id=0)['messages'], [])
        self.store.get_context(60)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([m['id'] for m in self.store.get_recent(1000)], ids)
        self.store.add('new', 'needle new', _bulk=True)
        self.assertEqual(self.store.search(before_id=70, limit=50), second)

    def test_context_gaps_edges_and_returned_copies(self):
        self.store.delete([58, 59])
        context = self.store.get_context(61)
        rows = context['messages']
        self.assertLessEqual(len(rows), 41)
        self.assertIn(61, [m['id'] for m in rows])
        self.assertTrue(all(m['channel'] == 'general' for m in rows))
        self.assertEqual([m['id'] for m in rows], sorted(m['id'] for m in rows))
        rows[0]['text'] = 'modified copy'
        self.assertNotEqual(self.store.get_by_id(rows[0]['id'])['text'], 'modified copy')
        self.assertIsNone(self.store.get_context(59))
        self.assertIsNone(self.store.get_context(99999))
        self.assertEqual(self.store.get_context(0)['messages'][0]['id'], 0)


class SearchHTTPTests(SearchTests):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        api = FastAPI()
        api.add_api_route('/api/history/search', app.search_history, methods=['POST'])
        api.add_api_route('/api/history/context/{msg_id}', app.history_context, methods=['GET'])
        registry = Mock()
        registry.resolve_token.return_value = {'name': 'codex'}
        for name, value in {'app': api, 'store': self.store, 'registry': registry}.items():
            self.stack.enter_context(patch.object(app, name, value))
        app._install_security_middleware('history-test', {'server': {'port': 8300}})
        self.client = self.stack.enter_context(TestClient(api))
        self.headers = {'X-Session-Token': 'history-test'}

    def test_actual_middleware_rejects_unauthorized_search_and_context(self):
        for headers in [{}, {'X-Session-Token': 'wrong'}, {'Authorization': 'Bearer agent-token'},
                        {**self.headers, 'Origin': 'https://evil.invalid'}]:
            self.assertEqual(self.client.post('/api/history/search', json={}, headers=headers).status_code, 403)
            self.assertEqual(self.client.get('/api/history/context/1', headers=headers).status_code, 403)
        response = self.client.post('/api/history/search', json={'text': 'needle'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['messages']), 25)
        self.assertEqual(self.client.get('/api/history/context/1', headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get('/api/history/context/999', headers=self.headers).status_code, 404)

    def test_invalid_inputs_are_generic_without_reflection(self):
        secret = 'PRIVATE_CANARY_TERM'
        for body in [[], None, {'text': [secret]}, {'channel': [secret]}, {'sender': {}},
                     {'limit': secret}, {'limit': True}, {'limit': 0}, {'before_id': -1},
                     {'from_ts': secret}, {'to_ts': '2026-01-01'}, {'from_ts': float('inf')},
                     {'from_ts': 20, 'to_ts': 10}]:
            # Allow JSON infinity in order to exercise server-side finite checks.
            import json
            response = self.client.post('/api/history/search', content=json.dumps(body),
                                        headers={**self.headers, 'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 400, str(body))
            self.assertNotIn(secret, response.text)
        self.assertEqual(self.client.post('/api/history/search', content='{bad', headers=self.headers).status_code, 400)


if __name__ == '__main__':
    unittest.main()
