import importlib.util
import io
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_PATH = Path(__file__).parents[1] / "localai" / "__init__.py"


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

    module_name = "localai_test_plugin"
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


class LocalAITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.events = load_plugin_module()

    def setUp(self):
        self.plugin = self.module.Plugin()
        self.plugin.settings = dict(self.module.Plugin.settings)
        self.plugin.echoes = []
        self.plugin.sent = []
        self.plugin.echo_message = self.plugin.echoes.append
        self.plugin.output = self.plugin.echoes.append
        self.plugin.send_public = lambda room, message: self.plugin.sent.append((room, message))

    def test_ollama_request_shape_and_response(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse({"message": {"content": "  hello\nthere "}})

        client = self.module.LocalAIClient("ollama", "http://127.0.0.1:11434/", "test-model")
        with patch.object(self.module, "urlopen", fake_urlopen):
            answer = client.chat("Hi", "Be brief", 64, 20)

        self.assertEqual(answer, "hello there")
        self.assertEqual(captured["url"], "http://127.0.0.1:11434/api/chat")
        self.assertFalse(captured["payload"]["stream"])
        self.assertEqual(captured["payload"]["options"]["num_predict"], 64)
        self.assertEqual(captured["timeout"], 20)

    def test_llamacpp_request_shape_and_api_key(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data)
            captured["authorization"] = request.headers.get("Authorization")
            return FakeResponse({"choices": [{"message": {"content": "answer"}}]})

        client = self.module.LocalAIClient("llamacpp", "http://localhost:8080", "local", "secret")
        with patch.object(self.module, "urlopen", fake_urlopen):
            answer = client.chat("Question", "", 128, 30)

        self.assertEqual(answer, "answer")
        self.assertEqual(captured["url"], "http://localhost:8080/v1/chat/completions")
        self.assertEqual(captured["payload"]["max_tokens"], 128)
        self.assertEqual(captured["authorization"], "Bearer secret")

    def test_invalid_server_and_backend_are_rejected(self):
        with self.assertRaises(self.module.AIError):
            self.module.LocalAIClient("unknown", "http://localhost:1", "model")
        with self.assertRaises(self.module.AIError):
            self.module.LocalAIClient("ollama", "file:///tmp/socket", "model")
        with self.assertRaises(self.module.AIError):
            self.module.LocalAIClient("ollama", "http://localhost:1", "")

    def test_reply_chunking_is_bounded_and_marks_truncation(self):
        text = "word " * 200
        chunks = self.module._chunk_text(text, 200, 2)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[-1].endswith("…"))
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))

    def test_remote_prompts_are_opt_in_and_room_limited(self):
        self.assertIsNone(self.plugin._remote_prompt("!ai hello"))
        self.plugin.settings["allow_remote_requests"] = True
        self.plugin.settings["allowed_rooms"] = "Books, Help"
        self.assertEqual(self.plugin._remote_prompt("!AI   hello "), "hello")
        self.assertTrue(self.plugin._room_allowed("books"))
        self.assertFalse(self.plugin._room_allowed("general"))

    def test_concurrency_limit_rejects_second_request(self):
        release = threading.Event()
        started = threading.Event()

        def blocking_worker(*_args):
            started.set()
            release.wait(1)

        self.plugin._request_worker = blocking_worker
        self.assertTrue(self.plugin._start_request("one", lambda _message: None))
        self.assertTrue(started.wait(1))
        self.assertFalse(self.plugin._start_request("two", lambda _message: None))
        release.set()

    def test_worker_returns_to_main_thread_and_releases_slot(self):
        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def chat(self, **_kwargs):
                return "a useful answer"

        delivered = []
        self.plugin._active_requests = 1
        invocation_count = len(self.events.invocations)
        with patch.object(self.module, "LocalAIClient", FakeClient):
            self.plugin._request_worker("question", delivered.append)

        self.assertEqual(self.plugin._active_requests, 0)
        self.assertEqual(len(self.events.invocations), invocation_count + 1)
        self.assertEqual(delivered, ["a useful answer"])

    def test_ai_command_captures_room_for_async_reply(self):
        captured = {}

        def fake_start(prompt, reply):
            captured["prompt"] = prompt
            reply("response")
            return True

        self.plugin._start_request = fake_start
        self.assertTrue(self.plugin.ai_command(" hello ", room="Help"))
        self.assertEqual(captured["prompt"], "hello")
        self.assertEqual(self.plugin.sent, [("Help", "response")])
        self.assertEqual(self.plugin.echoes, ["Waiting for the local AI model…"])


if __name__ == "__main__":
    unittest.main()
