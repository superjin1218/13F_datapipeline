"""Platform operations for a pipeline running on the user's computer."""

import asyncio
import os
import string
import subprocess
import sys
from contextlib import contextmanager


@contextmanager
def instance_lock(path):
    windows = sys.platform == 'win32'
    with path.open('a+b') as lock:
        try:
            if windows:
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('동일한 상태 폴더를 사용 중인 파이프라인이 있습니다.') from exc
        try:
            yield
        finally:
            if windows:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def worker_process_options():
    if sys.platform == 'win32':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def drive_roots():
    if sys.platform != 'win32':
        return []
    from ctypes import windll

    drives = windll.kernel32.GetLogicalDrives()
    return [f'{letter}:\\' for index, letter in enumerate(string.ascii_uppercase) if drives & (1 << index)]


async def open_path(path):
    if sys.platform == 'win32':
        os.startfile(os.fspath(path))
        return
    opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
    process = await asyncio.create_subprocess_exec(
        opener, os.fspath(path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        status = await asyncio.wait_for(process.wait(), 1)
    except TimeoutError:
        # Some desktop launchers run until the opened application closes.
        return
    if status:
        raise OSError(f'{opener} exited with status {status}')
