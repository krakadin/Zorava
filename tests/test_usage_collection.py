import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workers.kimi import KimiAdapter
from workers.qwen import QwenAdapter
from workers.base import ParsedOutput
from test_supervisor_core import FakeAdapter, SupervisorFixture


class KimiUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = KimiAdapter(self.root / 'tmp')
        self.job = self.root / 'job'
        self.adapter._job_dirs['test-job'] = self.job
        self.wire = self.job / 'kimi-home/sessions/wd_test/session_test/agents/main/wire.jsonl'
        self.wire.parent.mkdir(parents=True)

    def write(self, records):
        self.wire.write_text('\n'.join(json.dumps(r) for r in records))

    def test_sums_request_records_once_and_includes_cached_input(self):
        usage = {'inputOther': 10, 'output': 7, 'inputCacheRead': 20, 'inputCacheCreation': 3}
        self.write([{'type': 'usage.record', 'usage': usage},
                    {'type': 'agent.message.appended', 'message': {'usage': usage}},
                    {'type': 'context.append_loop_event', 'event': {'usage': usage}},
                    {'type': 'usage.record', 'usage': usage}])
        self.assertEqual(self.adapter.collect_usage('test-job'),
                         {'input_tokens': 66, 'output_tokens': 14, 'cached_tokens': 40})

    def test_missing_counter_is_unknown_instead_of_zero_or_partial_total(self):
        self.write([{'type': 'usage.record', 'usage': {'output': 7, 'inputCacheRead': 0}},
                    {'type': 'usage.record', 'usage': {'output': 4, 'inputCacheRead': 3,
                                                     'inputOther': 2, 'inputCacheCreation': 0}}])
        self.assertEqual(self.adapter.collect_usage('test-job'),
                         {'input_tokens': None, 'output_tokens': 11, 'cached_tokens': 3})

    def test_malformed_multiple_sessions_and_symlinks_are_unavailable(self):
        self.wire.write_text('{incomplete')
        self.assertIsNone(self.adapter.collect_usage('test-job'))
        self.wire.unlink()
        external = self.root / 'outside'
        external.write_text('{"type":"usage.record","usage":{"output":12}}')
        self.wire.symlink_to(external)
        self.assertIsNone(self.adapter.collect_usage('test-job'))
        self.wire.unlink()
        self.write([{'type': 'usage.record', 'usage': {'output': 12}}])
        second = self.job / 'kimi-home/sessions/wd_test/session_other/agents/main/wire.jsonl'
        second.parent.mkdir(parents=True)
        second.write_text(self.wire.read_text())
        self.assertIsNone(self.adapter.collect_usage('test-job'))
        self.assertIsNone(self.adapter.collect_usage('unrelated-job'))

    def test_qwen_reads_the_cli_cache_counter_name(self):
        raw = json.dumps([{'type': 'result', 'result': 'ok', 'usage': {
            'input_tokens': 23, 'output_tokens': 4, 'cache_read_input_tokens': 20}}]).encode()
        parsed = QwenAdapter(self.root / 'tmp').parse_output(raw)
        self.assertEqual(parsed.usage['cached_tokens'], 20)
        self.assertEqual(parsed.usage['input_tokens'], 23)


class UsagePersistenceTests(SupervisorFixture):
    def test_collects_job_scoped_usage_before_cleanup(self):
        adapter = FakeAdapter()
        expected = {'input_tokens': 10, 'output_tokens': 3, 'cached_tokens': 0}
        with patch.object(adapter, 'parse_output', return_value=ParsedOutput('ok')):
            adapter.collect_usage = lambda job_id: expected
            result = self.supervisor.run(self.request(), adapter)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(json.loads(self.supervisor.state.get_job(result.job_id)['usage_json']), expected)

    def test_usage_failure_does_not_fail_successful_job(self):
        adapter = FakeAdapter()
        with patch.object(adapter, 'parse_output', return_value=ParsedOutput('ok')):
            adapter.collect_usage = lambda job_id: (_ for _ in ()).throw(OSError('unavailable'))
            result = self.supervisor.run(self.request(), adapter)
        self.assertEqual(result.status, 'completed')
        self.assertIsNone(self.supervisor.state.get_job(result.job_id)['usage_json'])


if __name__ == '__main__':
    unittest.main()
