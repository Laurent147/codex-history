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


def token_cost(model, usage, request_size=None):
    rates = rates_for(model)
    if not rates or usage.get('cache_write_input_tokens', 0):
        return None
    if usage['cached_input_tokens'] > usage['input_tokens']:
        return None
    uncached = usage['input_tokens'] - usage['cached_input_tokens']
    factors = rates.get('long_context', {})
    long = request_size is not None and request_size > factors.get('threshold', float('inf'))
    return (uncached * rates['input'] * (factors['input_multiplier'] if long else 1)
            + usage['cached_input_tokens'] * rates['cached_input'] * (factors['input_multiplier'] if long else 1)
            + usage['output_tokens'] * rates['output'] * (factors['output_multiplier'] if long else 1)) / 1_000_000


class UsageStream:
    """One cumulative accounting source. Different sources must never share a baseline."""
    def __init__(self):
        self.total = empty_usage()
        self.buckets = {}
        self.found = False
        self.inconsistent = False
        self.cache_writes = False
        self.responses = set()
        self.request_costs = {}
        self.long_context_requests = 0

    def add_response(self, usage, identity, model):
        if identity is None or identity in self.responses or not isinstance(usage, dict):
            return
        if not all(k in usage for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')):
            return
        values = {k: int(usage.get(k, 0)) for k in FIELDS}
        if 'total_tokens' not in usage:
            values['total_tokens'] = values['input_tokens'] + values['output_tokens']
        if any(v < 0 for v in values.values()) or values['cached_input_tokens'] > values['input_tokens']:
            self.inconsistent = True
            return
        self.responses.add(identity)
        self.cache_writes |= bool(usage.get('cache_write_input_tokens', 0))
        self.add({k: self.total[k] + values[k] for k in FIELDS}, model)
        name = model or 'Unknown model'
        cost = token_cost(name, values, values['input_tokens'])
        if cost is None or self.request_costs.get(name, 0) is None:
            self.request_costs[name] = None
        else:
            self.request_costs[name] = self.request_costs.get(name, 0) + cost
        if values['input_tokens'] > (rates_for(name) or {}).get('long_context', {}).get('threshold', float('inf')):
            self.long_context_requests += 1

    def add(self, snapshot, model):
        if not isinstance(snapshot, dict) or not all(k in snapshot for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')):
            return
        current = {k: int(snapshot.get(k, 0)) for k in FIELDS}
        if 'total_tokens' not in snapshot:
            current['total_tokens'] = current['input_tokens'] + current['output_tokens']
        if any(v < 0 for v in current.values()) or current['cached_input_tokens'] > current['input_tokens']:
            self.inconsistent = True
            return
        self.found = True
        self.cache_writes |= bool(snapshot.get('cache_write_input_tokens', 0))
        delta = {k: current[k] - self.total[k] for k in FIELDS}
        if any(v < 0 for v in delta.values()):
            self.inconsistent = True
        else:
            bucket = self.buckets.setdefault(model or 'Unknown model', empty_usage())
            for k in FIELDS:
                bucket[k] += delta[k]
        self.total = current

    def covers(self, db_total):
        return self.found and not self.inconsistent and (db_total is None or self.total['total_tokens'] >= db_total)


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
        requests = UsageStream()
        events = UsageStream()
        ledger = UsageStream()
        model = row.get('model')
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    payload = record.get('payload') or {}
                    if record.get('type') == 'turn_context':
                        model = payload.get('model') or model
                    if record.get('type') == 'token_usage_record':
                        requests.add(payload.get('thread_token_usage'), model)
                        identity = payload.get('response_id')
                        if not identity and record.get('ordinal') is not None:
                            identity = ('ordinal', record['ordinal'])
                        ledger.add_response(payload.get('usage'), identity, model)
                    elif record.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                        events.add((payload.get('info') or {}).get('total_token_usage'), model)
                except (ValueError, TypeError, AttributeError):
                    continue
        if not requests.found and not events.found and not ledger.found:
            return fallback
        # Request and UI counters can diverge after compaction. Prefer individually
        # identified responses when their sum covers all recorded usage.
        expected = max(db_total or 0, requests.total['total_tokens'], events.total['total_tokens'])
        if ledger.covers(expected):
            chosen, source = ledger, 'Individual request records'
        elif events.covers(db_total):
            chosen, source = events, 'Legacy usage snapshots'
        elif requests.covers(db_total):
            chosen, source = requests, 'Request usage snapshots'
        else:
            return {**fallback, 'note': 'Usage counters reset or the rollout is incomplete; cost unavailable.'}
        result = {**chosen.total, 'cost_usd': 0.0, 'models': [], 'note': '',
                  'source': source, 'database_total_tokens': db_total,
                  'long_context_requests': chosen.long_context_requests,
                  'base_rates_only': chosen is not ledger}
        if db_total is not None and db_total != chosen.total['total_tokens']:
            result['note'] = 'Request accounting differs from the sidebar database total; using the complete usage records.'
        unknown = []
        for name, usage in chosen.buckets.items():
            if not usage['total_tokens']:
                continue
            # Cached input is a subset of input. Reasoning is already in output.
            cost = chosen.request_costs.get(name) if chosen is ledger else token_cost(name, usage)
            if cost is not None:
                result['cost_usd'] += cost
            else:
                unknown.append(name)
            result['models'].append({'model': name, **usage, 'cost_usd': cost})
        if unknown or chosen.cache_writes:
            result['cost_usd'] = None
            result['note'] = ('No verified rate for: ' + ', '.join(unknown)) if unknown else 'Cache-write usage present; cost unavailable.'
        return result
