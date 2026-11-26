import datetime as dt
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import History, handler_for

IDS = ['00000000-0000-4000-8000-00000000000' + str(n) for n in range(1, 5)]


def fixture(home):
    (home / 'sessions').mkdir()
    rollout = home / 'sessions' / 'legacy.jsonl'
    rollout.write_text(json.dumps({'type': 'response_item', 'payload': {
        'type': 'message', 'role': 'user', 'content': [{'text': 'Hidden legacy needle <script>evil()</script>'}]}}) + '\n')
    timestamp = int(dt.datetime(2026, 11, 20, 12).timestamp())
    with sqlite3.connect(home / 'state_5.sqlite') as conn:
        conn.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, cwd TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER, first_user_message TEXT, agent_role TEXT, source TEXT, rollout_path TEXT, history_mode TEXT)')
        for n, sid in enumerate(IDS):
            conn.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)', (
                sid, ['Fix login', 'Write tests', 'Old project', 'Worker'][n],
                '/projects/app' if n < 2 else '/projects/other', timestamp, timestamp + n,
                int(n == 2), 'First prompt', 'worker' if n == 3 else None,
                'cli', str(rollout), 'paginated' if n == 1 else 'legacy'))
    with sqlite3.connect(home / 'thread_history_1.sqlite') as conn:
        conn.execute('CREATE TABLE thread_items(thread_id TEXT, rollout_ordinal INTEGER, item_json TEXT)')
        conn.execute('INSERT INTO thread_items VALUES(?,?,?)', (IDS[1], 1, json.dumps({
            'type': 'agentMessage', 'text': 'Paginated answer with secretword'})))
    return History(home)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.history = fixture(self.home)

    def ids(self, **params):
        return {r['id'] for r in self.history.sessions(params)['sessions']}

    def test_filters_and_literal_search(self):
        self.assertEqual(self.ids(), set(IDS[:2]))
        self.assertEqual(self.ids(status='all', subagents='1'), set(IDS))
        self.assertEqual(self.ids(status='archived'), {IDS[2]})
        self.assertEqual(self.ids(directory='/projects/app', q='LOGIN'), {IDS[0]})
        self.assertEqual(self.ids(q="' OR 1=1 --"), set())
        self.assertEqual(self.ids(after='2026-11-20', before='2026-11-20'), set(IDS[:2]))
        self.assertEqual(self.ids(after='2026-11-21'), set())
        self.assertEqual(self.ids(before='2026-11-19'), set())

    def test_both_transcript_formats_and_full_search(self):
        self.assertEqual(self.ids(q='secretword'), set())
        self.assertEqual(self.ids(q='secretword', fulltext='1'), {IDS[1]})
        self.assertEqual(self.ids(q='legacy needle', fulltext='1'), {IDS[0]})
        self.assertEqual(self.history.detail(IDS[1])['messages'][0]['role'], 'assistant')
        self.assertIn('<script>', self.history.detail(IDS[0])['messages'][0]['text'])

    def test_missing_optional_schema_columns(self):
        with sqlite3.connect(self.home / 'state_5.sqlite') as conn:
            conn.execute('ALTER TABLE threads DROP COLUMN history_mode')
        self.assertEqual(self.ids(), set(IDS[:2]))

    def test_external_rollout_path_not_read(self):
        outside = self.home / 'private.jsonl'
        outside.write_text('private data')
        with sqlite3.connect(self.home / 'state_5.sqlite') as conn:
            conn.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(outside), IDS[0]))
        self.assertEqual(self.history.detail(IDS[0])['messages'], [])

    @patch('app.shutil.which', return_value='/bin/codex')
    @patch('app.subprocess.run')
    def test_delete_uses_cli_exact_ids_and_reports_partial_failure(self, run, _which):
        run.side_effect = [subprocess.CompletedProcess([], 0, '', ''),
                           subprocess.CompletedProcess([], 1, '', 'Session busy')]
        result = self.history.delete([IDS[0], IDS[1], IDS[0]])
        self.assertEqual([r['ok'] for r in result['results']], [True, False])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ['codex', 'delete', '--force', IDS[0]])
        self.assertEqual(run.call_args.kwargs['env']['CODEX_HOME'], str(self.home))
        # The application never issues a direct SQL DELETE, even on CLI success.
        self.assertEqual(len(self.history.rows()), 4)

    @patch('app.subprocess.run')
    def test_invalid_deletion_never_invokes_cli(self, run):
        for ids in ([], ['--help'], ['00000000-0000-4000-8000-999999999999'], [12], IDS * 126):
            with self.assertRaises(ValueError):
                self.history.delete(ids)
        run.assert_not_called()

    @patch('app.shutil.which', return_value='/bin/codex')
    @patch('app.subprocess.run', side_effect=subprocess.TimeoutExpired('codex', 60))
    def test_timeout_is_per_session_error(self, _run, _which):
        result = self.history.delete([IDS[0]])
        self.assertFalse(result['results'][0]['ok'])
        self.assertIn('timed out', result['results'][0]['error'])

    def test_http_boundaries_and_confirmation_payload(self):
        handler = handler_for(self.history)

        def request(method, path, body='', headers=None):
            # Exercise the real HTTP parser/handler using byte streams, without a socket.
            headers = {'Host': '127.0.0.1:8765', 'Content-Length': str(len(body.encode())), **(headers or {})}
            raw = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in headers.items()) + '\r\n' + body
            instance = handler.__new__(handler)
            instance.rfile = io.BytesIO(raw.encode())
            instance.wfile = io.BytesIO()
            instance.server = SimpleNamespace(server_port=8765)
            instance.client_address = ('127.0.0.1', 12345)
            instance.handle_one_request()
            head, payload = instance.wfile.getvalue().split(b'\r\n\r\n', 1)
            return int(head.split()[1]), json.loads(payload)

        status, data = request('GET', '/api/sessions')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['sessions']), 2)
        self.assertEqual(request('GET', '/api/token', headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(request('POST', '/api/delete', '{}', {'Content-Type': 'application/json'})[0], 403)
        token = request('GET', '/api/token')[1]['token']
        with patch.object(self.history, 'delete', return_value={'results': []}) as delete:
            status, _ = request('POST', '/api/delete', json.dumps({'ids': [IDS[0]]}), {
                'Content-Type': 'application/json', 'X-CSRF-Token': token,
                'Origin': 'https://attacker.example'})
            self.assertEqual(status, 403)
            delete.assert_not_called()
            status, _ = request('POST', '/api/delete', json.dumps({'ids': [IDS[0]]}), {
                'Content-Type': 'application/json', 'X-CSRF-Token': token})
            self.assertEqual(status, 200)
            delete.assert_called_once_with([IDS[0]])


if __name__ == '__main__':
    unittest.main()
