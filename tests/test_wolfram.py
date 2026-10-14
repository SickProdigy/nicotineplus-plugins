import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_PATH = Path(__file__).parents[1] / "wolfram" / "__init__.py"


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
    module_name = "wolfram_test_plugin"
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
    def __init__(self, body):
        self._body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._body.read(size)


SAMPLE_XML = b"""<?xml version='1.0'?>
<queryresult success='true'>
  <pod title='Input interpretation'><subpod><plaintext>2 + 2</plaintext></subpod></pod>
  <pod title='Result'><subpod><plaintext>4</plaintext></subpod></pod>
</queryresult>
"""


class WolframTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.events = load_plugin_module()

    def setUp(self):
        self.plugin = self.module.Plugin()
        self.plugin.settings = dict(self.module.Plugin.settings)
        self.plugin.settings["app_id"] = "test-app-id"
        self.plugin.core = types.SimpleNamespace(users=types.SimpleNamespace(login_username="owner"))
        self.plugin.sent = []
        self.plugin.echoes = []
        self.plugin.logs = []
        self.plugin.echo_message = self.plugin.echoes.append
        self.plugin.output = self.plugin.echoes.append
        self.plugin.send_public = lambda room, message: self.plugin.sent.append((room, message))
        self.plugin.send_private = lambda user, message, **_kwargs: self.plugin.sent.append((user, message))
        self.plugin.log = lambda message, args=None: self.plugin.logs.append((message, args))

    def test_parse_plaintext_pods(self):
        self.assertEqual(
            self.module._parse_answers(SAMPLE_XML, solve=False, max_pods=3),
            ["Input interpretation: 2 + 2", "Result: 4"],
        )

    def test_geoip_result_is_omitted(self):
        body = b"<queryresult><pod title='Current geoIP location'><subpod><plaintext>Secret</plaintext></subpod></pod></queryresult>"
        self.assertEqual(self.module._parse_answers(body, False, 3), [self.module.NO_RESULT])

    def test_malformed_xml_has_specific_failure(self):
        with self.assertRaises(self.module.WolframError) as raised:
            self.module._parse_answers(b"not xml", False, 3)
        self.assertIs(raised.exception.failure, self.module.Failure.INVALID_RESPONSE)

    def test_client_request_and_public_source_link(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return FakeResponse(SAMPLE_XML)

        client = self.module.WolframClient("secret-app-id")
        with patch.object(self.module, "urlopen", fake_urlopen):
            answer = client.query("2+2", solve=False, timeout=12, max_pods=3)
        self.assertIn("appid=secret-app-id", captured["url"])
        self.assertIn("input=2%2B2", captured["url"])
        self.assertEqual(captured["timeout"], 12)
        self.assertNotIn("secret-app-id", answer.source_url)
        self.assertEqual(answer.lines[-1], "Result: 4")

    def test_solve_adds_step_by_step_podstate(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse(SAMPLE_XML)

        with patch.object(self.module, "urlopen", fake_urlopen):
            self.module.WolframClient("key").query("x=1", True, 10, 3)
        self.assertIn("podstate=Step-by-step+solution", captured["url"])

    def test_remote_queries_require_enable_approval_and_room(self):
        self.assertIsNone(self.plugin._remote_question("Books", "friend", "!wolfram 2+2"))
        self.plugin.settings["allow_remote_queries"] = True
        self.plugin.settings["approved_users"] = "Friend"
        self.plugin.settings["allowed_rooms"] = "Help"
        self.assertIsNone(self.plugin._remote_question("Books", "friend", "!wolfram 2+2"))
        with patch.object(self.module.time, "monotonic", return_value=100):
            self.assertEqual(
                self.plugin._remote_question("Help", "FRIEND", "!WOLFRAM 2+2"),
                "2+2",
            )

    def test_worker_returns_answer_via_main_thread_and_releases_slot(self):
        class FakeClient:
            def __init__(self, _app_id):
                pass

            def query(self, **_kwargs):
                return self_answer

        self_answer = self.module.WolframAnswer(("Result: 4",), "https://example.test/source")
        delivered = []
        self.plugin._active_queries = 1
        invocation_count = len(self.events.invocations)
        with patch.object(self.module, "WolframClient", FakeClient):
            self.plugin._query_worker("2+2", False, delivered.append)
        self.assertEqual(self.plugin._active_queries, 0)
        self.assertEqual(len(self.events.invocations), invocation_count + 1)
        self.assertEqual(delivered, ["Result: 4", "Source: https://example.test/source"])

    def test_missing_key_is_rejected_before_thread_start(self):
        self.plugin.settings["app_id"] = ""
        self.assertFalse(self.plugin.wolfram_command("2+2", room="Help"))
        self.assertIn("Configure a Wolfram|Alpha AppID", self.plugin.echoes[-1])

    def test_command_captures_room_for_async_reply(self):
        captured = {}

        def fake_start(question, solve, reply):
            captured["question"] = question
            captured["solve"] = solve
            reply("Result: 4")
            return True

        self.plugin._start_query = fake_start
        self.assertTrue(self.plugin.wolfram_command(" 2+2 ", room="Help"))
        self.assertEqual(captured, {"question": "2+2", "solve": False})
        self.assertEqual(self.plugin.sent, [("Help", "Result: 4")])


if __name__ == "__main__":
    unittest.main()
