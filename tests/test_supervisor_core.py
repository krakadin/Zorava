"""Offline tests of validation, redaction, persistence and process control."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ai_router.request import DEFAULT_TIMEOUT, MAX_TIMEOUT, RequestError, parse_request
from ai_router.security import redact, summarize
from ai_router.state import StateStore, ensure_private_directory
from ai_router.supervisor import Supervisor
from workers.base import ParsedOutput, common_child_environment, validate_usage_report


FAKE = Path(__file__).parent/'fixtures/fake_worker.py'
FAKE_BEARER = 'fake-crash-token-value'
FAKE_KEY = 'sk-test-FAKE-NOT-REAL-123456789'


class FakeAdapter:
    name = 'qwen'
    role = 'test worker'
    requested_model = 'fake-v1'
    executable = Path(sys.executable)

    def __init__(self, mode='success', extra_env=None):
        self.mode = mode
        self.extra_env = extra_env or {}

    def version(self):
        return 'fake-1'

    def build_command(self, request, job_id):
        return [sys.executable,'-B',str(FAKE),self.mode]

    def build_payload(self, request, job_id):
        return json.dumps({'job_id':job_id,'task':request.task,'context':request.context,
                           'cwd':str(request.cwd),'mode':request.mode}).encode()

    def build_environment(self, job_id=None):
        return common_child_environment() | self.extra_env

    def parse_output(self, raw):
        data=json.loads(raw.decode())
        if not isinstance(data,dict) or not isinstance(data.get('text'),str):
            raise ValueError('invalid output')
        return ParsedOutput(data['text'],data.get('reported_model'),data.get('usage'))


class SupervisorFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ai-router-supervisor-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.runtime=self.root/'runtime'
        self.runtime.mkdir(mode=0o700)
        self.project=self.root/'project'
        self.project.mkdir()
        self.supervisor=Supervisor(self.runtime,(self.root,),{'qwen':FakeAdapter()})

    def request(self,**overrides):
        data={'worker':'qwen','task':'investigate','cwd':str(self.project),'mode':'read-only','timeout_seconds':10}
        data.update(overrides)
        return json.dumps(data).encode()

    def run_mode(self,mode='success',**overrides):
        return self.supervisor.run(self.request(**overrides),FakeAdapter(mode))


class RequestAndRedactionTests(SupervisorFixture):
    def test_valid_request_defaults_and_unicode(self):
        req=parse_request(json.dumps({'worker':'qwen','task':'trace café','cwd':str(self.project)}).encode(),(self.root,))
        self.assertEqual(req.timeout_seconds,1800)
        self.assertEqual(req.mode,'isolated-edit')
        # Omitted budgets stay unset at parse time; the supervisor applies the
        # saved coding preset and the read-only policy keeps its fixed default.
        self.assertIsNone(req.max_tool_calls)
        self.assertIn('café',req.task)

    def test_default_timeout_is_thirty_minutes_for_both_coders(self):
        self.assertEqual(DEFAULT_TIMEOUT,{'qwen':1800,'kimi':1800})
        # The default equals the unchanged ceiling; an omitted timeout must
        # still parse for either worker and the range stays 10-1800 seconds.
        self.assertEqual(MAX_TIMEOUT,1800)
        for worker in ('qwen','kimi'):
            for mode in ('isolated-edit','read-only'):
                with self.subTest(worker=worker,mode=mode):
                    payload=json.dumps({'worker':worker,'task':'implement','cwd':str(self.project),
                                        'mode':mode}).encode()
                    self.assertEqual(parse_request(payload,(self.root,)).timeout_seconds,1800)
        explicit=json.dumps({'worker':'kimi','task':'implement','cwd':str(self.project),
                             'timeout_seconds':60}).encode()
        self.assertEqual(parse_request(explicit,(self.root,)).timeout_seconds,60)

    def test_qwen_tool_budget_accepts_unlimited_and_bounded_integers(self):
        for value,expected in ((0,0),(-1,-1),(1000000,1000000),(200,200)):
            with self.subTest(value=value):
                req=parse_request(self.request(max_tool_calls=value),(self.root,))
                self.assertEqual(req.max_tool_calls,expected)
        for value in (True,-2,1000001,'0',1.5,'unlimited'):
            with self.subTest(value=value), self.assertRaises(RequestError):
                parse_request(self.request(max_tool_calls=value),(self.root,))
        with self.assertRaises(RequestError):
            parse_request(self.request(worker='kimi',max_tool_calls=0),(self.root,))
        with self.assertRaises(RequestError):
            parse_request(self.request(worker='kimi',max_tool_calls=-1),(self.root,))

    def test_invalid_worker_and_mode_are_rejected(self):
        for changes,code in [({'worker':'other'},'INVALID_WORKER'),({'mode':'edit'},'INVALID_MODE')]:
            with self.subTest(code=code):
                with self.assertRaises(RequestError) as caught:
                    parse_request(self.request(**changes),(self.root,))
                self.assertEqual(caught.exception.code,code)

    def test_both_coders_support_isolated_edit_and_explicit_read_only(self):
        for worker in ('qwen','kimi'):
            for mode in ('isolated-edit','read-only'):
                with self.subTest(worker=worker,mode=mode):
                    request=parse_request(self.request(worker=worker,mode=mode),(self.root,))
                    self.assertEqual(request.mode,mode)

    def test_bad_json_and_size_limits(self):
        with self.assertRaises(RequestError): parse_request(b'{', (self.root,))
        with self.assertRaises(RequestError) as caught:
            parse_request(self.request(task='x'*(32*1024+1)),(self.root,))
        self.assertEqual(caught.exception.code,'INVALID_TASK')
        with self.assertRaises(RequestError) as caught:
            parse_request(b' '* (320*1024+1),(self.root,))
        self.assertEqual(caught.exception.code,'REQUEST_TOO_LARGE')

    def test_timeout_validation(self):
        for timeout in (True,9,1801,'10'):
            with self.subTest(timeout=timeout), self.assertRaises(RequestError):
                parse_request(self.request(timeout_seconds=timeout),(self.root,))

    def test_path_escape_symlink_and_missing_directory(self):
        outside=self.root.parent/'ai-router-outside-test'
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: outside.rmdir() if outside.exists() else None)
        link=self.project/'escape'
        link.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(RequestError) as caught:
            parse_request(self.request(cwd=str(link)),(self.root,))
        self.assertEqual(caught.exception.code,'PATH_NOT_ALLOWED')
        with self.assertRaises(RequestError): parse_request(self.request(cwd='/etc'),(self.root,))
        with self.assertRaises(RequestError): parse_request(self.request(cwd=str(self.root/'missing')),(self.root,))

    def test_sanitizer_removes_secrets_bearers_keys_queries_ansi_and_controls(self):
        source=(f'Authorization: Bearer {FAKE_BEARER}\n"api_key": "{FAKE_KEY}" '
                'URL?access_token=abc123\nCookie: session=fake-cookie; tracking=fake-tracking\n'
                '-----BEGIN OPENSSH PRIVATE KEY-----\nfake-private-material\n-----END OPENSSH PRIVATE KEY-----\n'
                '\x1b[31mRED\x1b[0m\x01')
        safe,count=redact(source)
        self.assertGreaterEqual(count,5)
        for secret in (FAKE_BEARER,FAKE_KEY,'abc123','fake-cookie','fake-tracking','fake-private-material','\x1b','\x01'):
            self.assertNotIn(secret,safe)
        self.assertGreaterEqual(safe.count('[REDACTED]'),3)

    def test_summarize_is_deterministic_and_bounded(self):
        self.assertEqual(summarize('  trace\n  this  '),'trace this')
        self.assertLessEqual(len(summarize('x'*300)),180)

    def test_child_environment_allowlist_excludes_provider_secrets(self):
        with patch.dict(os.environ,{'ANTHROPIC_API_KEY':'fake-a','ANTHROPIC_AUTH_TOKEN':'fake-b',
                                    'DASHSCOPE_API_KEY':'fake-c','KIMI_API_KEY':'fake-d',
                                    'OPENAI_API_KEY':'fake-e','LC_TEST':'utf8'},clear=True):
            env=common_child_environment()
        for name in ('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','DASHSCOPE_API_KEY','KIMI_API_KEY','OPENAI_API_KEY'):
            self.assertNotIn(name,env)
        self.assertEqual(env['LC_TEST'],'utf8')
        self.assertEqual(env['HOME'],'/home/krakadin')


class StateAndProcessTests(SupervisorFixture):
    def test_invalid_usage_report_is_persisted_as_failure(self):
        # A completed response without the worker's bounded usage report fails.
        result=self.supervisor.run(self.request(),job_type='provider_test')
        self.assertEqual(result.status,'invalid_output')
        self.assertEqual(result.error['code'],'USAGE_REPORT_MISMATCH')
        job=self.supervisor.state.get_job(result.job_id)
        self.assertEqual(job['error_code'],'USAGE_REPORT_MISMATCH')
        self.assertIsNotNone(job['completed_at'])
        self.assertIsNone(self.supervisor.state.last_successful_test('qwen'))

    def test_valid_usage_report_completes_provider_test(self):
        result=self.supervisor.run(self.request(),FakeAdapter('usage-report'),job_type='provider_test')
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.result,'QWEN_USAGE_REPORT input=3 output=4')
        job=self.supervisor.state.get_job(result.job_id)
        self.assertEqual(job['status'],'completed')
        self.assertEqual(job['usage_json'],'{"input_tokens":3,"output_tokens":4}')
        self.assertIsNotNone(self.supervisor.state.last_successful_test('qwen'))

    def test_usage_report_validation_is_bounded_and_per_worker(self):
        self.assertIsNone(validate_usage_report('qwen','QWEN_USAGE_REPORT input=3 output=4'))
        self.assertIsNone(validate_usage_report('kimi','  KIMI_USAGE_REPORT unavailable  '))
        for worker,bad in (('qwen','KIMI_USAGE_REPORT input=3 output=4'),
                           ('qwen','fake worker completed'),
                           ('qwen','QWEN_USAGE_REPORT input=3\noutput=4'),
                           ('qwen','QWEN_USAGE_REPORT <script>alert(1)</script>'),
                           ('qwen','QWEN_USAGE_REPORT '+'x'*300),
                           ('kimi','')):
            with self.subTest(worker=worker,bad=bad[:40]):
                self.assertIsNotNone(validate_usage_report(worker,bad))

    def test_qwen_exit_55_classifies_as_budget_exhausted(self):
        outcome = Supervisor._classify({'cancelled':False,'timed_out':False,'oversized':False},
                                       55, 'tool call budget exceeded', None, None)
        status, code, message = outcome
        self.assertEqual((status, code), ('failed', 'BUDGET_EXHAUSTED'))
        self.assertIn('worktree', message)
        self.assertIn('continued', message)

    def test_kimi_five_hour_and_weekly_limits_classify_as_quota(self):
        for diagnostic in (
            "You've reached your 5-hour usage limit. Your quota will reset when the current 5-hour window ends.",
            "You've reached your weekly (7-day) usage limit.",
            "You've reached your monthly usage limit for this billing cycle.",
        ):
            with self.subTest(diagnostic=diagnostic):
                outcome = Supervisor._classify(
                    {'cancelled':False,'timed_out':False,'oversized':False},
                    1, diagnostic, None, 'INVALID_OUTPUT')
                self.assertEqual(outcome[:2], ('failed','QUOTA_OR_BILLING'))

    def test_success_result_and_job_event_trail(self):
        result=self.run_mode()
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.reported_model,'fake-v1')
        self.assertEqual(result.worker_version,'fake-1')
        job=self.supervisor.state.get_job(result.job_id)
        self.assertEqual(job['status'],'completed')
        self.assertEqual(job['worker_version'],'fake-1')
        self.assertEqual(job['usage_json'],'{"input_tokens":3,"output_tokens":4}')
        self.assertEqual([e['event'] for e in job['events']],['queued','started','completed'])

    def test_secret_like_worker_result_redacted_before_database(self):
        result=self.run_mode('secret')
        self.assertEqual(result.status,'completed')
        self.assertNotIn(FAKE_BEARER,result.result)
        self.assertNotIn(FAKE_KEY,result.result)
        for path in (self.runtime/'workers.db',self.runtime/'workers.db-wal'):
            if path.exists():
                raw=path.read_bytes()
                self.assertNotIn(FAKE_BEARER.encode(),raw)
                self.assertNotIn(FAKE_KEY.encode(),raw)
        job=self.supervisor.state.get_job(result.job_id)
        self.assertTrue(any(e['event']=='security redaction' for e in job['events']))

    def test_malformed_output_is_distinct(self):
        result=self.run_mode('malformed')
        self.assertEqual(result.status,'invalid_output')
        self.assertEqual(result.error['code'],'INVALID_OUTPUT')

    def test_crash_and_auth_errors_are_sanitized(self):
        result=self.run_mode('crash')
        self.assertEqual(result.status,'auth_error')
        job=self.supervisor.state.get_job(result.job_id)
        self.assertNotIn(FAKE_BEARER,json.dumps(job))

    def test_large_output_is_stopped_at_limit(self):
        small=Supervisor(self.runtime,(self.root,),{'qwen':FakeAdapter()},stdout_limit=1024)
        result=small.run(self.request(),FakeAdapter('huge'))
        self.assertEqual(result.error['code'],'OUTPUT_LIMIT')

    def test_job_waits_queued_when_all_worker_slots_are_held(self):
        # Qwen's default capacity is two cross-process slots; holding both
        # leaves a new job queued instead of failing WORKER_BUSY.
        fds=[]
        for index in range(2):
            fd=os.open(self.runtime/'locks'/f'qwen.{index}.lock',os.O_CREAT|os.O_RDWR,0o600)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            fds.append(fd)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(self.run_mode)
                deadline=time.monotonic()+4
                job=None
                while time.monotonic()<deadline:
                    rows=self.supervisor.state.jobs()
                    queued=next((row for row in rows if row['status']=='queued'),None)
                    if queued:
                        job=queued
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(job,'job did not stay queued while slots were held')
                accepted,code=self.supervisor.cancel(job['id'])
                self.assertTrue(accepted)
                self.assertEqual(code,'CANCELLED')
                result=future.result(timeout=5)
            self.assertEqual(result.status,'cancelled')
            self.assertEqual(result.error['code'],'CANCELLED')
            persisted=self.supervisor.state.get_job(result.job_id)
            self.assertEqual(persisted['status'],'cancelled')
            self.assertIsNone(persisted['started_at'])
        finally:
            for fd in fds:
                os.close(fd)

    def test_one_held_slot_does_not_block_a_second_qwen_job(self):
        fd=os.open(self.runtime/'locks'/'qwen.0.lock',os.O_CREAT|os.O_RDWR,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            result=self.run_mode()
            self.assertEqual(result.status,'completed')
        finally:
            os.close(fd)

    def test_cancel_terminates_only_owned_worker_process_group(self):
        child_file=self.root/'child.pid'
        adapter=FakeAdapter('child',{'AI_TEST_CHILD_PID':str(child_file)})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.supervisor.run,self.request(),adapter)
            deadline=time.monotonic()+4
            job=None
            while time.monotonic()<deadline:
                rows=self.supervisor.state.jobs()
                running=next((row for row in rows if row['status']=='running'),None)
                if running and child_file.exists():
                    job=running
                    break
                time.sleep(0.05)
            self.assertIsNotNone(job,'fake worker did not start')
            pid=int(child_file.read_text())
            accepted,code=self.supervisor.cancel(job['id'])
            self.assertTrue(accepted)
            self.assertIn(code,('CANCEL_REQUESTED','CANCEL_REQUESTED_PROCESS_ALREADY_EXITED'))
            result=future.result(timeout=5)
        self.assertEqual(result.status,'cancelled',result.json())
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and Path('/proc',str(pid)).exists():
            try:
                fields=Path('/proc',str(pid),'stat').read_text()
                if fields[fields.rfind(')')+2:].split()[0]=='Z': break
            except OSError: break
            time.sleep(0.05)
        if Path('/proc',str(pid)).exists():
            fields=Path('/proc',str(pid),'stat').read_text()
            self.assertEqual(fields[fields.rfind(')')+2:].split()[0],'Z')

    def test_timeout_kills_owned_process_group(self):
        adapter=FakeAdapter('sleep')
        start=time.monotonic()
        result=self.supervisor.run(self.request(timeout_seconds=10),adapter)
        self.assertEqual(result.status,'timed_out')
        self.assertLess(time.monotonic()-start,16)

    def test_database_rejects_symlink_and_nonprivate_directory(self):
        outside=self.root/'other.db'
        outside.write_bytes(b'')
        link=self.root/'link.db'
        link.symlink_to(outside)
        with self.assertRaises(RuntimeError): StateStore(link)
        public=self.root/'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(RuntimeError): ensure_private_directory(public)


if __name__=='__main__':
    unittest.main()
