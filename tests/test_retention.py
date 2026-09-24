from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import os
import stat
import unittest
import uuid

from ai_router.retention import apply_cleanup, preview_cleanup
from ai_router.state import StateStore


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ai-router-retention-')
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)/'state'
        self.runtime.mkdir(mode=0o700)
        os.chmod(self.runtime,0o700)
        self.store = StateStore(self.runtime/'workers.db')

    def add_job(self, *, status='completed', mode='read-only', age_days=45):
        job_id = str(uuid.uuid4())
        request = SimpleNamespace(worker='qwen', cwd=Path(self.tmp.name), mode=mode,
                                  task='test job', parent_job_id=None, delegation_group_id=None)
        self.store.create_job(job_id,request,'qwen3.8-max','Qwen Researcher','0.24.4')
        old = (datetime.now(timezone.utc)-timedelta(days=age_days)).isoformat(timespec='milliseconds')
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET status=?,created_at=?,completed_at=?,result=? WHERE id=?',
                       (status,old,old,'synthetic result',job_id))
        return job_id

    def test_preview_and_confirm_remove_old_terminal_history_and_events(self):
        expired=self.add_job()
        recent=self.add_job(age_days=2)
        active=self.add_job(status='running')
        preview=preview_cleanup(self.store,self.runtime)
        self.assertEqual(preview['eligible_count'],1)
        self.assertEqual(preview['eligible'][0]['id'],expired)
        result=apply_cleanup(self.store,self.runtime)
        self.assertEqual(result['deleted_count'],1)
        self.assertIsNone(self.store.get_job(expired))
        self.assertIsNotNone(self.store.get_job(recent))
        self.assertIsNotNone(self.store.get_job(active))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_events WHERE job_id=?',(expired,)).fetchone()[0],0)

    def test_retained_edit_worktree_is_never_purged(self):
        job_id=self.add_job(mode='isolated-edit')
        job_dir=self.runtime/'jobs'/job_id
        worktree=job_dir/'worktree'
        worktree.mkdir(parents=True,mode=0o700)
        os.chmod(job_dir,0o700);os.chmod(worktree,0o700)
        preview=preview_cleanup(self.store,self.runtime)
        self.assertEqual(preview['eligible_count'],0)
        self.assertEqual(preview['protected_count'],1)
        self.assertIsNotNone(self.store.get_job(job_id))

    def test_unsafe_job_directory_protects_records_and_cleanup_never_follows_it(self):
        job_id=self.add_job()
        jobs=self.runtime/'jobs';jobs.mkdir(mode=0o700);os.chmod(jobs,0o700)
        outside=Path(self.tmp.name)/'outside';outside.mkdir(mode=0o700)
        (jobs/job_id).symlink_to(outside,target_is_directory=True)
        preview=preview_cleanup(self.store,self.runtime)
        self.assertEqual(preview['eligible_count'],0)
        self.assertEqual(preview['protected_count'],1)
        self.assertTrue(outside.is_dir())


if __name__ == '__main__':
    unittest.main()
