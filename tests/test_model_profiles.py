"""Offline tests for verified model profile catalogs, selection, and persistence.

No provider calls and no real user configuration are used: discovery is fed
synthetic provider-owned config files in temporary directories or patched out
entirely.
"""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

import workers.kimi as kimi_module
import workers.qwen as qwen_module
from ai_router.request import WorkerRequest
from ai_router.settings import (DEFAULT_KIMI_MODEL_PROFILE, DEFAULT_QWEN_MODEL_PROFILE,
                                SettingsError, load_settings, load_worker_model_profile,
                                save_settings, settings_path, validate_model_profile_selection,
                                verified_model_catalog)
from ai_router.supervisor import Supervisor
from workers.base import ParsedOutput, WorkerSetupError, common_child_environment
from workers.kimi import KIMI_MODEL, KimiAdapter
from workers.qwen import QWEN_MODEL, QWEN_TOKEN_PLAN_URL, QwenAdapter


DASHSCOPE_PAYG_URL = 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1'


def write_qwen_settings(path: Path, entries, *, model_name=QWEN_MODEL,
                        model_base_url=QWEN_TOKEN_PLAN_URL):
    path.write_text(json.dumps({
        'model': {'name': model_name, 'baseUrl': model_base_url},
        'modelProviders': {'openai': entries},
        'env': {'DASHSCOPE_API_KEY': 'synthetic-key'},
    }))
    os.chmod(path, 0o600)
    return path


class QwenCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-qwen-catalog-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / 'settings.json'

    def test_catalog_lists_only_token_plan_profiles(self):
        write_qwen_settings(self.settings, [
            {'id': 'qwen3.8-max', 'baseUrl': QWEN_TOKEN_PLAN_URL,
             'apiKey': 'synthetic-key', 'contextWindow': 1000000},
            {'id': 'qwen3.8-max-0902', 'baseUrl': QWEN_TOKEN_PLAN_URL},
            {'id': 'qwen3.7-max', 'baseUrl': DASHSCOPE_PAYG_URL, 'apiKey': 'synthetic-key'},
        ])
        profiles = qwen_module.verified_model_profiles(self.settings)
        self.assertEqual([p['id'] for p in profiles], ['qwen3.8-max', 'qwen3.8-max-0902'])
        self.assertEqual(profiles[0]['context_window'], 1000000)
        self.assertIsNone(profiles[1]['context_window'])
        # The pay-as-you-go DashScope entry and all credential material stay out.
        self.assertNotIn('qwen3.7-max', [p['id'] for p in profiles])
        for profile in profiles:
            self.assertEqual(sorted(profile), ['context_window', 'display_name', 'id'])
        self.assertNotIn('synthetic-key', repr(profiles))
        self.assertNotIn('dashscope', repr(profiles).lower())
        self.assertNotIn('baseUrl', repr(profiles))

    def test_catalog_rejects_unsafe_or_missing_qwen_settings(self):
        with self.assertRaises(WorkerSetupError):
            qwen_module.verified_model_profiles(self.settings)  # missing
        write_qwen_settings(self.settings, [{'id': QWEN_MODEL, 'baseUrl': QWEN_TOKEN_PLAN_URL}])
        os.chmod(self.settings, 0o644)
        with self.assertRaises(WorkerSetupError):
            qwen_module.verified_model_profiles(self.settings)
        os.chmod(self.settings, 0o600)
        self.settings.write_bytes(b'{oops')
        with self.assertRaises(WorkerSetupError):
            qwen_module.verified_model_profiles(self.settings)

    def test_catalog_falls_back_to_reviewed_default_when_discovery_unavailable(self):
        with patch.object(qwen_module, 'verified_model_profiles',
                          side_effect=WorkerSetupError('CONFIG_ERROR', 'unavailable')):
            self.assertEqual(verified_model_catalog('qwen'),
                             [{'id': DEFAULT_QWEN_MODEL_PROFILE,
                               'display_name': DEFAULT_QWEN_MODEL_PROFILE,
                               'context_window': None}])
        with patch.object(qwen_module, 'verified_model_profiles', return_value=[]):
            self.assertEqual(verified_model_catalog('qwen')[0]['id'], DEFAULT_QWEN_MODEL_PROFILE)
        self.assertEqual(DEFAULT_QWEN_MODEL_PROFILE, QWEN_MODEL)

    def test_selection_validation_uses_the_token_plan_allowlist(self):
        profiles = [{'id': 'qwen3.8-max'}, {'id': 'qwen3.8-max-0902'}]
        with patch.object(qwen_module, 'verified_model_profiles', return_value=profiles):
            self.assertEqual(validate_model_profile_selection('qwen', 'qwen3.8-max-0902'),
                             'qwen3.8-max-0902')
            self.assertEqual(validate_model_profile_selection('qwen', ' qwen3.8-max '),
                             'qwen3.8-max')
            for bad in ('qwen3.7-max', 'gpt-5', 'kimi-code/k3', ''):
                with self.subTest(bad=bad), self.assertRaises(SettingsError) as caught:
                    validate_model_profile_selection('qwen', bad)
                self.assertEqual(caught.exception.code, 'INVALID_REQUEST')

    def test_selection_falls_back_to_default_only_when_discovery_unavailable(self):
        with patch.object(qwen_module, 'verified_model_profiles',
                          side_effect=WorkerSetupError('CONFIG_ERROR', 'unavailable')):
            self.assertEqual(validate_model_profile_selection('qwen', QWEN_MODEL), QWEN_MODEL)
            with self.assertRaises(SettingsError):
                validate_model_profile_selection('qwen', 'qwen3.8-max-0902')

    def test_selection_rejects_malformed_ids_and_non_strings(self):
        for bad in (None, 5, True, 'has space', 'https://evil.example/v1',
                    '../escape', '-leading-dash', 'x' * 300, 'token\nplan'):
            with self.subTest(bad=bad), self.assertRaises(SettingsError) as caught:
                validate_model_profile_selection('qwen', bad)
            self.assertEqual(caught.exception.code, 'INVALID_REQUEST')
        with self.assertRaises(SettingsError):
            validate_model_profile_selection('claude', QWEN_MODEL)


class QwenAdapterModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-qwen-model-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime_tmp = self.root / 'runtime' / 'tmp'
        self.runtime_tmp.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime_tmp, 0o700)
        self.settings = write_qwen_settings(self.root / 'settings.json', [
            {'id': 'qwen3.8-max', 'baseUrl': QWEN_TOKEN_PLAN_URL, 'apiKey': 'synthetic-key'},
            {'id': 'qwen3.8-max-0902', 'baseUrl': QWEN_TOKEN_PLAN_URL},
            {'id': 'qwen3.7-max', 'baseUrl': DASHSCOPE_PAYG_URL, 'apiKey': 'synthetic-key'},
        ])
        self.adapter = QwenAdapter(self.runtime_tmp, settings_path=self.settings)
        self.request = WorkerRequest('qwen', 'trace the app entry point', 'context',
                                     self.root, 'read-only', 300)

    def test_default_behavior_is_unchanged_without_selection(self):
        command = self.adapter.build_command(self.request, 'ad147ed0-a95a-40d6-8e72-bff65fdb18be')
        self.assertEqual(command[command.index('--model') + 1], QWEN_MODEL)

    def test_selected_verified_profile_reaches_the_command(self):
        self.adapter.select_model_profile('qwen3.8-max-0902')
        self.assertEqual(self.adapter.requested_model, 'qwen3.8-max-0902')
        command = self.adapter.build_command(self.request, 'ad147ed0-a95a-40d6-8e72-bff65fdb18be')
        self.assertEqual(command[command.index('--model') + 1], 'qwen3.8-max-0902')

    def test_pay_as_you_go_profile_is_never_selectable(self):
        with self.assertRaises(WorkerSetupError) as caught:
            self.adapter.select_model_profile('qwen3.7-max')
        self.assertEqual(caught.exception.code, 'CONFIG_ERROR')
        self.assertNotIn('synthetic-key', str(caught.exception))
        self.assertEqual(self.adapter.requested_model, QWEN_MODEL)

    def test_execution_revalidates_the_endpoint_pairing(self):
        # A tampered in-memory selection must fail closed at command build time
        # even when selection-time validation was bypassed.
        self.adapter.requested_model = 'qwen3.7-max'
        with self.assertRaises(WorkerSetupError) as caught:
            self.adapter.build_command(self.request, 'ad147ed0-a95a-40d6-8e72-bff65fdb18be')
        self.assertEqual(caught.exception.code, 'CONFIG_ERROR')
        self.adapter.requested_model = 'unknown-model'
        with self.assertRaises(WorkerSetupError):
            self.adapter.build_command(self.request, 'ad147ed0-a95a-40d6-8e72-bff65fdb18be')


