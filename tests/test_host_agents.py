"""Focused tests for local host-agent detection, its dashboard API, and the UI.

Host agents (Claude Code and the Codex CLI) are the orchestrators above the
managed Qwen/Kimi workers. These tests keep that separation honest: detection
is local and fixed-path only, the dashboard route is a cached GET with no
action, no host credential is read, and the host agents appear as cards beside
(but never as) the managed workers. A hosted ChatGPT controller remains
documentation-roadmap material only and has no runtime entry anywhere.
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
    def setUp(self):
        # Mock-only tests stay isolated from this machine's real NVM
        # installation: the discovery fallback root is an empty temporary tree.
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-nvm-empty-')
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(hosts, 'NVM_ROOT', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_snapshot_reports_the_two_hosts_in_a_fixed_order(self):
        snapshot = hosts.detect_hosts(resolver=lambda name: None, probe=RecordingProbe())
        self.assertEqual([host['id'] for host in snapshot['hosts']],
                         ['claude-code', 'codex-cli'])
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
        for host in snapshot['hosts']:
            self.assertEqual(host['status'], hosts.STATUS_NOT_INSTALLED)
            self.assertIsNone(host['executable'])
            self.assertIsNone(host['version'])
        self.assertEqual(probe.calls, [])

    def test_snapshot_has_no_roadmap_or_hosted_entries(self):
        probe = RecordingProbe()
        snapshot = hosts.detect_hosts(resolver=lambda name: None, probe=probe)
        # Exactly the two real local host integrations exist; a future hosted
        # ChatGPT controller stays in documentation only and invents no entry,
        # executable, or status of its own.
        self.assertEqual(len(snapshot['hosts']), 2)
        for host in snapshot['hosts']:
            self.assertEqual(host['kind'], hosts.KIND_LOCAL_CLI)
            self.assertIn(host['status'], (hosts.STATUS_LOCAL, hosts.STATUS_NOT_INSTALLED))
            self.assertTrue(host['executable_name'])
        raw = json.dumps(snapshot).lower()
        self.assertNotIn('chatgpt-hosted', raw)
        self.assertNotIn('roadmap', raw)
        self.assertNotIn('hosted', raw)
        self.assertEqual(probe.calls, [])

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


class NvmHostDiscoveryTests(unittest.TestCase):
    """NVM fallback discovery/probing against temporary installation trees.

    These tests build realistic NVM layouts -- vMAJOR.MINOR.PATCH install
    directories with bin/<name> npm symlinks into the install's own lib tree,
    real file permissions, and fake version runners -- under a patched
    ``hosts.NVM_ROOT`` so the machine's real NVM installation is never used.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-router-nvm-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'versions' / 'node'
        self.root.mkdir(parents=True)
        patcher = patch.object(hosts, 'NVM_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_install_at(self, base, version, names=('codex',), mode=0o755):
        """Realistic npm layout: bin/<name> symlinks into the install lib tree."""
        install = Path(base) / version
        target_dir = install / 'lib' / 'node_modules' / '@fake' / 'hostcli' / 'bin'
        target_dir.mkdir(parents=True)
        bin_dir = install / 'bin'
        bin_dir.mkdir(exist_ok=True)
        links = {}
        for name in names:
            target = target_dir / f'{name}.js'
            target.write_text('#!/usr/bin/env node\nconsole.log("fake")\n', encoding='utf-8')
            os.chmod(target, mode)
            link = bin_dir / name
            link.symlink_to(os.path.relpath(target, bin_dir))
            links[name] = link
        return install, links

    def make_install(self, version, names=('codex',), mode=0o755):
        return self.make_install_at(self.root, version, names, mode)

    def resolve_without_fixed_path(self, name):
        with patch.object(hosts.shutil, 'which', return_value=None):
            return hosts.resolve_executable(name)

    def test_fixed_path_precedence_over_nvm_installations(self):
        self.make_install('v22.23.1')
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            with patch.object(hosts.shutil, 'which', return_value='/usr/local/bin/codex'):
                self.assertEqual(hosts.resolve_executable('codex'), Path('/usr/local/bin/codex'))

    def test_discovery_without_nvm_on_path_uses_npm_symlink(self):
        _install, links = self.make_install('v22.23.1')
        with patch.dict(os.environ, {'PATH': '/usr/local/bin:/usr/bin:/bin'}):
            # The reported executable is the normal npm bin symlink inside the
            # installation; no specific Node version is hardcoded anywhere.
            self.assertEqual(self.resolve_without_fixed_path('codex'), links['codex'])
            self.assertEqual(os.readlink(links['codex']),
                             os.path.join('..', 'lib', 'node_modules', '@fake',
                                          'hostcli', 'bin', 'codex.js'))
            self.assertIsNone(self.resolve_without_fixed_path('claude'))

    def test_highest_numeric_node_version_wins(self):
        self.make_install('v22.9.0')
        _newer_install, newer = self.make_install('v22.10.0')
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            # Numeric, not lexical: v22.10.0 beats v22.9.0.
            self.assertEqual(self.resolve_without_fixed_path('codex'), newer['codex'])

    def test_active_validated_nvm_bin_is_preferred(self):
        _older_install, older = self.make_install('v22.9.0')
        self.make_install('v22.10.0')
        active = f'{older["codex"].parent}{os.pathsep}/usr/bin:/bin'
        with patch.dict(os.environ, {'PATH': active}):
            # The active NVM bin on PATH wins over a higher installed version.
            self.assertEqual(self.resolve_without_fixed_path('codex'), older['codex'])

    def test_missing_nonexecutable_and_misnamed_installs_are_skipped(self):
        self.make_install('v22.11.0', mode=0o644)          # not executable
        empty = self.root / 'v22.12.0'
        (empty / 'bin').mkdir(parents=True)                 # no codex at all
        (self.root / 'latest').mkdir()                      # not vMAJOR.MINOR.PATCH
        (self.root / 'v22').mkdir()
        (self.root / 'not-a-version').mkdir()
        _usable_install, usable = self.make_install('v22.8.0')
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            self.assertEqual(self.resolve_without_fixed_path('codex'), usable['codex'])

    def test_arbitrary_inherited_path_directories_are_never_searched(self):
        elsewhere = Path(self.temp.name) / 'elsewhere'
        elsewhere.mkdir()
        fake = elsewhere / 'codex'
        fake.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        os.chmod(fake, 0o755)
        with patch.dict(os.environ, {'PATH': f'{elsewhere}:/usr/bin:/bin'}):
            self.assertIsNone(self.resolve_without_fixed_path('codex'))
            self.assertIsNone(self.resolve_without_fixed_path('claude'))

    def test_project_symlink_alias_into_trusted_install_never_wins(self):
        _install, links = self.make_install('v22.23.1')
        project = Path(self.temp.name) / 'project'
        project.mkdir()
        # A project-controlled symlink whose target is the trusted install.
        (project / 'v22.23.1').symlink_to(self.root / 'v22.23.1', target_is_directory=True)
        alias_bin = project / 'v22.23.1' / 'bin'
        with patch.dict(os.environ, {'PATH': f'{alias_bin}:/usr/bin:/bin'}):
            # The alias never gains active-version priority; the fallback
            # reports the actual trusted installation path instead.
            self.assertEqual(self.resolve_without_fixed_path('codex'), links['codex'])

    def test_dotdot_traversal_alias_into_trusted_install_never_wins(self):
        _install, links = self.make_install('v22.23.1')
        # Absolute, and resolves inside the trusted root, but the offered
        # path itself is not a direct lexical child of it.
        traversal_bin = self.root / '..' / 'node' / 'v22.23.1' / 'bin'
        with patch.dict(os.environ, {'PATH': f'{traversal_bin}:/usr/bin'}):
            self.assertEqual(self.resolve_without_fixed_path('codex'), links['codex'])

    def test_relative_alias_into_trusted_install_never_wins(self):
        _install, links = self.make_install('v22.23.1')
        # A relative PATH entry that would resolve to the genuine NVM bin from
        # the current working directory still never qualifies.
        relative_bin = os.path.relpath(links['codex'].parent, Path.cwd())
        self.assertFalse(Path(relative_bin).is_absolute())
        with patch.dict(os.environ, {'PATH': f'{relative_bin}:/usr/bin'}):
            self.assertEqual(self.resolve_without_fixed_path('codex'), links['codex'])
        # A relative installation path is never a valid NVM install either.
        self.assertIsNone(hosts._valid_nvm_install(Path('v22.23.1'), self.root))

    def test_installation_directory_symlink_escape_is_rejected(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        self.make_install_at(outside, 'v22.23.1')
        (self.root / 'v22.23.1').symlink_to(outside / 'v22.23.1', target_is_directory=True)
        escaped_bin = self.root / 'v22.23.1' / 'bin'
        with patch.dict(os.environ, {'PATH': f'{escaped_bin}:/usr/bin:/bin'}):
            # Rejected both as an active PATH bin and as a discovered install.
            self.assertIsNone(self.resolve_without_fixed_path('codex'))

    def test_executable_symlink_escaping_the_install_is_rejected(self):
        bin_dir = self.root / 'v22.23.1' / 'bin'
        bin_dir.mkdir(parents=True)
        evil = Path(self.temp.name) / 'evil-codex'
        evil.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        os.chmod(evil, 0o755)
        (bin_dir / 'codex').symlink_to(evil)
        with patch.dict(os.environ, {'PATH': f'{bin_dir}:/usr/bin:/bin'}):
            self.assertIsNone(self.resolve_without_fixed_path('codex'))

    def test_bin_directory_symlink_escaping_the_install_is_rejected(self):
        install = self.root / 'v22.23.1'
        install.mkdir()
        outside_bin = Path(self.temp.name) / 'outside-bin'
        outside_bin.mkdir()
        target = outside_bin / 'codex'
        target.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        os.chmod(target, 0o755)
        (install / 'bin').symlink_to(outside_bin, target_is_directory=True)
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            self.assertIsNone(self.resolve_without_fixed_path('codex'))

    def test_nvm_probe_prepends_only_the_validated_nvm_bin(self):
        _install, links = self.make_install('v22.23.1')
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return FakeCompleted(b'codex-cli 0.157.1\n')

        version = hosts.probe_host_version(links['codex'], runner=runner)
        self.assertEqual(version, '0.157.1')
        command, kwargs = calls[0]
        self.assertEqual(command, [str(links['codex']), '--version'])
        # Only the validated NVM bin precedes the unchanged fixed base PATH, so
        # #!/usr/bin/env node resolves to this installation's own runtime.
        self.assertEqual(kwargs['env']['PATH'],
                         f"{links['codex'].parent}{os.pathsep}{hosts.SEARCH_PATH}")
        self.assertEqual(kwargs['timeout'], hosts.PROBE_TIMEOUT_SECONDS)

    def test_probe_keeps_the_base_path_for_an_untrusted_nvm_executable(self):
        bin_dir = self.root / 'v22.23.1' / 'bin'
        bin_dir.mkdir(parents=True)
        evil = Path(self.temp.name) / 'evil-codex'
        evil.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        os.chmod(evil, 0o755)
        link = bin_dir / 'codex'
        link.symlink_to(evil)
        calls = []

        def runner(command, **kwargs):
            calls.append(kwargs)
            return FakeCompleted(b'codex-cli 9.9.9\n')

        version = hosts.probe_host_version(link, runner=runner)
        self.assertEqual(version, '9.9.9')
        # The escaping symlink never earns an NVM PATH entry.
        self.assertEqual(calls[0]['env']['PATH'], hosts.SEARCH_PATH)


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
        self.assertEqual(list(by_id), ['claude-code', 'codex-cli'])
        self.assertEqual(by_id['claude-code']['status'], 'LOCAL')
        self.assertEqual(by_id['claude-code']['version'], '2.1.3')
        self.assertEqual(by_id['claude-code']['role'], 'Host and controller')
        self.assertEqual(by_id['codex-cli']['status'], 'LOCAL')
        self.assertEqual(by_id['codex-cli']['skill_path'], str(CODEX_SKILL))
        self.assertTrue(by_id['codex-cli']['skill_present'])
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

    @staticmethod
    def safe_provider_snapshot():
        """Offline provider fixture; real adapter/CLI loading is never run."""
        return {'claude': {'worker': 'claude', 'status': 'CONFIGURED',
                           'requested_model': 'claude-local', 'provider': 'Anthropic',
                           'endpoint_host': 'api.anthropic.com',
                           'authentication': 'Claude Code-managed Max OAuth; not tested by ai-worker',
                           'routing': 'DIRECT'},
                'qwen': {'worker': 'qwen', 'status': 'CONFIGURED', 'requested_model': 'qwen-test',
                         'provider': 'Token Plan', 'endpoint_host': 'token-plan.example',
                         'endpoint_url': 'https://token-plan.example/v1',
                         'authentication': 'credential present', 'version': '1.2.3'},
                'kimi': {'worker': 'kimi', 'status': 'CONFIGURED', 'requested_model': 'kimi-test',
                         'provider': 'Kimi', 'endpoint_host': 'api.kimi.example',
                         'authentication': 'OAuth', 'version': '2.3.4'}}

    def test_providers_and_status_include_codex_cached_host_metadata(self):
        with patch.object(DashboardController, '_load_provider_snapshot',
                          return_value=self.safe_provider_snapshot()):
            for endpoint in ('/api/v1/providers', '/api/v1/status'):
                status, body = self.request('GET', endpoint)
                self.assertEqual(status, 200)
                providers = json.loads(body)['providers']
                self.assertEqual(list(providers), ['claude', 'codex', 'qwen', 'kimi'])
                codex = providers['codex']
                self.assertEqual(codex['worker'], 'codex')
                self.assertEqual(codex['name'], 'Codex CLI')
                self.assertTrue(codex['host'])
                self.assertEqual(codex['role'], 'Host and controller')
                self.assertEqual(codex['status'], 'LOCAL')
                self.assertEqual(codex['provider'], 'OpenAI')
                self.assertEqual(codex['executable'], '/home/krakadin/.local/bin/codex')
                self.assertEqual(codex['version'], '0.156.1')
                self.assertEqual(codex['skill_path'], str(CODEX_SKILL))
                self.assertEqual(codex['skill_state'], hosts.SKILL_DETECTED)
                self.assertIn('Codex CLI', codex['authentication'])
                self.assertIn('Codex CLI', codex['routing'])
                # No invented model, login, test, usage, quota, capacity, or
                # upgrade values, and no worker-only suffix on the host role.
                self.assertIsNone(codex['requested_model'])
                self.assertNotIn('separate worktree', codex['role'])
                for absent in ('usage', 'concurrency', 'update_state', 'last_test_at',
                               'last_test_status', 'queued_jobs', 'running_jobs'):
                    self.assertNotIn(absent, codex)
                # Claude keeps its existing model/routing fields and gains the
                # same cached local metadata. Its badge comes from the same
                # host detection as Codex: installed locally, not a verified
                # login or provider health signal.
                claude = providers['claude']
                self.assertEqual(claude['name'], 'Claude Code')
                self.assertTrue(claude['host'])
                self.assertEqual(claude['role'], 'Host and controller')
                self.assertEqual(claude['status'], hosts.STATUS_LOCAL)
                self.assertEqual(claude['host_status'], hosts.STATUS_LOCAL)
                self.assertEqual(claude['status_detail'], codex['status_detail'])
                self.assertIn('Installed locally', claude['status_detail'])
                self.assertIn('not tested', claude['status_detail'])
                self.assertEqual(claude['requested_model'], 'claude-local')
                self.assertEqual(claude['routing'], 'DIRECT')
                self.assertEqual(claude['provider'], 'Anthropic')
                self.assertEqual(claude['executable'], '/home/krakadin/.local/bin/claude')
                self.assertEqual(claude['version'], '2.1.3')
                self.assertEqual(claude['skill_path'],
                                 '/home/krakadin/.claude/skills/delegate-workers/SKILL.md')
                self.assertNotIn('separate worktree', claude['role'])

    def test_providers_report_missing_host_installations_honestly_and_in_parity(self):
        with patch.object(hosts, 'resolve_executable', return_value=None), \
             patch.object(DashboardController, '_load_provider_snapshot',
                          return_value=self.safe_provider_snapshot()):
            self.controller.host_snapshot = None
            for endpoint in ('/api/v1/providers', '/api/v1/status'):
                status, body = self.request('GET', endpoint)
                self.assertEqual(status, 200)
                providers = json.loads(body)['providers']
                codex = providers['codex']
                self.assertEqual(codex['status'], hosts.STATUS_NOT_INSTALLED)
                self.assertIsNone(codex['executable'])
                self.assertIsNone(codex['version'])
                self.assertEqual(codex['provider'], 'OpenAI')
                # The card is present but never faked as healthy or logged in.
                self.assertNotIn('usage', codex)
                self.assertNotIn('update_state', codex)
                # Claude derives the same NOT_INSTALLED badge from the same
                # cached detection while keeping its model/routing metadata.
                claude = providers['claude']
                self.assertEqual(claude['status'], hosts.STATUS_NOT_INSTALLED)
                self.assertEqual(claude['host_status'], hosts.STATUS_NOT_INSTALLED)
                self.assertIsNone(claude['executable'])
                self.assertEqual(claude['status_detail'], codex['status_detail'])
                self.assertIn('Not installed', claude['status_detail'])
                self.assertEqual(claude['requested_model'], 'claude-local')
                self.assertEqual(claude['routing'], 'DIRECT')

    def test_host_detection_is_cached_across_provider_polls(self):
        with patch.object(DashboardController, '_load_provider_snapshot',
                          return_value=self.safe_provider_snapshot()):
            self.request('GET', '/api/v1/providers')
            self.request('GET', '/api/v1/providers')
            self.request('GET', '/api/v1/status')
            self.request('GET', '/api/v1/hosts')
        # One bounded probe per local host for the whole controller lifetime.
        self.assertEqual(len(self.probe.calls), 2)
        self.assertIsNotNone(self.controller.host_snapshot)

    def test_worker_actions_stay_restricted_to_qwen_and_kimi(self):
        for path in ('/api/v1/test/codex', '/api/v1/test/claude',
                     '/api/v1/test/chatgpt'):
            self.assertEqual(self.request('POST', path, body='{}',
                                          headers=self.csrf_headers())[0], 404)
        for payload in ('{"workers":["codex"]}', '{"workers":["claude"]}'):
            status, body = self.request('POST', '/api/v1/updates/check', body=payload,
                                        headers=self.csrf_headers())
            self.assertEqual(status, 400, body)
        for worker in ('codex', 'claude'):
            with self.subTest(worker=worker):
                with self.assertRaises(ValueError):
                    self.controller.start_test(worker)

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

    def test_overview_and_providers_share_one_four_card_order(self):
        # One shared display order drives the initial Overview and Providers
        # renders and every refresh path, so the Codex card never disappears
        # on a poll: two host agents first, then the two managed workers.
        self.assertIn("const PROVIDER_ORDER=['claude','codex','qwen','kimi'];", self.js)
        self.assertIn('PROVIDER_ORDER.forEach(name => cards.append(providerCard(data.providers[name])));',
                      self.js)
        self.assertIn('PROVIDER_ORDER.forEach(name=>cards.append(providerCard(data.providers[name])));',
                      self.js)
        self.assertIn('cards.replaceChildren(...PROVIDER_ORDER.map(name=>providerCard(data.providers[name])))',
                      self.js)
        self.assertNotIn("['claude','qwen','kimi']", self.js)
        self.assertIn("dataset.action='test'", self.js)

    def test_host_provider_cards_show_metadata_without_worker_actions(self):
        self.assertIn('const isHost = info.host === true;', self.js)
        self.assertIn("info.name || (info.worker || '').toUpperCase()", self.js)
        self.assertIn("isHost ? info.role : `${info.role} · separate worktree`", self.js)
        self.assertIn("'Not detected on this machine'", self.js)
        # Test/upgrade/usage actions stay gated on the two managed workers.
        self.assertIn("if (info.worker === 'qwen' || info.worker === 'kimi') {", self.js)
        self.assertIn("if (info.usage) card.append(usageSection(info));", self.js)

    def test_host_statuses_render_as_human_labels_with_explanation(self):
        # LOCAL/NOT_INSTALLED API values are preserved, but badges render as
        # Installed/Not installed with a nearby explanation that installed is
        # not a verified login or provider health signal.
        self.assertIn("'LOCAL': 'Installed'", self.js)
        self.assertIn("'NOT_INSTALLED': 'Not installed'", self.js)
        self.assertIn('hostStatusDetail', self.js)
        self.assertIn('host.status_detail || hostStatusDetail[host.status]', self.js)
        self.assertIn("node('p',hostDetail,'status-detail')", self.js)
        self.assertIn('.status-detail', self.css)
        self.assertIn('textContent', self.js)
        self.assertNotIn('innerHTML', self.js)

    def test_host_section_offers_no_action_and_no_post(self):
        self.assertNotIn("post('/api/v1/hosts", self.js)
        self.assertNotIn("dataset.action='test-host", self.js)
        self.assertNotIn("dataset.action='refresh-host", self.js)
        self.assertNotIn('fetch(\'/api/v1/hosts\',{method', self.js)

    def test_no_roadmap_host_remains_in_the_ui(self):
        for text in ('ROADMAP', 'host-roadmap', 'Planned hosted controller',
                     'hosted integration is planned', 'ChatGPT', 'hostSummary',
                     "panel('HOST AGENTS')"):
            self.assertNotIn(text, self.js)
        self.assertNotIn('host-roadmap', self.css)
        self.assertNotIn('status-roadmap', self.css)
        self.assertIn('.cards .card.host-card', self.css)
        self.assertIn('.status.status-local', self.css)
        self.assertIn('.status.status-not_installed', self.css)


if __name__ == '__main__':
    unittest.main()
