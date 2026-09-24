"""Offline tests for read-only provider CLI version metadata.

Nothing here performs a live request, and no provider credential is used.
"""

import http.client
import inspect
import json
import os
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from ai_router import updates


QWEN_SOURCE = 'https://registry.npmjs.org/@qwen-code/qwen-code/latest'
KIMI_SOURCE = 'https://code.kimi.com/kimi-code/latest'


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class FakeResponse:
    def __init__(self, raw=b'', *, status=200, url=QWEN_SOURCE, headers=None):
        self.raw = raw
        self.status = status
        self._url = url
        self.headers = headers or {}
        self.reads = []

    def getcode(self):
        return self.status

    def geturl(self):
        return self._url

    def read(self, size=-1):
        self.reads.append(size)
        return self.raw if size is None or size < 0 else self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class RecordingOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.response


class ExplodingOpener:
    def open(self, request, timeout=None):
        raise AssertionError('A refused source must never be requested.')


class BlockingFetcher:
    """Fetcher that stays inside the check until the test releases it."""

    def __init__(self, result=('9.9.9', None)):
        self.result = result
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, worker):
        self.calls.append(worker)
        self.entered.set()
        self.release.wait(10)
        return self.result


class ScriptedFetcher:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, worker):
        self.calls.append(worker)
        value = self.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class VersionParsingTests(unittest.TestCase):
    def test_parse_version_accepts_only_stable_releases(self):
        self.assertEqual(updates.parse_version('1.2.3'), (1, 2, 3))
        self.assertEqual(updates.parse_version('0.24.4'), (0, 24, 4))
        # Strict contract: no surrounding whitespace, padding, prefix, or suffix.
        for value in ('1.2', '1.2.3.4', 'v1.2.3', '1.2.3-rc.1', '1.2.3+build.5',
                      '01.2.3', '1.02.3', ' 1.2.3', '1.2.3 ', '1.2.3\n',
                      '\t0.24.4', 'latest', '', None, 123, ['1.2.3']):
            with self.subTest(value=value):
                self.assertIsNone(updates.parse_version(value))
        self.assertEqual(updates.format_version('2.0.2'), '2.0.2')
        self.assertIsNone(updates.format_version(' 2.0.2 '))
        self.assertIsNone(updates.format_version('2.0.2-beta'))

    def test_comparison_is_numeric_not_lexical(self):
        self.assertEqual(updates.compare_versions('1.2.9', '1.2.10'), -1)
        self.assertEqual(updates.compare_versions('1.10.0', '1.9.9'), 1)
        self.assertEqual(updates.compare_versions('2.0.2', '2.0.2'), 0)
        self.assertIsNone(updates.compare_versions('1.2.3', '1.2.3-rc.1'))
        self.assertIsNone(updates.compare_versions(None, '1.2.3'))

    def test_update_state_classification(self):
        cases = (
            (('1.2.3', '1.2.3'), updates.STATE_UP_TO_DATE),
            (('1.2.3', '1.2.4'), updates.STATE_UPDATE_AVAILABLE),
            (('1.2.9', '1.2.10'), updates.STATE_UPDATE_AVAILABLE),
            (('1.2.4', '1.2.3'), updates.STATE_STALE),
            (('1.2.3', None), updates.STATE_UNKNOWN),
            ((None, '1.2.3'), updates.STATE_UNKNOWN),
            (('1.2.3-rc.1', '1.2.3'), updates.STATE_UNKNOWN),
            (('nightly', '1.2.3'), updates.STATE_UNKNOWN),
        )
        for (installed, latest), expected in cases:
            with self.subTest(installed=installed, latest=latest):
                self.assertEqual(updates.classify_update_state(installed, latest), expected)
        # An in-flight check always reports checking.
        self.assertEqual(updates.classify_update_state('1.2.3', '1.2.4', checking=True),
                         updates.STATE_CHECKING)
        self.assertEqual(updates.classify_update_state(None, None, checking=True),
                         updates.STATE_CHECKING)
        self.assertEqual(len(updates.UPDATE_STATES), 5)


