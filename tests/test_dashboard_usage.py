import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from http.server import ThreadingHTTPServer

from ai_router.kimi_quota import QUOTA_REFRESH_COOLDOWN_S, QuotaCache
from ai_router.state import StateStore
from dashboard.server import DashboardController, make_handler, _percent


OK_PAYLOAD = {'status': 'available',
              'windows': [{'name': '5-hour', 'used_ratio': 0.5,
                           'reset_at': '2026-09-23T17:00:00+00:00'}],
              'message': 'Account quota reported by Kimi Code.'}


class DashboardUsageTests(unittest.TestCase):
    """Offline usage/quota dashboard tests; the quota fetcher is always injected."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-usage-')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'state'
        self.runtime.mkdir(mode=0o700)
        os.chmod(self.runtime, 0o700)
        (self.runtime / 'locks').mkdir(mode=0o700)
        safe = {'claude': {'worker': 'claude', 'status': 'CONFIGURED', 'requested_model': 'claude-local',
                           'provider': 'Anthropic', 'endpoint_host': 'api.anthropic.com',
                           'authentication': 'OAuth', 'routing': 'DIRECT'},
                'qwen': {'worker': 'qwen', 'status': 'CONFIGURED', 'requested_model': 'qwen-test',
                         'provider': 'Token Plan', 'endpoint_host': 'token-plan.example',
                         'endpoint_url': 'https://token-plan.example/v1',
                         'authentication': 'credential present', 'version': '1.2.3'},
                'kimi': {'worker': 'kimi', 'status': 'CONFIGURED', 'requested_model': 'kimi-test',
                         'provider': 'Kimi', 'endpoint_host': 'api.kimi.example',
                         'authentication': 'OAuth', 'version': '2.3.4'}}
        self.mono = [1000.0]
        self.wall = ['2026-09-23T12:00:00+00:00']
        self.fetcher = Mock(return_value=dict(OK_PAYLOAD, windows=[dict(w) for w in OK_PAYLOAD['windows']]))
        # Synchronous spawner keeps refresh behavior deterministic and offline.
        self.quota_cache = QuotaCache(fetcher=self.fetcher,
                                      clock=lambda: self.wall[0],
                                      now=lambda: self.mono[0],
                                      spawner=lambda target: target())
        with patch.object(DashboardController, '_load_provider_snapshot', return_value=safe):
            self.controller = DashboardController(self.runtime, port=8787, quota_cache=self.quota_cache)
        self.controller.provider_snapshot = safe
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.controller))
        self.server.daemon_threads = True
        self.controller.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.store = StateStore(self.runtime / 'workers.db')

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.controller.shutdown_owned_tests()

    def request(self, method, path, *, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        status = response.status
        conn.close()
        return status, data

    def csrf_headers(self, origin=None):
        return {'Host': f'127.0.0.1:{self.server.server_port}',
                'Origin': origin or f'http://127.0.0.1:{self.server.server_port}',
                'Sec-Fetch-Site': 'same-origin', 'X-AI-Worker-CSRF': self.controller.csrf_token,
                'Content-Type': 'application/json'}

    def add_job(self, worker='qwen', status='completed', usage=None):
        job_id = str(uuid.uuid4())
        request = SimpleNamespace(worker=worker, cwd=Path(self.temp.name), mode='read-only',
                                  task='fixture task', parent_job_id=None, delegation_group_id=None)
        self.store.create_job(job_id, request, worker + '-test', worker, '1.0')
        if status in ('queued', 'running'):
            if status == 'running':
                self.store.start_job(job_id, 123, 123, 'fixture')
        else:
            self.store.finish_job(job_id, status, duration_ms=5,
                                  error_code='FAKE' if status != 'completed' else None,
                                  usage=usage)
        return job_id

    def test_quota_cache_is_injected_and_get_never_fetches(self):
        for endpoint in ('/api/v1/usage', '/api/v1/providers', '/api/v1/status'):
            status, body = self.request('GET', endpoint)
            self.assertEqual(status, 200, body)
        self.fetcher.assert_not_called()
        status, body = self.request('GET', '/api/v1/usage')
        data = json.loads(body)
        self.assertTrue(data['cache_only'])
        self.assertEqual(data['kimi_quota']['state'], 'unavailable')
        self.assertEqual(data['kimi_quota']['windows'], [])
        self.assertIn('not been checked', data['kimi_quota']['detail'])
        self.assertEqual(data['kimi_quota']['refresh_cooldown_seconds'], int(QUOTA_REFRESH_COOLDOWN_S))
        self.fetcher.assert_not_called()

    def test_default_controller_builds_a_real_idle_cache(self):
        with patch.object(DashboardController, '_load_provider_snapshot', return_value={}):
            controller = DashboardController(self.runtime, port=8787)
        self.assertIsInstance(controller.quota_cache, QuotaCache)
        snapshot = controller.usage_snapshot()
        self.assertEqual(snapshot['kimi_quota']['state'], 'unavailable')

    def test_refresh_is_explicit_csrf_protected_and_reports_cooldown(self):
        # Cross-origin, wrong-token, and cross-site posts are all rejected
        # before any refresh can be requested.
        headers = self.csrf_headers(); headers['X-AI-Worker-CSRF'] = 'wrong'
        self.assertEqual(self.request('POST', '/api/v1/usage/refresh', body='{}', headers=headers)[0], 403)
        self.assertEqual(self.request('POST', '/api/v1/usage/refresh', body='{}',
                                      headers=self.csrf_headers(origin='http://attacker.example'))[0], 403)
        headers = self.csrf_headers(); headers['Sec-Fetch-Site'] = 'cross-site'
        self.assertEqual(self.request('POST', '/api/v1/usage/refresh', body='{}', headers=headers)[0], 403)
        self.fetcher.assert_not_called()
        # The action is POST-only.
        self.assertEqual(self.request('GET', '/api/v1/usage/refresh')[0], 404)
        self.fetcher.assert_not_called()
        # First authorized request starts a refresh.
        status, body = self.request('POST', '/api/v1/usage/refresh', body='{}', headers=self.csrf_headers())
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload['status'], 'accepted')
        self.assertTrue(payload['refresh_started'])
        self.assertFalse(payload['deferred'])
        self.assertEqual(payload['kimi_quota']['state'], 'available')
        self.assertEqual(self.fetcher.call_count, 1)
        # Within the cooldown the request is honestly reported as deferred.
        status, body = self.request('POST', '/api/v1/usage/refresh', body='{}', headers=self.csrf_headers())
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertFalse(payload['refresh_started'])
        self.assertTrue(payload['deferred'])
        self.assertIn('cooldown', payload['note'])
        self.assertEqual(self.fetcher.call_count, 1)
        # After the cooldown a new refresh starts.
        self.mono[0] += QUOTA_REFRESH_COOLDOWN_S + 1
        status, body = self.request('POST', '/api/v1/usage/refresh', body='{}', headers=self.csrf_headers())
        self.assertEqual(status, 202, body)
        self.assertTrue(json.loads(body)['refresh_started'])
        self.assertEqual(self.fetcher.call_count, 2)

    def test_refresh_response_is_bounded_metadata_only(self):
        status, body = self.request('POST', '/api/v1/usage/refresh', body='{}', headers=self.csrf_headers())
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(sorted(payload), ['deferred', 'kimi_quota', 'note', 'refresh_started', 'status'])
        self.assertEqual(sorted(payload['kimi_quota']),
                         ['checked_at', 'detail', 'last_attempt_at', 'refresh_cooldown_seconds',
                          'state', 'windows', 'worker'])
        for window in payload['kimi_quota']['windows']:
            self.assertEqual(sorted(window), ['name', 'remaining_percent', 'reset_at', 'used_percent'])
        for forbidden in (b'token', b'bearer', b'Bearer', b'Authorization', b'server.token',
                          b'127.0.0.1:', b'api/v1/oauth'):
            self.assertNotIn(forbidden, body)

    def test_percentage_conversion_boundaries(self):
        self.assertEqual(_percent(0), 0)
        self.assertEqual(_percent(1), 100)
        self.assertEqual(_percent(0.25), 25)
        self.assertEqual(_percent(0.999), 100)
        self.assertEqual(_percent(0.001), 0)
        self.fetcher.return_value = {'status': 'available', 'message': 'ok', 'windows': [
            {'name': '5-hour', 'used_ratio': 0, 'reset_at': None},
            {'name': '7-day', 'used_ratio': 1, 'reset_at': '2026-09-30T00:00:00+00:00'},
            {'name': 'Monthly total', 'used_ratio': 0.25, 'reset_at': None}]}
        self.assertTrue(self.quota_cache.request_check())
        status, body = self.request('GET', '/api/v1/usage')
        self.assertEqual(status, 200)
        windows = json.loads(body)['kimi_quota']['windows']
        self.assertEqual([(w['name'], w['used_percent'], w['remaining_percent']) for w in windows],
                         [('5-hour', 0, 100), ('7-day', 100, 0), ('Monthly total', 25, 75)])
        self.assertEqual(windows[1]['reset_at'], '2026-09-30T00:00:00+00:00')
        # Every window carries both directions and they always sum to 100.
        for window in windows:
            self.assertEqual(window['used_percent'] + window['remaining_percent'], 100)

    def test_stale_and_failed_states_are_reported_honestly(self):
        self.assertTrue(self.quota_cache.request_check())
        self.mono[0] += 301  # cached success ages past the staleness bound
        status, body = self.request('GET', '/api/v1/usage')
        quota = json.loads(body)['kimi_quota']
        self.assertEqual(quota['state'], 'stale')
        self.assertEqual(len(quota['windows']), 1)
        self.fetcher.assert_called_once_with()  # aging never triggers a refetch
        self.fetcher.return_value = {'status': 'unavailable', 'windows': [],
                                     'message': 'Kimi quota unavailable.'}
        self.mono[0] += QUOTA_REFRESH_COOLDOWN_S + 1
        status, body = self.request('POST', '/api/v1/usage/refresh', body='{}', headers=self.csrf_headers())
        self.assertEqual(status, 202)
        quota = json.loads(body)['kimi_quota']
        self.assertEqual(quota['state'], 'stale')  # last good windows survive
        self.assertEqual(quota['detail'], 'Kimi quota unavailable.')

    def test_qwen_reports_quota_unavailable_without_a_fabricated_percentage(self):
        status, body = self.request('GET', '/api/v1/usage')
        self.assertEqual(status, 200)
        qwen = json.loads(body)['qwen_quota']
        self.assertEqual(qwen['state'], 'unavailable')
        self.assertEqual(qwen['windows'], [])
        self.assertIn('unavailable for Qwen', qwen['detail'])
        self.assertNotIn('used_percent', json.dumps(qwen))
        self.assertNotIn('remaining_percent', json.dumps(qwen))
        # The provider card payload carries the same honest unavailability.
        status, body = self.request('GET', '/api/v1/providers')
        self.assertEqual(status, 200)
        usage = json.loads(body)['providers']['qwen']['usage']
        self.assertEqual(usage['state'], 'unavailable')
        self.assertIn('unavailable for Qwen', usage['detail'])
        self.assertNotIn('remaining_percent', json.dumps(usage))
        # Claude has no usage section at all.
        self.assertNotIn('usage', json.loads(body)['providers']['claude'])
        self.fetcher.assert_not_called()

    def test_kimi_provider_card_carries_quota_and_job_tokens(self):
        self.add_job('kimi', 'completed', {'input_tokens': 10, 'output_tokens': 4, 'cached_tokens': 2})
        self.assertTrue(self.quota_cache.request_check())
        status, body = self.request('GET', '/api/v1/providers')
        self.assertEqual(status, 200)
        usage = json.loads(body)['providers']['kimi']['usage']
        self.assertEqual(usage['state'], 'available')
        self.assertEqual(usage['windows'][0]['used_percent'], 50)
        self.assertEqual(usage['windows'][0]['remaining_percent'], 50)
        self.assertEqual(usage['job_tokens']['input_tokens'], 10)
        self.assertEqual(usage['job_tokens']['output_tokens'], 4)
        self.assertEqual(usage['job_tokens']['cached_tokens'], 2)
        self.assertIn('Completed jobs', usage['job_tokens']['scope'])
        # Existing card fields stay intact.
        info = json.loads(body)['providers']['kimi']
        self.assertEqual(info['requested_model'], 'kimi-test')
        self.assertEqual(info['version'], '2.3.4')

    def test_job_token_totals_completed_only_and_missing_counters_stay_unknown(self):
        self.add_job('qwen', 'completed', {'input_tokens': 10, 'output_tokens': 3, 'cached_tokens': 1})
        self.add_job('qwen', 'completed', {'input_tokens': 5, 'output_tokens': None, 'cached_tokens': 0})
        self.add_job('qwen', 'failed', {'input_tokens': 99, 'output_tokens': 99, 'cached_tokens': 99})
        self.add_job('qwen', 'running', None)
        self.add_job('kimi', 'completed', None)  # worker reported no usage
        totals = self.store.job_token_totals()
        self.assertEqual(totals['scope'], 'Completed jobs in retained history')
        qwen = totals['qwen']
        self.assertEqual(qwen['jobs_completed'], 2)
        self.assertEqual(qwen['jobs_with_usage'], 2)
        self.assertEqual(qwen['input_tokens'], 15)
        self.assertEqual(qwen['cached_tokens'], 1)
        # One record lacked the output counter, so the total is honestly unknown.
        self.assertIsNone(qwen['output_tokens'])
        kimi = totals['kimi']
        self.assertEqual(kimi['jobs_completed'], 1)
        self.assertEqual(kimi['jobs_with_usage'], 0)
        self.assertIsNone(kimi['input_tokens'])
        # The API exposes the same aggregate and never a Qwen account balance.
        status, body = self.request('GET', '/api/v1/usage')
        data = json.loads(body)
        self.assertEqual(data['job_token_totals']['qwen']['input_tokens'], 15)
        self.assertIsNone(data['job_token_totals']['qwen']['output_tokens'])
        self.assertNotIn('balance', body.lower())

    def test_job_token_totals_is_bounded(self):
        self.add_job('qwen', 'completed', {'input_tokens': 1, 'output_tokens': 1, 'cached_tokens': 0})
        self.add_job('qwen', 'completed', {'input_tokens': 1, 'output_tokens': 1, 'cached_tokens': 0})
        totals = self.store.job_token_totals(limit=1)
        self.assertEqual(totals['qwen']['jobs_completed'], 1)
        self.assertEqual(totals['qwen']['input_tokens'], 1)

    def test_static_ui_renders_usage_sections_and_explicit_refresh(self):
        js = (Path(__file__).resolve().parents[1] / 'dashboard/static/app.js').read_text(encoding='utf-8')
        # Kimi windows show used/remaining percentages, reset time, and state.
        self.assertIn('`${item.used_percent}% used`', js)
        self.assertIn('`${item.remaining_percent}% remaining`', js)
        self.assertIn('resets ${item.reset_at', js)
        self.assertIn('Quota state:', js)
        # The refresh is an explicit operator action against the fixed endpoint.
        self.assertIn('Refresh usage', js)
        self.assertIn("dataset.action='refresh-usage'", js)
        self.assertIn("post('/api/v1/usage/refresh')", js)
        self.assertIn('followUsageRefresh', js)
        # The follow-up loop reads only the cache-only endpoint.
        self.assertIn("get('/api/v1/usage')", js)
        # Qwen messaging is honest: no account percentage is ever shown.
        self.assertIn('unavailable for Qwen', js)
        self.assertIn('No completed jobs with recorded token usage yet.', js)
        self.assertIn('usage-window', js)
        # Existing rendering, scroll memory, and XSS-safe DOM writes are intact.
        self.assertIn("dataset.action='check-updates'", js)
        self.assertIn("dataset.action='test'", js)
        self.assertIn('rerenderKeepingScroll', js)
        self.assertIn('sessionStorage.setItem(scrollKey, String(offset))', js)
        self.assertIn('textContent', js)
        self.assertNotIn('innerHTML', js)
        status, body = self.request('GET', '/static/app.js')
        self.assertEqual(status, 200)
        self.assertIn(b'Refresh usage', body)
        status, body = self.request('GET', '/static/dashboard.css')
        self.assertEqual(status, 200)
        self.assertIn(b'usage-window', body)


if __name__ == '__main__':
    unittest.main()
