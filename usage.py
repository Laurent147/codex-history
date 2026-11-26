"""Token accounting from local rollout records; no transcript content is cached."""
import json
from pathlib import Path
import re
import threading

PRICING = json.loads((Path(__file__).parent / 'pricing.json').read_text())
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def rates_for(model):
    # Dated snapshots inherit the named model rate; never guess a different model family.
    model = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', model or '')
    return PRICING['models'].get(model)


def empty_usage():
    return {key: 0 for key in FIELDS}


class UsageReader:
    def __init__(self, home):
        self.home = home
        self.cache = {}
        self.lock = threading.Lock()

    def read(self, row):
        raw = row.get('rollout_path')
        db_total = row.get('tokens_used')
        fallback = {'total_tokens': db_total, 'input_tokens': None, 'cached_input_tokens': None,
                    'output_tokens': None, 'reasoning_output_tokens': None,
                    'cost_usd': 0.0 if db_total == 0 else None,
                    'models': [], 'note': 'Only the database total is available; no token breakdown.'}
        if not raw:
            return fallback
        path = Path(raw).expanduser().resolve()
        if path.suffix == '.zst' or not any(path.is_relative_to((self.home / root).resolve())
                                          for root in ('sessions', 'archived_sessions')):
            return fallback
        try:
            stat = path.stat()
            key = (str(path), stat.st_mtime_ns, stat.st_size, row.get('model'), db_total)
            with self.lock:
                cached = self.cache.get(row['id'])
            if cached and cached[0] == key:
                return cached[1]
            result = self.scan(path, row, fallback)
            with self.lock:
                self.cache[row['id']] = (key, result)
            return result
        except OSError:
            return fallback

    def scan(self, path, row, fallback):
        db_total = row.get('tokens_used')
        previous = empty_usage()
        buckets = {}
        model = row.get('model')
        found = False
        inconsistent = False
        cache_writes = False
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    payload = record.get('payload') or {}
                    if record.get('type') == 'turn_context':
                        model = payload.get('model') or model
                    if record.get('type') == 'token_usage_record':
                        snapshot = payload.get('thread_token_usage')
                    elif record.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                        snapshot = (payload.get('info') or {}).get('total_token_usage')
                    else:
                        continue
                    if not isinstance(snapshot, dict) or not all(k in snapshot for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')):
                        continue
                    current = {k: int(snapshot.get(k, 0)) for k in FIELDS}
                    if 'total_tokens' not in snapshot:
                        current['total_tokens'] = current['input_tokens'] + current['output_tokens']
                    if any(v < 0 for v in current.values()) or current['cached_input_tokens'] > current['input_tokens']:
                        inconsistent = True
                        continue
                    found = True
                    cache_writes |= bool(snapshot.get('cache_write_input_tokens', 0))
                    # Both record formats can report the same cumulative snapshot. Only its
                    # increase is charged, so repeated notifications never multiply usage.
                    delta = {k: current[k] - previous[k] for k in FIELDS}
                    if any(v < 0 for v in delta.values()):
                        inconsistent = True
                    else:
                        bucket = buckets.setdefault(model or 'Unknown model', empty_usage())
                        for k in FIELDS:
                            bucket[k] += delta[k]
                    previous = current
                except (ValueError, TypeError, AttributeError):
                    continue
        if not found:
            return fallback
        result = {**previous, 'cost_usd': 0.0, 'models': [], 'note': ''}
        if inconsistent or (db_total is not None and db_total > previous['total_tokens']):
            return {**fallback, 'note': 'Usage counters reset or the rollout is incomplete; cost unavailable.'}
        unknown = []
        for name, usage in buckets.items():
            if not usage['total_tokens']:
                continue
            rates = rates_for(name)
            cost = None
            # Cached input is a subset of input. Reasoning is already in output.
            if rates and usage['cached_input_tokens'] <= usage['input_tokens']:
                cost = ((usage['input_tokens'] - usage['cached_input_tokens']) * rates['input']
                        + usage['cached_input_tokens'] * rates['cached_input']
                        + usage['output_tokens'] * rates['output']) / 1_000_000
                result['cost_usd'] += cost
            else:
                unknown.append(name)
            result['models'].append({'model': name, **usage, 'cost_usd': cost})
        if unknown or cache_writes:
            result['cost_usd'] = None
            result['note'] = ('No verified rate for: ' + ', '.join(unknown)) if unknown else 'Cache-write usage present; cost unavailable.'
        return result
