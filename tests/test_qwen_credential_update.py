"""Offline tests using synthetic credentials only; no provider calls."""

import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tools import update_qwen_credential as helper


OLD = 'sk-test-OLD-NOT-A-REAL-KEY-123456'
NEW = 'sk-test-NEW-NOT-A-REAL-KEY-654321'
PLAN_KEY = 'sk-sp-test-NOT-A-REAL-KEY-123456'
WORKSPACE_KEY = 'sk-ws-test-NOT-A-REAL-KEY-654321'


class CredentialUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='ai-router-credential-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = self.root / 'settings.json'
        self.original = (
            '{\n  "env": { "DASHSCOPE_API_KEY": ' + json.dumps(OLD) + ', "EXAMPLE": "unchanged" },\n'
            '  "model": {"name": "example", "baseUrl": "https://example.invalid/v1"}\n}\n'
        ).encode()
        self.settings.write_bytes(self.original)
        self.settings.chmod(0o600)
        self.history = self.root / 'history.jsonl'
        self.history.write_text(json.dumps({'text': OLD}) + '\n')
        self.history.chmod(0o600)

    def update(self, key=NEW):
        helper.update_credential(self.settings, key, (self.history,))

    def assert_unchanged(self):
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob('.settings-key-update-*')), [])

    def test_only_credential_bytes_change_and_mode_is_private(self):
        before = self.settings.stat()
        history = self.history.read_bytes()
        self.update()
        self.assertEqual(self.settings.read_bytes(), self.original.replace(OLD.encode(), NEW.encode()))
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)
        self.assertEqual((self.settings.stat().st_uid, self.settings.stat().st_gid), (before.st_uid, before.st_gid))
        self.assertEqual(self.history.read_bytes(), history)
        self.assertEqual(list(self.root.glob('.settings-key-update-*')), [])

    def test_reject_same_key(self):
        with self.assertRaises(helper.RemediationError):
            self.update(OLD)
        self.assert_unchanged()

    def test_reject_key_in_historical_transcript(self):
        self.history.write_text(json.dumps({'text': NEW}) + '\n')
        with self.assertRaises(helper.RemediationError):
            self.update()
        self.assert_unchanged()

    def test_reject_masked_whitespace_and_invalid_keys(self):
        for key in ('short', NEW + '\n', NEW + '*', NEW + ' space', NEW + '\x1b'):
            with self.subTest(kind='invalid input'):
                with self.assertRaises(helper.RemediationError):
                    self.update(key)
                self.assert_unchanged()

    def test_reject_unsafe_permissions(self):
        self.settings.chmod(0o664)
        with self.assertRaises(helper.RemediationError):
            self.update()
        self.assert_unchanged()

    def test_reject_symlink(self):
        target = self.root / 'other.json'
        self.settings.rename(target)
        self.settings.symlink_to(target)
        with self.assertRaises(helper.RemediationError):
            self.update()
        self.assertEqual(target.read_bytes(), self.original)

    def test_reject_hard_link(self):
        os.link(self.settings, self.root / 'other.json')
        with self.assertRaises(helper.RemediationError):
            self.update()
        self.assert_unchanged()

    def test_malformed_json_error_never_contains_input(self):
        self.settings.write_text('{bad json ' + OLD)
        with self.assertRaises(helper.RemediationError) as caught:
            self.update()
        self.assertNotIn(OLD, str(caught.exception))
        self.assertEqual(list(self.root.glob('.settings-key-update-*')), [])

    def test_duplicate_fields_rejected(self):
        self.settings.write_bytes(self.original.replace(b'"EXAMPLE": "unchanged"', b'"DASHSCOPE_API_KEY": "duplicate"'))
        with self.assertRaises(helper.RemediationError):
            self.update()

    def test_failed_atomic_replace_cleans_temporary_file(self):
        with patch.object(helper.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.update()
        self.assert_unchanged()

    def test_piped_input_refused_before_reading_configuration(self):
        with patch.object(helper.sys, 'argv', ['helper']), patch.object(helper.sys.stdin, 'isatty', return_value=False), patch.object(helper, 'read_settings') as read, patch.object(helper.sys, 'stderr', io.StringIO()) as err:
            self.assertEqual(helper.main(), 2)
            read.assert_not_called()
            self.assertNotIn(OLD, err.getvalue())

    def plan_settings(self):
        data = json.loads(self.original)
        data['model'] = {'name': helper.MODEL, 'baseUrl': helper.LEGACY_URL}
        data['security'] = {'auth': {'selectedType': 'openai'}}
        data['modelProviders'] = {'openai': [
            {'id': 'another-model', 'baseUrl': helper.LEGACY_URL, 'envKey': 'DASHSCOPE_API_KEY'},
            {'id': helper.MODEL, 'baseUrl': helper.LEGACY_URL, 'envKey': 'DASHSCOPE_API_KEY', 'generationConfig': {'untouched': True}},
        ]}
        self.original = (json.dumps(data, indent=4) + '\n').encode()
        self.settings.write_bytes(self.original)
        return data

    def test_plan_updates_exactly_credential_and_two_urls(self):
        expected = self.plan_settings()
        helper.update_credential(self.settings, PLAN_KEY, (self.history,), endpoint=helper.TOKEN_PLAN_URL)
        expected['env']['DASHSCOPE_API_KEY'] = PLAN_KEY
        expected['model']['baseUrl'] = helper.TOKEN_PLAN_URL
        expected['modelProviders']['openai'][1]['baseUrl'] = helper.TOKEN_PLAN_URL
        self.assertEqual(json.loads(self.settings.read_bytes()), expected)
        actual = self.settings.read_bytes()
        self.assertEqual(actual.count(helper.TOKEN_PLAN_URL.encode()), 2)
        restored = actual.replace(PLAN_KEY.encode(), OLD.encode()).replace(helper.TOKEN_PLAN_URL.encode(), helper.LEGACY_URL.encode())
        self.assertEqual(restored, self.original)

    def test_plan_key_without_explicit_migration_is_rejected(self):
        self.plan_settings()
        with self.assertRaises(helper.RemediationError):
            self.update(PLAN_KEY)
        self.assert_unchanged()

    def test_general_key_for_plan_is_rejected(self):
        self.plan_settings()
        with self.assertRaises(helper.RemediationError):
            helper.update_credential(self.settings, NEW, (self.history,), endpoint=helper.TOKEN_PLAN_URL)
        self.assert_unchanged()

    def test_plan_rejects_unexpected_active_model(self):
        self.plan_settings()
        self.original = self.original.replace(helper.MODEL.encode(), b'different-model')
        self.settings.write_bytes(self.original)
        with self.assertRaises(helper.RemediationError):
            helper.update_credential(self.settings, PLAN_KEY, (self.history,), endpoint=helper.TOKEN_PLAN_URL)
        self.assert_unchanged()

    def test_json_span_handles_nested_arrays_and_escaped_fields(self):
        source = '{"skip": [{"value": "not this"}], "nested": {"li\\u0073t": [0, {"key": "target"}]}}'
        start, end = helper.value_span(source, ('nested', 'list', 1, 'key'))
        self.assertEqual(source[start:end], '"target"')

    def test_workspace_key_updates_only_credential_and_two_urls(self):
        expected = self.plan_settings()
        helper.update_credential(self.settings, WORKSPACE_KEY, (self.history,), endpoint=helper.WORKSPACE_URL)
        expected['env']['DASHSCOPE_API_KEY'] = WORKSPACE_KEY
        expected['model']['baseUrl'] = helper.WORKSPACE_URL
        expected['modelProviders']['openai'][1]['baseUrl'] = helper.WORKSPACE_URL
        self.assertEqual(json.loads(self.settings.read_bytes()), expected)
        restored = self.settings.read_bytes().replace(WORKSPACE_KEY.encode(), OLD.encode()).replace(helper.WORKSPACE_URL.encode(), helper.LEGACY_URL.encode())
        self.assertEqual(restored, self.original)

    def test_reject_mixed_key_endpoint_pairs(self):
        self.plan_settings()
        for key, endpoint in ((PLAN_KEY, helper.WORKSPACE_URL), (WORKSPACE_KEY, helper.TOKEN_PLAN_URL)):
            with self.assertRaises(helper.RemediationError):
                helper.update_credential(self.settings, key, (self.history,), endpoint=endpoint)
            self.assert_unchanged()

    def test_terminal_decline_changes_nothing_and_does_not_echo_key(self):
        self.plan_settings()
        with patch.object(helper.sys, 'argv', ['helper', '--configure-endpoint']), \
             patch.object(helper, 'SETTINGS', self.settings), \
             patch.object(helper, 'HISTORIES', (self.history,)), \
             patch.object(helper.sys, 'stdout', io.StringIO()) as out, \
             patch.object(helper.sys, 'stderr', io.StringIO()) as err, \
             patch.object(helper.sys.stdin, 'isatty', return_value=True), \
             patch.object(helper.sys.stderr, 'isatty', return_value=True), \
             patch.object(helper.os, 'umask'), \
             patch.object(helper.getpass, 'getpass', side_effect=[WORKSPACE_KEY, WORKSPACE_KEY]), \
             patch('builtins.input', return_value='no'):
            self.assertEqual(helper.main(), 1)
            self.assertNotIn(WORKSPACE_KEY, out.getvalue() + err.getvalue())
        self.assert_unchanged()

    def test_terminal_confirmation_applies_update_without_echoing_key(self):
        self.plan_settings()
        with patch.object(helper.sys, 'argv', ['helper', '--configure-endpoint']), \
             patch.object(helper, 'SETTINGS', self.settings), \
             patch.object(helper, 'HISTORIES', (self.history,)), \
             patch.object(helper.sys, 'stdout', io.StringIO()) as out, \
             patch.object(helper.sys, 'stderr', io.StringIO()) as err, \
             patch.object(helper.sys.stdin, 'isatty', return_value=True), \
             patch.object(helper.sys.stderr, 'isatty', return_value=True), \
             patch.object(helper.os, 'umask'), \
             patch.object(helper.getpass, 'getpass', side_effect=[WORKSPACE_KEY, WORKSPACE_KEY]), \
             patch('builtins.input', return_value='UPDATE'):
            self.assertEqual(helper.main(), 0)
            self.assertNotIn(WORKSPACE_KEY, out.getvalue() + err.getvalue())
            self.assertNotIn(OLD, out.getvalue() + err.getvalue())
        self.assertEqual(json.loads(self.settings.read_bytes())['model']['baseUrl'], helper.WORKSPACE_URL)

    def test_hidden_input_failure_does_not_fall_back_or_update(self):
        with patch.object(helper.sys, 'argv', ['helper']), \
             patch.object(helper, 'SETTINGS', self.settings), \
             patch.object(helper.sys, 'stderr', io.StringIO()) as err, \
             patch.object(helper.sys.stdin, 'isatty', return_value=True), \
             patch.object(helper.sys.stderr, 'isatty', return_value=True), \
             patch.object(helper.os, 'umask'), \
             patch.object(helper.getpass, 'getpass', side_effect=helper.getpass.GetPassWarning('synthetic warning')):
            self.assertEqual(helper.main(), 1)
            self.assertNotIn(OLD, err.getvalue())
        self.assert_unchanged()


if __name__ == '__main__':
    unittest.main()