KIMI_CONFIG_TEXT = '''
default_model = "kimi-code/k3"

[providers.kimi-code]
type = "kimi"
base_url = "https://api.kimi.com/coding/v1"
api_key = ""

[providers.paas]
type = "openai"
base_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
api_key = "synthetic-key"

[models.k3]
provider = "kimi-code"
model = "k3"
max_context_size = 1048576

[models.k3-256k]
provider = "kimi-code"
model = "k3-256k"
max_context_size = 262144

[models."kimi-code/kimi-for-coding"]
provider = "kimi-code"
model = "kimi-for-coding"

[models.qwen3-max]
provider = "paas"
model = "qwen3-max"
'''


class KimiCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-kimi-catalog-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.toml'

    def write_config(self, text=KIMI_CONFIG_TEXT, mode=0o600):
        self.config.write_text(text)
        os.chmod(self.config, mode)
        return self.config

    def test_catalog_lists_only_managed_kimi_code_aliases(self):
        self.write_config()
        profiles = kimi_module.verified_model_profiles(self.config)
        ids = [p['id'] for p in profiles]
        self.assertEqual(ids, ['kimi-code/k3', 'kimi-code/k3-256k', 'kimi-code/kimi-for-coding'])
        self.assertEqual(profiles[0]['context_window'], 1048576)
        self.assertEqual(profiles[1]['context_window'], 262144)
        self.assertIsNone(profiles[2]['context_window'])
        # Aliases bound to a static-key provider or another endpoint stay out.
        self.assertNotIn('paas/qwen3-max', ids)
        for profile in profiles:
            self.assertEqual(sorted(profile), ['context_window', 'display_name', 'id'])
        self.assertNotIn('synthetic-key', repr(profiles))
        self.assertNotIn('base_url', repr(profiles))

    def test_wrong_endpoint_provider_is_excluded(self):
        text = KIMI_CONFIG_TEXT.replace('https://api.kimi.com/coding/v1',
                                        'https://evil.example/v1')
        self.write_config(text)
        self.assertEqual(kimi_module.verified_model_profiles(self.config), [])

    def test_catalog_rejects_unsafe_or_missing_kimi_config(self):
        with self.assertRaises(WorkerSetupError):
            kimi_module.verified_model_profiles(self.config)  # missing
        self.write_config(mode=0o644)
        with self.assertRaises(WorkerSetupError):
            kimi_module.verified_model_profiles(self.config)
        self.write_config('not [toml', mode=0o600)
        with self.assertRaises(WorkerSetupError):
            kimi_module.verified_model_profiles(self.config)

    def test_catalog_falls_back_to_reviewed_default_when_discovery_unavailable(self):
        with patch.object(kimi_module, 'verified_model_profiles',
                          side_effect=WorkerSetupError('CONFIG_ERROR', 'unavailable')):
            self.assertEqual(verified_model_catalog('kimi'),
                             [{'id': DEFAULT_KIMI_MODEL_PROFILE,
                               'display_name': DEFAULT_KIMI_MODEL_PROFILE,
                               'context_window': None}])
        self.assertEqual(DEFAULT_KIMI_MODEL_PROFILE, KIMI_MODEL)

    def test_selection_validation_uses_the_managed_alias_allowlist(self):
        profiles = [{'id': 'kimi-code/k3'}, {'id': 'kimi-code/k3-256k'}]
        with patch.object(kimi_module, 'verified_model_profiles', return_value=profiles):
            self.assertEqual(validate_model_profile_selection('kimi', 'kimi-code/k3-256k'),
                             'kimi-code/k3-256k')
            for bad in ('qwen3.8-max', 'paas/qwen3-max', 'kimi-code/unknown', ''):
                with self.subTest(bad=bad), self.assertRaises(SettingsError) as caught:
                    validate_model_profile_selection('kimi', bad)
                self.assertEqual(caught.exception.code, 'INVALID_REQUEST')


class KimiAdapterModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-kimi-model-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime_tmp = self.root / 'tmp'
        self.runtime_tmp.mkdir(mode=0o700)
        os.chmod(self.runtime_tmp, 0o700)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.config = self.root / 'config.toml'
        self.config.write_text(KIMI_CONFIG_TEXT)
        os.chmod(self.config, 0o600)
        self.adapter = KimiAdapter(self.runtime_tmp, config_path=self.config)
        self.request = WorkerRequest('kimi', 'trace authentication', 'context',
                                     self.project, 'read-only', 600)

    def test_default_behavior_is_unchanged_without_selection(self):
        command = self.adapter.build_command(self.request, 'b7c10e09-04ef-41ad-b903-96f975b761ad')
        self.assertEqual(command[command.index('--model') + 1], KIMI_MODEL)
        self.adapter.cleanup('b7c10e09-04ef-41ad-b903-96f975b761ad')

    def test_selected_verified_alias_reaches_commands_and_smoke_test(self):
        self.adapter.select_model_profile('kimi-code/k3-256k')
        self.assertEqual(self.adapter.requested_model, 'kimi-code/k3-256k')
        command = self.adapter.build_command(self.request, 'b7c10e09-04ef-41ad-b903-96f975b761ad')
        self.assertEqual(command[command.index('--model') + 1], 'kimi-code/k3-256k')
        self.adapter.cleanup('b7c10e09-04ef-41ad-b903-96f975b761ad')
        test_command = self.adapter.build_test_command(self.request, 'f0b43ac1-7443-403c-b588-c2aa9f486c60')
        self.assertEqual(test_command[test_command.index('--model') + 1], 'kimi-code/k3-256k')

    def test_unknown_or_foreign_alias_is_never_selectable(self):
        for bad in ('kimi-code/unknown', 'paas/qwen3-max', 'qwen3.8-max'):
            with self.subTest(bad=bad), self.assertRaises(WorkerSetupError) as caught:
                self.adapter.select_model_profile(bad)
            self.assertEqual(caught.exception.code, 'CONFIG_ERROR')
        self.assertEqual(self.adapter.requested_model, KIMI_MODEL)

    def test_default_survives_missing_config_but_other_aliases_do_not(self):
        adapter = KimiAdapter(self.runtime_tmp, config_path=self.root / 'missing.toml')
        adapter.select_model_profile(None)
        self.assertEqual(adapter.requested_model, KIMI_MODEL)
        adapter.select_model_profile(KIMI_MODEL)
        with self.assertRaises(WorkerSetupError):
            adapter.select_model_profile('kimi-code/k3-256k')

    def test_execution_revalidates_non_default_selection(self):
        self.adapter.requested_model = 'kimi-code/unknown'
        with self.assertRaises(WorkerSetupError) as caught:
            self.adapter.build_command(self.request, 'b7c10e09-04ef-41ad-b903-96f975b761ad')
        self.assertEqual(caught.exception.code, 'CONFIG_ERROR')
        self.adapter.requested_model = 'kimi-code/unknown'
        with self.assertRaises(WorkerSetupError):
            self.adapter.build_test_command(self.request, 'f0b43ac1-7443-403c-b588-c2aa9f486c60')


class ModelProfileSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-model-settings-')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'runtime'
        self.runtime.mkdir(mode=0o700)
        qwen_profiles = [{'id': 'qwen3.8-max'}, {'id': 'qwen3.8-flash'}]
        kimi_profiles = [{'id': 'kimi-code/k3'}, {'id': 'kimi-code/k3-256k'}]
        qwen_patch = patch.object(qwen_module, 'verified_model_profiles', return_value=qwen_profiles)
        kimi_patch = patch.object(kimi_module, 'verified_model_profiles', return_value=kimi_profiles)
        qwen_patch.start()
        kimi_patch.start()
        self.addCleanup(qwen_patch.stop)
        self.addCleanup(kimi_patch.stop)

    def test_defaults_and_backward_compatible_load(self):
        settings = load_settings(self.runtime)
        self.assertEqual(settings['qwen_model_profile'], 'qwen3.8-max')
        self.assertEqual(settings['kimi_model_profile'], 'kimi-code/k3')
        self.assertEqual(load_worker_model_profile(self.runtime, 'qwen'), 'qwen3.8-max')
        self.assertEqual(load_worker_model_profile(self.runtime, 'kimi'), 'kimi-code/k3')
        with self.assertRaises(SettingsError):
            load_worker_model_profile(self.runtime, 'other')
        # A schema-1 file saved before the model fields existed keeps working.
        path = settings_path(self.runtime)
        path.write_bytes(json.dumps({'schema_version': 1, 'qwen_concurrency': 3}).encode())
        os.chmod(path, 0o600)
        settings = load_settings(self.runtime)
        self.assertEqual(settings['qwen_concurrency'], 3)
        self.assertEqual(settings['qwen_model_profile'], 'qwen3.8-max')
        self.assertEqual(settings['kimi_model_profile'], 'kimi-code/k3')

    def test_save_roundtrip_preserves_other_settings_and_permissions(self):
        save_settings(self.runtime, qwen_coding_max_tool_calls=75, kimi_concurrency=2)
        saved = save_settings(self.runtime, qwen_model_profile='qwen3.8-flash')
        self.assertEqual(saved['qwen_model_profile'], 'qwen3.8-flash')
        self.assertEqual(saved['kimi_model_profile'], 'kimi-code/k3')
        self.assertEqual(saved['qwen_coding_max_tool_calls'], 75)
        self.assertEqual(saved['kimi_concurrency'], 2)
        saved = save_settings(self.runtime, kimi_model_profile='kimi-code/k3-256k')
        self.assertEqual(saved['kimi_model_profile'], 'kimi-code/k3-256k')
        self.assertEqual(saved['qwen_model_profile'], 'qwen3.8-flash')
        path = settings_path(self.runtime)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        raw = json.loads(path.read_text())
        # Only IDs are stored: no endpoints, URLs, or credential fields.
        self.assertEqual(raw['qwen_model_profile'], 'qwen3.8-flash')
        self.assertNotIn('token-plan', repr(raw))
        self.assertNotIn('api', repr(raw).lower())
        for leftover in self.runtime.iterdir():
            self.assertEqual(leftover.name, 'settings.json')

    def test_save_rejects_unverified_ids_without_writing(self):
        for worker, bad in (('qwen', 'qwen3.7-max'), ('qwen', 'gpt-5'),
                            ('kimi', 'qwen3.8-flash'), ('kimi', 'kimi-code/unknown')):
            with self.subTest(worker=worker, bad=bad):
                with self.assertRaises(SettingsError) as caught:
                    save_settings(self.runtime, **{f'{worker}_model_profile': bad})
                self.assertEqual(caught.exception.code, 'INVALID_REQUEST')
        self.assertFalse(settings_path(self.runtime).exists())

    def test_saved_malformed_model_id_fails_closed_on_load(self):
        path = settings_path(self.runtime)
        for bad in ('not a model', 'https://evil.example', 5, True):
            with self.subTest(bad=bad):
                path.write_bytes(json.dumps({'schema_version': 1,
                                             'qwen_model_profile': bad}).encode())
                os.chmod(path, 0o600)
                with self.assertRaises(SettingsError):
                    load_settings(self.runtime)


