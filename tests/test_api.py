import importlib.util
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid


class Events:
    def __init__(self):
        self.calls = queue.Queue()
        self.scheduled = {}
        self.callbacks = {}

    def invoke_main_thread(self, callback):
        self.calls.put(callback)

    def connect(self, name, callback):
        self.callbacks[name] = callback

    def disconnect(self, name, callback):
        self.callbacks.pop(name, None)

    def schedule(self, delay, callback, args):
        ident = len(self.scheduled)+1
        self.scheduled[ident] = (callback, args)
        return ident

    def cancel_scheduled(self, ident):
        self.scheduled.pop(ident, None)


fake_events = Events()
nicotine = ModuleType("pynicotine")
nicotine.__version__ = "3.3.11"
event_module = ModuleType("pynicotine.events")
event_module.events = fake_events
plugins = ModuleType("pynicotine.pluginsystem")
plugins.BasePlugin = type("BasePlugin", (), {"log": lambda *args: None})
previous = {name: sys.modules.get(name) for name in ("pynicotine", "pynicotine.events", "pynicotine.pluginsystem")}
sys.modules.update({"pynicotine": nicotine, "pynicotine.events": event_module, "pynicotine.pluginsystem": plugins})
spec = importlib.util.spec_from_file_location("api_plugin", Path(__file__).parents[1]/"api/__init__.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
for name, module in previous.items():
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module


class Core:
    def __init__(self):
        self.thread = threading.get_ident()
        self.users = SimpleNamespace(login_username="local-test-user")
        self.search = SimpleNamespace(searches={}, do_search=self.search_for, remove_search=self.remove,
                                      add_allowed_token=lambda token: None)
        self.downloads = SimpleNamespace(transfers={}, enqueue_download=self.enqueue)
        self.enqueued = []

    def search_for(self, query, mode, switch_page):
        assert threading.get_ident() == self.thread
        assert mode == "global"
        self.search.searches[len(self.search.searches)+1] = query
        return None  # Nicotine+ 3.3.11 behavior, deliberately not a token.

    def remove(self, token):
        self.search.searches.pop(token, None)

    def enqueue(self, username, filename, folder_path, size):
        assert threading.get_ident() == self.thread
        self.enqueued.append(filename)
        self.downloads.transfers[username+filename] = SimpleNamespace(folder_path=folder_path, size=size, status="Queued")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.core = Core()
        self.plugin = self.make_plugin()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.base = f"http://127.0.0.1:{self.plugin.server.server_address[1]}"

    def make_plugin(self):
        plugin = api.Plugin()
        plugin.settings = {**api.Plugin.settings, "port": 0, "api_key": "test-key-"+"a"*32,
                           "download_directory": str(self.root/"downloads")}
        plugin.core = self.core
        plugin.config = SimpleNamespace(data_folder_path=str(self.root/"data"), sections={"transfers": {"downloaddir": str(self.root/"downloads")}})
        plugin.init()
        return plugin

    def tearDown(self):
        self.plugin.disable()
        self.pool.shutdown()
        self.temp.cleanup()

    def request(self, path, data=None, key=True, raw=None):
        body = json.dumps(data).encode() if data is not None else raw
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-API-Key"] = self.plugin.api_key
        def send():
            try:
                with urlopen(Request(self.base+path, data=body, headers=headers), timeout=15) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)
        pending = self.pool.submit(send)
        until = time.monotonic()+15
        while not pending.done():
            if time.monotonic() > until:
                self.fail("HTTP dispatch timed out")
            try:
                fake_events.calls.get(timeout=.01)()
            except queue.Empty:
                pass
        return pending.result()

    def search(self):
        ident = str(uuid.uuid4())
        self.request("/api/v1/searches", {"id": ident, "searchText": "Artist Album"})
        token = next(iter(self.plugin.tokens))
        self.plugin.search_response(SimpleNamespace(token=token, username="peer", list=[(1, "Artist\\Album\\01 Song.flac", 4, None, {0: 320, 1: 180})], inqueue=0, ulspeed=123))
        self.plugin.finish_search(token)
        return ident

    def batch(self):
        search = self.search()
        batch = str(uuid.uuid4())
        data = {"id": batch, "searchId": search, "username": "peer", "files": [{"filename": "Artist\\Album\\01 Song.flac", "size": 4}], "options": {"destination": "arr-scripts/lidarr/"+batch}}
        return batch, data

    def test_auth_required_on_reads_and_writes(self):
        self.assertEqual(self.request("/api/v1/application", key=False)[0], 401)
        self.assertEqual(self.request("/api/v1/searches", {"id": str(uuid.uuid4())}, key=False)[0], 401)

    def test_connection_capabilities_and_version(self):
        code, state = self.request("/api/v1/application")
        self.assertEqual(code, 200)
        self.assertTrue(state["server"]["isConnected"])
        self.assertIn("download-batches", state["capabilities"])
        self.assertEqual(self.request("/api/v1/application/version"), (200, "0.1.4"))

    def test_search_captures_token_when_core_returns_none(self):
        ident = self.search()
        code, result = self.request("/api/v1/searches/"+ident)
        self.assertEqual(code, 200)
        self.assertTrue(result["isComplete"])
        self.assertEqual(result["responses"][0]["files"][0]["length"], 180)
        self.assertFalse(self.core.search.searches)

    def test_unknown_tokens_and_locked_files_are_ignored(self):
        ident = str(uuid.uuid4())
        self.request("/api/v1/searches", {"id": ident, "searchText": "Song"})
        self.plugin.search_response(SimpleNamespace(token=999, list=[(1, "secret", 4, None, {})]))
        self.assertEqual(self.request("/api/v1/searches/"+ident)[1]["responses"], [])

    def test_duplicate_search_id_is_idempotent(self):
        ident = self.search()
        self.assertEqual(self.request("/api/v1/searches", {"id": ident, "searchText": "Artist Album"})[0], 200)
        self.assertEqual(self.request("/api/v1/searches", {"id": ident, "searchText": "Different"})[0], 409)

    def test_disconnected_search_does_not_start(self):
        self.core.users.login_username = None
        self.assertEqual(self.request("/api/v1/searches", {"id": str(uuid.uuid4()), "searchText": "Song"})[0], 503)
        self.assertFalse(self.core.search.searches)

    def test_search_concurrency_limit(self):
        for _ in range(3):
            self.assertEqual(self.request("/api/v1/searches", {"id": str(uuid.uuid4()), "searchText": "Song"})[0], 201)
        self.assertEqual(self.request("/api/v1/searches", {"id": str(uuid.uuid4()), "searchText": "Song"})[0], 429)

    def test_batch_duplicate_does_not_requeue(self):
        ident, data = self.batch()
        self.assertEqual(self.request("/api/v1/transfers/downloads/batches", data)[0], 201)
        self.assertEqual(self.request("/api/v1/transfers/downloads/batches", data)[0], 409)
        self.assertEqual(len(self.core.enqueued), 1)
        self.assertEqual(self.request("/api/v1/transfers/downloads/batches/"+ident)[0], 200)

    def test_unobserved_file_cannot_be_queued(self):
        _, data = self.batch()
        data["files"][0]["size"] = 999
        self.assertEqual(self.request("/api/v1/transfers/downloads/batches", data)[0], 400)
        self.assertFalse(self.core.enqueued)

    def test_offline_batch_reports_status_and_progress(self):
        ident,data = self.batch()
        self.request("/api/v1/transfers/downloads/batches",data)
        transfer = self.core.downloads.transfers["peer"+data["files"][0]["filename"]]
        transfer.status,transfer.current_byte_offset = "User logged off",2
        result=self.request("/api/v1/transfers/downloads/batches/"+ident)[1]
        self.assertTrue(result["peerOffline"])
        self.assertEqual(result["transfers"][0]["bytesTransferred"],2)
        transfer.status="Transferring"
        self.assertFalse(self.request("/api/v1/transfers/downloads/batches/"+ident)[1]["peerOffline"])

    def test_cancel_is_scoped_and_closes_owned_partial_before_removal(self):
        ident,data=self.batch()
        self.request("/api/v1/transfers/downloads/batches",data)
        transfer=self.core.downloads.transfers["peer"+data["files"][0]["filename"]]
        transfer.status="Transferring"
        root=self.root/"incomplete"
        root.mkdir()
        partial=root/"owned-partial"
        partial.write_bytes(b"te")
        personal=root/"personal-partial"
        personal.write_bytes(b"personal")
        self.core.downloads.get_incomplete_download_folder=lambda:str(root)
        self.core.downloads.get_incomplete_download_file_path=lambda *args:str(partial)
        def abort(rows):
            self.assertEqual(rows,[transfer])
            self.assertTrue(partial.exists())
            transfer.status="Paused"
        self.core.downloads.abort_downloads=abort
        path="/api/v1/transfers/downloads/batches/"+ident+"/cancel"
        self.assertEqual(self.request(path,{"cleanupIncomplete":True})[0],200)
        self.assertFalse(partial.exists())
        self.assertTrue(personal.exists())

    def test_cancel_detaches_foreign_download_without_mutation(self):
        ident,data=self.batch()
        self.request("/api/v1/transfers/downloads/batches",data)
        transfer=self.core.downloads.transfers["peer"+data["files"][0]["filename"]]
        transfer.folder_path=str(self.root/"personal")
        self.core.downloads.abort_downloads=lambda rows:self.fail("foreign transfer cancelled")
        path="/api/v1/transfers/downloads/batches/"+ident+"/cancel"
        self.assertEqual(self.request(path,{})[0],200)
        self.assertIs(self.core.downloads.transfers["peer"+data["files"][0]["filename"]],transfer)

    def test_clear_batch_refuses_active_but_detaches_foreign_transfer(self):
        ident,data = self.batch()
        self.request("/api/v1/transfers/downloads/batches",data)
        key = "peer"+data["files"][0]["filename"]
        transfer = self.core.downloads.transfers[key]
        self.core.downloads.clear_downloads = lambda rows: self.fail("must not clear")
        self.assertEqual(self.request("/api/v1/transfers/downloads/batches/"+ident+"/clear",{})[0],409)
        transfer.status = "Finished"
        transfer.folder_path = str(self.root/"personal")
        self.assertEqual(self.request("/api/v1/transfers/downloads/batches/"+ident+"/clear",{})[0],200)
        self.assertIs(self.core.downloads.transfers[key],transfer)

    def test_clear_batch_refuses_changed_owned_transfer(self):
        ident,data = self.batch()
        self.request("/api/v1/transfers/downloads/batches",data)
        transfer = self.core.downloads.transfers["peer"+data["files"][0]["filename"]]
        transfer.status = "Finished"
        transfer.size += 1
        self.core.downloads.clear_downloads = lambda rows: self.fail("must not clear")
        self.assertEqual(self.request("/api/v1/transfers/downloads/batches/"+ident+"/clear",{})[0],409)

    def test_clear_batch_is_scoped_idempotent_and_retains_files(self):
        ident,data = self.batch()
        self.request("/api/v1/transfers/downloads/batches",data)
        key = "peer"+data["files"][0]["filename"]
        transfer = self.core.downloads.transfers[key]
        transfer.status = "Finished"
        file = Path(transfer.folder_path)/"01 Song.flac"
        file.write_bytes(b"test")
        personal = SimpleNamespace(status="Transferring")
        self.core.downloads.transfers["personal"] = personal
        def clear(rows):
            self.assertEqual(rows,[transfer])
            self.core.downloads.transfers.pop(key)
        self.core.downloads.clear_downloads = clear
        path = "/api/v1/transfers/downloads/batches/"+ident+"/clear"
        self.assertEqual(self.request(path,{})[0],200)
        self.assertEqual(self.request(path,{})[0],200)
        self.assertTrue(file.exists())
        self.assertIs(self.core.downloads.transfers["personal"],personal)

    def test_destination_traversal_cannot_escape_root(self):
        for value in ("../escape", "/absolute", "C:\\escape", "..\\escape", "safe/../../escape"):
            _, data = self.batch()
            data["options"]["destination"] = value
            self.assertEqual(self.request("/api/v1/transfers/downloads/batches", data)[0], 400)
        self.assertFalse(self.core.enqueued)

    def test_conflicting_gui_download_is_preserved(self):
        _, data = self.batch()
        filename = data["files"][0]["filename"]
        original = SimpleNamespace(folder_path=str(self.root/"manual"), size=4, status="Transferring")
        self.core.downloads.transfers["peer"+filename] = original
        _, result = self.request("/api/v1/transfers/downloads/batches", data)
        self.assertEqual(result["batch"]["transfers"][0]["state"], "Completed, Rejected")
        self.assertIs(self.core.downloads.transfers["peer"+filename], original)
        self.assertFalse(self.core.enqueued)

    def test_completion_survives_cleared_gui_transfer_and_plugin_restart(self):
        ident, data = self.batch()
        self.request("/api/v1/transfers/downloads/batches", data)
        folder = self.plugin.root/data["options"]["destination"]
        (folder/"01 Song.flac").write_bytes(b"test")
        self.core.downloads.transfers.clear()
        self.plugin.disable()
        self.plugin = self.make_plugin()
        self.base = f"http://127.0.0.1:{self.plugin.server.server_address[1]}"
        result = self.request("/api/v1/transfers/downloads/batches/"+ident)[1]
        self.assertEqual(result["transfers"][0]["state"], "Completed, Succeeded")
        self.assertEqual(len(self.core.enqueued), 1)

    def test_missing_or_partial_file_is_not_success(self):
        ident, data = self.batch()
        self.request("/api/v1/transfers/downloads/batches", data)
        folder = self.plugin.root/data["options"]["destination"]
        (folder/"01 Song.flac").write_bytes(b"bad")
        self.core.downloads.transfers["peer"+data["files"][0]["filename"]].status = "Finished"
        result = self.request("/api/v1/transfers/downloads/batches/"+ident)[1]
        self.assertEqual(result["transfers"][0]["state"], "Completed, Errored")

    def test_manually_repaired_own_transfer_can_be_reconciled(self):
        ident, data = self.batch()
        self.request("/api/v1/transfers/downloads/batches", data)
        transfer = self.core.downloads.transfers["peer"+data["files"][0]["filename"]]
        transfer.status = "Local file error"
        path = "/api/v1/transfers/downloads/batches/"+ident
        self.assertEqual(self.request(path)[1]["transfers"][0]["state"], "Completed, Errored")
        (Path(transfer.folder_path)/"01 Song.flac").write_bytes(b"test")
        transfer.status = "Finished"
        self.assertEqual(self.request(path)[1]["transfers"][0]["state"], "Completed, Succeeded")

    def test_malformed_json_and_oversized_body_rejected(self):
        self.assertEqual(self.request("/api/v1/searches", raw=b"not json")[0], 400)
        self.assertEqual(self.request("/api/v1/searches", raw=b"a"*65537)[0], 413)


if __name__ == "__main__":
    unittest.main()