class MetadataParsingTests(unittest.TestCase):
    def test_npm_document_parser(self):
        body = json.dumps({'name': '@qwen-code/qwen-code', 'version': '0.24.4'}).encode()
        self.assertEqual(updates.parse_npm_metadata(body), '0.24.4')
        for bad in (b'', b'not json', b'[]', b'"0.24.4"', b'{}',
                    json.dumps({'version': '1.0.0-rc.1'}).encode(),
                    json.dumps({'version': 123}).encode(),
                    json.dumps({'dist-tags': {'latest': '1.2.3'}}).encode(),
                    b'\xff\xfe{"version":"1.2.3"}'):
            with self.subTest(body=bad):
                self.assertIsNone(updates.parse_npm_metadata(bad))
        self.assertIsNone(updates.parse_npm_metadata('1.2.3'))

    def test_plain_semver_parser(self):
        self.assertEqual(updates.parse_plain_semver(b'2.0.2'), '2.0.2')
        self.assertEqual(updates.parse_plain_semver(b'  2.0.2  \n'), '2.0.2')
        for bad in (b'', b'\n\n', b'2.0.2\n2.0.3\n', b'v2.0.2', b'2.0.2-rc.1',
                    b'<html>2.0.2</html>', b'\xff\xfe', json.dumps({'version': '2.0.2'}).encode()):
            with self.subTest(body=bad):
                self.assertIsNone(updates.parse_plain_semver(bad))

    def test_source_normalization_is_explicit_and_strict_elsewhere(self):
        # Only the fixed plain-text endpoint trims its own line padding. JSON
        # metadata and locally installed versions meet the strict contract.
        self.assertEqual(updates.parse_plain_semver(b'  2.0.2  \n'), '2.0.2')
        self.assertIsNone(updates.parse_plain_semver(b'  2.0.2  2.0.3  \n'))
        self.assertIsNone(
            updates.parse_npm_metadata(json.dumps({'version': ' 0.24.4 '}).encode()))
        self.assertIsNone(updates.format_version(' 0.24.4 '))


class SourcePolicyTests(unittest.TestCase):
    def test_sources_are_fixed_https_and_allowlisted(self):
        self.assertEqual(updates.SOURCES, {'qwen': QWEN_SOURCE, 'kimi': KIMI_SOURCE})
        self.assertEqual(updates.ALLOWED_HOSTS, frozenset({'registry.npmjs.org', 'code.kimi.com'}))
        for worker, url in updates.SOURCES.items():
            with self.subTest(worker=worker):
                self.assertTrue(url.startswith('https://'))
                self.assertEqual(updates._source_url(worker), url)

    def test_request_bounds_are_documented(self):
        self.assertEqual(updates.REQUEST_TIMEOUT_SECONDS, 6.0)
        self.assertEqual(updates.MAX_RESPONSE_BYTES, 8192)
        self.assertLessEqual(updates.REQUEST_TIMEOUT_SECONDS, 10.0)

    def test_unknown_worker_or_tampered_source_is_refused_without_a_request(self):
        self.assertEqual(updates.fetch_latest_version('claude', opener=ExplodingOpener()),
                         (None, updates.ERROR_SOURCE_NOT_ALLOWED))
        self.assertEqual(updates.fetch_latest_version(None, opener=ExplodingOpener()),
                         (None, updates.ERROR_SOURCE_NOT_ALLOWED))
        for url in ('https://attacker.example/latest', 'http://registry.npmjs.org/latest',
                    'file:///etc/passwd', 'https://registry.npmjs.org.evil.example/latest'):
            with self.subTest(url=url), patch.dict(updates.SOURCES, {'qwen': url}):
                self.assertEqual(updates.fetch_latest_version('qwen', opener=ExplodingOpener()),
                                 (None, updates.ERROR_SOURCE_NOT_ALLOWED))

    def test_no_caller_supplied_url_and_no_install_action_exists(self):
        parameters = inspect.signature(updates.fetch_latest_version).parameters
        self.assertEqual(list(parameters)[0], 'worker')
        self.assertNotIn('url', parameters)
        self.assertNotIn('source', parameters)
        names = [name.lower() for name in dir(updates)]
        for forbidden in ('install', 'upgrade', 'uninstall', 'download', 'apply_update'):
            self.assertFalse(any(forbidden in name for name in names), forbidden)
        # A version check cannot run a package manager or any other process.
        self.assertNotIn('subprocess', names)
        self.assertNotIn('os', names)


