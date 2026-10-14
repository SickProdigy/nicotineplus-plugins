import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_PATH = Path(__file__).parents[1] / "booksearch" / "__init__.py"


class FakeEvents:
    def __init__(self):
        self.invocations = []

    def invoke_main_thread(self, callback, *args):
        self.invocations.append((callback, args))
        callback(*args)


class FakeBasePlugin:
    core = types.SimpleNamespace(users=types.SimpleNamespace(login_username="owner"))

    def log(self, *_args):
        pass


def load_plugin_module():
    fake_events = FakeEvents()
    pynicotine = types.ModuleType("pynicotine")
    events_module = types.ModuleType("pynicotine.events")
    events_module.events = fake_events
    pluginsystem_module = types.ModuleType("pynicotine.pluginsystem")
    pluginsystem_module.BasePlugin = FakeBasePlugin

    module_name = "booksearch_test_plugin"
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


class BookSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.events = load_plugin_module()

    def setUp(self):
        self.plugin = self.module.Plugin()
        self.plugin.settings = dict(self.module.Plugin.settings)
        self.plugin.echoes = []
        self.plugin.sent = []
        self.plugin.echo_message = self.plugin.echoes.append
        self.plugin.send_message = self.plugin.sent.append
        self.plugin.send_public = lambda room, message: self.plugin.sent.append((room, message))

    def test_clean_text_removes_newlines_and_extra_space(self):
        self.assertEqual(self.module._clean_text("  one\n two\r\nthree  "), "one two three")

    def test_result_format_includes_availability(self):
        result = self.module.SearchResult(
            title="A Book", author="An Author", year=2020,
            url="https://example.test/work", formats=("borrowable",),
        )
        self.assertEqual(
            self.module.format_result(result),
            "A Book — An Author, 2020; borrowable — https://example.test/work",
        )

    def test_remote_requests_are_opt_in(self):
        self.assertIsNone(self.plugin._remote_query("!book dune"))
        self.plugin.settings["allow_remote_requests"] = True
        self.assertEqual(self.plugin._remote_query("!BOOK   dune "), "dune")

    def test_room_allowlist_is_case_insensitive(self):
        self.plugin.settings["allowed_rooms"] = "Books, Audiobooks"
        self.assertTrue(self.plugin._room_allowed("books"))
        self.assertFalse(self.plugin._room_allowed("general"))

    def test_worker_dispatches_results_via_main_thread(self):
        self.plugin.providers = (types.SimpleNamespace(search=lambda *_args: [
            self.module.SearchResult("Dune", "Frank Herbert", 1965, "https://example.test/dune")
        ]),)
        invocation_count = len(self.events.invocations)
        delivered = []
        self.plugin._search_worker("dune", delivered.append)
        self.assertEqual(len(self.events.invocations), invocation_count + 1)
        self.assertEqual(delivered, ["Dune — Frank Herbert, 1965 — https://example.test/dune"])

    def test_command_starts_background_search(self):
        started = threading.Event()
        self.plugin._search_worker = lambda *_args: started.set()
        self.assertTrue(self.plugin.booksearch_command("dune", room="Books"))
        self.assertTrue(started.wait(1))
        self.assertEqual(self.plugin.echoes, ['Searching book catalogs for "dune"…'])

    def test_invalid_settings_are_bounded(self):
        self.assertEqual(self.module._bounded_int("bad", 3, 1, 5), 3)
