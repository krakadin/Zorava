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

from ai_router.state import StateStore
from dashboard.server import DashboardController, make_handler, serve


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ai-router-dashboard-')
        self.addCleanup(self.temp.cleanup)
        self.runtime=Path(self.temp.name)/'state';self.runtime.mkdir(mode=0o700);os.chmod(self.runtime,0o700)
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

    def test_host_validation_rejects_untrusted_host(self):
        status,_,_=self.request('GET','/',headers={'Host':'evil.example'})
        self.assertEqual(status,400)

    def test_provider_and_settings_api_are_safe_and_cached(self):
        status,_,body=self.request('GET','/api/v1/providers')
        self.assertEqual(status,200);data=json.loads(body)
        self.assertEqual(data['providers']['qwen']['requested_model'],'qwen-test')
        self.assertEqual(data['providers']['qwen']['endpoint_url'],'https://token-plan.example/v1')
        status,_,body=self.request('GET','/api/v1/settings')
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['retention_days'],30)
        status,_,body=self.request('GET','/api/v1/permissions')
        self.assertEqual(status,200);self.assertIn('same user',json.loads(body)['os_sandbox'])

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

    def test_dashboard_refuses_public_bind(self):
        with self.assertRaises(ValueError):
            serve(host='0.0.0.0',port=8787)


if __name__ == '__main__':
    unittest.main()