class FetchTests(unittest.TestCase):
    def test_fetch_reads_the_pinned_source_without_proxies_or_credentials(self):
        body = json.dumps({'version': '0.24.4'}).encode()
        captured = {}

        def fake_build_opener(*handlers):
            captured['handlers'] = handlers
            return RecordingOpener(FakeResponse(body, url=QWEN_SOURCE))

        environment = {'HTTPS_PROXY': 'http://proxy.example:3128', 'https_proxy': 'http://proxy.example:3128',
                       'HTTP_PROXY': 'http://proxy.example:3128', 'ALL_PROXY': 'socks5://proxy.example:1080'}
        with patch.dict(os.environ, environment), \
             patch('urllib.request.getproxies', side_effect=AssertionError('environment proxies must be ignored')) as getproxies, \
             patch('urllib.request.build_opener', side_effect=fake_build_opener):
            self.assertEqual(updates.fetch_latest_version('qwen'), ('0.24.4', None))
        getproxies.assert_not_called()
        handlers = captured['handlers']
        self.assertEqual([type(handler) for handler in handlers],
                         [urllib.request.ProxyHandler, updates.NoRedirect, urllib.request.HTTPSHandler])
        self.assertEqual(handlers[0].proxies, {})
        self.assertFalse(any('auth' in type(handler).__name__.lower() for handler in handlers))

    def test_redirects_are_refused_not_followed(self):
        request = urllib.request.Request(QWEN_SOURCE)
        with self.assertRaises(urllib.error.HTTPError):
            updates.NoRedirect().redirect_request(request, None, 302, 'Found', {},
                                                  'https://attacker.example/payload')
        opener = RecordingOpener(error=urllib.error.HTTPError(QWEN_SOURCE, 302, 'Found', {}, None))
        self.assertEqual(updates.fetch_latest_version('qwen', opener=opener), (None, 'HTTP_302'))

    def test_request_is_a_bounded_get_with_no_credential_headers(self):
        opener = RecordingOpener(FakeResponse(b'2.0.2\n', url=KIMI_SOURCE))
        self.assertEqual(updates.fetch_latest_version('kimi', opener=opener), ('2.0.2', None))
        request = opener.requests[0]
        self.assertEqual(request.full_url, KIMI_SOURCE)
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(opener.timeouts, [updates.REQUEST_TIMEOUT_SECONDS])
        forbidden = {'authorization', 'cookie', 'proxy-authorization', 'x-api-key', 'x-auth-token'}
        for headers in (request.headers, request.unredirected_hdrs):
            names = {name.lower() for name in headers}
            self.assertFalse(names & forbidden)
            for value in headers.values():
                text = str(value)
                self.assertNotIn('Bearer', text)
                self.assertNotIn('sk-', text)
        self.assertEqual(opener.response.reads, [updates.MAX_RESPONSE_BYTES + 1])

    def test_fetch_bounds_status_size_and_metadata(self):
        cases = (
            (FakeResponse(b'', status=404, url=QWEN_SOURCE), 'HTTP_404'),
            (FakeResponse(b'', status=500, url=QWEN_SOURCE), 'HTTP_500'),
            (FakeResponse(b'x' * (updates.MAX_RESPONSE_BYTES + 1), url=QWEN_SOURCE),
             updates.ERROR_RESPONSE_TOO_LARGE),
            (FakeResponse(b'{}', url=QWEN_SOURCE,
                          headers={'Content-Length': str(updates.MAX_RESPONSE_BYTES + 1)}),
             updates.ERROR_RESPONSE_TOO_LARGE),
            (FakeResponse(b'not json', url=QWEN_SOURCE), updates.ERROR_INVALID_METADATA),
            (FakeResponse(json.dumps({'version': '1.0.0-rc.1'}).encode(), url=QWEN_SOURCE),
             updates.ERROR_INVALID_METADATA),
            (FakeResponse(b'{}', url='https://attacker.example/latest'),
             updates.ERROR_SOURCE_NOT_ALLOWED),
            (FakeResponse(b'<html><body>2.0.2</body></html>', url=QWEN_SOURCE),
             updates.ERROR_INVALID_METADATA),
        )
        for response, expected in cases:
            with self.subTest(expected=expected, url=response.geturl()):
                opener = RecordingOpener(response)
                self.assertEqual(updates.fetch_latest_version('qwen', opener=opener), (None, expected))

    def test_network_failures_become_fixed_error_codes(self):
        failures = (urllib.error.URLError('name resolution failed'), TimeoutError(),
                    OSError('network unreachable'), http.client.BadStatusLine('junk'),
                    ValueError('bad url'))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(updates.fetch_latest_version('kimi', opener=RecordingOpener(error=failure)),
                                 (None, updates.ERROR_NETWORK))
        self.assertEqual(updates.fetch_latest_version(
            'kimi', opener=RecordingOpener(error=urllib.error.HTTPError(KIMI_SOURCE, 503, 'Busy', {}, None))),
            (None, 'HTTP_503'))

    def test_error_codes_are_bounded_tokens(self):
        self.assertIsNone(updates._safe_error_code(None))
        self.assertEqual(updates._safe_error_code('NETWORK_ERROR'), 'NETWORK_ERROR')
        self.assertEqual(updates._safe_error_code('HTTP_404'), 'HTTP_404')
        for value in ('secret=abc', 'lower case', 'A' * 64, 42, ['x']):
            with self.subTest(value=value):
                self.assertEqual(updates._safe_error_code(value), updates.ERROR_CHECK_FAILED)


