"""Offline tests for configurable cross-process worker slot concurrency."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
import unittest
import uuid

from ai_router.request import parse_request
from ai_router.settings import (DEFAULT_KIMI_CONCURRENCY, DEFAULT_QWEN_CONCURRENCY,
                                load_worker_concurrency, save_settings)
from ai_router.supervisor import Supervisor
from workers.base import ParsedOutput, WorkerSetupError, common_child_environment
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

    def serialized(self, ids):
        """True once every job row exists and exactly one job holds the slot.

        Comparing statuses before both rows exist races with job creation, so
        single-capacity assertions wait for this predicate instead.
        """
        rows = [self.job(job_id) for job_id in ids]
        if any(row is None for row in rows):
            return False
        return {row['status'] for row in rows} == {'running', 'queued'}

    @staticmethod
    def open_fd_count():
        """Descriptor count for this process; scandir's own fd is a constant."""
        with os.scandir('/proc/self/fd') as entries:
            return len(list(entries))

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
        # Both rows must exist before their statuses are compared; reading the
        # second job before its row was created raced with job creation.
        self.assertTrue(self.wait_for(lambda: self.serialized(ids)),
                        'a single Kimi slot did not keep one job running and one queued')
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
        self.assertTrue(self.wait_for(lambda: self.serialized(qwen_ids)),
                        'saved qwen_concurrency=1 did not serialize Qwen jobs')
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


class SlotAcquisitionTests(ConcurrencyFixture):
    """Slot acquisition is descriptor-clean and honours queued cancellation."""

    def test_slot_acquisition_returns_exactly_one_descriptor(self):
        before = self.open_fd_count()
        lock = self.supervisor._wait_for_slot('qwen', DEFAULT_QWEN_CONCURRENCY, str(uuid.uuid4()))
        try:
            self.assertIsNotNone(lock)
            # Both candidate slots are opened; only the acquired one survives.
            self.assertEqual(self.open_fd_count(), before + 1)
        finally:
            lock.close()
        self.assertEqual(self.open_fd_count(), before)
        # The released slot is usable again.
        again = self.supervisor._wait_for_slot('qwen', DEFAULT_QWEN_CONCURRENCY, str(uuid.uuid4()))
        self.assertIsNotNone(again)
        again.close()

    def test_refused_slot_lock_leaves_no_descriptor_open(self):
        unsafe = self.runtime/'locks'/'qwen.0.lock'
        unsafe.write_bytes(b'')
        os.chmod(unsafe, 0o644)
        before = self.open_fd_count()
        with self.assertRaises(WorkerSetupError) as caught:
            self.supervisor._wait_for_slot('qwen', DEFAULT_QWEN_CONCURRENCY, str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, 'CONFIG_ERROR')
        self.assertEqual(self.open_fd_count(), before)

    def test_job_cancelled_while_queued_releases_the_slot_without_a_process(self):
        job_id = str(uuid.uuid4())
        request = parse_request(self.request(), (self.root,))
        self.supervisor.state.create_job(job_id, request, 'fake-v1', 'test worker', 'fake-1')
        self.assertTrue(self.supervisor.state.cancel_queued(job_id))
        before = self.open_fd_count()
        # Even with a slot free, a cancelled queued job hands it straight back.
        self.assertIsNone(self.supervisor._wait_for_slot('qwen', 1, job_id))
        self.assertEqual(self.open_fd_count(), before)
        other = self.supervisor._wait_for_slot('qwen', 1, str(uuid.uuid4()))
        self.assertIsNotNone(other)
        other.close()


class SlotLockSecurityTests(ConcurrencyFixture):
    """Slot locks must be private to this user; unsafe ones fail closed."""

    def assert_failed_closed(self, worker='qwen'):
        job_id = str(uuid.uuid4())
        result = self.supervisor.run(self.request(worker=worker), SlotAdapter(worker, 'success'),
                                     job_id=job_id)
        self.assertEqual(result.status, 'failed', result.json())
        self.assertEqual(result.error['code'], 'CONFIG_ERROR')
        job = self.job(job_id)
        self.assertIsNone(job['started_at'])  # no worker process was started
        self.assertIsNone(job['pid'])
        self.assertEqual([event['event'] for event in job['events']], ['queued', 'failed'])

    def test_group_readable_slot_lock_is_refused_and_never_repaired(self):
        lock = self.runtime/'locks'/'qwen.0.lock'
        lock.write_bytes(b'')
        os.chmod(lock, 0o640)
        self.assert_failed_closed()
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o640)
        self.assertEqual(lock.read_bytes(), b'')

    def test_symlinked_slot_lock_is_refused_without_touching_its_target(self):
        outside = self.root/'outside.lock'
        outside.write_bytes(b'held-by-someone-else')
        os.chmod(outside, 0o600)
        (self.runtime/'locks'/'qwen.0.lock').symlink_to(outside)
        self.assert_failed_closed()
        self.assertTrue((self.runtime/'locks'/'qwen.0.lock').is_symlink())
        self.assertEqual(outside.read_bytes(), b'held-by-someone-else')

    def test_slot_lock_that_is_not_a_regular_file_is_refused(self):
        (self.runtime/'locks'/'qwen.0.lock').mkdir(mode=0o700)
        self.assert_failed_closed()

    def test_hard_linked_slot_lock_is_refused_then_accepted_once_removed(self):
        lock = self.runtime/'locks'/'qwen.0.lock'
        lock.write_bytes(b'')
        os.chmod(lock, 0o600)
        os.link(lock, self.runtime/'locks'/'qwen.0.copy')
        self.assert_failed_closed()
        self.assertEqual(os.stat(lock).st_nlink, 2)
        os.unlink(self.runtime/'locks'/'qwen.0.copy')
        result = self.supervisor.run(self.request(), SlotAdapter('qwen', 'success'),
                                     job_id=str(uuid.uuid4()))
        self.assertEqual(result.status, 'completed', result.json())

    def test_kimi_slot_locks_are_validated_too(self):
        lock = self.runtime/'locks'/'kimi.0.lock'
        lock.write_bytes(b'')
        os.chmod(lock, 0o604)
        self.assert_failed_closed('kimi')


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
                # Mirrors the real adapters: a prepared job gets its own
                # environment, anything else falls back to a shared baseline.
                if job_id is not None:
                    with self._dirs_lock:
                        job_env = self._job_env.get(job_id)
                    if job_env is not None:
                        return dict(job_env)
                return {'SHARED': 'baseline'}

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
        self.assertEqual(adapter.build_environment('job-a'), {'TMPDIR': str(dirs['job-a'])})
        self.assertEqual(adapter.build_environment('job-b'), {'TMPDIR': str(dirs['job-b'])})
        self.assertNotEqual(adapter.build_environment('job-a'), adapter.build_environment('job-b'))
        self.assertEqual(adapter.build_environment('job-unknown'), {'SHARED': 'baseline'})
        adapter.cleanup('job-a')
        with adapter._dirs_lock:
            self.assertNotIn('job-a', adapter._job_dirs)
            self.assertNotIn('job-a', adapter._job_env)
            self.assertEqual(adapter._job_dirs['job-b'], dirs['job-b'])
            self.assertEqual(adapter._job_env['job-b'], {'TMPDIR': str(dirs['job-b'])})
        # The cleaned job keeps no stale per-job state of its own.
        self.assertEqual(adapter.build_environment('job-a'), {'SHARED': 'baseline'})
        self.assertFalse(dirs['job-a'].exists())
        self.assertTrue(dirs['job-b'].exists())
        shutil.rmtree(dirs['job-b'])


if __name__ == '__main__':
    unittest.main()
