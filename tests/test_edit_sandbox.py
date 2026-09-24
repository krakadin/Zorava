import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SANDBOX = PROJECT / 'bin' / 'sandbox_exec.py'


class LandlockTests(unittest.TestCase):
    def test_worker_can_write_only_inside_granted_root_including_symlink_escape(self):
        with tempfile.TemporaryDirectory(prefix='ai-worker-landlock-') as temp:
            base = Path(temp)
            primary = base / 'primary'
            worker = base / 'worker'
            primary.mkdir()
            worker.mkdir()
            protected = primary / 'sentinel.txt'
            protected.write_text('untouched')
            (worker / 'escape').symlink_to(protected)
            script = (
                'from pathlib import Path; import sys; '
                'Path(sys.argv[1], "allowed.txt").write_text("inside"); '
                'out=[]; '
                'exec("try:\\n Path(sys.argv[2], \\\"blocked.txt\\\").write_text(\\\"bad\\\")\\n out.append(\\\"OUTSIDE_ALLOWED\\\")\\nexcept OSError: out.append(\\\"OUTSIDE_DENIED\\\")"); '
                'exec("try:\\n Path(sys.argv[3]).write_text(\\\"bad\\\")\\n out.append(\\\"SYMLINK_ALLOWED\\\")\\nexcept OSError: out.append(\\\"SYMLINK_DENIED\\\")"); '
                'print(" ".join(out))'
            )
            command = [sys.executable, '-B', str(SANDBOX), '--write-root', str(worker), '--',
                       sys.executable, '-B', '-c', script, str(worker), str(primary), str(worker / 'escape')]
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((worker / 'allowed.txt').is_file())
            self.assertEqual(protected.read_text(), 'untouched')
            self.assertFalse((primary / 'blocked.txt').exists())
            self.assertIn('OUTSIDE_DENIED', result.stdout)
            self.assertIn('SYMLINK_DENIED', result.stdout)

    def test_sandbox_fails_closed_for_missing_write_root(self):
        with tempfile.TemporaryDirectory(prefix='ai-worker-landlock-') as temp:
            command = [sys.executable, '-B', str(SANDBOX), '--write-root', str(Path(temp) / 'missing'), '--',
                       sys.executable, '-B', '-c', 'pass']
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=10, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('confinement unavailable', result.stderr)


if __name__ == '__main__':
    unittest.main()
