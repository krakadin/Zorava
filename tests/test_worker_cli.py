"""Offline tests for bin/worker.py command_status; never calls providers."""

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import uuid

from ai_router.state import StateStore


WORKER_PATH = Path(__file__).resolve().parents[1] / 'bin' / 'worker.py'


def load_worker_module():
    spec = importlib.util.spec_from_file_location('ai_worker_cli', WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load_worker_module()


class FakeQwenAdapter:
    """Static local metadata so status never inspects real provider configuration."""

    requested_model = 'qwen-fake-model'

    def __init__(self, runtime_tmp):
        self.executable = Path('/fake/qwen')

    def configuration_info(self):
        return {'provider': 'Fake Provider', 'endpoint_host': 'fake.example',
                'cli_model': 'qwen-fake-model', 'credential_present': True}


BASE = datetime(2026, 9, 23, tzinfo=timezone.utc)


def stamp(minutes):
    return (BASE + timedelta(minutes=minutes)).isoformat(timespec='milliseconds')


class CommandStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ai-router-worker-cli-')
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / 'state'
        self.runtime.mkdir(mode=0o700)
        os.chmod(self.runtime, 0o700)
        self.store = StateStore(self.runtime / 'workers.db')

    def add_job(self, worker_name, *, job_type='delegation', status='completed',
                created_minutes=0, completed_minutes=None, error_code=None, error_message=None):
        job_id = str(uuid.uuid4())
        request = SimpleNamespace(worker=worker_name, cwd=self.runtime, mode='read-only',
                                  task='synthetic test job', parent_job_id=None,
                                  delegation_group_id=None)
        self.store.create_job(job_id, request, 'fake-model', 'fake role', 'fake-1',
                              job_type=job_type)
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET status=?, created_at=?, completed_at=?, '
                       'error_code=?, error_message=? WHERE id=?',
                       (status, stamp(created_minutes),
                        stamp(completed_minutes) if completed_minutes is not None else None,
                        error_code, error_message, job_id))
        return job_id

    def run_status(self):
        buffer = io.StringIO()
        args = SimpleNamespace(json=True)
        with patch.object(worker, 'RUNTIME', self.runtime), \
             patch.object(worker, 'QwenAdapter', FakeQwenAdapter), \
             patch.object(Path, 'is_file', return_value=False), \
             contextlib.redirect_stdout(buffer):
            code = worker.command_status(args)
        return code, json.loads(buffer.getvalue())

    def test_completed_and_auth_error_statuses_for_both_workers(self):
        kimi_test = self.add_job('kimi', job_type='provider_test', status='completed',
                                 created_minutes=0, completed_minutes=1)
        qwen_test = self.add_job('qwen', job_type='provider_test', status='auth_error',
                                 created_minutes=2, completed_minutes=3,
                                 error_code='AUTH_ERROR', error_message='Authentication required.')
        code, value = self.run_status()
        self.assertEqual(code, 0)
        self.assertEqual(value['schema_version'], 1)
        kimi = value['kimi']
        self.assertEqual(kimi['status'], 'LAST_TEST_SUCCEEDED')
        self.assertEqual(kimi['last_test_id'], kimi_test)
        self.assertEqual(kimi['last_test_at'], stamp(1))
        self.assertEqual(kimi['last_test_status'], 'completed')
        self.assertIsNone(kimi['last_test_error'])
        self.assertEqual(kimi['last_success_at'], stamp(1))
        self.assertEqual(kimi['authentication'], 'Kimi Code-managed OAuth; credential not inspected')
        qwen = value['qwen']
        self.assertEqual(qwen['status'], 'AUTH_REQUIRED')
        self.assertEqual(qwen['last_test_id'], qwen_test)
        self.assertEqual(qwen['last_test_at'], stamp(3))
        self.assertEqual(qwen['last_test_status'], 'auth_error')
        self.assertEqual(qwen['last_test_error'], 'AUTH_ERROR')
        self.assertIsNone(qwen['last_success_at'])
        # Routing/auth safeguards and unrelated JSON fields are preserved.
        self.assertIn('routing', value['claude'])
        self.assertEqual(value['claude']['provider'], 'Anthropic')
        self.assertEqual(value['runtime'], str(self.runtime))

    def test_queued_running_then_completed_transitions(self):
        test_id = self.add_job('qwen', job_type='provider_test', status='queued',
                               created_minutes=0)
        _, value = self.run_status()
        qwen = value['qwen']
        self.assertEqual(qwen['status'], 'TESTING')
        self.assertEqual(qwen['last_test_id'], test_id)
        self.assertEqual(qwen['last_test_status'], 'queued')
        # A queued test has no completion timestamp; created_at identifies it.
        self.assertEqual(qwen['last_test_at'], stamp(0))
        self.assertIsNone(qwen['last_success_at'])
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                       (stamp(0), test_id))
        _, value = self.run_status()
        qwen = value['qwen']
        self.assertEqual(qwen['status'], 'TESTING')
        self.assertEqual(qwen['last_test_status'], 'running')
        self.assertEqual(qwen['last_test_at'], stamp(0))
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='completed', completed_at=? WHERE id=?",
                       (stamp(1), test_id))
        _, value = self.run_status()
        qwen = value['qwen']
        self.assertEqual(qwen['status'], 'LAST_TEST_SUCCEEDED')
        self.assertEqual(qwen['last_test_id'], test_id)
        self.assertEqual(qwen['last_test_at'], stamp(1))
        self.assertEqual(qwen['last_success_at'], stamp(1))

    def test_terminal_failure_and_no_tests(self):
        self.add_job('kimi', job_type='provider_test', status='timed_out',
                     created_minutes=0, completed_minutes=1,
                     error_code='TIMEOUT', error_message='Worker timed out.')
        _, value = self.run_status()
        self.assertEqual(value['kimi']['status'], 'LAST_TEST_FAILED')
        self.assertEqual(value['kimi']['last_test_error'], 'TIMEOUT')
        self.assertIsNone(value['kimi']['last_success_at'])
        qwen = value['qwen']
        self.assertEqual(qwen['status'], 'UNKNOWN')
        for field in ('last_test_id', 'last_test_at', 'last_test_status',
                      'last_test_error', 'last_success_at'):
            self.assertIsNone(qwen[field])

    def test_older_test_visible_beyond_recent_job_window(self):
        old_test = self.add_job('kimi', job_type='provider_test', status='completed',
                                created_minutes=0, completed_minutes=0)
        for index in range(120):
            self.add_job('kimi' if index % 2 else 'qwen', created_minutes=index + 1)
        _, value = self.run_status()
        kimi = value['kimi']
        self.assertEqual(kimi['status'], 'LAST_TEST_SUCCEEDED')
        self.assertEqual(kimi['last_test_id'], old_test)
        self.assertEqual(kimi['last_test_at'], stamp(0))
        self.assertEqual(kimi['last_success_at'], stamp(0))


if __name__ == '__main__':
    unittest.main()
