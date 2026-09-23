"""Local data location and process ownership for the desktop service."""

import os
import socket
from contextlib import contextmanager
from pathlib import Path


class StartupError(RuntimeError):
    pass


def data_directory(value=None):
    selected = value if value is not None else os.environ.get("COURSEDECK_DATA_DIR")
    return (
        Path(selected).expanduser().resolve()
        if selected
        else Path(__file__).resolve().parent.parent / "data"
    )


@contextmanager
def instance_lock(directory):
    """An OS-held lock survives stale files and is released when the process exits."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "coursedeck.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise StartupError("CourseDeck is already using this data folder.") from exc
            try:
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StartupError("CourseDeck is already using this data folder.") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def listening_socket(port):
    """Reserve the port before ASGI startup can open databases or start syncing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(socket.SOMAXCONN)
        except OSError as exc:
            raise StartupError(
                f"Port {port} is unavailable. CourseDeck may already be running."
            ) from exc
        sock.set_inheritable(True)
        yield sock
