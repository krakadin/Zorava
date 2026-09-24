"""Offline tests for configurable cross-process worker slot concurrency."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
import uuid

from ai_router.settings import (DEFAULT_KIMI_CONCURRENCY, DEFAULT_QWEN_CONCURRENCY,
                                load_worker_concurrency, save_settings)
from ai_router.supervisor import Supervisor
from workers.base import ParsedOutput, common_child_environment
from workers.workspace import IsolatedWorkspace


FAKE = Path(__file__).parent/'fixtures'/'fake_worker.py'


class SlotAdapter:
    """Fake worker whose process waits on a test-controlled gate file."""

    role = 'test worker'
    requested_model = 'fake-v1'
    executable = Path(sys.executable)

    def __init__(self, name='qwen', mode='gate', gate=None):
        self.name = name
        self.mode = mode
        self.gate = gate

    def version(self):
        return 'fake-1'

    def build_command(self, request, job_id):
        return [sys.executable, '-B', str(FAKE), self.mode]

    def build_payload(self, request, job_id):
        return json.dumps({'job_id': job_id, 'task': request.task}).encode()

    def build_environment(self, job_id=None):
        env = common_child_environment()
        if self.gate is not None:
            env['AI_TEST_GATE'] = str(self.gate)
        return env

    def parse_output(self, raw):
        data = json.loads(raw.decode())
        return ParsedOutput(data['text'], data.get('reported_model'))


class ConcurrencyFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-concurrency-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root/'runtime'
        self.runtime.mkdir(mode=0o700)
        self.project = self.root/'project'
        self.project.mkdir()
        self.gate = self.root/'gate-file'
        self.supervisor = Supervisor(self.runtime, (self.root,), {})

    def request(self, worker='qwen', task='work', **overrides):
        data = {'worker': worker, 'task': task, 'cwd': str(self.project),
                'mode': 'read-only', 'timeout_seconds': 30}
        data.update(overrides)
        return json.dumps(data).encode()

    def adapter(self, worker='qwen', mode='gate'):
        return SlotAdapter(worker, mode, self.gate if mode == 'gate' else None)

    def job(self, job_id):
        return self.supervisor.state.get_job(job_id)

    def open_gate(self):
        self.gate.write_text('go')

    @staticmethod
    def wait_for(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def run_jobs(self, entries):
        """Run (worker, job_id, mode) tuples concurrently; returns futures map."""
        pool = ThreadPoolExecutor(max_workers=len(entries))
        self.addCleanup(pool.shutdown, True)
        return self.submit_jobs(pool, entries)

    def submit_jobs(self, pool, entries):
        futures = {}
        for worker, job_id, mode in entries:
            futures[job_id] = pool.submit(self.supervisor.run, self.request(worker=worker),
                                          self.adapter(worker, mode), job_id=job_id)
        return futures


class SlotConcurrencyTests(ConcurrencyFixture):
    def test_qwen_default_capacity_is_two_and_runs_two_jobs_concurrently(self):
        self.assertEqual(DEFAULT_QWEN_CONCURRENCY, 2)
        self.assertEqual(DEFAULT_KIMI_CONCURRENCY, 1)
        self.assertEqual(load_worker_concurrency(self.runtime, 'qwen'), 2)
        self.assertEqual(load_worker_concurrency(self.runtime, 'kimi'), 1)
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        futures = self.run_jobs([('qwen', ids[0], 'gate'), ('qwen', ids[1], 'gate')])
        self.assertTrue(self.wait_for(lambda: all((self.job(i) or {}).get('status') == 'running'
                                                  for i in ids)),
                        'two Qwen jobs did not run concurrently at the default capacity')
        self.open_gate()
        for job_id, future in futures.items():
            result = future.result(timeout=20)
            self.assertEqual(result.status, 'completed', result.json())
            self.assertEqual(self.job(job_id)['status'], 'completed')

    def test_third_qwen_job_waits_queued_then_starts_when_a_slot_frees(self):
        ids = [str(uuid.uuid4()) for _ in range(3)]
        pool = ThreadPoolExecutor(max_workers=3)
        self.addCleanup(pool.shutdown, True)
        futures = self.submit_jobs(pool, [('qwen', ids[0], 'gate'), ('qwen', ids[1], 'gate')])
        self.assertTrue(self.wait_for(lambda: ((self.job(ids[0]) or {}).get('status') == 'running'
                                               and (self.job(ids[1]) or {}).get('status') == 'running')),
                        'the first two Qwen jobs did not both reach running state')
        # Both slots are now provably held, so the third job must wait queued.
        futures.update(self.submit_jobs(pool, [('qwen', ids[2], 'gate')]))
        self.assertTrue(self.wait_for(lambda: self.job(ids[2]) is not None))
        third = self.job(ids[2])
        self.assertEqual(third['status'], 'queued')
        self.assertIsNone(third['started_at'])
        self.open_gate()
        results = {job_id: future.result(timeout=20) for job_id, future in futures.items()}
        for job_id, result in results.items():
            self.assertEqual(result.status, 'completed', result.json())
        third = self.job(ids[2])
        self.assertEqual([e['event'] for e in third['events']], ['queued', 'started', 'completed'])
        # The queued job only started after one of the first two finished.
        first_completed = min(self.job(ids[0])['completed_at'], self.job(ids[1])['completed_at'])
        self.assertGreaterEqual(third['started_at'], first_completed)

    def test_queued_qwen_job_cancellation_is_clean(self):
        ids = [str(uuid.uuid4()) for _ in range(3)]
        pool = ThreadPoolExecutor(max_workers=3)
        self.addCleanup(pool.shutdown, True)
        futures = self.submit_jobs(pool, [('qwen', ids[0], 'gate'), ('qwen', ids[1], 'gate')])
        self.assertTrue(self.wait_for(lambda: ((self.job(ids[0]) or {}).get('status') == 'running'
                                               and (self.job(ids[1]) or {}).get('status') == 'running')),
                        'the first two Qwen jobs did not both reach running state')
        futures.update(self.submit_jobs(pool, [('qwen', ids[2], 'success')]))
        self.assertTrue(self.wait_for(lambda: (self.job(ids[2]) or {}).get('status') == 'queued'))
        accepted, code = self.supervisor.cancel(ids[2])
        self.assertTrue(accepted)
        self.assertEqual(code, 'CANCELLED')
        result = futures[ids[2]].result(timeout=10)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.error['code'], 'CANCELLED')
        cancelled = self.job(ids[2])
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(cancelled['error_code'], 'CANCELLED')
        self.assertIsNone(cancelled['started_at'])  # never started a process
        self.assertIsNotNone(cancelled['completed_at'])
        self.open_gate()
        for job_id in ids[:2]:
            self.assertEqual(futures[job_id].result(timeout=20).status, 'completed')
        # A freed slot then accepts new work immediately.
        followup = str(uuid.uuid4())
        result = self.supervisor.run(self.request(), SlotAdapter('qwen', 'success'), job_id=followup)
        self.assertEqual(result.status, 'completed', result.json())

    def test_kimi_default_single_slot_serializes_jobs(self):
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        futures = self.run_jobs([('kimi', ids[0], 'gate'), ('kimi', ids[1], 'gate')])
        self.assertTrue(self.wait_for(lambda: (self.job(ids[0]) or {}).get('status') == 'running'
                                              or (self.job(ids[1]) or {}).get('status') == 'running'))
        statuses = {self.job(i)['status'] for i in ids}
        self.assertEqual(statuses, {'running', 'queued'})
        self.open_gate()
        results = {job_id: future.result(timeout=20) for job_id, future in futures.items()}
        for result in results.values():
            self.assertEqual(result.status, 'completed', result.json())
        first, second = sorted((self.job(i) for i in ids), key=lambda row: row['started_at'])
        self.assertGreaterEqual(second['started_at'], first['completed_at'])

    def test_saved_capacity_changes_are_respected(self):
        save_settings(self.runtime, kimi_concurrency=2, qwen_concurrency=1)
        self.assertEqual(load_worker_concurrency(self.runtime, 'kimi'), 2)
        self.assertEqual(load_worker_concurrency(self.runtime, 'qwen'), 1)
        kimi_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        futures = self.run_jobs([('kimi', kimi_ids[0], 'gate'), ('kimi', kimi_ids[1], 'gate')])
        self.assertTrue(self.wait_for(lambda: all((self.job(i) or {}).get('status') == 'running'
                                                  for i in kimi_ids)),
                        'saved kimi_concurrency=2 did not allow two concurrent Kimi jobs')
        self.open_gate()
        for future in futures.values():
            self.assertEqual(future.result(timeout=20).status, 'completed')
        qwen_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        futures = self.run_jobs([('qwen', qwen_ids[0], 'gate'), ('qwen', qwen_ids[1], 'gate')])
        self.gate.unlink()
        self.assertTrue(self.wait_for(lambda: (self.job(qwen_ids[0]) or {}).get('status') == 'running'
                                              or (self.job(qwen_ids[1]) or {}).get('status') == 'running'))
        statuses = {self.job(i)['status'] for i in qwen_ids}
        self.assertEqual(statuses, {'running', 'queued'},
                         'saved qwen_concurrency=1 did not serialize Qwen jobs')
        self.open_gate()
        for future in futures.values():
            self.assertEqual(future.result(timeout=20).status, 'completed')

    def test_concurrent_jobs_keep_their_own_payload_and_result(self):
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        pool = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(pool.shutdown, True)
        futures = [pool.submit(self.supervisor.run, self.request(task=f'task-{index}'),
                               SlotAdapter('qwen', 'echo'), job_id=ids[index])
                   for index in range(2)]
        for index, future in enumerate(futures):
            result = future.result(timeout=20)
            self.assertEqual(result.status, 'completed', result.json())
            self.assertIn(ids[index], result.result)
            self.assertIn(f'task-{index}', result.result)
            self.assertNotIn(ids[1 - index], result.result)


class WorkspaceStateIsolationTests(unittest.TestCase):
    """Per-job adapter runtime maps stay isolated when jobs overlap."""

    def test_cleanup_of_one_job_preserves_another_job_state(self):
        temp = tempfile.TemporaryDirectory(prefix='ai-router-isolation-test-')
        self.addCleanup(temp.cleanup)
        runtime_tmp = Path(temp.name)/'tmp'
        runtime_tmp.mkdir(mode=0o700)

        class Bare(IsolatedWorkspace):
            name = 'fake'

            def build_environment(self, job_id=None):
                return {}

        adapter = Bare()
        adapter._init_workspace(runtime_tmp)
        dirs = {}
        for job_id in ('job-a', 'job-b'):
            job_dir = runtime_tmp/job_id
            job_dir.mkdir(mode=0o700)
            dirs[job_id] = job_dir
            with adapter._dirs_lock:
                adapter._job_dirs[job_id] = job_dir
                adapter._job_env[job_id] = {'TMPDIR': str(job_dir)}
        self.assertNotEqual(adapter.build_environment('job-a'), adapter.build_environment('job-b'))
        adapter.cleanup('job-a')
        with adapter._dirs_lock:
            self.assertNotIn('job-a', adapter._job_dirs)
            self.assertNotIn('job-a', adapter._job_env)
            self.assertEqual(adapter._job_dirs['job-b'], dirs['job-b'])
            self.assertEqual(adapter._job_env['job-b'], {'TMPDIR': str(dirs['job-b'])})
        self.assertFalse(dirs['job-a'].exists())
        self.assertTrue(dirs['job-b'].exists())
        shutil.rmtree(dirs['job-b'])


if __name__ == '__main__':
    unittest.main()