class RequestedWorkersTests(unittest.TestCase):
    def test_default_is_both_coders_and_validation_is_strict(self):
        self.assertEqual(updates.requested_workers(None), ['qwen', 'kimi'])
        self.assertEqual(updates.requested_workers(['kimi']), ['kimi'])
        self.assertEqual(updates.requested_workers(('qwen',)), ['qwen'])
        for bad in ('qwen', [], ['claude'], ['qwen', 'claude'], ['qwen', 'qwen'],
                    ['qwen', 'kimi', 'qwen'], [['qwen']], [None], {'workers': ['qwen']}, 5):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    updates.requested_workers(bad)

    def test_status_rejects_unknown_workers(self):
        with self.assertRaises(ValueError):
            updates.UpdateRegistry().status('claude')


class RegistryTests(unittest.TestCase):
    def test_check_is_explicit_cached_and_bounded_to_one_thread_per_worker(self):
        fetcher = BlockingFetcher()
        registry = updates.UpdateRegistry(fetcher=fetcher)
        self.addCleanup(registry.shutdown, 5)
        self.assertEqual(registry.status('qwen', '1.2.3').update_state, updates.STATE_UNKNOWN)
        queued = registry.request_check(['qwen'])
        self.assertEqual(queued, {'requested': ['qwen'], 'already_checking': [], 'deferred': []})
        self.assertTrue(fetcher.entered.wait(5))
        checking = registry.status('qwen', '1.2.3')
        self.assertTrue(checking.checking)
        self.assertEqual(checking.update_state, updates.STATE_CHECKING)
        self.assertIsNone(checking.latest_version)
        # Reading status/snapshot never performs I/O.
        registry.snapshot({'qwen': '1.2.3', 'kimi': '2.3.4'})
        self.assertEqual(fetcher.calls, ['qwen'])
        # A repeated click neither queues a second thread nor a second request.
        again = registry.request_check(['qwen'])
        self.assertEqual(again['requested'], [])
        self.assertEqual(again['already_checking'], ['qwen'])
        self.assertEqual(fetcher.calls, ['qwen'])
        fetcher.release.set()
        registry.shutdown(timeout=5)
        done = registry.status('qwen', '1.2.3')
        self.assertFalse(done.checking)
        self.assertEqual(done.latest_version, '9.9.9')
        self.assertEqual(done.update_state, updates.STATE_UPDATE_AVAILABLE)
        self.assertEqual(done.installed_version, '1.2.3')
        self.assertIsNone(done.error)
        self.assertIsNotNone(done.last_check_at)
        self.assertEqual(done.last_success_at, done.last_check_at)
        self.assertEqual(done.source, QWEN_SOURCE)
        self.assertEqual(fetcher.calls, ['qwen'])

    def test_thread_bound_defers_extra_requests(self):
        fetcher = BlockingFetcher()
        registry = updates.UpdateRegistry(fetcher=fetcher, max_checks=1)
        self.addCleanup(registry.shutdown, 5)
        result = registry.request_check()
        self.assertEqual(result['requested'], ['qwen'])
        self.assertEqual(result['deferred'], ['kimi'])
        self.assertTrue(fetcher.entered.wait(5))
        self.assertEqual(fetcher.calls, ['qwen'])
        fetcher.release.set()
        registry.shutdown(timeout=5)
        self.assertEqual(registry.status('kimi', '2.3.4').update_state, updates.STATE_UNKNOWN)

    def test_failures_are_recorded_and_keep_the_last_known_version(self):
        fetcher = ScriptedFetcher([('2.0.0', None), (None, 'NETWORK_ERROR'), RuntimeError('boom'),
                                   (None, 'response text with a secret'), (None, None)])
        registry = updates.UpdateRegistry(fetcher=fetcher)
        self.addCleanup(registry.shutdown, 5)
        expected_errors = (None, 'NETWORK_ERROR', updates.ERROR_CHECK_FAILED,
                           updates.ERROR_CHECK_FAILED, updates.ERROR_CHECK_FAILED)
        status = None
        for expected in expected_errors:
            registry.request_check(['kimi'])
            self.assertTrue(wait_for(lambda: not registry.status('kimi', '2.3.4').checking))
            status = registry.status('kimi', '2.3.4')
            self.assertEqual(status.error, expected)
            # The last published version survives a failed refresh.
            self.assertEqual(status.latest_version, '2.0.0')
        self.assertEqual(fetcher.calls, ['kimi'] * len(expected_errors))
        self.assertEqual(status.update_state, updates.STATE_STALE)
        self.assertIsNotNone(status.last_success_at)
        self.assertIsNotNone(status.last_check_at)

    def test_unparsable_installed_version_stays_unknown(self):
        registry = updates.UpdateRegistry(fetcher=ScriptedFetcher([('1.2.3', None)]))
        self.addCleanup(registry.shutdown, 5)
        registry.request_check(['qwen'])
        self.assertTrue(wait_for(lambda: registry.status('qwen', '1.2.3').latest_version))
        self.assertEqual(registry.status('qwen', '1.2.3').update_state, updates.STATE_UP_TO_DATE)
        ahead = registry.status('qwen', '1.2.3-rc.1')
        self.assertIsNone(ahead.installed_version)
        self.assertEqual(ahead.update_state, updates.STATE_UNKNOWN)
        self.assertEqual(ahead.latest_version, '1.2.3')

    def test_snapshot_payload_shape_is_serializable(self):
        registry = updates.UpdateRegistry(fetcher=ScriptedFetcher([('1.2.3', None)]))
        self.addCleanup(registry.shutdown, 5)
        registry.request_check(['qwen'])
        self.assertTrue(wait_for(lambda: registry.status('qwen', '1.2.3').latest_version))
        snapshot = registry.snapshot({'qwen': '1.2.3', 'kimi': '2.3.4'})
        self.assertEqual(sorted(snapshot), ['kimi', 'qwen'])
        self.assertEqual(sorted(snapshot['qwen']),
                         sorted(['installed_version', 'latest_version', 'update_state', 'update_checking',
                                 'update_source', 'last_update_check_at', 'last_update_success_at',
                                 'last_update_error']))
        self.assertEqual(snapshot['qwen']['update_state'], updates.STATE_UP_TO_DATE)
        self.assertEqual(snapshot['qwen']['update_source'], QWEN_SOURCE)
        self.assertEqual(snapshot['kimi']['update_state'], updates.STATE_UNKNOWN)
        self.assertEqual(snapshot['kimi']['installed_version'], '2.3.4')
        json.dumps(snapshot)

    def test_shutdown_refuses_new_checks(self):
        registry = updates.UpdateRegistry(fetcher=BlockingFetcher())
        registry.shutdown(timeout=1)
        with self.assertRaises(RuntimeError):
            registry.request_check(['qwen'])

    def test_status_uses_only_the_fixed_source_field(self):
        registry = updates.UpdateRegistry(fetcher=ScriptedFetcher([('1.2.3', None)]))
        status = registry.status('kimi', '2.3.4')
        self.assertEqual(status.source, KIMI_SOURCE)
        self.assertEqual(SimpleNamespace(**status.as_dict()).update_source, KIMI_SOURCE)


if __name__ == '__main__':
    unittest.main()
