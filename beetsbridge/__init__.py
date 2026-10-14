"""Queue completed Nicotine+ music folders for beets import."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin


DEFAULT_EXTENSIONS = ".aac,.aiff,.alac,.ape,.dsf,.flac,.m4a,.mp3,.ogg,.opus,.wav,.wv"


class BeetsError(RuntimeError):
    """A safe, actionable beets bridge failure."""


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _extensions(value: object) -> set[str]:
    parsed = set()
    for item in str(value or "").split(","):
        extension = item.strip().lower()
        if extension and not extension.startswith("."):
            extension = f".{extension}"
        if extension:
            parsed.add(extension)
    return parsed


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class BeetsRunner:
    """Build and run a non-interactive beets import without invoking a shell."""

    def __init__(
        self,
        executable: str,
        config_path: str,
        library_path: str,
        destination: str,
        import_mode: str,
        quiet_fallback: str,
        incremental: bool,
        timeout: int,
    ):
        self.executable = executable.strip()
        self.config_path = config_path.strip()
        self.library_path = library_path.strip()
        self.destination = destination.strip()
        self.import_mode = import_mode.strip().lower()
        self.quiet_fallback = quiet_fallback.strip().lower()
        self.incremental = bool(incremental)
        self.timeout = timeout

        if not self.executable:
            raise BeetsError("Configure the beets executable, usually 'beet'.")
        if self.import_mode not in {"copy", "move", "inplace"}:
            raise BeetsError("Import mode must be copy, move, or inplace.")
        if self.quiet_fallback not in {"skip", "asis"}:
            raise BeetsError("Quiet fallback must be skip or asis.")

    def command(self, folder: Path) -> list[str]:
        command = [self.executable]
        if self.config_path:
            command.extend(["-c", self.config_path])
        if self.library_path:
            command.extend(["-l", self.library_path])
        if self.destination:
            command.extend(["-d", self.destination])

        command.extend(["import", "-q", "--quiet-fallback", self.quiet_fallback])
        if self.import_mode == "copy":
            command.extend(["-c", "-M"])
        elif self.import_mode == "move":
            command.append("-m")
        else:
            command.extend(["-C", "-M"])
        if self.incremental:
            command.append("-i")
        command.append(os.fspath(folder))
        return command

    def import_folder(self, folder: Path) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                self.command(folder),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise BeetsError(f"Beets executable was not found: {self.executable}") from error
        except subprocess.TimeoutExpired as error:
            raise BeetsError(f"Beets import exceeded the {self.timeout}-second timeout.") from error
        except OSError as error:
            raise BeetsError(f"Could not start beets: {error}") from error


class Plugin(BasePlugin):
    settings = {
        "auto_import": False,
        "beet_executable": "beet",
        "config_path": "",
        "library_path": "",
        "destination": "",
        "allowed_download_root": "",
        "import_mode": "copy",
        "quiet_fallback": "skip",
        "incremental": False,
        "settle_seconds": 180,
        "import_timeout_seconds": 1800,
        "audio_extensions": DEFAULT_EXTENSIONS,
    }
    metasettings = {
        "auto_import": {"description": "Automatically import completed music folders", "type": "bool"},
        "beet_executable": {"description": "Beets executable path or command name", "type": "str"},
        "config_path": {"description": "Optional beets YAML config file", "type": "str"},
        "library_path": {"description": "Optional beets library database override", "type": "str"},
        "destination": {"description": "Optional organized music directory override", "type": "str"},
        "allowed_download_root": {"description": "Required root for automatic imports", "type": "str"},
        "import_mode": {"description": "Import mode: copy, move, or inplace", "type": "str"},
        "quiet_fallback": {"description": "Uncertain match action: skip or asis", "type": "str"},
        "incremental": {"description": "Enable beets incremental import mode", "type": "bool"},
        "settle_seconds": {"description": "Wait after the last completed file (30-3600 seconds)", "type": "int"},
        "import_timeout_seconds": {"description": "Maximum import runtime (60-21600 seconds)", "type": "int"},
        "audio_extensions": {"description": "Comma-separated audio extensions", "type": "str"},
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._import_lock = threading.Lock()
        self._pending: dict[Path, tuple[int, threading.Timer]] = {}
        self._generation = 0
        self._stopping = False
        self.commands = {
            "beetsimport": {
                "callback": self.beets_import_command,
                "parameters": ["<folder>"],
                "description": "Queue a folder under the allowed root for beets import",
                "disable": ["cli"],
            },
            "beetsstatus": {
                "callback": self.beets_status_command,
                "description": "Show the configured beets post-download mode",
                "disable": ["cli"],
            },
        }

    def loaded_notification(self):
        self._stopping = False
        state = "enabled" if bool(self.settings.get("auto_import", False)) else "disabled"
        self.log("Loaded. Automatic beets imports are %s.", state)

    def disable(self):
        self._stop_timers()

    def shutdown_notification(self):
        self._stop_timers()

    def download_finished_notification(self, user: str, virtual_path: str, real_path: str) -> None:
        del user, virtual_path
        if not bool(self.settings.get("auto_import", False)):
            return
        path = Path(real_path).expanduser().resolve(strict=False)
        if path.suffix.lower() not in _extensions(self.settings.get("audio_extensions", DEFAULT_EXTENSIONS)):
            return
        try:
            folder = self._validated_folder(path.parent, automatic=True)
        except BeetsError as error:
            self.log("Ignoring completed download: %s", str(error))
            return
        self._schedule(folder)

    def beets_import_command(
        self,
        args: str,
        room: Optional[str] = None,
        user: Optional[str] = None,
    ) -> bool:
        del room, user
        try:
            folder = self._validated_folder(Path(args.strip()).expanduser(), automatic=False)
        except BeetsError as error:
            self.output(str(error))
            return False
        self._schedule(folder, delay=0)
        self.echo_message(f"Queued beets import: {folder}")
        return True

    def beets_status_command(self, _args: str, **_context: object) -> bool:
        mode = str(self.settings.get("import_mode", "copy"))
        state = "enabled" if bool(self.settings.get("auto_import", False)) else "disabled"
        root = str(self.settings.get("allowed_download_root", "")).strip() or "not configured"
        self.echo_message(f"Beets auto-import: {state}; mode: {mode}; allowed root: {root}")
        return True

    def _validated_folder(self, folder: Path, automatic: bool) -> Path:
        folder = folder.resolve(strict=False)
        root_setting = str(self.settings.get("allowed_download_root", "")).strip()
        if automatic and not root_setting:
            raise BeetsError("set Allowed download root before enabling automatic imports")
        if root_setting:
            root = Path(root_setting).expanduser().resolve(strict=False)
            if folder == root or not _is_within(folder, root):
                raise BeetsError(f"folder is outside the allowed download root: {folder}")
        if not folder.is_dir():
            raise BeetsError(f"folder does not exist: {folder}")
        if not any(
            child.is_file() and child.suffix.lower() in _extensions(self.settings.get("audio_extensions"))
            for child in folder.iterdir()
        ):
            raise BeetsError(f"folder contains no configured audio files: {folder}")
        return folder

    def _schedule(self, folder: Path, delay: Optional[int] = None) -> None:
        if delay is None:
            delay = _bounded_int(self.settings.get("settle_seconds"), 180, 30, 3600)
        with self._lock:
            previous = self._pending.pop(folder, None)
            if previous:
                previous[1].cancel()
            self._generation += 1
            generation = self._generation
            timer = threading.Timer(delay, self._timer_elapsed, args=(folder, generation))
            timer.daemon = True
            self._pending[folder] = (generation, timer)
            timer.start()

    def _timer_elapsed(self, folder: Path, generation: int) -> None:
        with self._lock:
            pending = self._pending.get(folder)
            if self._stopping or pending is None or pending[0] != generation:
                return
            del self._pending[folder]
        threading.Thread(
            target=self._import_worker,
            args=(folder,),
            name="nicotine-beets-import",
            daemon=True,
        ).start()

    def _import_worker(self, folder: Path) -> None:
        try:
            runner = self._make_runner()
            with self._import_lock:
                result = runner.import_folder(folder)
            output = " ".join((result.stdout or "").split())[-1000:]
            events.invoke_main_thread(self._report_result, folder, result.returncode, output)
        except BeetsError as error:
            events.invoke_main_thread(self._report_error, folder, str(error))
        except Exception as error:
            events.invoke_main_thread(self._report_error, folder, f"unexpected failure: {error}")

    def _make_runner(self) -> BeetsRunner:
        return BeetsRunner(
            executable=str(self.settings.get("beet_executable", "beet")),
            config_path=str(self.settings.get("config_path", "")),
            library_path=str(self.settings.get("library_path", "")),
            destination=str(self.settings.get("destination", "")),
            import_mode=str(self.settings.get("import_mode", "copy")),
            quiet_fallback=str(self.settings.get("quiet_fallback", "skip")),
            incremental=bool(self.settings.get("incremental", False)),
            timeout=_bounded_int(self.settings.get("import_timeout_seconds"), 1800, 60, 21600),
        )

    def _report_result(self, folder: Path, returncode: int, output: str) -> None:
        if self._stopping:
            return
        if returncode == 0:
            self.log("Beets import completed for %s", str(folder))
        else:
            detail = f" Output: {output}" if output else ""
            self.log("Beets import failed for %s with exit code %s.%s", (str(folder), returncode, detail))

    def _report_error(self, folder: Path, error: str) -> None:
        if not self._stopping:
            self.log("Beets import failed for %s: %s", (str(folder), error))

    def _stop_timers(self) -> None:
        self._stopping = True
        with self._lock:
            for _generation, timer in self._pending.values():
                timer.cancel()
            self._pending.clear()
