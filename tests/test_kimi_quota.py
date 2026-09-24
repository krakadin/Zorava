import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ai_router.kimi_quota import (KimiQuotaCache, QuotaCache, fetch_kimi_quota,
                                  parse_quota, _server_port, _NoRedirect)


class KimiQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.payload = {'code': 0, 'data': {'kind': 'ok', 'quota': {'usages': {
            'limit5h': {'usedRatio': 0.25, 'resetAt': '2026-09-24T04:00:00Z'},
            'monthCode': {'usedRatio': 0}}, 'extraUsage': {'balanceCents': 100}}}}

    def token(self):
        path = self.home / 'server.token'
        path.write_text('synthetic-local-server-token')
        path.chmod(0o600)
        return path

    def test_documented_windows_only_and_no_account_or_wallet_details(self):
        result = parse_quota(self.payload)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(len(result['windows']), 2)
        self.assertEqual(result['windows'][0]['used_ratio'], .25)
        self.assertEqual(result['windows'][1]['used_ratio'], 0)
        self.assertNotIn('balanceCents', json.dumps(result))

    def test_errors_and_invalid_ratios_are_not_reported_as_zero(self):
        for data in ({'code': 0, 'data': {'kind': 'error', 'message': 'provider text'}},
                     {'code': 40101}, [], {'code': 0, 'data': []}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_quota(data)
        for ratio in (True, -1, 1.1, float('nan'), float('inf'), '0.3', None):
            self.payload['data']['quota']['usages'] = {'limit5h': {'usedRatio': ratio}}
            with self.subTest(ratio=ratio), self.assertRaises(ValueError):
                parse_quota(self.payload)

    def test_no_server_is_unavailable_without_network_or_token_access(self):
        with patch('ai_router.kimi_quota.build_opener') as opener:
            self.assertEqual(fetch_kimi_quota(self.home)['status'], 'unavailable')
            opener.assert_not_called()

    def test_local_request_uses_only_server_token_and_fixed_usage_route(self):
        self.token()
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(self.payload).encode())
        with patch('ai_router.kimi_quota._server_port', return_value=58627), \
             patch('ai_router.kimi_quota.build_opener', return_value=opener):
            result = fetch_kimi_quota(self.home)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:58627/api/v1/oauth/usage')
        self.assertEqual(result['status'], 'available')
        self.assertNotIn('synthetic-local-server-token', json.dumps(result))

    def test_unsafe_token_and_redirect_are_rejected(self):
        token = self.token()
        token.chmod(0o644)
        with patch('ai_router.kimi_quota._server_port', return_value=58627), \
             patch('ai_router.kimi_quota.build_opener') as opener:
            self.assertEqual(fetch_kimi_quota(self.home)['status'], 'unavailable')
            opener.assert_not_called()
        token.unlink()
        token.symlink_to(self.home / 'elsewhere')
        with patch('ai_router.kimi_quota._server_port', return_value=58627):
            self.assertEqual(fetch_kimi_quota(self.home)['status'], 'unavailable')
        with self.assertRaises(ValueError):
            _NoRedirect().redirect_request(None, None, 302, '', {}, 'https://untrusted.example/')

    def test_discovery_rejects_remote_hosts_and_unrelated_processes(self):
        directory = self.home / 'server/instances'
        directory.mkdir(parents=True)
        # mkdir honors the process umask (0775 under umask 002); the safe
        # reader rightly rejects group-writable metadata directories, so the
        # fixture must pin non-writable permissions to reach discovery logic.
        os.chmod(self.home / 'server', 0o755)
        os.chmod(directory, 0o755)
        path = directory / 'test.json'
        record = {'pid': os.getpid(), 'host': 'remote.example', 'port': 58627}
        path.write_text(json.dumps(record))
        # write_text also follows the umask; keep the record non-writable.
        path.chmod(0o644)
        with patch('ai_router.kimi_quota.KIMI_BINARY', Path(sys.executable)):
            self.assertIsNone(_server_port(self.home))
            record['host'] = '127.0.0.1'
            path.write_text(json.dumps(record))
            self.assertEqual(_server_port(self.home), 58627)
        self.assertIsNone(_server_port(self.home))


