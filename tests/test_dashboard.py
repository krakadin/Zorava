import fcntl
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from http.server import ThreadingHTTPServer

from ai_router import updates
from ai_router.request import DEFAULT_TIMEOUT, MAX_TIMEOUT
from ai_router.state import StateStore
from dashboard.server import DashboardController, make_handler, serve


class OfflineFetcher:
    """Offline stand-in for updates.fetch_latest_version; records every call."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, worker):
        self.calls.append(worker)
        return self.results.get(worker, (None, 'NETWORK_ERROR'))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ai-router-dashboard-')
        self.addCleanup(self.temp.cleanup)
        self.runtime=Path(self.temp.name)/'state';self.runtime.mkdir(mode=0o700);os.chmod(self.runtime,0o700)
        (self.runtime/'locks').mkdir(mode=0o700)
        for worker in ('qwen','kimi'):
            (self.runtime/'locks'/f'{worker}.lock').touch(mode=0o600)
        safe={'claude':{'worker':'claude','status':'CONFIGURED','requested_model':'claude-local',
                        'provider':'Anthropic','endpoint_host':'api.anthropic.com','authentication':'OAuth','routing':'DIRECT'},
              'qwen':{'worker':'qwen','status':'CONFIGURED','requested_model':'qwen-test','provider':'Token Plan',
                      'endpoint_host':'token-plan.example','endpoint_url':'https://token-plan.example/v1','authentication':'credential present','version':'1.2.3'},
              'kimi':{'worker':'kimi','status':'CONFIGURED','requested_model':'kimi-test','provider':'Kimi','endpoint_host':'api.kimi.example','authentication':'OAuth','version':'2.3.4'}}
        self.safe_provider_snapshot=safe
        with patch.object(DashboardController,'_load_provider_snapshot',return_value=safe):
            self.controller=DashboardController(self.runtime,port=8787)
        self.controller.provider_snapshot=safe
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.controller))
        self.server.daemon_threads=True
        self.controller.port=self.server.server_port
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_server)
        self.store=StateStore(self.runtime/'workers.db')

    def stop_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
        self.controller.shutdown_owned_tests()

    def request(self,method,path,*,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request(method,path,body=body,headers=headers or {})
        response=conn.getresponse();data=response.read();status=response.status;response_headers=dict(response.getheaders());conn.close()
        return status,response_headers,data

    def add_job(self,*,status='completed',result='safe result'):
        job_id=str(uuid.uuid4())
        request=SimpleNamespace(worker='qwen',cwd=Path(self.temp.name),mode='read-only',task='fixture task',
                                parent_job_id=None,delegation_group_id=None)
        self.store.create_job(job_id,request,'qwen-test','Qwen Researcher','1.2.3')
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET status=?,created_at=?,started_at=?,completed_at=?,result=?,error_code=? WHERE id=?',
                       (status,'2026-09-23T12:00:00+00:00','2026-09-23T12:00:01+00:00',
                        None if status=='running' else '2026-09-23T12:00:02+00:00',result,
                        'FAKE_FAILURE' if status=='failed' else None,job_id))
        self.store.event(job_id,'failed' if status=='failed' else 'started','fixture event')
        return job_id

    def csrf_headers(self,origin=None):
        return {'Host':f'127.0.0.1:{self.server.server_port}',
                'Origin':origin or f'http://127.0.0.1:{self.server.server_port}',
                'Sec-Fetch-Site':'same-origin','X-AI-Worker-CSRF':self.controller.csrf_token,
                'Content-Type':'application/json'}

    def test_loopback_bind_and_static_dashboard_security_headers(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
        status,headers,body=self.request('GET','/')
        self.assertEqual(status,200)
        self.assertIn('default-src \'self\'',headers['Content-Security-Policy'])
        self.assertEqual(headers['X-Frame-Options'],'DENY')
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn('Access-Control-Allow-Origin',headers)

    def test_visible_branding_names_zorava_without_renaming_internals(self):
        root=Path(__file__).resolve().parents[1]
        html=(root/'dashboard/index.html').read_text(encoding='utf-8')
        self.assertIn('<title>Zorava Dashboard</title>',html)
        self.assertIn('>ZORAVA <span>LOCAL OPERATIONS</span>',html)
        self.assertIn('<footer>Zorava ',html)
        self.assertNotIn('AI Worker Dashboard',html)
        js=(root/'dashboard/static/app.js').read_text(encoding='utf-8')
        self.assertIn("'Zorava secrets'",js)
        self.assertIn('Expired Zorava job records',js)
        # Internal identifiers are deliberately unchanged by the rename.
        self.assertIn('ai-router:scroll:${location.pathname}${location.search}',js)
        server=(root/'dashboard/server.py').read_text(encoding='utf-8')
        self.assertIn("Zorava Dashboard\\nhttp://127.0.0.1:",server)
        status,_,body=self.request('GET','/')
        self.assertEqual(status,200)
        self.assertIn(b'Zorava Dashboard',body);self.assertIn(b'ZORAVA',body)

    def test_host_validation_rejects_untrusted_host(self):
        status,_,_=self.request('GET','/',headers={'Host':'evil.example'})
        self.assertEqual(status,400)

    def test_provider_and_settings_api_are_safe_and_cached(self):
        status,_,body=self.request('GET','/api/v1/providers')
        self.assertEqual(status,200);data=json.loads(body)
        self.assertEqual(data['providers']['qwen']['requested_model'],'qwen-test')
        self.assertEqual(data['providers']['qwen']['endpoint_url'],'https://token-plan.example/v1')
        status,_,body=self.request('GET','/api/v1/settings')
        self.assertEqual(status,200);settings=json.loads(body)
        self.assertEqual(settings['retention_days'],30)
        # Timeout values are served from the request constants rather than
        # duplicated here: 30-minute defaults for both coders, unchanged cap.
        self.assertEqual(settings['qwen_timeout_seconds'],DEFAULT_TIMEOUT['qwen'])
        self.assertEqual(settings['kimi_timeout_seconds'],DEFAULT_TIMEOUT['kimi'])
        self.assertEqual(settings['max_timeout_seconds'],MAX_TIMEOUT)
        self.assertEqual((settings['qwen_timeout_seconds'],settings['kimi_timeout_seconds'],
                          settings['max_timeout_seconds']),(1800,1800,1800))
        status,_,body=self.request('GET','/api/v1/permissions')
        self.assertEqual(status,200);self.assertIn('same user',json.loads(body)['os_sandbox'])

    def test_open_tab_can_refresh_its_action_token_after_restart(self):
        old_headers=self.csrf_headers()
        self.controller.csrf_token='new-dashboard-session'
        self.assertEqual(self.request('POST','/api/v1/cleanup/preview',body='{}',headers=old_headers)[0],403)
        status,headers,body=self.request('GET','/api/v1/session')
        self.assertEqual(status,200)
        self.assertEqual(headers['Cache-Control'],'no-store')
        old_headers['X-AI-Worker-CSRF']=json.loads(body)['csrf_token']
        self.assertEqual(self.request('POST','/api/v1/cleanup/preview',body='{}',headers=old_headers)[0],200)
        self.assertEqual(self.request('GET','/api/v1/session',headers={'Host':'evil.example'})[0],400)

    def test_repeated_tests_update_id_and_completion_time_for_each_provider(self):
        for worker in ('qwen','kimi'):
            first=self.add_provider_test(worker)
            self.store.finish_job(first,'completed',duration_ms=1,result='WORKER_OK')
            first_time=self.controller.providers()[worker]['last_test_at']
            second=self.add_provider_test(worker)
            self.store.start_job(second,123,123,'fixture')
            running=self.controller.providers()[worker]
            self.assertEqual(running['last_test_id'],second)
            self.assertEqual(running['last_success_at'],first_time)
            self.assertIsNone(running['last_test_completed_at'])
            self.store.finish_job(second,'completed',duration_ms=2,result='WORKER_OK')
            completed=self.store.get_job(second)['completed_at']
            for endpoint in ('/api/v1/providers','/api/v1/status'):
                status,_,body=self.request('GET',endpoint)
                self.assertEqual(status,200)
                info=json.loads(body)['providers'][worker]
                self.assertEqual(info['last_test_id'],second)
                self.assertEqual(info['last_test_at'],completed)
                self.assertEqual(info['last_success_at'],completed)
                self.assertEqual(info['last_test_completed_at'],completed)

    def test_provider_history_is_not_limited_to_recent_dashboard_jobs(self):
        job_id=self.add_provider_test('kimi')
        self.store.finish_job(job_id,'completed',duration_ms=1,result='KIMI_WORKER_OK')
        with patch.object(self.controller.store,'dashboard_jobs',return_value=[]):
            self.assertEqual(self.controller.providers()['kimi']['last_test_id'],job_id)

    def add_provider_test(self,worker):
        job_id=str(uuid.uuid4())
        request=SimpleNamespace(worker=worker,cwd=Path(self.temp.name),mode='read-only',task='fixture test',
                                parent_job_id=None,delegation_group_id=None)
        self.store.create_job(job_id,request,worker+'-test',worker,job_type='provider_test')
        return job_id

    def assert_provider_status(self,worker,status,test_status):
        for endpoint in ('/api/v1/providers','/api/v1/status'):
            code,_,body=self.request('GET',endpoint)
            self.assertEqual(code,200)
            provider=json.loads(body)['providers'][worker]
            self.assertEqual(provider['status'],status)
            self.assertEqual(provider['last_test_status'],test_status)

    def test_provider_test_stays_testing_until_completed(self):
        for worker in ('qwen','kimi'):
            with self.subTest(worker=worker):
                job_id=self.add_provider_test(worker)
                self.assert_provider_status(worker,'TESTING','queued')
                self.store.start_job(job_id,123,123,'fixture')
                self.assert_provider_status(worker,'TESTING','running')
                self.store.finish_job(job_id,'completed',duration_ms=22000,result='WORKER_OK')
                self.assert_provider_status(worker,'READY','completed')

    def test_provider_test_reports_real_failure_after_testing(self):
        for status,error_code,expected in (('failed','WORKER_CRASH','DEGRADED'),
                                           ('auth_error','AUTH_ERROR','AUTH_REQUIRED'),
                                           ('rate_limited','RATE_LIMITED','DEGRADED')):
            with self.subTest(error_code=error_code):
                job_id=self.add_provider_test('qwen')
                self.store.start_job(job_id,123,123,'fixture')
                self.assert_provider_status('qwen','TESTING','running')
                self.store.finish_job(job_id,status,duration_ms=1000,error_code=error_code)
                self.assert_provider_status('qwen',expected,status)

    def test_provider_is_testing_while_wrapper_starts_before_job_is_recorded(self):
        with patch.dict(self.controller._test_processes,{'pending-test':('qwen',object())}):
            self.assert_provider_status('qwen','TESTING',None)

    def test_jobs_detail_logs_and_malformed_ids(self):
        job_id=self.add_job(result='<script>fake()</script>')
        status,_,body=self.request('GET','/api/v1/jobs')
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['jobs'][0]['id'],job_id)
        self.assertNotIn(b'<script>',body)  # list view omits large result bodies
        status,_,body=self.request('GET',f'/api/v1/jobs/{job_id}')
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['job']['result'],'<script>fake()</script>')
        js=(Path(__file__).resolve().parents[1]/'dashboard/static/app.js').read_text()
        self.assertIn('textContent',js);self.assertNotIn('innerHTML',js)
        self.assertEqual(self.request('GET','/api/v1/jobs/not-a-uuid')[0],400)
        self.assertEqual(self.request('GET','/api/v1/jobs/'+str(uuid.uuid4()))[0],404)
        self.add_job(status='failed')
        status,_,body=self.request('GET','/api/v1/logs?errors=1')
        self.assertEqual(status,200);self.assertTrue(json.loads(body)['events'])

    def test_static_app_restores_scroll_offset_after_reload_only(self):
        js=(Path(__file__).resolve().parents[1]/'dashboard/static/app.js').read_text()
        # The page renders asynchronously, so the browser cannot restore an offset itself.
        self.assertIn("history.scrollRestoration = 'manual'",js)
        # The offset is keyed by the current URL and holds nothing but a number.
        self.assertIn('ai-router:scroll:${location.pathname}${location.search}',js)
        self.assertIn('sessionStorage.setItem(scrollKey, String(offset))',js)
        self.assertIn('sessionStorage.removeItem(scrollKey)',js)
        self.assertNotIn('localStorage',js)
        # It is saved on unload and restored after render, deferred one frame.
        self.assertIn("addEventListener('pagehide', rememberScroll)",js)
        self.assertIn("addEventListener('beforeunload', rememberScroll)",js)
        self.assertIn('render().then',js);self.assertIn('restoreScroll()',js)
        self.assertIn('requestAnimationFrame(apply)',js)
        # Blocked or full storage degrades to starting at the top instead of failing.
        self.assertIn('catch (error) { return 0; }',js)
        # Reload/back-forward reuse the offset; link navigation still starts at the top.
        self.assertIn("navigationEntry.type === 'reload'",js)
        self.assertIn("navigationEntry.type === 'back_forward'",js)
        # The cancel action's reload and the timed re-render behavior are preserved.
        self.assertIn('location.reload()',js)
        self.assertIn('rerenderKeepingScroll(overview)',js)
        status,_,body=self.request('GET','/static/app.js')
        self.assertEqual(status,200);self.assertIn(b'scrollRestoration',body)

    def test_state_changing_requests_require_csrf_and_same_origin(self):
        headers=self.csrf_headers(origin='http://attacker.example')
        status,_,_=self.request('POST','/api/v1/cleanup/preview',body='{}',headers=headers)
        self.assertEqual(status,403)
        headers=self.csrf_headers();headers['X-AI-Worker-CSRF']='wrong'
        status,_,_=self.request('POST','/api/v1/cleanup/preview',body='{}',headers=headers)
        self.assertEqual(status,403)

    def test_provider_test_is_fixed_action_and_returns_job_id(self):
        expected=str(uuid.uuid4());seen=[]
        self.controller.start_test=lambda worker:(seen.append(worker) or expected)
        status,_,body=self.request('POST','/api/v1/test/qwen',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,202);self.assertEqual(seen,['qwen']);self.assertEqual(json.loads(body)['job_id'],expected)
        status,_,_=self.request('POST','/api/v1/shell',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,404)

    def test_kimi_test_recovers_exited_job_instead_of_staying_busy(self):
        stale_id=self.add_provider_test('kimi')
        self.store.start_job(stale_id,123,123,'fixture')
        with patch('dashboard.server.Supervisor._group_exists',return_value=False), \
             patch('dashboard.server.subprocess.Popen') as launch, \
             patch.object(self.controller,'_collect_test'):
            status,_,body=self.request('POST','/api/v1/test/kimi',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,202,body)
        launch.assert_called_once()
        self.assertNotEqual(json.loads(body)['job_id'],stale_id)
        stale=self.store.get_job(stale_id)
        self.assertEqual(stale['status'],'failed')
        self.assertEqual(stale['error_code'],'SUPERVISOR_INTERRUPTED')
        self.assertIsNotNone(stale['completed_at'])
        self.assertEqual(stale['events'][-1]['detail'],'SUPERVISOR_INTERRUPTED')

    def test_test_does_not_recover_job_while_supervisor_still_holds_lock(self):
        job_id=self.add_provider_test('kimi')
        self.store.start_job(job_id,123,123,'fixture')
        with (self.runtime/'locks/kimi.lock').open('rb') as lock, \
             patch('dashboard.server.Supervisor._group_exists',return_value=False), \
             patch('dashboard.server.subprocess.Popen') as launch:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            status,_,body=self.request('POST','/api/v1/test/kimi',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,409,body)
        launch.assert_not_called()
        self.assertEqual(self.store.get_job(job_id)['status'],'running')

    def test_test_does_not_recover_job_with_surviving_process_group(self):
        job_id=self.add_provider_test('kimi')
        self.store.start_job(job_id,123,123,'fixture')
        with patch('dashboard.server.Supervisor._group_exists',return_value=True), \
             patch('dashboard.server.subprocess.Popen') as launch:
            status,_,body=self.request('POST','/api/v1/test/kimi',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,409,body)
        launch.assert_not_called()
        self.assertEqual(self.store.get_job(job_id)['status'],'running')

    def test_test_does_not_recover_queued_job_waiting_to_start(self):
        job_id=self.add_provider_test('kimi')
        with patch('dashboard.server.subprocess.Popen') as launch:
            status,_,body=self.request('POST','/api/v1/test/kimi',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,409,body)
        launch.assert_not_called()
        self.assertEqual(self.store.get_job(job_id)['status'],'queued')

    def test_cancel_requires_existing_running_job_and_is_post_only(self):
        job_id=self.add_job(status='running')
        called=threading.Event()
        def cancel(value): called.set();return {'job_id':value,'status':'cancel_requested'}
        self.controller.cancel_job=cancel
        status,_,body=self.request('POST',f'/api/v1/jobs/{job_id}/cancel',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,202);self.assertTrue(called.wait(2));self.assertEqual(json.loads(body)['status'],'cancellation_requested')

    def test_cleanup_confirmation_is_required_and_dry_run_does_not_mutate(self):
        old_id=self.add_job()
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?",(old_id,))
        status,_,body=self.request('POST','/api/v1/cleanup/preview',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['eligible_count'],1)
        self.assertIsNotNone(self.store.get_job(old_id))
        status,_,_=self.request('POST','/api/v1/cleanup/confirm',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,400)
        status,_,body=self.request('POST','/api/v1/cleanup/confirm',body='{"confirm":true}',headers=self.csrf_headers())
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['deleted_count'],1)
        self.assertIsNone(self.store.get_job(old_id))

    def install_update_fetcher(self, fetcher):
        """Use an offline fetcher: a dashboard GET must never reach a source.

        Returns the supplied fetcher, so a test can inspect the calls it recorded.
        """
        self.controller.updates=updates.UpdateRegistry(fetcher=fetcher)
        self.addCleanup(self.controller.updates.shutdown,5)
        return fetcher

    def test_provider_cards_include_cache_only_update_fields(self):
        fetcher=self.install_update_fetcher(OfflineFetcher())
        for endpoint in ('/api/v1/providers','/api/v1/status'):
            status,_,body=self.request('GET',endpoint)
            self.assertEqual(status,200);providers=json.loads(body)['providers']
            for worker,installed in (('qwen','1.2.3'),('kimi','2.3.4')):
                info=providers[worker]
                self.assertEqual(info['installed_version'],installed)
                self.assertIsNone(info['latest_version'])
                self.assertEqual(info['update_state'],'unknown')
                self.assertFalse(info['update_checking'])
                self.assertEqual(info['update_source'],updates.SOURCES[worker])
                self.assertIsNone(info['last_update_check_at'])
                self.assertIsNone(info['last_update_success_at'])
                self.assertIsNone(info['last_update_error'])
            self.assertNotIn('update_state',providers['claude'])
        # Reading provider state never contacts a version source.
        self.assertEqual(fetcher.calls,[])

    def test_updates_endpoint_is_cache_only_and_publishes_its_fixed_sources(self):
        fetcher=self.install_update_fetcher(OfflineFetcher({'qwen':('9.9.9',None)}))
        status,headers,body=self.request('GET','/api/v1/updates')
        self.assertEqual(status,200);self.assertEqual(headers['Cache-Control'],'no-store')
        data=json.loads(body)
        self.assertTrue(data['cache_only'])
        self.assertEqual(data['workers'],['qwen','kimi'])
        self.assertEqual(data['sources'],updates.SOURCES)
        self.assertEqual(sorted(data['updates']),['kimi','qwen'])
        self.assertEqual(data['updates']['qwen']['installed_version'],'1.2.3')
        self.assertEqual(data['updates']['qwen']['update_state'],'unknown')
        self.assertIsNone(data['updates']['kimi']['last_update_check_at'])
        self.assertEqual(fetcher.calls,[])
        self.assertEqual(self.request('GET','/api/v1/updates/anything')[0],404)

    def test_update_check_is_explicit_csrf_protected_and_bounded(self):
        started=threading.Event();release=threading.Event()
        def fetcher(worker):
            started.set();release.wait(10);return ('9.9.9',None)
        self.install_update_fetcher(fetcher)
        headers=self.csrf_headers();headers['X-AI-Worker-CSRF']='wrong'
        self.assertEqual(self.request('POST','/api/v1/updates/check',body='{}',headers=headers)[0],403)
        self.assertEqual(self.request('POST','/api/v1/updates/check',body='{}',
                                      headers=self.csrf_headers(origin='http://attacker.example'))[0],403)
        self.assertFalse(started.is_set())
        for body in ('{"workers":["claude"]}','{"workers":"qwen"}','{"workers":[]}','{"workers":["qwen","qwen"]}'):
            status,_,payload=self.request('POST','/api/v1/updates/check',body=body,headers=self.csrf_headers())
            self.assertEqual(status,400,payload)
        self.assertFalse(started.is_set())
        status,_,body=self.request('POST','/api/v1/updates/check',body='{"workers":["qwen"]}',headers=self.csrf_headers())
        self.assertEqual(status,202,body);payload=json.loads(body)
        self.assertEqual(payload['status'],'accepted')
        self.assertEqual(payload['requested'],['qwen'])
        self.assertEqual(payload['already_checking'],[])
        self.assertTrue(started.wait(5))
        # While it runs, a GET reports "checking" and a repeat click is coalesced.
        status,_,body=self.request('GET','/api/v1/providers')
        info=json.loads(body)['providers']['qwen']
        self.assertTrue(info['update_checking']);self.assertEqual(info['update_state'],'checking')
        status,_,body=self.request('POST','/api/v1/updates/check',body='{"workers":["qwen"]}',headers=self.csrf_headers())
        self.assertEqual(status,202);payload=json.loads(body)
        self.assertEqual(payload['requested'],[]);self.assertEqual(payload['already_checking'],['qwen'])
        release.set();self.controller.updates.shutdown(timeout=5)
        status,_,body=self.request('GET','/api/v1/providers')
        info=json.loads(body)['providers']['qwen']
        self.assertEqual(info['latest_version'],'9.9.9')
        self.assertEqual(info['update_state'],'update-available')
        self.assertEqual(info['installed_version'],'1.2.3')
        self.assertFalse(info['update_checking'])
        self.assertIsNotNone(info['last_update_check_at'])
        self.assertIsNotNone(info['last_update_success_at'])
        self.assertIsNone(info['last_update_error'])
        status,_,body=self.request('GET','/api/v1/updates')
        self.assertEqual(json.loads(body)['updates']['kimi']['update_state'],'unknown')

    def test_update_check_defaults_to_both_workers_and_classifies_versions(self):
        fetcher=self.install_update_fetcher(OfflineFetcher({'qwen':('1.2.3',None),'kimi':('2.3.0',None)}))
        status,_,body=self.request('POST','/api/v1/updates/check',body='{}',headers=self.csrf_headers())
        self.assertEqual(status,202,body)
        self.assertEqual(sorted(json.loads(body)['requested']),['kimi','qwen'])
        self.controller.updates.shutdown(timeout=5)
        status,_,body=self.request('GET','/api/v1/providers')
        self.assertEqual(status,200);providers=json.loads(body)['providers']
        self.assertEqual(providers['qwen']['update_state'],'up-to-date')
        self.assertEqual(providers['qwen']['latest_version'],'1.2.3')
        # Installed 2.3.4 is newer than the published 2.3.0, so it is stale.
        self.assertEqual(providers['kimi']['update_state'],'stale')
        self.assertEqual(providers['kimi']['latest_version'],'2.3.0')
        self.assertEqual(sorted(fetcher.calls),['kimi','qwen'])

    def test_failed_update_check_reports_a_fixed_error_code(self):
        fetcher=self.install_update_fetcher(OfflineFetcher({'qwen':(None,'NETWORK_ERROR')}))
        status,_,body=self.request('POST','/api/v1/updates/check',body='{"workers":["qwen"]}',headers=self.csrf_headers())
        self.assertEqual(status,202,body)
        self.controller.updates.shutdown(timeout=5)
        status,_,body=self.request('GET','/api/v1/providers')
        info=json.loads(body)['providers']['qwen']
        self.assertEqual(info['update_state'],'unknown')
        self.assertIsNone(info['latest_version'])
        self.assertEqual(info['last_update_error'],'NETWORK_ERROR')
        self.assertIsNotNone(info['last_update_check_at'])
        self.assertIsNone(info['last_update_success_at'])
        self.assertEqual(fetcher.calls,['qwen'])
        # Provider test status is unchanged by version metadata.
        self.assertEqual(info['status'],'UNKNOWN')

    def test_static_ui_shows_versions_and_an_explicit_upgrade_check(self):
        js=(Path(__file__).resolve().parents[1]/'dashboard/static/app.js').read_text(encoding='utf-8')
        # Cards report the installed CLI, the published latest, and a pending label.
        self.assertIn("addKV(dl,'Installed CLI'",js)
        self.assertIn("addKV(dl,'Latest available'",js)
        self.assertIn("addKV(dl,'Pending upgrade'",js)
        self.assertIn("'update-available': 'Upgrade pending'",js)
        self.assertIn("info.update_state === 'update-available'",js)
        self.assertIn('upgrade-pill',js)
        # The refresh is an explicit operator action against the fixed endpoint,
        # and it is followed through the cache-only endpoint.
        self.assertIn('Check for upgrades',js)
        self.assertIn("dataset.action='check-updates'",js)
        self.assertIn("post('/api/v1/updates/check',{workers:[worker]})",js)
        self.assertIn("get('/api/v1/updates')",js)
        self.assertIn('followUpdateCheck',js)
        self.assertIn('entry.update_checking',js)  # poll reads the API's own key
        # Existing behavior is preserved and nothing installs an upgrade.
        self.assertIn("dataset.action='test'",js)
        self.assertIn('rerenderKeepingScroll',js)
        self.assertIn('sessionStorage.setItem(scrollKey, String(offset))',js)
        self.assertIn('const session = await get(\'/api/v1/session\')',js)
        self.assertIn('textContent',js);self.assertNotIn('innerHTML',js)
        for forbidden in ('npm install','pip install','brew upgrade','/api/v1/updates/install',
                          'installUpgrade','autoUpdate'):
            self.assertNotIn(forbidden,js)
        status,_,body=self.request('GET','/static/app.js')
        self.assertEqual(status,200)
        self.assertIn(b'Check for upgrades',body);self.assertIn(b'Installed CLI',body)
        status,_,body=self.request('GET','/static/dashboard.css')
        self.assertEqual(status,200);self.assertIn(b'upgrade-pill',body)

    def test_dashboard_refuses_public_bind(self):
        with self.assertRaises(ValueError):
            serve(host='0.0.0.0',port=8787)


if __name__ == '__main__':
    unittest.main()
