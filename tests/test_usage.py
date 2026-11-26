import json
from pathlib import Path
import tempfile
import unittest

from app import History
from usage import UsageReader, rates_for
from test_app import fixture, IDS


def event(input_count, cached, output, reasoning=0):
    return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'total_token_usage': {'input_tokens': input_count, 'cached_input_tokens': cached,
                              'output_tokens': output, 'reasoning_output_tokens': reasoning,
                              'total_tokens': input_count + output}}}}


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'sessions').mkdir()
        self.path = self.home / 'sessions' / 'usage.jsonl'
        self.reader = UsageReader(self.home)
        self.row = {'id': 'example', 'model': 'gpt-5.5', 'rollout_path': str(self.path), 'tokens_used': None}

    def write(self, records):
        self.path.write_text('\n'.join(json.dumps(r) for r in records) + '\n')

    def test_cached_and_reasoning_tokens_are_not_double_charged(self):
        self.write([event(1_000_000, 800_000, 100_000, 50_000)] * 3)
        result = self.reader.read(self.row)
        self.assertEqual(result['total_tokens'], 1_100_000)
        self.assertEqual(result['cost_usd'], 4.4)  # 0.2M*$5 + 0.8M*$0.50 + 0.1M*$30

    def test_model_switch_and_duplicate_paginated_snapshot(self):
        first = event(1000, 800, 100)
        self.write([{'type': 'turn_context', 'payload': {'model': 'gpt-5.4'}}, first,
                    {'type': 'turn_context', 'payload': {'model': 'gpt-5.5'}},
                    {'type': 'token_usage_record', 'payload': {
                        'thread_token_usage': event(2000, 1600, 200)['payload']['info']['total_token_usage']}},
                    event(2000, 1600, 200)])
        result = self.reader.read(self.row)
        self.assertEqual(result['total_tokens'], 2200)
        self.assertAlmostEqual(result['cost_usd'], 0.0022 + 0.0044)
        self.assertEqual(len(result['models']), 2)

    def test_missing_and_unknown_models_do_not_get_zero_cost(self):
        self.row['tokens_used'] = 999
        self.assertEqual(self.reader.read(self.row)['total_tokens'], 999)
        self.assertIsNone(self.reader.read(self.row)['cost_usd'])
        self.write([event(1000, 800, 100)])
        self.row['model'] = 'unpublished-model'
        self.assertIsNone(self.reader.read(self.row)['cost_usd'])
        self.assertEqual(self.reader.read(self.row)['total_tokens'], 1100)

    def test_counter_reset_and_truncated_rollout_are_not_priced(self):
        self.write([event(1000, 800, 100), event(500, 400, 50)])
        self.row['tokens_used'] = 2000
        result = self.reader.read(self.row)
        self.assertEqual(result['total_tokens'], 2000)
        self.assertIsNone(result['cost_usd'])
        self.write([event(500, 400, 50)])
        self.assertIsNone(self.reader.read(self.row)['cost_usd'])

    def test_cache_invalidates_and_malformed_lines_are_ignored(self):
        self.write([event(1000, 800, 100)])
        self.assertEqual(self.reader.read(self.row)['total_tokens'], 1100)
        with self.path.open('a') as stream:
            stream.write('invalid partial record\n' + json.dumps(event(2000, 1600, 200)) + '\n')
        self.assertEqual(self.reader.read(self.row)['total_tokens'], 2200)

    def test_snapshot_alias_and_unknown_rates(self):
        self.assertEqual(rates_for('gpt-5.5-2026-04-23'), rates_for('gpt-5.5'))
        self.assertIsNone(rates_for('gpt-5.5-mystery'))

    def test_summary_follows_filters_and_partial_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            history = fixture(Path(directory))
            path = Path(directory) / 'sessions' / 'legacy.jsonl'
            with path.open('a') as stream:
                stream.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.5'}}) + '\n')
                stream.write(json.dumps(event(1000, 800, 100)) + '\n')
            result = history.sessions({'q': IDS[0]})
            self.assertEqual(result['summary']['total_tokens'], 1100)
            self.assertEqual(result['summary']['sessions'], 1)
            self.assertEqual(result['summary']['cost_available'], 1)
            result = history.sessions({'q': 'no matches'})
            self.assertEqual(result['summary']['total_tokens'], 0)
            self.assertEqual(result['summary']['cost_usd'], 0)


if __name__ == '__main__':
    unittest.main()
