"""Authenticated HTTP API for Nicotine+. All core operations run on its main thread."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid

from pynicotine import __version__ as NICOTINE_VERSION
from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin

VERSION = "0.1.0"
MAX_BODY = 65536


class ApiFailure(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ApiFailure(400, "A UUID is required") from None


def safe_filename(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ApiFailure(400, "Invalid filename")
    parts = re.split(r"[\\/]", value)
    if ".." in parts or "." in parts or not PureWindowsPath(value).name:
        raise ApiFailure(400, "Invalid filename")
    if re.search(r'[<>:"|?*]', PureWindowsPath(value).name):
        raise ApiFailure(400, "Filename needs unsupported sanitization")
    return value


def destination(root, relative):
    if not isinstance(relative, str) or not relative or len(relative) > 512:
        raise ApiFailure(400, "A relative destination is required")
    if PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise ApiFailure(400, "Destination must be relative")
    parts = re.split(r"[\\/]", relative)
    if any(p in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9_. -]+", p) for p in parts):
        raise ApiFailure(400, "Invalid destination")
    folder = root.joinpath(*parts).resolve()
    if not folder.is_relative_to(root.resolve()):
        raise ApiFailure(400, "Destination escapes download directory")
    return folder


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, plugin):
        self.plugin = plugin
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "NicotineAPI"

    def log_message(self, *args):
        pass  # Never log keys, queries or peer filenames.

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_api("GET")

    def do_POST(self):
        self.handle_api("POST")

    def handle_api(self, method):
        plugin = self.server.plugin
        given = self.headers.get("X-API-Key", "")
        if not given:
            authorization = self.headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                given = authorization[7:]
        if not hmac.compare_digest(given.encode(), plugin.api_key.encode()):
            self.send_json(401, {"error": "API key required"})
            return
        try:
            if len(self.path) > 2048:
                raise ApiFailure(414, "URI too long")
            data = None
            if method == "POST":
                if self.headers.get_content_type() != "application/json":
                    raise ApiFailure(415, "Use application/json")
                if self.headers.get("Transfer-Encoding"):
                    raise ApiFailure(400, "Use Content-Length")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ApiFailure(400, "Invalid Content-Length") from None
                if not 0 < length <= MAX_BODY:
                    raise ApiFailure(413, "Request body limit is 64 KiB")
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError()
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ApiFailure(400, "Invalid JSON") from None
                if not isinstance(data, dict):
                    raise ApiFailure(400, "JSON object required")
            status, value = plugin.dispatch(method, urlsplit(self.path).path.rstrip("/"), data)
            self.send_json(status, value)
        except ApiFailure as error:
            self.send_json(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            self.send_json(500, {"error": "Internal API error"})


class Plugin(BasePlugin):
    settings = {"host": "127.0.0.1", "port": 12339, "api_key": "", "download_directory": ""}
    metasettings = {
        "host": {"description": "Listen address (use a private interface for containers)", "type": "str"},
        "port": {"description": "HTTP API port", "type": "int"},
        "api_key": {"description": "API key (at least 32 characters)", "type": "str"},
        "download_directory": {"description": "Completed download root (empty uses Nicotine download directory)", "type": "str"},
    }

    def init(self):
        self.closed = True
        self.server = None
        self.db = None
        self.tokens = {}
        self.expiry_events = {}
        version = re.match(r"^(\d+)\.(\d+)\.(\d+)", NICOTINE_VERSION)
        if not version or tuple(map(int, version.groups())) < (3, 3, 11):
            self.log("HTTP API requires Nicotine+ 3.3.11 or newer")
            return
        self.api_key = self.settings["api_key"]
        if not isinstance(self.api_key, str) or len(self.api_key) < 32:
            self.log("Set an API key of at least 32 characters in plugin settings, then disable/re-enable this plugin")
            return
        directory = self.settings["download_directory"] or self.config.sections["transfers"]["downloaddir"]
        if not directory or not Path(directory).is_absolute():
            self.log("Set an absolute completed download directory")
            return
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        state = Path(self.config.data_folder_path) / "api"
        state.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state / "jobs.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS searches (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.commit()
        try:
            self.server = Server((self.settings["host"], int(self.settings["port"])), self)
        except (OSError, ValueError):
            self.db.close()
            self.db = None
            self.log("Unable to bind HTTP API; check listen address and port")
            return
        # API searches cannot retain network tokens across a process restart.
        for row in self.db.execute("SELECT * FROM searches").fetchall():
            value = json.loads(row["data"])
            if not value["isComplete"]:
                value.update(isComplete=True, state="Completed", responses=[])
                self.store("searches", row["id"], value)
        self.closed = False
        events.connect("file-search-response", self.search_response)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        self.log("HTTP API started; searches and transfers run through the existing Nicotine+ session")

    def store(self, table, ident, value):
        if table not in ("searches", "batches"):
            raise ValueError("Unknown table")
        with self.db:
            self.db.execute(f"INSERT INTO {table}(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (ident, json.dumps(value)))

    def read(self, table, ident):
        if table not in ("searches", "batches"):
            raise ValueError("Unknown table")
        row = self.db.execute(f"SELECT data FROM {table} WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise ApiFailure(404, "Not found")
        return json.loads(row["data"])

    def dispatch(self, method, path, data):
        complete = threading.Event()
        result = []

        def run():
            try:
                if self.closed:
                    raise ApiFailure(503, "Plugin unavailable")
                result.append(self.route(method, path, data))
            except ApiFailure as error:
                result.append(error)
            except Exception:
                result.append(ApiFailure(500, "Core operation failed"))
            finally:
                complete.set()

        events.invoke_main_thread(run)
        if not complete.wait(10):
            # The operation can still complete later. Callers reconcile its UUID.
            raise ApiFailure(503, "Core busy; reconcile request ID before retrying")
        if isinstance(result[0], ApiFailure):
            raise result[0]
        return result[0]

    def connected(self):
        return bool(self.core.users.login_username)

    def route(self, method, path, data):
        if method == "GET" and path == "/api/v1/application/version":
            return 200, VERSION
        if method == "GET" and path == "/api/v1/application":
            return 200, {"client": "nicotine", "nicotineVersion": NICOTINE_VERSION,
                         "server": {"isConnected": self.connected()},
                         "capabilities": ["search", "download-batches", "batch-destination"]}
        if method == "POST" and path == "/api/v1/searches":
            return self.start_search(data)
        if method == "GET" and path.startswith("/api/v1/searches/"):
            ident = identifier(path.removeprefix("/api/v1/searches/"))
            return 200, self.read("searches", ident)
        if method == "POST" and path == "/api/v1/transfers/downloads/batches":
            return self.start_batch(data)
        if method == "GET" and path.startswith("/api/v1/transfers/downloads/batches/"):
            ident = identifier(path.removeprefix("/api/v1/transfers/downloads/batches/"))
            return 200, self.batch_status(ident)
        raise ApiFailure(404, "Endpoint not found")

    def start_search(self, data):
        ident = identifier(data.get("id"))
        query = data.get("searchText")
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ApiFailure(400, "Search text must contain 1 to 256 characters")
        try:
            existing = self.read("searches", ident)
        except ApiFailure:
            existing = None
        if existing is not None:
            if existing["searchText"] != query:
                raise ApiFailure(409, "Search UUID belongs to a different query")
            return 200, existing
        if not self.connected():
            raise ApiFailure(503, "Nicotine+ is disconnected")
        if len(self.tokens) >= 3:
            raise ApiFailure(429, "At most three API searches can run at once")
        timeout = data.get("searchTimeout", 15000)
        if not isinstance(timeout, int) or not 1000 <= timeout <= 60000:
            raise ApiFailure(400, "Search timeout must be 1000 to 60000 milliseconds")
        value = {"id": ident, "searchText": query, "isComplete": False, "state": "InProgress", "responses": []}
        self.store("searches", ident, value)
        previous = set(self.core.search.searches)
        self.core.search.do_search(query, "global", switch_page=False)
        added = set(self.core.search.searches)-previous
        if len(added) != 1:
            self.store("searches", ident, {**value, "isComplete": True, "state": "Completed"})
            raise ApiFailure(500, "Search token unavailable")
        token = added.pop()
        self.tokens[token] = ident
        self.expiry_events[token] = events.schedule(timeout/1000, self.finish_search, (token,))
        return 201, value

    def search_response(self, message):
        ident = self.tokens.get(message.token)
        if self.closed or ident is None or not message.list:
            return
        value = self.read("searches", ident)
        if len(value["responses"]) >= 100:
            return
        remaining = 10000-sum(len(r["files"]) for r in value["responses"])
        files = []
        for item in message.list[:max(0, remaining)]:
            try:
                _, name, size, _, attributes = item
                safe_filename(name)
                if not isinstance(size, int) or size <= 0:
                    continue
                file = {"filename": name, "size": size}
                for key, number in (("bitRate", 0), ("length", 1), ("sampleRate", 4), ("bitDepth", 5)):
                    if number in attributes:
                        file[key] = attributes[number]
                files.append(file)
            except (ApiFailure, ValueError, TypeError):
                continue
        if not files:
            return
        value["responses"].append({"username": message.username, "files": files,
                                   "queueLength": message.inqueue or 0, "uploadSpeed": message.ulspeed or 0})
        self.store("searches", ident, value)

    def finish_search(self, token):
        ident = self.tokens.pop(token, None)
        self.expiry_events.pop(token, None)
        if self.closed or ident is None:
            return
        value = self.read("searches", ident)
        value.update(isComplete=True, state="Completed")
        self.store("searches", ident, value)
        self.core.search.remove_search(token)

    def start_batch(self, data):
        ident = identifier(data.get("id"))
        try:
            self.read("batches", ident)
        except ApiFailure:
            pass
        else:
            raise ApiFailure(409, "Batch UUID already exists")
        if not self.connected():
            raise ApiFailure(503, "Nicotine+ is disconnected")
        search = self.read("searches", identifier(data.get("searchId")))
        username = data.get("username")
        available = {(f["filename"], f["size"]) for r in search["responses"] if r["username"] == username for f in r["files"]}
        incoming = data.get("files")
        if not isinstance(incoming, list) or not 1 <= len(incoming) <= 250:
            raise ApiFailure(400, "Batch must contain 1 to 250 files")
        files, names = [], set()
        for file in incoming:
            if not isinstance(file, dict):
                raise ApiFailure(400, "Invalid file")
            name = safe_filename(file.get("filename"))
            size = file.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or (name, size) not in available:
                raise ApiFailure(400, "File must match the referenced search response")
            basename = PureWindowsPath(name).name
            if basename.casefold() in names:
                raise ApiFailure(400, "Duplicate file basename")
            names.add(basename.casefold())
            files.append({"filename": name, "size": size, "state": "Pending"})
        options = data.get("options") or {}
        if not isinstance(options, dict):
            raise ApiFailure(400, "Invalid options")
        folder = destination(self.root, options.get("destination"))
        value = {"id": ident, "username": username, "searchId": search["id"],
                 "destination": str(folder.relative_to(self.root)), "transfers": files}
        self.store("batches", ident, value)
        folder.mkdir(parents=True, exist_ok=True)
        for file in files:
            transfer = self.core.downloads.transfers.get(username+file["filename"])
            if transfer is not None and (Path(transfer.folder_path).resolve() != folder or transfer.size != file["size"]):
                file["state"] = "Completed, Rejected"
                self.store("batches", ident, value)
                continue
            file["state"] = "Submitting"
            self.store("batches", ident, value)
            try:
                self.core.downloads.enqueue_download(username, file["filename"], folder_path=str(folder), size=file["size"])
            except Exception:
                file["state"] = "Completed, Errored"
            else:
                file["state"] = "Queued"
            self.store("batches", ident, value)
        return 201, {"batch": self.batch_status(ident)}

    def batch_status(self, ident):
        value = self.read("batches", ident)
        folder = destination(self.root, value["destination"])
        for file in value["transfers"]:
            if file["state"] == "Completed, Rejected":
                continue
            transfer = self.core.downloads.transfers.get(value["username"]+file["filename"])
            path = folder / PureWindowsPath(file["filename"]).name
            if path.is_file() and path.resolve().is_relative_to(folder) and path.stat().st_size == file["size"] and (transfer is None or transfer.status == "Finished"):
                file["state"] = "Completed, Succeeded"
            elif transfer is None:
                # Never resubmit uncertain calls or manually cleared transfers.
                file["state"] = "Completed, Aborted"
            elif transfer.status in ("Cancelled", "Filtered", "Download folder error", "Local file error"):
                file["state"] = "Completed, Errored"
            elif transfer.status == "Finished":
                file["state"] = "Completed, Errored"
            else:
                file["state"] = "Queued" if transfer.status != "Transferring" else "InProgress"
        self.store("batches", ident, value)
        return value

    def download_finished_notification(self, user, virtual_path, real_path):
        if self.closed:
            return
        # Retain completion after Nicotine+ automatically clears finished rows.
        for row in self.db.execute("SELECT id FROM batches").fetchall():
            value = self.read("batches", row["id"])
            if value["username"] == user and any(f["filename"] == virtual_path for f in value["transfers"]):
                self.batch_status(row["id"])

    def disable(self):
        if getattr(self, "closed", True):
            return
        self.closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        events.disconnect("file-search-response", self.search_response)
        for token, event in list(self.expiry_events.items()):
            events.cancel_scheduled(event)
            self.core.search.remove_search(token)
        self.tokens.clear()
        self.expiry_events.clear()
        self.db.close()
        self.db = None

    def shutdown_notification(self):
        self.disable()
