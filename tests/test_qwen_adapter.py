import json
import os
from pathlib import Path
import unittest
import stat
import subprocess
import tempfile
from dataclasses import replace

from ai_router.request import WorkerRequest
from workers.base import WorkerSetupError
from workers.qwen import (QwenAdapter, QWEN_EXECUTABLE, QWEN_MODEL,
                         QWEN_PROFILE, _READ_TOOLS)


class QwenAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = QwenAdapter(Path('/home/krakadin/.local/state/ai-workers/tmp'))
        self.request = WorkerRequest('qwen', 'trace the app entry point', 'only relevant context',
                                     Path('/home/krakadin/myDev/ai-router'), 'read-only', 300)

    def test_configuration_reports_only_safe_token_plan_metadata(self):
        info = self.adapter.configuration_info()
        self.assertEqual(info['provider'], 'Alibaba Model Studio Token Plan')
        self.assertEqual(info['endpoint_host'], 'token-plan.maas.qwencloudapi.com')
        self.assertEqual(info['cli_model'], QWEN_MODEL)
        self.assertTrue(info['credential_present'])
        self.assertNotIn('DASHSCOPE_API_KEY', repr(info))
        self.assertNotIn('baseUrl', repr(info))

    def test_command_pins_cli_model_plan_mode_and_disables_history(self):
        command = self.adapter.build_command(self.request, 'ad147ed0-a95a-40d6-8e72-bff65fdb18be')
        self.assertEqual(command[0], str(QWEN_EXECUTABLE))
        self.assertEqual(command[command.index('--model')+1], QWEN_MODEL)
        self.assertEqual(command[command.index('--approval-mode')+1], 'plan')
        self.assertEqual(command[command.index('--max-tool-calls')+1], '24')
        self.assertEqual(command[command.index('--output-format')+1], 'json')
        self.assertIn('--no-chat-recording', command)
        self.assertEqual(command[-1], QWEN_PROFILE.read_text())
        self.assertEqual(tuple(command[command.index('--core-tools')+1:command.index('--exclude-tools')]), _READ_TOOLS)
        self.assertNotIn(self.request.task, command)
        self.assertNotIn(self.request.context, command)
        self.assertEqual(command[command.index('--max-wall-time')+1], '300s')
        test_request = WorkerRequest('qwen', 'Reply exactly OK.', '', self.request.cwd,
                                     'read-only', 120, max_tool_calls=0)
        test_command = self.adapter.build_command(test_request, 'a0955703-e1dc-4592-9d3e-1fbd551364d7')
        self.assertEqual(test_command[test_command.index('--max-tool-calls')+1], '0')

    def test_payload_is_stdin_json_and_environment_does_not_cross_providers(self):
        payload = json.loads(self.adapter.build_payload(self.request, 'ad147ed0-a95a-40d6-8e72-bff65fdb18be'))
        self.assertEqual(payload['task'], self.request.task)
        self.assertEqual(payload['context'], self.request.context)
        env = self.adapter.build_environment()
        for name in ('DASHSCOPE_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'KIMI_API_KEY'):
            self.assertNotIn(name, env)

    def test_profile_names_parent_and_refuses_repository_instructions_expanding_access(self):
        profile = QWEN_PROFILE.read_text()
        self.assertIn('active parent Claude Code session', profile)
        self.assertIn('untrusted data', profile)
        self.assertIn('Do not seek or reproduce secrets', profile)
        self.assertIn('writing files', profile)

    def test_parses_qwen_json_result_and_explicit_model_usage(self):
        raw = json.dumps([
            {'type':'system','model':'qwen3.8-max'},
            {'type':'assistant','message':{'content':'The app starts in main.py.','model':'qwen3.8-max'}},
            {'type':'result','result':'The app starts in main.py.','usage':{'input_tokens':17,'output_tokens':8}},
        ]).encode()
        result = self.adapter.parse_output(raw)
        self.assertEqual(result.text, 'The app starts in main.py.')
        self.assertEqual(result.reported_model, 'qwen3.8-max')
        self.assertEqual(result.usage, {'input_tokens':17,'output_tokens':8,'cached_tokens':None})

    def test_parser_does_not_invent_reported_model_or_usage(self):
        result = self.adapter.parse_output(b'[{"type":"result","result":"WORKER_OK"}]')
        self.assertIsNone(result.reported_model)
        self.assertIsNone(result.usage)

    def test_parser_rejects_malformed_or_missing_result(self):
        for raw in (b'not-json', b'{}', b'[]', b'[{"type":"result","result":" "}]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.adapter.parse_output(raw)

    def test_wrong_endpoint_configuration_fails_closed_without_disclosing_settings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp)/'settings.json'
            settings.write_text(json.dumps({'model':{'name':QWEN_MODEL,'baseUrl':'https://wrong.invalid'},
                                            'modelProviders':{'openai':[{'id':QWEN_MODEL,'baseUrl':'https://wrong.invalid'}]},
                                            'env':{'DASHSCOPE_API_KEY':'synthetic-value'}}))
            os.chmod(settings, 0o600)
            adapter = QwenAdapter(self.adapter.runtime_tmp, settings_path=settings)
            with self.assertRaises(WorkerSetupError) as caught:
                adapter.configuration_info()
            self.assertNotIn('synthetic-value', str(caught.exception))

    def test_read_only_jobs_get_private_runtime_and_tmp_state(self):
        # Two concurrent read-only sessions must not share mutable Qwen state.
        with tempfile.TemporaryDirectory(prefix='qwen-readonly-test-') as tmp:
            runtime_tmp = Path(tmp)/'runtime'/'tmp'
            runtime_tmp.mkdir(parents=True, mode=0o700)
            os.chmod(runtime_tmp, 0o700)
            adapter = QwenAdapter(runtime_tmp)
            first = 'ad147ed0-a95a-40d6-8e72-bff65fdb18be'
            second = 'b7c10e09-04ef-41ad-b903-96f975b761ad'
            # Read-only keeps the caller's directory: no worktree, no diff.
            self.assertEqual(adapter.prepare_request(self.request, first), self.request)
            self.assertEqual(adapter.prepare_request(self.request, second), self.request)
            envs = [adapter.build_environment(job) for job in (first, second)]
            self.assertNotEqual(envs[0]['TMPDIR'], envs[1]['TMPDIR'])
            self.assertNotEqual(envs[0]['QWEN_RUNTIME_DIR'], envs[1]['QWEN_RUNTIME_DIR'])
            for env in envs:
                self.assertNotIn('DASHSCOPE_API_KEY', env)
                for name in ('TMPDIR', 'QWEN_RUNTIME_DIR'):
                    path = Path(env[name])
                    self.assertTrue(path.is_dir())
                    self.assertFalse(path.is_symlink())
                    self.assertTrue(path.is_relative_to(runtime_tmp))
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                    self.assertEqual(path.stat().st_uid, os.getuid())
            self.assertEqual(len(list(runtime_tmp.iterdir())), 2)
            adapter.cleanup(first)
            self.assertFalse(Path(envs[0]['TMPDIR']).exists())
            self.assertFalse(Path(envs[0]['QWEN_RUNTIME_DIR']).exists())
            # Cleaning one job leaves the other job's private state intact.
            self.assertTrue(Path(envs[1]['TMPDIR']).is_dir())
            self.assertTrue(Path(envs[1]['QWEN_RUNTIME_DIR']).is_dir())
            self.assertEqual(adapter.build_environment(second), envs[1])
            # A finished job keeps no per-job environment of its own.
            self.assertEqual(adapter.build_environment(first), adapter.build_environment())
            adapter.cleanup(second)
            self.assertEqual(list(runtime_tmp.iterdir()), [])

    def test_read_only_runtime_preparation_fails_closed_on_an_unsafe_tmp_directory(self):
        with tempfile.TemporaryDirectory(prefix='qwen-readonly-unsafe-') as tmp:
            runtime_tmp = Path(tmp)/'tmp'
            runtime_tmp.mkdir(mode=0o755)
            os.chmod(runtime_tmp, 0o755)
            adapter = QwenAdapter(runtime_tmp)
            job_id = 'ad147ed0-a95a-40d6-8e72-bff65fdb18be'
            with self.assertRaises(WorkerSetupError) as caught:
                adapter.prepare_request(self.request, job_id)
            self.assertEqual(caught.exception.code, 'CONFIG_ERROR')
            self.assertEqual(list(runtime_tmp.iterdir()), [])
            self.assertEqual(adapter._job_dirs, {})
            self.assertEqual(adapter._job_env, {})
            adapter.cleanup(job_id)  # nothing recorded, nothing removed

    def test_qwen_coder_edits_are_isolated_and_return_a_reviewable_diff(self):
        with tempfile.TemporaryDirectory(prefix='qwen-worktree-test-') as tmp:
            root=Path(tmp)
            repo=root/'repo';repo.mkdir()
            subprocess.run(['git','init','-q',str(repo)],check=True)
            (repo/'app.py').write_text('value = 1\n')
            subprocess.run(['git','-C',str(repo),'add','app.py'],check=True)
            subprocess.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.invalid',
                            'commit','-qm','fixture'],check=True)
            adapter=QwenAdapter(root/'runtime'/'tmp')
            job_id='184d54a6-34c8-41c8-b265-8116968089f3'
            request=replace(self.request,cwd=repo,mode='isolated-edit')
            launch=adapter.prepare_request(request,job_id)
            try:
                command=adapter.build_command(launch,job_id)
                self.assertIn('--write-root',command)
                self.assertIn('mcp__aiworker__write_file',command)
                self.assertEqual(command[command.index('--approval-mode')+1],'auto-edit')
                # Coding jobs default to an unlimited tool-call budget (-1).
                self.assertEqual(command[command.index('--max-tool-calls')+1],'-1')
                bounded=adapter.build_command(replace(launch,max_tool_calls=100),job_id)
                self.assertEqual(bounded[bounded.index('--max-tool-calls')+1],'100')
                env=adapter.build_environment(job_id)
                self.assertTrue(Path(env['QWEN_RUNTIME_DIR']).is_relative_to(adapter.runtime_jobs/job_id))
                self.assertNotIn('DASHSCOPE_API_KEY',env)
                self.assertFalse((adapter.runtime_jobs/job_id/'settings.json').exists())
                (launch.cwd/'app.py').write_text('value = 2\n')
                workspace,diff,_=adapter.collect_edit_output(job_id)
                self.assertEqual(workspace['changed_files'],['app.py'])
                self.assertIn('+value = 2',diff)
                self.assertEqual((repo/'app.py').read_text(),'value = 1\n')
                adapter.cleanup(job_id)
                self.assertTrue(launch.cwd.exists())
                self.assertFalse((adapter.runtime_jobs/job_id/'request.json').exists())
            finally:
                subprocess.run(['git','-C',str(repo),'worktree','remove','--force',str(launch.cwd)],check=True)


if __name__ == '__main__':
    unittest.main()
