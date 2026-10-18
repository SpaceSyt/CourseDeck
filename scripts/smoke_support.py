"""Lifecycle helpers for disposable smoke backends; never target saved services."""

import os
import subprocess
from contextlib import contextmanager


@contextmanager
def _windows_process_tree(pid):
    """Hold handles before termination, so child exit can be waited on reliably."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        method = getattr(kernel, name)
        method.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        method.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        success = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            success = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    descendants = {pid}
    while True:
        added = {child for child, parent in parents.items() if parent in descendants}
        if added <= descendants:
            break
        descendants.update(added)
    handles = []
    try:
        for child in descendants:
            handle = kernel.OpenProcess(0x00100000, False, child)  # SYNCHRONIZE only.
            if handle:
                handles.append(handle)
            elif ctypes.get_last_error() != 87:  # Already exited before opening.
                raise ctypes.WinError(ctypes.get_last_error())
        yield lambda: all(kernel.WaitForSingleObject(handle, 10000) == 0 for handle in handles)
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)


def stop_backend(process: subprocess.Popen):
    if process.poll() is not None:
        return
    if os.name == "nt":
        # A Windows venv executable may launch a second Python process. Terminating
        # only the redirector leaves the backend and its SQLite handles alive.
        with _windows_process_tree(process.pid) as wait_tree:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
            )
            if result.returncode and process.poll() is None:
                raise RuntimeError("Could not stop the disposable backend process tree")
            # taskkill initiates termination; waiting on the venv redirector alone
            # can return while Python still owns SQLite file mappings.
            if not wait_tree():
                raise RuntimeError("Disposable backend descendants did not exit")
    else:
        process.terminate()
    process.wait(timeout=10)
