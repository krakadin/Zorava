"""Conservative retention for ai-router-owned SQLite job history only."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat

DEFAULT_RETENTION_DAYS = 30


def _retained_worktree_ids(runtime_jobs: Path) -> set[str]:
    if runtime_jobs.is_symlink():
        return {'*'}
    if not runtime_jobs.exists():
        return set()
    try:
        info = runtime_jobs.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return {'*'}
    except OSError:
        return {'*'}
    retained = set()
    try:
        entries = list(runtime_jobs.iterdir())
    except OSError:
        return {'*'}
    for entry in entries:
        # Any unexpected/unsafe job directory is preserved. Cleanup never
        # traverses or removes filesystem job artifacts.
        if entry.is_symlink() or not entry.is_dir():
            retained.add(entry.name)
            continue
        try:
            info = entry.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                retained.add(entry.name)
                continue
            if any(entry.iterdir()):
                retained.add(entry.name)
        except OSError:
            retained.add(entry.name)
    return retained


def preview_cleanup(store, runtime: Path, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    if not 1 <= retention_days <= 3650:
        raise ValueError('Retention must be between 1 and 3650 days.')
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec='milliseconds')
    candidates = store.cleanup_candidates(cutoff)
    retained = _retained_worktree_ids(runtime / 'jobs')
    eligible = [row for row in candidates if row['id'] not in retained and '*' not in retained]
    held = [row for row in candidates if row not in eligible]
    return {'retention_days': retention_days, 'cutoff': cutoff,
            'eligible_count': len(eligible), 'protected_count': len(held),
            'eligible': eligible, 'protected': held}


def apply_cleanup(store, runtime: Path, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    preview = preview_cleanup(store, runtime, retention_days=retention_days)
    deleted = store.delete_jobs([row['id'] for row in preview['eligible']])
    return {**preview, 'deleted_count': deleted}