class QuotaCacheTests(unittest.TestCase):
    """Offline tests: injected fetcher/clock/now/spawner, no network."""

    def setUp(self):
        self.mono = [1000.0]
        self.wall = ['2026-09-23T12:00:00+00:00']

    def make(self, fetcher, spawner=None):
        return QuotaCache(fetcher=fetcher,
                          clock=lambda: self.wall[0],
                          now=lambda: self.mono[0],
                          spawner=spawner or (lambda target: target()))

    @staticmethod
    def ok(message='Account quota reported by Kimi Code.'):
        return {'status': 'available',
                'windows': [{'name': '5-hour', 'used_ratio': 0.5,
                             'reset_at': '2026-09-23T17:00:00+00:00'}],
                'message': message}

    @staticmethod
    def down(message='Start kimi web --no-open on localhost, then refresh quota.'):
        return {'status': 'unavailable', 'windows': [], 'message': message}

    def test_alias_and_construction_and_describe_never_fetch(self):
        self.assertIs(KimiQuotaCache, QuotaCache)
        fetcher = Mock()
        cache = self.make(fetcher)
        snapshot = cache.describe()
        self.assertEqual(snapshot['state'], 'unavailable')
        self.assertEqual(snapshot['windows'], [])
        self.assertIn('not been checked', snapshot['detail'])
        self.assertIsNone(snapshot['checked_at'])
        self.assertIsNone(snapshot['last_attempt_at'])
        self.assertIsNone(snapshot['last_success_at'])
        fetcher.assert_not_called()
        # Pure defaults also stay idle (real fetcher is never called here).
        self.assertEqual(QuotaCache().describe()['state'], 'unavailable')

    def test_success_records_windows_and_timestamps(self):
        fetcher = Mock(return_value=self.ok())
        cache = self.make(fetcher)
        self.assertTrue(cache.request_check())
        snapshot = cache.describe()
        self.assertEqual(snapshot['state'], 'available')
        self.assertEqual(snapshot['windows'], [{'name': '5-hour', 'used_ratio': 0.5,
                                                'reset_at': '2026-09-23T17:00:00+00:00'}])
        self.assertEqual(snapshot['detail'], 'Account quota reported by Kimi Code.')
        self.assertEqual(snapshot['checked_at'], self.wall[0])
        self.assertEqual(snapshot['last_attempt_at'], self.wall[0])
        self.assertEqual(snapshot['last_success_at'], self.wall[0])
        fetcher.assert_called_once_with()
        # Returned windows are copies, not the cached structures.
        snapshot['windows'][0]['used_ratio'] = 0.99
        self.assertEqual(cache.describe()['windows'][0]['used_ratio'], 0.5)

    def test_request_check_is_nonblocking_and_single_flight(self):
        entered, release = threading.Event(), threading.Event()

        def fetcher():
            entered.set()
            self.assertTrue(release.wait(5))
            return self.ok()

        cache = QuotaCache(fetcher=fetcher, clock=lambda: self.wall[0],
                           now=lambda: self.mono[0])  # real daemon-thread spawner
        self.assertTrue(cache.request_check())
        self.assertTrue(entered.wait(5))
        # The fetch is still blocked, yet describe() answered immediately.
        self.assertEqual(cache.describe()['state'], 'checking')
        self.assertFalse(cache.request_check())  # single flight
        release.set()
        deadline = time.monotonic() + 5
        while cache.describe()['state'] == 'checking' and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(cache.describe()['state'], 'available')

    def test_repeated_attempts_are_rate_limited(self):
        fetcher = Mock(return_value=self.ok())
        cache = self.make(fetcher)
        self.assertTrue(cache.request_check())
        self.assertFalse(cache.request_check())
        self.mono[0] += 59
        self.assertFalse(cache.request_check())
        self.mono[0] += 2
        self.assertTrue(cache.request_check())
        self.assertEqual(fetcher.call_count, 2)

    def test_missing_server_stops_checking_with_honest_detail(self):
        fetcher = Mock(return_value=self.down())
        cache = self.make(fetcher)
        self.assertTrue(cache.request_check())
        snapshot = cache.describe()
        self.assertEqual(snapshot['state'], 'unavailable')
        self.assertEqual(snapshot['windows'], [])
        self.assertEqual(snapshot['detail'],
                         'Start kimi web --no-open on localhost, then refresh quota.')
        self.assertIsNone(snapshot['last_success_at'])
        self.assertIsNone(snapshot['checked_at'])
        self.assertEqual(snapshot['last_attempt_at'], self.wall[0])

    def test_failure_after_success_preserves_windows_and_marks_stale(self):
        fetcher = Mock(side_effect=[self.ok(), self.down('Kimi quota unavailable.')])
        cache = self.make(fetcher)
        self.assertTrue(cache.request_check())
        first_success = self.wall[0]
        self.mono[0] += 61
        self.wall[0] = '2026-09-23T12:01:01+00:00'
        self.assertTrue(cache.request_check())
        snapshot = cache.describe()
        self.assertEqual(snapshot['state'], 'stale')
        self.assertEqual(snapshot['windows'], [{'name': '5-hour', 'used_ratio': 0.5,
                                                'reset_at': '2026-09-23T17:00:00+00:00'}])
        self.assertEqual(snapshot['detail'], 'Kimi quota unavailable.')
        self.assertEqual(snapshot['last_success_at'], first_success)
        self.assertEqual(snapshot['checked_at'], first_success)
        self.assertEqual(snapshot['last_attempt_at'], '2026-09-23T12:01:01+00:00')

    def test_cached_success_stales_with_age_without_refetch(self):
        fetcher = Mock(return_value=self.ok())
        cache = self.make(fetcher)
        self.assertTrue(cache.request_check())
        self.mono[0] += 299
        self.assertEqual(cache.describe()['state'], 'available')
        self.mono[0] += 2
        snapshot = cache.describe()
        self.assertEqual(snapshot['state'], 'stale')
        self.assertEqual(len(snapshot['windows']), 1)
        self.assertIn('stale', snapshot['detail'])
        fetcher.assert_called_once_with()  # no silent auto-fetch

    def test_fetcher_exception_is_sanitized(self):
        def fetcher():
            raise RuntimeError('dial failed with bearer token synthetic-secret')

        cache = self.make(fetcher)
        self.assertTrue(cache.request_check())
        snapshot = cache.describe()
        self.assertEqual(snapshot['state'], 'unavailable')
        self.assertNotIn('synthetic-secret', json.dumps(snapshot))
        self.assertNotIn('RuntimeError', json.dumps(snapshot))

    def test_spawn_failure_returns_false_and_stops_checking(self):
        fetcher = Mock()
        spawner = Mock(side_effect=RuntimeError('no threads'))
        cache = self.make(fetcher, spawner=spawner)
        self.assertFalse(cache.request_check())
        snapshot = cache.describe()
        self.assertEqual(snapshot['state'], 'unavailable')
        self.assertIn('could not be started', snapshot['detail'])
        self.assertEqual(snapshot['last_attempt_at'], self.wall[0])
        fetcher.assert_not_called()


if __name__ == '__main__':
    unittest.main()
