"""Fail-closed Landlock write confinement for delegated editing workers."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
from pathlib import Path


class LandlockUnavailable(RuntimeError):
    pass


_SYSCALLS = {
    'x86_64': (444, 445, 446),
    'aarch64': (444, 445, 446),
    'riscv64': (444, 445, 446),
}
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38


class _RulesetAttr(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def landlock_abi() -> int:
    syscalls = _SYSCALLS.get(platform.machine())
    if syscalls is None:
        raise LandlockUnavailable('Landlock syscall numbers are unknown on this architecture.')
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(syscalls[0], ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(_CREATE_RULESET_VERSION))
    if abi < 1:
        err = ctypes.get_errno()
        raise LandlockUnavailable(f'Landlock is unavailable: {errno.errorcode.get(err, err)}.')
    return int(abi)


def _write_rights(abi: int) -> int:
    rights = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12))
    if abi >= 2:
        rights |= 1 << 13  # REFER
    if abi >= 3:
        rights |= 1 << 14  # TRUNCATE
    if abi >= 5:
        rights |= 1 << 15  # IOCTL_DEV
    return rights


def restrict_writes(allowed_roots: list[Path]) -> int:
    """Restrict this process and descendants to writes beneath listed roots.

    Read and execute access are intentionally left unchanged. No fallback is
    provided: an unavailable kernel feature is a hard failure for edit mode.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    abi = landlock_abi()
    syscalls = _SYSCALLS[platform.machine()]
    create_nr, add_nr, restrict_nr = syscalls

    paths: list[Path] = []
    for candidate in allowed_roots:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise LandlockUnavailable('A permitted write root is missing or unsafe.') from None
        if not resolved.is_dir():
            raise LandlockUnavailable('A permitted write root is not a directory.')
        if resolved not in paths:
            paths.append(resolved)
    if not paths:
        raise LandlockUnavailable('No write roots were supplied.')

    rights = _write_rights(int(abi))
    attr = _RulesetAttr(rights)
    ruleset_fd = libc.syscall(create_nr, ctypes.byref(attr), ctypes.sizeof(attr), ctypes.c_uint(0))
    if ruleset_fd < 0:
        err = ctypes.get_errno()
        raise LandlockUnavailable(f'Could not create Landlock ruleset: {errno.errorcode.get(err, err)}.')
    try:
        for path in paths:
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                rule = _PathBeneathAttr(rights, path_fd)
                if libc.syscall(add_nr, ctypes.c_int(ruleset_fd), ctypes.c_int(_RULE_PATH_BENEATH),
                                ctypes.byref(rule), ctypes.c_uint(0)) < 0:
                    err = ctypes.get_errno()
                    raise LandlockUnavailable(f'Could not add a Landlock write root: {errno.errorcode.get(err, err)}.')
            finally:
                os.close(path_fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            raise LandlockUnavailable(f'Could not set no_new_privs: {errno.errorcode.get(err, err)}.')
        if libc.syscall(restrict_nr, ctypes.c_int(ruleset_fd), ctypes.c_uint(0)) < 0:
            err = ctypes.get_errno()
            raise LandlockUnavailable(f'Could not activate Landlock: {errno.errorcode.get(err, err)}.')
    finally:
        os.close(ruleset_fd)
    return int(abi)
