"""登记 Windows 下的项目实例，供停止脚本确认进程身份。"""

import os
import re
from contextlib import contextmanager


class InstanceAlreadyRunning(RuntimeError):
    """本项目已有实例持有进程登记文件的锁。"""


def _process_creation_time(pid=None):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_times = kernel32.GetProcessTimes
    filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
    get_times.argtypes = (wintypes.HANDLE, filetime_pointer, filetime_pointer,
                          filetime_pointer, filetime_pointer)
    get_times.restype = wintypes.BOOL
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if pid is None:
        handle = kernel32.GetCurrentProcess()
    else:
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

    try:
        if not get_times(handle, created, exited, kernel, user):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if pid is not None:
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle(handle)
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def active_dashboard_url(project_dir):
    """读取已就绪的本机地址，并核对进程创建时间。"""
    if os.name != 'nt':
        return None
    try:
        pid, created, url = (project_dir / 'electric_monitor.pid').read_text(encoding='ascii').splitlines()
        if re.fullmatch(r'http://(?:127\.0\.0\.1|\[::1\]):[0-9]{1,5}/', url):
            if _process_creation_time(int(pid)) == int(created):
                return url
    except (OSError, ValueError):
        return None
    return None


@contextmanager
def register_instance(project_dir):
    if os.name != 'nt':
        yield None
        return

    import msvcrt

    with (project_dir / 'electric_monitor.lock').open('a+b') as lock_file:
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except PermissionError as exc:
            raise InstanceAlreadyRunning from exc

        registration = project_dir / 'electric_monitor.pid'
        registration.write_text(f'{os.getpid()}\n{_process_creation_time()}\n', encoding='ascii')
        # Windows 在文件关闭或进程退出时释放锁，下一次启动会更新登记。
        yield registration
