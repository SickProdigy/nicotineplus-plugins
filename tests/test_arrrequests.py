import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_PATH = Path(__file__).parents[1] / "arrrequests" / "__init__.py"


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
    module_name = "arrrequests_test_plugin"
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


class FakeResponse:
    def __init__(self, payload):
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._body.read(size)


class ArrRequestsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.events = load_plugin_module()

    def setUp(self):
        self.plugin = self.module.Plugin()
        self.plugin.settings = dict(self.module.Plugin.settings)
        self.plugin.core = types.SimpleNamespace(users=types.SimpleNamespace(login_username="owner"))
        self.plugin.sent = []
        self.plugin.echoes = []
        self.plugin.echo_message = self.plugin.echoes.append
        self.plugin.output = self.plugin.echoes.append
        self.plugin.send_public = lambda room, message: self.plugin.sent.append((room, message))
        self.plugin.send_private = lambda user, message, **_kwargs: self.plugin.sent.append((user, message))

    def test_service_routes_and_api_key_header(self):
        cases = {
            "tv": ("v3/series/lookup", [{"title": "Example Show", "year": 2020, "tvdbId": 12}]),
            "movie": ("v3/movie/lookup", [{"title": "Example Movie", "year": 2021, "tmdbId": 34}]),
            "music": ("v1/artist/lookup", [{"artistName": "Example Artist", "foreignArtistId": "mbid"}]),
        }
        for media_type, (route, payload) in cases.items():
            captured = {}

            def fake_urlopen(request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return FakeResponse(payload)

            client = self.module.ArrClient(media_type, "http://arr.local/", "secret")
            with patch.object(self.module, "urlopen", fake_urlopen):
                results = client.lookup("example title", 3, 9)
            self.assertIn(route, captured["request"].full_url)
            self.assertIn("term=example+title", captured["request"].full_url)
            self.assertEqual(captured["request"].headers["X-api-key"], "secret")
            self.assertEqual(captured["request"].get_method(), "GET")
            self.assertEqual(captured["timeout"], 9)
            self.assertEqual(len(results), 1)

    def test_status_uses_service_api_version(self):
        captured = []

        def fake_urlopen(request, timeout):
            captured.append(request.full_url)
            return FakeResponse({"appName": "Sonarr", "version": "4.0"})

        client = self.module.ArrClient("tv", "http://sonarr:8989", "key")
        with patch.object(self.module, "urlopen", fake_urlopen):
            self.assertEqual(client.status(10), "Sonarr 4.0")
        self.assertEqual(captured, ["http://sonarr:8989/api/v3/system/status"])

    def test_parse_request_accepts_only_supported_types(self):
        self.assertEqual(self.module._parse_request("movie Dune"), ("movie", "Dune"))
        self.assertEqual(self.module._parse_request("TV The Expanse"), ("tv", "The Expanse"))
        self.assertIsNone(self.module._parse_request("book Dune"))
        self.assertIsNone(self.module._parse_request("movie"))

    def test_remote_requests_require_enable_and_approval(self):
        self.assertIsNone(self.plugin._remote_request("friend", "!request movie Dune"))
        self.plugin.settings["allow_remote_requests"] = True
        self.plugin.settings["approved_users"] = "Friend, Trusted"
        self.assertIsNone(self.plugin._remote_request("stranger", "!request movie Dune"))
        with patch.object(self.module.time, "monotonic", return_value=100):
            self.assertEqual(
                self.plugin._remote_request("friend", "!REQUEST movie Dune"),
                ("movie", "Dune"),
            )

    def test_lookup_worker_is_read_only_and_returns_main_thread(self):
        class FakeClient:
            def lookup(self, query, limit, timeout):
                return [self_result]

        self_result = self.module.LookupResult("Dune", 2021, "tmdb:438631")
        self.plugin._client = lambda _media_type: FakeClient()
        self.plugin._active_requests = 1
        delivered = []
        invocation_count = len(self.events.invocations)
        self.plugin._lookup_worker("movie", "Dune", delivered.append)
        self.assertEqual(self.plugin._active_requests, 0)
        self.assertEqual(len(self.events.invocations), invocation_count + 1)
        self.assertEqual(delivered, [
            "movie: Dune (2021, tmdb:438631)",
            "Lookup only—nothing was added or downloaded.",
        ])

    def test_status_skips_unconfigured_services(self):
        self.plugin._active_requests = 1
        delivered = []
        self.plugin._status_worker(delivered.append)
        self.assertEqual(self.plugin._active_requests, 0)
        self.assertEqual(delivered, [
            "sonarr: not configured",
            "radarr: not configured",
            "lidarr: not configured",
        ])

    def test_invalid_url_and_missing_key_are_rejected(self):
        with self.assertRaises(self.module.ArrError):
            self.module.ArrClient("movie", "file:///tmp/radarr", "key")
        with self.assertRaises(self.module.ArrError):
            self.module.ArrClient("movie", "http://radarr:7878", "")


if __name__ == "__main__":
    unittest.main()
