"""Offline tests for saved Qwen coding budget presets; no providers or real user config."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_router.settings import (DEFAULT_KIMI_CONCURRENCY, DEFAULT_KIMI_MODEL_PROFILE,
                                DEFAULT_QWEN_CODING_BUDGET,
                                DEFAULT_QWEN_CONCURRENCY, DEFAULT_QWEN_MODEL_PROFILE,
                                MAX_CONCURRENCY, MAX_TOOL_CALLS_LIMIT,
                                SETTINGS_FIELDS, UNLIMITED_TOOL_CALLS, SettingsError,
                                load_qwen_coding_budget, load_settings, load_worker_concurrency,
                                parse_budget_token, parse_concurrency_token,
                                save_settings, settings_path, validate_budget, validate_concurrency)
from ai_router.supervisor import Supervisor
from workers.base import ParsedOutput, common_child_environment


WORKER_PATH = Path(__file__).resolve().parents[1] / 'bin' / 'worker.py'


def load_worker_module():
    spec = importlib.util.spec_from_file_location('ai_worker_cli_settings', WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SettingsModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-settings-test-')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'runtime'
        self.runtime.mkdir(mode=0o700)

    def write_raw(self, content: bytes, mode=0o600):
        path = settings_path(self.runtime)
        path.write_bytes(content)
        os.chmod(path, mode)
        return path

    def test_missing_file_yields_unlimited_default(self):
        self.assertEqual(DEFAULT_QWEN_CODING_BUDGET, UNLIMITED_TOOL_CALLS)
        self.assertEqual(load_settings(self.runtime),
                         {'qwen_coding_max_tool_calls': UNLIMITED_TOOL_CALLS,
                          'qwen_concurrency': DEFAULT_QWEN_CONCURRENCY,
                          'kimi_concurrency': DEFAULT_KIMI_CONCURRENCY,
                          'qwen_model_profile': DEFAULT_QWEN_MODEL_PROFILE,
                          'kimi_model_profile': DEFAULT_KIMI_MODEL_PROFILE})
        self.assertEqual(load_qwen_coding_budget(self.runtime), UNLIMITED_TOOL_CALLS)

    def test_default_concurrency_is_two_for_qwen_and_one_for_kimi(self):
        self.assertEqual(DEFAULT_QWEN_CONCURRENCY, 2)
        self.assertEqual(DEFAULT_KIMI_CONCURRENCY, 1)
        self.assertEqual(load_worker_concurrency(self.runtime, 'qwen'), 2)
        self.assertEqual(load_worker_concurrency(self.runtime, 'kimi'), 1)
        with self.assertRaises(SettingsError):
            load_worker_concurrency(self.runtime, 'other')

    def test_validate_and_parse_concurrency_are_strictly_bounded(self):
        for value in (True, False, '2', 1.5, 0, -1, MAX_CONCURRENCY + 1, None):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                validate_concurrency(value)
        for value in (1, 2, MAX_CONCURRENCY):
            self.assertEqual(validate_concurrency(value), value)
        self.assertEqual(parse_concurrency_token(' 2 '), 2)
        for token in ('', 'abc', 'unlimited', '0', str(MAX_CONCURRENCY + 1), '-1', '1.5', '0x2'):
            with self.subTest(token=token), self.assertRaises(SettingsError) as caught:
                parse_concurrency_token(token)
            self.assertEqual(caught.exception.code, 'INVALID_REQUEST')

    def test_schema_one_file_without_concurrency_migrates_to_defaults(self):
        # Migration compatibility: a settings.json saved before the
        # concurrency fields existed keeps its budget and gains defaults.
        self.write_raw(json.dumps({'schema_version': 1, 'qwen_coding_max_tool_calls': 50}).encode())
        settings = load_settings(self.runtime)
        self.assertEqual(settings['qwen_coding_max_tool_calls'], 50)
        self.assertEqual(settings['qwen_concurrency'], 2)
        self.assertEqual(settings['kimi_concurrency'], 1)

    def test_save_preserves_unspecified_fields(self):
        save_settings(self.runtime, qwen_coding_max_tool_calls=75)
        saved = save_settings(self.runtime, qwen_concurrency=3)
        self.assertEqual(saved['qwen_coding_max_tool_calls'], 75)
        self.assertEqual(saved['qwen_concurrency'], 3)
        self.assertEqual(saved['kimi_concurrency'], 1)
        saved = save_settings(self.runtime, kimi_concurrency=2)
        self.assertEqual(saved['qwen_coding_max_tool_calls'], 75)
        self.assertEqual(saved['qwen_concurrency'], 3)
        self.assertEqual(saved['kimi_concurrency'], 2)
        path = settings_path(self.runtime)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for leftover in self.runtime.iterdir():
            self.assertEqual(leftover.name, 'settings.json')

    def test_invalid_saved_concurrency_is_rejected(self):
        for bad in (0, MAX_CONCURRENCY + 1, True, '2'):
            with self.subTest(bad=bad):
                self.write_raw(json.dumps({'schema_version': 1, 'qwen_concurrency': bad}).encode())
                with self.assertRaises(SettingsError):
                    load_settings(self.runtime)

    def test_field_schema_is_introspectable(self):
        field = SETTINGS_FIELDS['qwen_coding_max_tool_calls']
        self.assertEqual(field['default'], UNLIMITED_TOOL_CALLS)
        self.assertEqual(field['maximum'], MAX_TOOL_CALLS_LIMIT)

    def test_save_roundtrip_is_atomic_and_private(self):
        saved = save_settings(self.runtime, qwen_coding_max_tool_calls=100)
        self.assertEqual(saved['qwen_coding_max_tool_calls'], 100)
        path = settings_path(self.runtime)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(load_qwen_coding_budget(self.runtime), 100)
        for leftover in self.runtime.iterdir():
            self.assertEqual(leftover.name, 'settings.json')

    def test_validate_budget_rejects_bad_types_and_ranges(self):
        for value in (True, False, '100', 1.5, -2, MAX_TOOL_CALLS_LIMIT + 1, None):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                validate_budget(value)
        for value in (UNLIMITED_TOOL_CALLS, 0, 24, MAX_TOOL_CALLS_LIMIT):
            self.assertEqual(validate_budget(value), value)

    def test_parse_budget_token(self):
        self.assertEqual(parse_budget_token('unlimited'), UNLIMITED_TOOL_CALLS)
        self.assertEqual(parse_budget_token(' Unlimited '), UNLIMITED_TOOL_CALLS)
        self.assertEqual(parse_budget_token('0'), 0)
        self.assertEqual(parse_budget_token('200'), 200)
        self.assertEqual(parse_budget_token(str(MAX_TOOL_CALLS_LIMIT)), MAX_TOOL_CALLS_LIMIT)
        for token in ('', 'abc', '1.5', '-2', '1_0', '+5', str(MAX_TOOL_CALLS_LIMIT + 1), '0x10'):
            with self.subTest(token=token), self.assertRaises(SettingsError) as caught:
                parse_budget_token(token)
            self.assertEqual(caught.exception.code, 'INVALID_REQUEST')

    def test_malformed_config_is_rejected_and_never_overwritten(self):
        path = self.write_raw(b'{not json')
        with self.assertRaises(SettingsError):
            load_settings(self.runtime)
        with self.assertRaises(SettingsError):
            save_settings(self.runtime, qwen_coding_max_tool_calls=50)
        self.assertEqual(path.read_bytes(), b'{not json')

    def test_invalid_saved_budget_is_rejected(self):
        self.write_raw(json.dumps({'schema_version': 1, 'qwen_coding_max_tool_calls': -2}).encode())
        with self.assertRaises(SettingsError):
            load_settings(self.runtime)
        self.write_raw(json.dumps({'schema_version': 1, 'qwen_coding_max_tool_calls': True}).encode())
        with self.assertRaises(SettingsError):
            load_settings(self.runtime)
        self.write_raw(json.dumps({'schema_version': 99}).encode())
        with self.assertRaises(SettingsError):
            load_settings(self.runtime)

    def test_symlink_and_nonprivate_files_are_rejected(self):
        target = self.write_raw(b'{}')
        os.unlink(target)
        outside = Path(self.temp.name) / 'elsewhere.json'
        outside.write_text('{}')
        target.symlink_to(outside)
        with self.assertRaises(SettingsError):
            load_settings(self.runtime)
        os.unlink(target)
        self.write_raw(b'{}', mode=0o644)
        with self.assertRaises(SettingsError):
            load_settings(self.runtime)

    def test_save_requires_private_runtime_directory(self):
        self.runtime.chmod(0o755)
        self.addCleanup(self.runtime.chmod, 0o700)
        with self.assertRaises(SettingsError):
            save_settings(self.runtime, qwen_coding_max_tool_calls=10)


class CapturingAdapter:
    """Minimal fake worker that records the launch request it receives."""

    name = 'qwen'
    role = 'test worker'
    requested_model = 'fake-v1'

    def __init__(self):
        self.captured = None

    def version(self):
        return 'fake-1'

    def build_command(self, request, job_id):
        self.captured = request
        return [sys.executable, '-B', '-c',
                'import json,sys; json.load(sys.stdin); '
                'print(json.dumps({"text":"ok","reported_model":"fake-v1"}))']

    def build_payload(self, request, job_id):
        return json.dumps({'job_id': job_id, 'task': request.task}).encode()

    def build_environment(self, job_id=None):
        return common_child_environment()

    def parse_output(self, raw):
        data = json.loads(raw.decode())
        return ParsedOutput(data['text'], data.get('reported_model'), data.get('usage'))


class SupervisorBudgetPresetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-budget-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.adapter = CapturingAdapter()
        self.supervisor = Supervisor(self.runtime, (self.root,), {'qwen': self.adapter})

    def request(self, **overrides):
        data = {'worker': 'qwen', 'task': 'implement the change', 'cwd': str(self.project),
                'mode': 'isolated-edit', 'timeout_seconds': 30}
        data.update(overrides)
        return json.dumps(data).encode()

    def test_new_install_coding_default_is_unlimited(self):
        result = self.supervisor.run(self.request(), self.adapter)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(self.adapter.captured.max_tool_calls, UNLIMITED_TOOL_CALLS)

    def test_saved_preset_applies_to_isolated_edit_only(self):
        save_settings(self.runtime, qwen_coding_max_tool_calls=7)
        self.supervisor.run(self.request(), self.adapter)
        self.assertEqual(self.adapter.captured.max_tool_calls, 7)
        # Read-only requests keep their fixed policy; the preset is not applied.
        self.supervisor.run(self.request(mode='read-only'), self.adapter)
        self.assertIsNone(self.adapter.captured.max_tool_calls)

    def test_explicit_request_override_wins_over_preset(self):
        save_settings(self.runtime, qwen_coding_max_tool_calls=7)
        self.supervisor.run(self.request(max_tool_calls=3), self.adapter)
        self.assertEqual(self.adapter.captured.max_tool_calls, 3)
        self.supervisor.run(self.request(max_tool_calls=UNLIMITED_TOOL_CALLS), self.adapter)
        self.assertEqual(self.adapter.captured.max_tool_calls, UNLIMITED_TOOL_CALLS)

    def test_unsafe_or_malformed_preset_fails_closed(self):
        path = settings_path(self.runtime)
        path.write_bytes(b'{oops')
        os.chmod(path, 0o600)
        result = self.supervisor.run(self.request(), self.adapter)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error['code'], 'CONFIG_ERROR')
        self.assertIsNone(self.adapter.captured)
        # No retry, no overwrite: the malformed file is untouched.
        self.assertEqual(path.read_bytes(), b'{oops')


class SettingsCliTests(unittest.TestCase):
    def setUp(self):
        self.worker = load_worker_module()
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-settings-cli-')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'state'
        self.runtime.mkdir(mode=0o700)
        os.chmod(self.runtime, 0o700)

    def invoke(self, func, **kwargs):
        buffer = io.StringIO()
        args = SimpleNamespace(json=True, **kwargs)
        with patch.object(self.worker, 'RUNTIME', self.runtime), \
             contextlib.redirect_stdout(buffer):
            code = func(args)
        return code, json.loads(buffer.getvalue())

    def test_show_defaults_and_set_roundtrip(self):
        code, value = self.invoke(self.worker.command_settings_show)
        self.assertEqual(code, 0)
        self.assertEqual(value['settings']['qwen_coding_max_tool_calls'], UNLIMITED_TOOL_CALLS)
        self.assertEqual(value['settings']['qwen_coding_budget'], 'unlimited')
        code, value = self.invoke(self.worker.command_settings_set,
                                  key='qwen-coding-budget', value='200')
        self.assertEqual(code, 0)
        self.assertEqual(value['settings']['qwen_coding_max_tool_calls'], 200)
        code, value = self.invoke(self.worker.command_settings_show)
        self.assertEqual(value['settings']['qwen_coding_budget'], 200)
        code, value = self.invoke(self.worker.command_settings_set,
                                  key='qwen-coding-budget', value='unlimited')
        self.assertEqual(code, 0)
        self.assertEqual(value['settings']['qwen_coding_max_tool_calls'], UNLIMITED_TOOL_CALLS)

    def test_set_rejects_invalid_values_without_writing(self):
        for token in ('abc', '-2', '1.5', str(MAX_TOOL_CALLS_LIMIT + 1)):
            with self.subTest(token=token):
                code, value = self.invoke(self.worker.command_settings_set,
                                          key='qwen-coding-budget', value=token)
                self.assertEqual(code, 2)
                self.assertEqual(value['error']['code'], 'INVALID_REQUEST')
        self.assertFalse(settings_path(self.runtime).exists())

    def test_set_refuses_to_overwrite_malformed_config(self):
        path = settings_path(self.runtime)
        path.write_bytes(b'junk')
        os.chmod(path, 0o600)
        code, value = self.invoke(self.worker.command_settings_set,
                                  key='qwen-coding-budget', value='10')
        self.assertEqual(code, 3)
        self.assertEqual(value['error']['code'], 'CONFIG_ERROR')
        self.assertEqual(path.read_bytes(), b'junk')
        code, value = self.invoke(self.worker.command_settings_show)
        self.assertEqual(code, 3)

    def test_concurrency_set_show_and_preservation(self):
        code, value = self.invoke(self.worker.command_settings_show)
        self.assertEqual(code, 0)
        self.assertEqual(value['settings']['qwen_concurrency'], 2)
        self.assertEqual(value['settings']['kimi_concurrency'], 1)
        code, value = self.invoke(self.worker.command_settings_set,
                                  key='qwen-concurrency', value='3')
        self.assertEqual(code, 0)
        self.assertEqual(value['settings']['qwen_concurrency'], 3)
        self.assertEqual(value['settings']['kimi_concurrency'], 1)
        code, value = self.invoke(self.worker.command_settings_set,
                                  key='qwen-coding-budget', value='90')
        self.assertEqual(code, 0)
        # Setting one key never disturbs another saved key.
        self.assertEqual(value['settings']['qwen_concurrency'], 3)
        self.assertEqual(value['settings']['qwen_coding_max_tool_calls'], 90)
        code, value = self.invoke(self.worker.command_settings_set,
                                  key='kimi-concurrency', value='2')
        self.assertEqual(code, 0)
        self.assertEqual(value['settings']['kimi_concurrency'], 2)
        code, value = self.invoke(self.worker.command_settings_show)
        self.assertEqual((value['settings']['qwen_concurrency'], value['settings']['kimi_concurrency']), (3, 2))

    def test_concurrency_set_rejects_invalid_values_without_writing(self):
        for key in ('qwen-concurrency', 'kimi-concurrency'):
            for token in ('0', '5', 'abc', 'unlimited', '-1', '1.5'):
                with self.subTest(key=key, token=token):
                    code, value = self.invoke(self.worker.command_settings_set,
                                              key=key, value=token)
                    self.assertEqual(code, 2)
                    self.assertEqual(value['error']['code'], 'INVALID_REQUEST')
        self.assertFalse(settings_path(self.runtime).exists())

    def test_model_profile_set_show_and_validation(self):
        profiles = [{'id': 'qwen3.8-max'}, {'id': 'qwen3.8-flash'}]
        kimi_profiles = [{'id': 'kimi-code/k3'}, {'id': 'kimi-code/k3-256k'}]
        with patch('workers.qwen.verified_model_profiles', return_value=profiles), \
             patch('workers.kimi.verified_model_profiles', return_value=kimi_profiles):
            code, value = self.invoke(self.worker.command_settings_show)
            self.assertEqual(code, 0)
            self.assertEqual(value['settings']['qwen_model_profile'], 'qwen3.8-max')
            self.assertEqual(value['settings']['kimi_model_profile'], 'kimi-code/k3')
            code, value = self.invoke(self.worker.command_settings_set,
                                      key='qwen-model', value='qwen3.8-flash')
            self.assertEqual(code, 0)
            self.assertEqual(value['settings']['qwen_model_profile'], 'qwen3.8-flash')
            # Setting one key never disturbs another saved key.
            self.assertEqual(value['settings']['kimi_model_profile'], 'kimi-code/k3')
            code, value = self.invoke(self.worker.command_settings_set,
                                      key='kimi-model', value='kimi-code/k3-256k')
            self.assertEqual(code, 0)
            self.assertEqual(value['settings']['kimi_model_profile'], 'kimi-code/k3-256k')
            # Unverified or foreign model IDs are rejected without writing.
            for key, token in (('qwen-model', 'gpt-5'), ('qwen-model', 'qwen3.7-max'),
                               ('kimi-model', 'qwen3.8-flash'), ('qwen-model', 'bad id')):
                with self.subTest(key=key, token=token):
                    code, value = self.invoke(self.worker.command_settings_set,
                                              key=key, value=token)
                    self.assertEqual(code, 2)
                    self.assertEqual(value['error']['code'], 'INVALID_REQUEST')
        # The rejected writes left the saved selections untouched.
        code, value = self.invoke(self.worker.command_settings_show)
        self.assertEqual(value['settings']['qwen_model_profile'], 'qwen3.8-flash')
        self.assertEqual(value['settings']['kimi_model_profile'], 'kimi-code/k3-256k')

    def test_delegate_parses_max_tool_calls_flag(self):
        parser = self.worker.make_parser()
        args = parser.parse_args(['delegate', 'qwen', '--cwd', '/tmp',
                                  '--max-tool-calls', 'unlimited'])
        self.assertEqual(args.max_tool_calls, 'unlimited')
        args = parser.parse_args(['delegate', 'qwen', '--cwd', '/tmp',
                                  '--max-tool-calls', '150'])
        self.assertEqual(args.max_tool_calls, '150')


if __name__ == '__main__':
    unittest.main()