class ProfileCapturingAdapter:
    """Fake worker with verified-profile selection support."""

    name = 'qwen'
    role = 'test worker'

    def __init__(self, allowed=('qwen3.8-max', 'qwen3.8-flash')):
        self.allowed = set(allowed)
        self.requested_model = 'qwen3.8-max'
        self.selected = None
        self.captured = None

    def select_model_profile(self, profile_id):
        if profile_id not in self.allowed:
            raise WorkerSetupError('CONFIG_ERROR', f'{profile_id} is not a verified profile.')
        self.selected = profile_id
        self.requested_model = profile_id

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


class SupervisorModelProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-model-supervisor-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.project = self.root / 'project'
        self.project.mkdir()
        qwen_patch = patch.object(qwen_module, 'verified_model_profiles',
                                  return_value=[{'id': 'qwen3.8-max'}, {'id': 'qwen3.8-flash'}])
        qwen_patch.start()
        self.addCleanup(qwen_patch.stop)

    def request(self, **overrides):
        data = {'worker': 'qwen', 'task': 'implement the change', 'cwd': str(self.project),
                'mode': 'read-only', 'timeout_seconds': 30}
        data.update(overrides)
        return json.dumps(data).encode()

    def test_selected_model_propagates_to_command_and_job_snapshot(self):
        adapter = ProfileCapturingAdapter()
        supervisor = Supervisor(self.runtime, (self.root,), {'qwen': adapter})
        save_settings(self.runtime, qwen_model_profile='qwen3.8-flash')
        result = supervisor.run(self.request(), adapter)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(adapter.selected, 'qwen3.8-flash')
        self.assertIsNotNone(adapter.captured)
        self.assertEqual(result.requested_model, 'qwen3.8-flash')
        self.assertEqual(result.json()['requested_model'], 'qwen3.8-flash')
        persisted = supervisor.state.get_job(result.job_id)
        self.assertEqual(persisted['requested_model'], 'qwen3.8-flash')

    def test_default_model_used_when_nothing_is_saved(self):
        adapter = ProfileCapturingAdapter()
        supervisor = Supervisor(self.runtime, (self.root,), {'qwen': adapter})
        result = supervisor.run(self.request(), adapter)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(adapter.selected, 'qwen3.8-max')
        self.assertEqual(result.requested_model, 'qwen3.8-max')

    def test_unverifiable_saved_profile_fails_closed_without_a_process(self):
        # The saved ID passed settings validation earlier, but the provider
        # configuration no longer verifies it: no worker process may start.
        save_settings(self.runtime, qwen_model_profile='qwen3.8-flash')
        adapter = ProfileCapturingAdapter(allowed=('qwen3.8-max',))
        supervisor = Supervisor(self.runtime, (self.root,), {'qwen': adapter})
        result = supervisor.run(self.request(), adapter)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error['code'], 'CONFIG_ERROR')
        self.assertIsNone(adapter.captured)
        persisted = supervisor.state.get_job(result.job_id)
        self.assertEqual(persisted['status'], 'failed')
        self.assertEqual(persisted['error_code'], 'CONFIG_ERROR')

    def test_adapters_without_profile_support_keep_fixed_model(self):
        class PlainAdapter(ProfileCapturingAdapter):
            select_model_profile = None
        adapter = PlainAdapter()
        adapter.select_model_profile = None
        supervisor = Supervisor(self.runtime, (self.root,), {'qwen': adapter})
        result = supervisor.run(self.request(), adapter)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.requested_model, 'qwen3.8-max')


if __name__ == '__main__':
    unittest.main()
