"""Owner-only SQLite job metadata and a small audit-event trail."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from .security import redact, summarize


SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError('Runtime directory is a symlink; refusing to use it.')
    if not path.exists():
        old_umask = os.umask(0o077)
        try:
            path.mkdir(parents=True, mode=0o700)
        finally:
            os.umask(old_umask)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('Runtime directory must be user-owned with mode 0700.')


class StateStore:
    def __init__(self, database: Path):
        self.path = database
        ensure_private_directory(database.parent)
        if database.is_symlink():
            raise RuntimeError('Database path is a symlink; refusing to use it.')
        if not database.exists():
            old_umask = os.umask(0o077)
            try:
                fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            finally:
                os.umask(old_umask)
        info = database.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise RuntimeError('Database must be a user-owned regular file with mode 0600 and no hard links.')
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, worker TEXT NOT NULL, role TEXT NOT NULL,
                    requested_model TEXT NOT NULL, worker_version TEXT,
                    reported_model TEXT,
                    cwd TEXT NOT NULL, mode TEXT NOT NULL, task_summary TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
                    completed_at TEXT, duration_ms INTEGER, exit_code INTEGER,
                    result TEXT, partial_result TEXT, error_code TEXT, error_message TEXT,
                    pid INTEGER, process_group INTEGER, process_start TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    parent_job_id TEXT, delegation_group_id TEXT,
                    usage_json TEXT, job_type TEXT NOT NULL DEFAULT 'delegation'
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    event TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_status (
                    worker TEXT PRIMARY KEY, status TEXT NOT NULL,
                    last_test_at TEXT, last_success_at TEXT,
                    last_error_code TEXT, last_error TEXT,
                    executable_version TEXT, requested_model TEXT
                );
            ''')
            row = db.execute('SELECT version FROM schema_info LIMIT 1').fetchone()
            if row is None:
                db.execute('INSERT INTO schema_info(version) VALUES (?)', (SCHEMA_VERSION,))
            elif row[0] == 1:
                db.execute('ALTER TABLE jobs ADD COLUMN worker_version TEXT')
                db.execute('UPDATE schema_info SET version=?', (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise RuntimeError('Unsupported database schema version.')
        os.chmod(database, 0o600, follow_symlinks=False)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level='DEFERRED')
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, job_id: str, name: str, detail: str = '') -> None:
        safe, _ = redact(detail)
        with self.connect() as db:
            db.execute('INSERT INTO job_events(job_id,timestamp,event,detail) VALUES (?,?,?,?)',
                       (job_id, utc_now(), name, safe[:2000]))

    def create_job(self, job_id, request, model, role, worker_version=None, job_type='delegation'):
        with self.connect() as db:
            db.execute('''INSERT INTO jobs(id,worker,role,requested_model,worker_version,cwd,mode,task_summary,status,
                created_at,parent_job_id,delegation_group_id,job_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (job_id, request.worker, role, model, worker_version, str(request.cwd), request.mode,
                 summarize(request.task), 'queued', utc_now(), request.parent_job_id,
                 request.delegation_group_id, job_type))
            db.execute('INSERT INTO job_events(job_id,timestamp,event,detail) VALUES (?,?,?,?)',
                       (job_id, utc_now(), 'queued', request.worker))

    def start_job(self, job_id, pid, pgid, process_start):
        with self.connect() as db:
            cur = db.execute('''UPDATE jobs SET status='running',started_at=?,pid=?,process_group=?,process_start=?
                WHERE id=? AND status='queued' ''', (utc_now(), pid, pgid, process_start, job_id))
            if cur.rowcount != 1:
                raise RuntimeError('Job could not enter running state.')
            db.execute('INSERT INTO job_events(job_id,timestamp,event,detail) VALUES (?,?,?,?)',
                       (job_id, utc_now(), 'started', 'owned worker process created'))

    def finish_job(self, job_id, status, *, duration_ms, exit_code=None, result=None,
                   partial_result=None, reported_model=None, error_code=None,
                   error_message=None, usage=None):
        if status not in ('completed','failed','timed_out','cancelled','auth_error','rate_limited','invalid_output'):
            raise ValueError('Invalid terminal job status.')
        result, _ = redact(result or '')
        partial_result, _ = redact(partial_result or '')
        error_message, _ = redact(error_message or '')
        with self.connect() as db:
            row = db.execute('SELECT status,cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['status'] not in ('queued','running'):
                raise RuntimeError('Invalid job status transition.')
            if row['cancel_requested'] and row['status'] == 'running':
                status, error_code, error_message = 'cancelled', 'CANCELLED', 'Cancelled by user.'
            db.execute('''UPDATE jobs SET status=?,completed_at=?,duration_ms=?,exit_code=?,result=?,partial_result=?,
                reported_model=?,error_code=?,error_message=?,usage_json=? WHERE id=?''',
                (status, utc_now(), duration_ms, exit_code, result or None, partial_result or None,
                 reported_model, error_code, error_message or None,
                 json.dumps(usage, separators=(',',':')) if usage is not None else None, job_id))
            db.execute('INSERT INTO job_events(job_id,timestamp,event,detail) VALUES (?,?,?,?)',
                       (job_id, utc_now(), status, error_code or ''))

    def fail_queued(self, job_id, code, message):
        safe, _ = redact(message)
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='failed',completed_at=?,error_code=?,error_message=? WHERE id=? AND status='queued'",
                       (utc_now(), code, safe[:2000], job_id))
            db.execute('INSERT INTO job_events(job_id,timestamp,event,detail) VALUES (?,?,?,?)',
                       (job_id, utc_now(), 'failed', code))

    def get_job(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                return None
            value = dict(row)
            value['events'] = [dict(event) for event in db.execute(
                'SELECT timestamp,event,detail FROM job_events WHERE job_id=? ORDER BY sequence', (job_id,))]
            return value

    def jobs(self, limit=50):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?', (limit,))]

    def active_jobs(self, worker):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE worker=? AND status IN ('queued','running')", (worker,))]

    def provider_tests(self, worker, limit=2):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE worker=? AND job_type='provider_test' ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (worker, limit))]

    def last_successful_test(self, worker):
        with self.connect() as db:
            row = db.execute("""SELECT completed_at FROM jobs WHERE worker=? AND job_type='provider_test'
                AND status='completed' ORDER BY completed_at DESC, rowid DESC LIMIT 1""", (worker,)).fetchone()
            return row['completed_at'] if row else None

    def dashboard_jobs(self, *, limit=200, worker=None, status=None, search=None, date_utc=None):
        clauses = []
        args = []
        if worker in ('qwen', 'kimi'):
            clauses.append('worker=?'); args.append(worker)
        if status:
            clauses.append('status=?'); args.append(status)
        if isinstance(date_utc,str) and len(date_utc)==10 and date_utc[4]=='-' and date_utc[7]=='-':
            clauses.append('substr(created_at,1,10)=?'); args.append(date_utc)
        if search:
            clauses.append('(task_summary LIKE ? OR cwd LIKE ? OR worker LIKE ?)')
            term = '%' + search[:120].replace('%', r'\%').replace('_', r'\_') + '%'
            args.extend((term, term, term))
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        query = ('SELECT id,worker,role,requested_model,worker_version,reported_model,cwd,mode,task_summary,status,'
                 'created_at,started_at,completed_at,duration_ms,exit_code,result,partial_result,error_code,error_message,'
                 'usage_json,parent_job_id,delegation_group_id,job_type FROM jobs' + where +
                 ' ORDER BY created_at DESC LIMIT ?')
        args.append(max(1, min(int(limit), 500)))
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, args)]

    def dashboard_events(self, *, limit=250, worker=None, errors_only=False):
        clauses = []
        args = []
        if worker in ('qwen', 'kimi'):
            clauses.append('j.worker=?'); args.append(worker)
        if errors_only:
            clauses.append("(e.event IN ('failed','timed_out','cancelled','auth_error','rate_limited','invalid_output') OR j.error_code IS NOT NULL)")
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        query = ('SELECT e.timestamp,e.event,e.detail,e.job_id,j.worker,j.cwd,j.requested_model,j.error_code '
                 'FROM job_events e JOIN jobs j ON j.id=e.job_id' + where +
                 ' ORDER BY e.sequence DESC LIMIT ?')
        args.append(max(1, min(int(limit), 500)))
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, args)]

    def dashboard_counts(self):
        with self.connect() as db:
            rows = db.execute("SELECT status,COUNT(*) AS count FROM jobs WHERE created_at >= ? GROUP BY status",
                              (datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat(),)).fetchall()
            today = {row['status']: row['count'] for row in rows}
            active = db.execute("SELECT worker,COUNT(*) AS count FROM jobs WHERE status IN ('queued','running') GROUP BY worker").fetchall()
            active_counts = {row['worker']: row['count'] for row in active}
            return {'today': today, 'active': active_counts}

    def cleanup_candidates(self, cutoff: str):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,worker,mode,status,created_at FROM jobs WHERE created_at < ? AND status NOT IN ('queued','running') ORDER BY created_at",
                (cutoff,))]

    def delete_jobs(self, job_ids):
        ids = [str(value) for value in job_ids]
        if not ids:
            return 0
        placeholders = ','.join('?' for _ in ids)
        with self.connect() as db:
            db.execute(f'''DELETE FROM job_events WHERE job_id IN (
                SELECT id FROM jobs WHERE id IN ({placeholders}) AND status NOT IN ('queued','running')
            )''', ids)
            cursor = db.execute(f"DELETE FROM jobs WHERE id IN ({placeholders}) AND status NOT IN ('queued','running')", ids)
            deleted = cursor.rowcount
        with self.connect() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return deleted

    def cancel_queued(self, job_id):
        """Cancel a job still waiting for a worker slot.

        The status-scoped update is atomic: a job that has already entered
        running state is left for the running-process cancellation path.
        """
        now = utc_now()
        with self.connect() as db:
            cur = db.execute("""UPDATE jobs SET status='cancelled',completed_at=?,cancel_requested=1,
                error_code='CANCELLED',error_message='Cancelled by user.'
                WHERE id=? AND status='queued'""", (now, job_id))
            if cur.rowcount:
                db.execute('INSERT INTO job_events(job_id,timestamp,event,detail) VALUES (?,?,?,?)',
                           (job_id, now, 'cancelled', 'cancelled while queued'))
            return cur.rowcount == 1

    def request_cancel(self, job_id):
        with self.connect() as db:
            cur = db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=? AND status='running'", (job_id,))
            if cur.rowcount:
                db.execute('INSERT INTO job_events(job_id,timestamp,event,detail) VALUES (?,?,?,?)',
                           (job_id, utc_now(), 'cancellation requested', ''))
            return cur.rowcount == 1

    def worker_activity(self):
        """Per-worker queued/running job counts for status reporting."""
        counts = {}
        with self.connect() as db:
            rows = db.execute("SELECT worker,status,COUNT(*) AS count FROM jobs "
                              "WHERE status IN ('queued','running') GROUP BY worker,status").fetchall()
        for row in rows:
            entry = counts.setdefault(row['worker'], {'queued': 0, 'running': 0})
            entry[row['status']] = row['count']
        return counts

    def job_token_totals(self, *, limit=1000):
        """Bounded per-worker per-job token sums over retained completed jobs.

        Only local rows are read. A counter total stays None (unknown) when no
        completed job recorded usage or when any usage-bearing row lacked that
        counter; partial sums are never presented as complete totals, and these
        per-job sums are never account-level remaining quota.
        """
        keys = ('input_tokens', 'output_tokens', 'cached_tokens')
        with self.connect() as db:
            rows = db.execute(
                "SELECT worker,usage_json FROM jobs WHERE status='completed' "
                "AND worker IN ('qwen','kimi') ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 5000)),)).fetchall()
        totals = {}
        for row in rows:
            entry = totals.setdefault(row['worker'], {'jobs_completed': 0, 'jobs_with_usage': 0,
                                                      **{key: 0 for key in keys},
                                                      **{key + '_complete': True for key in keys}})
            entry['jobs_completed'] += 1
            try:
                usage = json.loads(row['usage_json']) if row['usage_json'] else None
            except ValueError:
                usage = None
            if not isinstance(usage, dict):
                continue
            entry['jobs_with_usage'] += 1
            for key in keys:
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    entry[key] += value
                else:
                    entry[key + '_complete'] = False
        result = {'scope': 'Completed jobs in retained history'}
        for worker in ('qwen', 'kimi'):
            entry = totals.get(worker)
            summary = {'jobs_completed': 0, 'jobs_with_usage': 0,
                       'input_tokens': None, 'output_tokens': None, 'cached_tokens': None}
            if entry is not None:
                summary['jobs_completed'] = entry['jobs_completed']
                summary['jobs_with_usage'] = entry['jobs_with_usage']
                if entry['jobs_with_usage']:
                    for key in keys:
                        if entry[key + '_complete']:
                            summary[key] = entry[key]
            result[worker] = summary
        return result

    def is_cancel_requested(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
            return bool(row and row['cancel_requested'])

    def mark_interrupted(self, job_id):
        """Retire a running job after its caller verifies the worker has exited."""
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute("""UPDATE jobs SET status='failed',completed_at=?,
                error_code='SUPERVISOR_INTERRUPTED',error_message='Worker process exited without a recorded result.'
                WHERE id=? AND status='running'""", (now, job_id))
            if cursor.rowcount:
                db.execute('INSERT INTO job_events(job_id,timestamp,event,detail) VALUES (?,?,?,?)',
                           (job_id, now, 'failed', 'SUPERVISOR_INTERRUPTED'))
            return cursor.rowcount == 1
