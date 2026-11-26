#!/usr/bin/env python3
"""Local Codex history browser. Standard library only; Codex owns all deletions."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import uuid
from usage import UsageReader, PRICING

ROOT = Path(__file__).resolve().parent


def read_db(path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


def latest_db(home, prefix):
    candidates = []
    for path in home.glob(f'{prefix}_*.sqlite'):
        version = path.stem.removeprefix(prefix + '_')
        if version.isdigit():
            candidates.append((int(version), path))
    if not candidates:
        return None
    return max(candidates)[1]


def message(item):
    """Extract visible conversation messages from both Codex history formats."""
    kind = item.get('type')
    if kind == 'agentMessage':
        return {'role': 'assistant', 'text': item.get('text', '')}
    if kind == 'userMessage':
        return {'role': 'user', 'text': '\n'.join(
            p.get('text', '') for p in item.get('content', []) if isinstance(p, dict))}
    if kind == 'message' and item.get('role') in ('user', 'assistant'):
        return {'role': item['role'], 'text': '\n'.join(
            p.get('text', '') for p in item.get('content', []) if isinstance(p, dict))}
    return None


class History:
    def __init__(self, home, codex='codex'):
        self.home = Path(home).expanduser().resolve()
        self.codex = codex
        self.delete_lock = threading.Lock()
        self.usage = UsageReader(self.home)

    def rows(self):
        path = latest_db(self.home, 'state')
        if not path:
            raise ValueError(f'No state_*.sqlite database found in {self.home}')
        with read_db(path) as conn:
            cols = {r['name'] for r in conn.execute('PRAGMA table_info(threads)')}
            fields = ['id', 'title', 'cwd', 'created_at', 'updated_at', 'archived',
                      'first_user_message', 'model', 'git_branch', 'source',
                      'agent_role', 'agent_nickname', 'rollout_path', 'history_mode', 'tokens_used']
            query = ', '.join(f'"{f}"' if f in cols else f"NULL AS \"{f}\"" for f in fields)
            return [dict(r) for r in conn.execute(f'SELECT {query} FROM threads ORDER BY updated_at DESC')]

    def conversation(self, row):
        path = latest_db(self.home, 'thread_history')
        if row.get('history_mode') == 'paginated' and path:
            with read_db(path) as conn:
                for record in conn.execute(
                        'SELECT item_json FROM thread_items WHERE thread_id = ? ORDER BY rollout_ordinal',
                        (row['id'],)):
                    try:
                        result = message(json.loads(record['item_json']))
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if result and result['text']:
                        yield result
            return
        raw_path = row.get('rollout_path')
        if not raw_path:
            return
        path = Path(raw_path).expanduser().resolve()
        # A corrupt or edited database must not turn this into an arbitrary file reader.
        if not any(path.is_relative_to((self.home / root).resolve())
                   for root in ('sessions', 'archived_sessions')):
            return
        if not path.is_file():
            return
        if path.suffix == '.zst':
            raise ValueError('Compressed legacy rollout: transcript preview is not supported yet.')
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    if record.get('type') != 'response_item':
                        continue
                    result = message(record.get('payload', {}))
                except (ValueError, TypeError, AttributeError):
                    continue
                if result and result['text']:
                    yield result

    def sessions(self, params):
        rows = self.rows()
        directories = sorted({r['cwd'] for r in rows if r['cwd']})
        query = params.get('q', '').strip().casefold()
        date_field = 'created_at' if params.get('date_field') == 'created' else 'updated_at'
        after = dt.datetime.fromisoformat(params['after']).timestamp() if params.get('after') else None
        before = (dt.datetime.fromisoformat(params['before']) + dt.timedelta(days=1)).timestamp() if params.get('before') else None
        results = []
        warnings = set()
        for row in rows:
            if params.get('directory') and row['cwd'] != params['directory']:
                continue
            if params.get('status', 'active') != 'all' and bool(row['archived']) != (params.get('status') == 'archived'):
                continue
            if params.get('subagents') != '1' and (row.get('agent_role') or 'subagent' in (row.get('source') or '').lower()):
                continue
            stamp = row.get(date_field) or 0
            if after is not None and stamp < after or before is not None and stamp >= before:
                continue
            if query and not any(query in str(row.get(f) or '').casefold()
                                 for f in ('id', 'title', 'first_user_message', 'cwd', 'model', 'git_branch')):
                if params.get('fulltext') != '1':
                    continue
                try:
                    if not any(query in m['text'].casefold() for m in self.conversation(row)):
                        continue
                except ValueError as exc:
                    warnings.add(str(exc))
                    continue
            usage = self.usage.read(row)
            results.append({**{k: v for k, v in row.items() if k not in ('rollout_path', 'history_mode', 'source')},
                            'usage': usage, 'total_tokens': usage['total_tokens'], 'cost_usd': usage['cost_usd']})
        summary = {
            'total_tokens': sum(r['total_tokens'] or 0 for r in results),
            'cost_usd': sum(r['cost_usd'] or 0 for r in results),
            'tokens_available': sum(r['total_tokens'] is not None for r in results),
            'cost_available': sum(r['cost_usd'] is not None for r in results),
            'priced_tokens': sum((r['total_tokens'] or 0) for r in results if r['cost_usd'] is not None),
            'sessions': len(results),
        }
        return {'sessions': results, 'directories': directories, 'total': len(rows),
                'home': str(self.home), 'warnings': sorted(warnings), 'summary': summary,
                'pricing': {k: v for k, v in PRICING.items() if k != 'models'}}

    def detail(self, session_id):
        row = next((r for r in self.rows() if r['id'] == session_id), None)
        if not row:
            raise ValueError('Session no longer exists. Refresh the table.')
        messages = []
        # Bound browser payload size. Search still scans the full conversation.
        size = 0
        truncated = False
        for item in self.conversation(row):
            if size + len(item['text']) > 2_000_000 or len(messages) >= 1000:
                truncated = True
                break
            messages.append(item)
            size += len(item['text'])
        return {'messages': messages, 'truncated': truncated}

    def delete(self, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
            raise ValueError('Select between 1 and 500 sessions per deletion.')
        if any(not isinstance(value, str) for value in ids):
            raise ValueError('Invalid session IDs.')
        ids = list(dict.fromkeys(str(uuid.UUID(value)) for value in ids))
        known = {r['id'] for r in self.rows()}
        if any(value not in known for value in ids):
            raise ValueError('Some sessions no longer exist. Refresh before deleting.')
        if not shutil.which(self.codex):
            raise ValueError('Codex CLI is not on PATH. Start this app from a terminal where codex works.')
        if not self.delete_lock.acquire(blocking=False):
            raise ValueError('Another deletion is in progress.')
        results = []
        try:
            for session_id in ids:
                try:
                    result = subprocess.run(
                        [self.codex, 'delete', '--force', session_id],
                        env={**os.environ, 'CODEX_HOME': str(self.home)},
                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
                    results.append({'id': session_id, 'ok': result.returncode == 0,
                                    'error': (result.stderr or result.stdout)[-2000:] if result.returncode else ''})
                except subprocess.TimeoutExpired:
                    results.append({'id': session_id, 'ok': False,
                                    'error': 'Codex timed out. Refresh to check whether deletion completed.'})
                except OSError as exc:
                    results.append({'id': session_id, 'ok': False, 'error': str(exc)})
        finally:
            self.delete_lock.release()
        return {'results': results}


def handler_for(history):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Do not log search queries or conversation identifiers.

        def respond(self, status, data, content_type='application/json; charset=utf-8'):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def boundary(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            host = self.headers.get('Host', '')
            origin = self.headers.get('Origin')
            if host not in allowed or (origin and origin != f'http://{host}'):
                self.respond(403, {'error': 'Only same-origin localhost requests are allowed.'})
                return False
            return True

        def do_GET(self):
            if not self.boundary():
                return
            url = urlsplit(self.path)
            try:
                if url.path == '/api/sessions':
                    params = {k: v[0] for k, v in parse_qs(url.query).items()}
                    self.respond(200, history.sessions(params))
                elif url.path.startswith('/api/session/'):
                    session_id = str(uuid.UUID(url.path.rsplit('/', 1)[1]))
                    self.respond(200, history.detail(session_id))
                elif url.path == '/api/token':
                    self.respond(200, {'token': token})
                elif url.path in ('/', '/app.js', '/style.css'):
                    name = 'index.html' if url.path == '/' else url.path[1:]
                    mime = {'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css'}[name]
                    self.respond(200, (ROOT / 'static' / name).read_bytes(), mime + '; charset=utf-8')
                else:
                    self.respond(404, {'error': 'Not found'})
            except (ValueError, sqlite3.Error, OSError) as exc:
                self.respond(400, {'error': str(exc)})

        def do_POST(self):
            if not self.boundary():
                return
            if self.headers.get('X-CSRF-Token') != token or self.headers.get('Content-Type') != 'application/json':
                self.respond(403, {'error': 'Refresh the page before deleting.'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32_000:
                    raise ValueError('Invalid request size.')
                body = json.loads(self.rfile.read(length))
                if self.path != '/api/delete':
                    self.respond(404, {'error': 'Not found'})
                    return
                self.respond(200, history.delete(body.get('ids')))
            except (ValueError, AttributeError, sqlite3.Error, OSError) as exc:
                self.respond(400, {'error': str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', '~/.codex'))
    parser.add_argument('--codex-bin', default='codex')
    args = parser.parse_args()
    history = History(args.codex_home, args.codex_bin)
    history.rows()  # Fail clearly before serving if the database is unavailable.
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(history))
    print(f'Codex History: http://127.0.0.1:{server.server_port}', flush=True)
    print(f'Reading {history.home} · Ctrl+C to stop', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
