import importlib.util
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_PATH = Path(__file__).parents[1] / "beetsbridge" / "__init__.py"


class FakeEvents:
    def __init__(self):
        self.invocations = []

    def invoke_main_thread(self, callback, *args):
        self.invocations.append((callback, args))
        callback(*args)


class FakeBasePlugin:
    def log(self, *_args):
        pass


def load_plugin_module():
    fake_events = FakeEvents()
    pynicotine = types.ModuleType("pynicotine")
    events_module = types.ModuleType("pynicotine.events")
    events_module.events = fake_events
    pluginsystem_module = types.ModuleType("pynicotine.pluginsystem")
    pluginsystem_module.BasePlugin = FakeBasePlugin

    module_name = "beetsbridge_test_plugin"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "pynicotine": pynicotine,
        "pynicotine.events": events_module,
        "pynicotine.pluginsystem": pluginsystem_module,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module, fake_events


class FakeTimer:
    instances = []

    def __init__(self, delay, callback, args):
        self.delay = delay
        self.callback = callback
        self.args = args
        self.daemon = False
        self.cancelled = False
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class BeetsBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.events = load_plugin_module()

    def setUp(self):
        FakeTimer.instances.clear()
        self.plugin = self.module.Plugin()
        self.plugin.settings = dict(self.module.Plugin.settings)
        self.plugin.echoes = []
        self.plugin.output = self.plugin.echoes.append
        self.plugin.echo_message = self.plugin.echoes.append

    def test_copy_command_uses_explicit_noninteractive_flags(self):
        runner = self.module.BeetsRunner(
            executable="beet",
            config_path="/config/config.yaml",
            library_path="/config/library.db",
            destination="/music",
            import_mode="copy",
            quiet_fallback="skip",
            incremental=True,
            timeout=60,
        )
        self.assertEqual(runner.command(Path("/downloads/Album")), [
            "beet", "-c", "/config/config.yaml", "-l", "/config/library.db",
            "-d", "/music", "import", "-q", "--quiet-fallback", "skip",
            "-c", "-M", "-i", "/downloads/Album",
        ])

    def test_move_and_inplace_modes_are_explicit(self):
        common = dict(
            executable="beet", config_path="", library_path="", destination="",
            quiet_fallback="asis", incremental=False, timeout=60,
        )
        move = self.module.BeetsRunner(import_mode="move", **common)
        inplace = self.module.BeetsRunner(import_mode="inplace", **common)
        self.assertIn("-m", move.command(Path("/album")))
        self.assertEqual(inplace.command(Path("/album"))[-3:-1], ["-C", "-M"])

    def test_runner_never_uses_a_shell(self):
        runner = self.module.BeetsRunner("beet", "", "", "", "copy", "skip", False, 60)
        completed = subprocess.CompletedProcess([], 0, stdout="done")
        with patch.object(self.module.subprocess, "run", return_value=completed) as run:
            result = runner.import_folder(Path("/downloads/Album"))
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_validation_requires_folder_below_allowed_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            album = root / "Artist" / "Album"
            album.mkdir(parents=True)
            (album / "track.flac").write_bytes(b"audio")
            self.plugin.settings["allowed_download_root"] = str(root)
            self.assertEqual(self.plugin._validated_folder(album, automatic=True), album.resolve())
            with self.assertRaises(self.module.BeetsError):
                self.plugin._validated_folder(root, automatic=True)
            with self.assertRaises(self.module.BeetsError):
                self.plugin._validated_folder(Path(temporary).parent, automatic=True)

    def test_automatic_import_requires_root_and_audio_extension(self):
        self.plugin.settings["auto_import"] = True
        scheduled = []
        self.plugin._schedule = lambda folder, delay=None: scheduled.append(folder)
        with tempfile.TemporaryDirectory() as temporary:
            album = Path(temporary) / "Album"
            album.mkdir()
            audio = album / "track.flac"
            text = album / "notes.txt"
            audio.write_bytes(b"audio")
            text.write_text("notes", encoding="utf-8")
            self.plugin.download_finished_notification("user", "track.flac", str(audio))
            self.assertEqual(scheduled, [])
            self.plugin.settings["allowed_download_root"] = temporary
            self.plugin.download_finished_notification("user", "notes.txt", str(text))
            self.assertEqual(scheduled, [])
            self.plugin.download_finished_notification("user", "track.flac", str(audio))
            self.assertEqual(scheduled, [album.resolve()])

    def test_scheduling_debounces_the_same_folder(self):
        folder = Path("/downloads/Album")
        with patch.object(self.module.threading, "Timer", FakeTimer):
            self.plugin._schedule(folder, delay=30)
            first = FakeTimer.instances[0]
            self.plugin._schedule(folder, delay=30)
            second = FakeTimer.instances[1]
        self.assertTrue(first.cancelled)
        self.assertTrue(first.started)
        self.assertTrue(second.started)
        self.assertFalse(second.cancelled)
        self.assertEqual(len(self.plugin._pending), 1)

    def test_worker_reports_result_on_main_thread(self):
        completed = subprocess.CompletedProcess([], 0, stdout="imported")
        runner = types.SimpleNamespace(import_folder=lambda _folder: completed)
        self.plugin._make_runner = lambda: runner
        reported = []
        self.plugin._report_result = lambda *args: reported.append(args)
        invocation_count = len(self.events.invocations)
        self.plugin._import_worker(Path("/downloads/Album"))
        self.assertEqual(len(self.events.invocations), invocation_count + 1)
        self.assertEqual(reported, [(Path("/downloads/Album"), 0, "imported")])

    def test_disable_cancels_pending_timers(self):
        with patch.object(self.module.threading, "Timer", FakeTimer):
            self.plugin._schedule(Path("/downloads/Album"), delay=30)
        timer = FakeTimer.instances[0]
        self.plugin.disable()
        self.assertTrue(timer.cancelled)
        self.assertEqual(self.plugin._pending, {})


if __name__ == "__main__":
    unittest.main()
