"""Current-user Windows startup entry for the local tray service."""

import ctypes
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _startup_folder() -> Path:
    # Ask Windows so redirected user profiles use their actual Startup folder.
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, 7, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise OSError("Windows Startup folder is unavailable.")
    return Path(buffer.value)


def _pythonw() -> Path:
    for candidate in (
        Path(sys.prefix) / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ):
        if candidate.is_file():
            return candidate.resolve()
    raise OSError("The Python windowless launcher is unavailable.")


def _vbs_string(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("Startup paths must not contain control characters.")
    return '"' + value.replace('"', '""') + '"'


class Autostart:
    def __init__(self, directory: Path, port: int):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid startup port.")
        self.directory = Path(directory).resolve()
        self.port = port
        identity = os.path.normcase(str(self.directory)).encode("utf-8")
        self.key = hashlib.sha256(identity).hexdigest()[:12]
        self.marker = f"' CourseDeck autostart {self.key}"

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def _entry(self) -> Path:
        return _startup_folder() / f"CourseDeck-{self.key}.vbs"

    def _owned(self, entry: Path) -> bool:
        if not entry.exists():
            return False
        if entry.is_symlink() or not entry.is_file():
            raise OSError("The startup entry is not managed by CourseDeck.")
        try:
            with entry.open(encoding="utf-16") as stream:
                marker = stream.readline().rstrip("\r\n")
        except UnicodeError as exc:
            raise OSError("The startup entry is not managed by CourseDeck.") from exc
        if marker != self.marker:
            raise OSError("The startup entry is not managed by CourseDeck.")
        return True

    def status(self) -> dict:
        return {
            "supported": self.supported,
            "enabled": self._owned(self._entry()) if self.supported else False,
        }

    def _script(self) -> str:
        command = subprocess.list2cmdline(
            [
                str(_pythonw()),
                "-m",
                "coursedeck.tray",
                "--background",
                "--port",
                str(self.port),
                "--data-dir",
                str(self.directory),
            ]
        )
        # WScript.Run expands environment variables. Preserve literal percent
        # characters in clone/data paths without invoking a command shell.
        command = command.replace("%", "%COURSEDECK_LITERAL_PERCENT%")
        working_directory = str(Path(__file__).resolve().parent.parent)
        return "\r\n".join(
            [
                self.marker,
                "Option Explicit",
                "Dim shell",
                'Set shell = CreateObject("WScript.Shell")',
                'shell.Environment("PROCESS")("COURSEDECK_LITERAL_PERCENT") = "%"',
                f"shell.CurrentDirectory = {_vbs_string(working_directory)}",
                f"shell.Run {_vbs_string(command)}, 0, False",
                "",
            ]
        )

    def set_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("Enabled must be a boolean.")
        if not self.supported:
            raise ValueError("Startup is only supported on Windows.")
        entry = self._entry()
        exists = self._owned(entry)
        if not enabled:
            if exists:
                entry.unlink()
            return self.status()
        contents = self._script().encode("utf-16")
        entry.parent.mkdir(parents=True, exist_ok=True)
        if not exists:
            # Exclusive creation cannot replace an unrelated entry appearing
            # between the ownership check and this write.
            with entry.open("xb") as stream:
                stream.write(contents)
        else:
            fd, temporary = tempfile.mkstemp(prefix=".coursedeck-", dir=entry.parent)
            temporary = Path(temporary)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(contents)
                self._owned(entry)
                temporary.replace(entry)
            finally:
                temporary.unlink(missing_ok=True)
        return self.status()
