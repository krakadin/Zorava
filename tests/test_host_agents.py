"""Focused tests for local host-agent detection, its dashboard API, and the UI.

Host agents (Claude Code, the Codex CLI, and the planned ChatGPT-hosted
controller) are the orchestrators above the managed Qwen/Kimi workers. These
tests keep that separation honest: detection is local and fixed-path only, the
dashboard route is a cached GET with no action, no host credential is read, and
the roadmap host is reported as unavailable instead of being faked.
"""

import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from http.server import ThreadingHTTPServer

from ai_router import hosts
from dashboard.server import DashboardController, make_handler


ROOT = Path(__file__).resolve().parents[1]
CODEX_SKILL = ROOT / '.agents' / 'skills' / 'delegate-workers' / 'SKILL.md'
SECRET_WORDS = ('authorization', 'bearer', 'api_key', 'password', 'private_key', 'server.token')


class FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout=b'', returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class RecordingProbe:
    """Injectable stand-in for hosts.probe_host_version; records every call."""

    def __init__(self, versions=None):
        self.versions = versions or {}
        self.calls = []

    def __call__(self, executable):
        self.calls.append(executable)
        return self.versions.get(str(executable))


class HostDetectionTests(unittest.TestCase):
    def test_snapshot_reports_the_three_hosts_in_a_fixed_order(self):
        snapshot = hosts.detect_hosts(resolver=lambda name: None, probe=RecordingProbe())
        self.assertEqual([host['id'] for host in snapshot['hosts']],
                         ['claude-code', 'codex-cli', 'chatgpt-hosted'])
        self.assertTrue(snapshot['generated_at'])
        self.assertIn('no provider or', snapshot['note'])

    def test_local_hosts_report_executable_version_and_skill_metadata(self):
        resolved = {'claude': Path('/home/krakadin/.local/bin/claude'),
                    'codex': Path('/home/krakadin/.local/bin/codex')}
        probe = RecordingProbe({str(resolved['claude']): '2.1.3',
                                str(resolved['codex']): '0.156.1'})
        snapshot = hosts.detect_hosts(resolver=resolved.get, probe=probe)
        claude, codex = snapshot['hosts'][0], snapshot['hosts'][1]
        self.assertEqual(claude['status'], hosts.STATUS_LOCAL)
        self.assertEqual(claude['kind'], hosts.KIND_LOCAL_CLI)
        self.assertEqual(claude['integration'], 'Local CLI host')
        self.assertEqual(claude['executable'], str(resolved['claude']))
        self.assertEqual(claude['version'], '2.1.3')
        self.assertEqual(claude['skill_path'], str(hosts.CLAUDE_SKILL_PATH))
        self.assertEqual(claude['skill_path'], '/home/krakadin/.claude/skills/delegate-workers/SKILL.md')
        self.assertEqual(codex['status'], hosts.STATUS_LOCAL)
        self.assertEqual(codex['version'], '0.156.1')
        # The Codex skill ships in this repository, so it is really detected.
        self.assertEqual(codex['skill_path'], str(CODEX_SKILL))
        self.assertTrue(codex['skill_present'])
        self.assertEqual(codex['skill_state'], hosts.SKILL_DETECTED)
        self.assertEqual(probe.calls, [resolved['claude'], resolved['codex']])

    def test_absent_host_executable_is_not_installed_and_is_never_probed(self):
        probe = RecordingProbe()
        snapshot = hosts.detect_hosts(resolver=lambda name: None, probe=probe)
        for host in snapshot['hosts'][:2]:
            self.assertEqual(host['status'], hosts.STATUS_NOT_INSTALLED)
            self.assertIsNone(host['executable'])
            self.assertIsNone(host['version'])
        self.assertEqual(probe.calls, [])

    def test_chatgpt_host_is_roadmap_without_an_executable_or_a_probe(self):
        probe = RecordingProbe()
        snapshot = hosts.detect_hosts(resolver=lambda name: Path('/bin/never-used'), probe=probe)
        hosted = snapshot['hosts'][2]
        self.assertEqual(hosted['status'], hosts.STATUS_ROADMAP)
        self.assertEqual(hosted['kind'], hosts.KIND_HOSTED)
        self.assertIsNone(hosted['executable'])
        self.assertIsNone(hosted['executable_name'])
        self.assertIsNone(hosted['version'])
        self.assertIsNone(hosted['skill_path'])
        self.assertFalse(hosted['skill_present'])
        self.assertEqual(hosted['skill_state'], hosts.SKILL_NOT_APPLICABLE)
        self.assertIn('ROADMAP', hosted['detail'])
        self.assertIn('planned', hosted['detail'])
        # Only the two local hosts are ever resolved and probed; the roadmap
        # entry never invents an executable or triggers a probe of its own.
        self.assertEqual(probe.calls, [Path('/bin/never-used'), Path('/bin/never-used')])

    def test_resolver_refuses_names_outside_the_fixed_allowlist(self):
        self.assertEqual(hosts.ALLOWED_EXECUTABLES, frozenset({'claude', 'codex'}))
        with patch.object(hosts.shutil, 'which') as which:
            for name in ('rm', 'bash', 'sh', '/bin/claude', 'claude --version', '', None, 7,
                         ('claude',), Path('/home/krakadin/.local/bin/claude')):
                self.assertIsNone(hosts.resolve_executable(name))
            which.assert_not_called()

    def test_resolver_only_searches_the_fixed_path(self):
        with patch.object(hosts.shutil, 'which',
                          return_value='/home/krakadin/.local/bin/codex') as which:
            self.assertEqual(hosts.resolve_executable('codex'),
                             Path('/home/krakadin/.local/bin/codex'))
        self.assertEqual(which.call_args.args[0], 'codex')
        self.assertEqual(which.call_args.kwargs['path'], hosts.SEARCH_PATH)
        with patch.object(hosts.shutil, 'which', return_value=None):
            self.assertIsNone(hosts.resolve_executable('codex'))
        with patch.object(hosts.shutil, 'which', side_effect=OSError('broken PATH')):
            self.assertIsNone(hosts.resolve_executable('claude'))

    def test_version_probe_runs_only_the_fixed_bounded_command(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return FakeCompleted(b'codex-cli 0.156.1\n')

        version = hosts.probe_host_version('/home/krakadin/.local/bin/codex', runner=runner)
        self.assertEqual(version, '0.156.1')
        command, kwargs = calls[0]
        self.assertEqual(command, ['/home/krakadin/.local/bin/codex', '--version'])
        self.assertEqual(kwargs['timeout'], hosts.PROBE_TIMEOUT_SECONDS)
        self.assertIs(kwargs['stdin'], subprocess.DEVNULL)
        self.assertIs(kwargs['stderr'], subprocess.DEVNULL)
        self.assertFalse(kwargs['check'])
        # Sanitized child environment: the fixed reviewed PATH, no provider keys.
        self.assertEqual(kwargs['env']['PATH'], hosts.SEARCH_PATH)

    def test_version_probe_fails_closed_and_stays_bounded(self):
        self.assertIsNone(hosts.probe_host_version(None))
        self.assertIsNone(hosts.probe_host_version(
            '/bin/claude', runner=lambda *a, **k: FakeCompleted(b'', returncode=1)))
        self.assertIsNone(hosts.probe_host_version(
            '/bin/claude', runner=lambda *a, **k: FakeCompleted(b'no version here')))
        self.assertIsNone(hosts.probe_host_version(
            '/bin/claude', runner=lambda *a, **k: FakeCompleted(None)))

        def boom(*args, **kwargs):
            raise OSError('no such file')

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd='claude', timeout=1)

        self.assertIsNone(hosts.probe_host_version('/bin/claude', runner=boom))
        self.assertIsNone(hosts.probe_host_version('/bin/claude', runner=timeout))
        # Output beyond the bounded prefix is discarded, not surfaced.
        noisy = FakeCompleted(b'x' * (hosts.MAX_OUTPUT_BYTES + 10) + b' 9.9.9')
        self.assertIsNone(hosts.probe_host_version('/bin/claude', runner=lambda *a, **k: noisy))

    def test_skill_state_checks_existence_only(self):
        self.assertEqual(hosts.skill_state(None), hosts.SKILL_NOT_APPLICABLE)
        with tempfile.TemporaryDirectory(prefix='ai-router-host-skill-') as temp:
            skill = Path(temp) / 'SKILL.md'
            self.assertEqual(hosts.skill_state(skill), hosts.SKILL_MISSING)
            skill.write_text('SENTINEL-SKILL-BODY', encoding='utf-8')
            self.assertEqual(hosts.skill_state(skill), hosts.SKILL_DETECTED)

    def test_skill_content_never_reaches_the_snapshot(self):
        with tempfile.TemporaryDirectory(prefix='ai-router-host-skill-') as temp:
            skill = Path(temp) / 'SKILL.md'
            skill.write_text('SENTINEL-SKILL-BODY', encoding='utf-8')
            spec = hosts.HostSpec('custom', 'Custom host', 'Host and controller',
                                  hosts.KIND_LOCAL_CLI, 'Local CLI host', 'claude', skill,
                                  'Fixed detail text.')
            entry = hosts.host_entry(spec, lambda name: None, RecordingProbe())
            self.assertTrue(entry['skill_present'])
            self.assertEqual(entry['skill_path'], str(skill))
            self.assertNotIn('SENTINEL-SKILL-BODY', json.dumps(entry))

    def test_snapshot_is_bounded_plain_metadata_without_secrets(self):
        snapshot = hosts.detect_hosts(
            resolver=lambda name: Path(f'/home/krakadin/.local/bin/{name}'),
            probe=lambda executable: '1.2.3')
        expected = {'id', 'name', 'role', 'kind', 'integration', 'status', 'executable',
                    'executable_name', 'version', 'skill_path', 'skill_present',
                    'skill_state', 'credentials', 'detail'}
        raw = json.dumps(snapshot)
        for host in snapshot['hosts']:
            self.assertEqual(set(host), expected)
            self.assertIn(host['status'], hosts.HOST_STATUSES)
            self.assertEqual(host['credentials'], hosts.CREDENTIAL_NOTE)
            for value in host.values():
                self.assertIsInstance(value, (str, bool, type(None)))
            self.assertLessEqual(len(host['detail']), 400)
        for word in SECRET_WORDS:
            self.assertNotIn(word, raw.lower())


class HostApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-hosts-')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'state'
        self.runtime.mkdir(mode=0o700)
        os.chmod(self.runtime, 0o700)
        (self.runtime / 'locks').mkdir(mode=0o700)
        self.probe = RecordingProbe({'/home/krakadin/.local/bin/claude': '2.1.3',
                                     '/home/krakadin/.local/bin/codex': '0.156.1'})
        resolver = patch.object(hosts, 'resolve_executable',
                                side_effect=lambda name: Path(f'/home/krakadin/.local/bin/{name}'))
        resolver.start()
        self.addCleanup(resolver.stop)
        self.controller = DashboardController(self.runtime, port=8787, host_probe=self.probe)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.controller))
        self.server.daemon_threads = True
        self.controller.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, *, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        status = response.status
        conn.close()
        return status, data.decode('utf-8')

    def csrf_headers(self, origin=None):
        return {'Host': f'127.0.0.1:{self.server.server_port}',
                'Origin': origin or f'http://127.0.0.1:{self.server.server_port}',
                'Sec-Fetch-Site': 'same-origin', 'X-AI-Worker-CSRF': self.controller.csrf_token,
                'Content-Type': 'application/json'}

    def test_hosts_endpoint_reports_hosts_separately_from_workers(self):
        status, body = self.request('GET', '/api/v1/hosts')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        by_id = {host['id']: host for host in payload['hosts']}
        self.assertEqual(list(by_id), ['claude-code', 'codex-cli', 'chatgpt-hosted'])
        self.assertEqual(by_id['claude-code']['status'], 'LOCAL')
        self.assertEqual(by_id['claude-code']['version'], '2.1.3')
        self.assertEqual(by_id['claude-code']['role'], 'Host and controller')
        self.assertEqual(by_id['codex-cli']['status'], 'LOCAL')
        self.assertEqual(by_id['codex-cli']['skill_path'], str(CODEX_SKILL))
        self.assertTrue(by_id['codex-cli']['skill_present'])
        self.assertEqual(by_id['chatgpt-hosted']['status'], 'ROADMAP')
        self.assertIsNone(by_id['chatgpt-hosted']['executable'])
        self.assertIsNone(by_id['chatgpt-hosted']['version'])
        self.assertIn('planned', by_id['chatgpt-hosted']['detail'])
        # No managed worker is mixed into the host view.
        self.assertNotIn('qwen', body)
        self.assertNotIn('kimi', body)

    def test_hosts_get_is_cached_and_probes_each_host_once(self):
        first = self.request('GET', '/api/v1/hosts')
        second = self.request('GET', '/api/v1/hosts')
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(json.loads(first[1]), json.loads(second[1]))
        self.assertEqual(len(self.probe.calls), 2)
        self.assertIsNotNone(self.controller.host_snapshot)

    def test_hosts_get_builds_no_provider_snapshot(self):
        with patch.object(DashboardController, '_load_provider_snapshot') as load:
            status, _ = self.request('GET', '/api/v1/hosts')
        self.assertEqual(status, 200)
        load.assert_not_called()

    def test_host_detection_module_has_no_network_or_credential_client(self):
        source = (ROOT / 'ai_router' / 'hosts.py').read_text(encoding='utf-8')
        for forbidden in ('urllib', 'http.client', 'socket', 'requests', 'server.token',
                          'credentials.json', 'auth.json', '.credentials', 'read_text',
                          'read_bytes', 'os.open'):
            self.assertNotIn(forbidden, source)

    def test_hosts_response_carries_no_secret_material(self):
        status, body = self.request('GET', '/api/v1/hosts')
        self.assertEqual(status, 200)
        lowered = body.lower()
        for word in SECRET_WORDS:
            self.assertNotIn(word, lowered)
        self.assertIn('Not read; owned by the host CLI', body)

    def test_hosts_endpoint_is_read_only(self):
        self.assertEqual(self.request('POST', '/api/v1/hosts', body='{}',
                                      headers=self.csrf_headers())[0], 404)
        self.assertEqual(self.request('POST', '/api/v1/hosts/refresh', body='{}',
                                      headers=self.csrf_headers())[0], 404)
        self.assertEqual(self.request('GET', '/api/v1/hosts/claude-code')[0], 404)
        self.assertEqual(self.request('GET', '/api/v1/hosts?executable=/bin/sh')[0], 200)
        # A query string cannot change what is probed or reported.
        self.assertEqual(len(self.probe.calls), 2)

    def test_hosts_endpoint_rejects_a_foreign_host_header(self):
        status, _ = self.request('GET', '/api/v1/hosts', headers={'Host': 'evil.example'})
        self.assertEqual(status, 400)


class HostUiTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / 'dashboard/index.html').read_text(encoding='utf-8')
        self.js = (ROOT / 'dashboard/static/app.js').read_text(encoding='utf-8')
        self.css = (ROOT / 'dashboard/static/dashboard.css').read_text(encoding='utf-8')

    def test_navigation_exposes_a_hosts_view(self):
        self.assertIn('<a href="/?page=hosts" data-nav="hosts">Hosts</a>', self.html)
        self.assertIn('HOST AGENTS', self.html)

    def test_hosts_page_renders_cards_from_the_local_api(self):
        self.assertIn('async function hostsPage()', self.js)
        self.assertIn("title('Host agents'", self.js)
        self.assertIn("get('/api/v1/hosts')", self.js)
        self.assertIn("else if(page==='hosts') await hostsPage();", self.js)
        self.assertIn("cards.id='host-cards'", self.js)
        self.assertIn('function hostCard(host)', self.js)
        self.assertIn("addKV(dl,'Delegation skill'", self.js)
        self.assertIn("addKV(dl,'Host credentials',host.credentials)", self.js)
        self.assertIn('textContent', self.js)
        self.assertNotIn('innerHTML', self.js)

    def test_overview_keeps_host_agents_separate_from_worker_cards(self):
        self.assertIn("panel('HOST AGENTS')", self.js)
        self.assertIn("root.append(hostSummary(await get('/api/v1/hosts')));", self.js)
        # The managed provider worker cards are unchanged.
        self.assertIn("['claude','qwen','kimi'].forEach(name => cards.append(providerCard(data.providers[name])));",
                      self.js)
        self.assertIn("dataset.action='test'", self.js)

    def test_host_section_offers_no_action_and_no_post(self):
        self.assertNotIn("post('/api/v1/hosts", self.js)
        self.assertNotIn("dataset.action='test-host", self.js)
        self.assertNotIn("dataset.action='refresh-host", self.js)
        self.assertNotIn('fetch(\'/api/v1/hosts\',{method', self.js)

    def test_roadmap_host_is_shown_as_planned_not_faked(self):
        self.assertIn("host.status === 'ROADMAP'", self.js)
        self.assertIn('Planned hosted controller', self.js)
        self.assertIn('None · hosted integration is planned', self.js)
        self.assertIn('host-roadmap', self.js)
        self.assertIn('.cards .card.host-card', self.css)
        self.assertIn('.cards .card.host-card.host-roadmap', self.css)
        self.assertIn('.status.status-roadmap', self.css)
        self.assertIn('.status.status-not_installed', self.css)


if __name__ == '__main__':
    unittest.main()
