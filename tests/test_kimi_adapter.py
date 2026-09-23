import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from ai_router.request import WorkerRequest
from workers.kimi import KimiAdapter, KIMI_EXECUTABLE, KIMI_MODEL


class KimiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-kimi-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime_tmp = self.root / 'tmp'
        self.runtime_tmp.mkdir(mode=0o700)
        os.chmod(self.runtime_tmp, 0o700)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.adapter = KimiAdapter(self.runtime_tmp)
        self.request = WorkerRequest('kimi', 'trace authentication', 'only relevant files',
                                     self.project, 'read-only', 600)

    def test_command_pins_cli_model_profile_and_json_without_task_in_argv(self):
        job_id = 'b7c10e09-04ef-41ad-b903-96f975b761ad'
        command = self.adapter.build_command(self.request, job_id)
        joined = '\0'.join(command)
        self.assertEqual(command[0], str(KIMI_EXECUTABLE))
        self.assertEqual(command[command.index('--model') + 1], KIMI_MODEL)
        self.assertEqual(command[command.index('--agent-file') + 1], str(self.adapter.profile))
        self.assertEqual(command[command.index('--output-format') + 1], 'stream-json')
        self.assertNotIn(self.request.task, joined)
        self.assertNotIn(self.request.context, joined)
        request_path = Path(command[command.index('--prompt') + 1].splitlines()[-1])
        request_data = json.loads(request_path.read_text())
        self.assertEqual(request_data['task'], self.request.task)
        self.assertEqual(request_data['context'], self.request.context)
        self.assertEqual(stat.S_IMODE(request_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(request_path.parent.stat().st_mode), 0o700)
        self.adapter.cleanup(job_id)
        self.assertFalse(request_path.parent.exists())

    def test_profile_allows_only_discovered_read_tools_and_no_subagents(self):
        profile = self.adapter.profile.read_text()
        self.assertIn('tools:\n  - Read\n  - Grep\n  - Glob', profile)
        self.assertIn('subagents: []', profile)

    def test_child_environment_has_no_cross_provider_credentials(self):
        env = self.adapter.build_environment()
        self.assertEqual(env['HOME'], '/home/krakadin')
        for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'DASHSCOPE_API_KEY',
                     'KIMI_API_KEY', 'KIMI_CODE_HOME'):
            self.assertNotIn(name, env)
        self.assertEqual(env['KIMI_CODE_NO_AUTO_UPDATE'], '1')

    def test_parses_assistant_jsonl_and_only_reports_explicit_usage_model(self):
        raw = b'\n'.join([
            json.dumps({'role':'assistant','content':'Diagnosis: configuration is loaded in src/config.py.'}).encode(),
            json.dumps({'role':'assistant','content':'Tests: add a config loading test.',
                        'usage':{'input_tokens':23,'output_tokens':9,'cached_tokens':4},
                        'reported_model':'k3'}).encode(),
        ])
        result = self.adapter.parse_output(raw)
        self.assertIn('Diagnosis:', result.text)
        self.assertIn('Tests:', result.text)
        self.assertEqual(result.reported_model, 'k3')
        self.assertEqual(result.usage, {'input_tokens':23,'output_tokens':9,'cached_tokens':4})

    def test_missing_reported_model_stays_unknown(self):
        result = self.adapter.parse_output(b'{"role":"assistant","content":"ok"}\n')
        self.assertIsNone(result.reported_model)
        self.assertIsNone(result.usage)

    def test_malformed_or_empty_stream_is_rejected(self):
        for raw in (b'not-json\n', b'{"role":"tool","content":"x"}\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.adapter.parse_output(raw)

    def test_task_files_require_private_runtime_directory(self):
        os.chmod(self.runtime_tmp, 0o755)
        with self.assertRaises(RuntimeError):
            self.adapter.build_command(self.request, 'f0b43ac1-7443-403c-b588-c2aa9f486c60')


if __name__ == '__main__':
    unittest.main()
