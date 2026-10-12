"""Offline compatibility smoke test against an installed/unpacked Nicotine+ core.

Usage: python -B tests/verify_nicotine_core.py --source /path/to/nicotine-plus
Only the local HTTP listener is used. Soulseek network sends are replaced.
"""
import argparse
import builtins
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.request import Request, urlopen
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--arr-scripts", type=Path, help="Also run the sibling Arr worker against this real plugin/core")
    args = parser.parse_args()
    if args.source:
        sys.path.insert(0, str(args.source))
    builtins._ = lambda value: value
    builtins.ngettext = lambda one, many, number: one if number == 1 else many
    from pynicotine import __version__
    from pynicotine.config import config
    from pynicotine.core import core
    from pynicotine.events import events
    from pynicotine.slskmessages import FileSearchResponse

    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary)
        # Core uses __slots__, so patch methods on its class rather than instance.
        for name in ("send_message_to_server", "send_message_to_peer", "send_message_to_network_thread"):
            stack.enter_context(patch.object(type(core), name, return_value=None))
        config.set_data_folder(str(root/"data"))
        config.set_config_file(str(root/"config/config"))
        core.init_components(enabled_components={"users", "search", "downloads", "network_filter", "pluginhandler", "statistics"}, isolated_mode=True)
        core.users.login_username = "local-offline-test"
        source = Path(__file__).parents[1]/"api/__init__.py"
        spec = importlib.util.spec_from_file_location("plugin_api", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = module.Plugin()
        plugin.settings = {**module.Plugin.settings, "port": 0, "api_key": "local-test-key-"+"a"*32,
                           "download_directory": str(root/"downloads")}
        plugin.config, plugin.core = config, core
        plugin.init()
        assert plugin.server is not None
        base = "http://127.0.0.1:"+str(plugin.server.server_address[1])
        pool = ThreadPoolExecutor(max_workers=1)

        def request(path, data=None):
            def send():
                body = json.dumps(data).encode() if data is not None else None
                with urlopen(Request(base+path, data=body, headers={"X-API-Key": plugin.api_key, "Content-Type": "application/json"}), timeout=15) as response:
                    return json.load(response)
            pending = pool.submit(send)
            until = time.monotonic()+15
            while not pending.done():
                if time.monotonic() > until:
                    raise RuntimeError("Main-thread dispatch timed out")
                events.process_thread_events()
                time.sleep(.01)
            return pending.result()

        try:
            ident = str(uuid.uuid4())
            request("/api/v1/searches", {"id": ident, "searchText": "Artist Album"})
            assert len(plugin.tokens) == 1
            token = next(iter(plugin.tokens))
            message = FileSearchResponse(search_username="test-peer", token=token,
                                         shares=[("Artist\\Album\\01 Song.flac", 4, None, 180)],
                                         freeulslots=True, ulspeed=1024, inqueue=0)
            parsed = FileSearchResponse()
            # Normally supplied by NetworkThread; network sends are disabled here.
            parsed.allowed_responses = {token}
            parsed.parse_network_message(message.make_network_message())
            parsed.username, parsed.addr = "test-peer", ("127.0.0.1", 50300)
            assert parsed.list and len(parsed.list[0]) == 5
            events.emit("file-search-response", parsed)
            plugin.finish_search(token)
            result = request("/api/v1/searches/"+ident)
            assert len(result["responses"]) == 1, result
            batch = str(uuid.uuid4())
            file = result["responses"][0]["files"][0]
            request("/api/v1/transfers/downloads/batches", {
                "id": batch, "searchId": ident, "username": "test-peer", "files": [file],
                "options": {"destination": "arr-scripts/lidarr/"+batch}})
            transfer = core.downloads.transfers["test-peer"+file["filename"]]
            assert transfer.folder_path == str(plugin.root/"arr-scripts/lidarr"/batch)
            (Path(transfer.folder_path)/"01 Song.flac").write_bytes(b"test")
            transfer.status = "Finished"
            result = request("/api/v1/transfers/downloads/batches/"+batch)
            assert result["transfers"][0]["state"] == "Completed, Succeeded", result
            transfer.status,transfer.current_byte_offset = "User logged off",2
            (Path(transfer.folder_path)/"01 Song.flac").unlink()
            result = request("/api/v1/transfers/downloads/batches/"+batch)
            assert result["peerOffline"] and result["transfers"][0]["bytesTransferred"] == 2
            config.sections["transfers"]["incompletedir"] = str(root/"incomplete")
            (root/"incomplete").mkdir()
            partial = Path(core.downloads.get_incomplete_download_file_path("test-peer",file["filename"]))
            partial.write_bytes(b"te")
            request("/api/v1/transfers/downloads/batches/"+batch+"/cancel",{"cleanupIncomplete":True})
            assert transfer.status == "Paused" and not partial.exists()
            request("/api/v1/transfers/downloads/batches/"+batch+"/clear",{})
            assert "test-peer"+file["filename"] not in core.downloads.transfers
            print(f"Actual Nicotine+ {__version__}: main-thread HTTP, search parsing, enqueue/completion, offline/progress status, scoped abort/clear and partial cleanup passed (Soulseek network sends disabled).")
            if args.arr_scripts:
                verify_arr(args.arr_scripts, plugin, core, events, base, FileSearchResponse)
        finally:
            plugin.disable()
            pool.shutdown()


def verify_arr(repository, plugin, core, events, base, message_type):
    spec = importlib.util.spec_from_file_location("arr_soulseek", repository/"universal/python/Soulseek.py")
    worker_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker_module)

    class Library:
        def __init__(self, app, filename):
            self.app, self.filename = app, filename
            self.complete, self.commands = False, []

        def call(self, path, data=None, **query):
            prefix = "api/v1/" if self.app == "lidarr" else "api/v3/"
            endpoint = path.removeprefix(prefix)
            record = {"id": 1, "monitored": True, "hasFile": self.complete}
            if endpoint == "queue":
                return {"records": [], "totalRecords": 0}
            if endpoint == "movie":
                return [{**record, "title": "Movie", "year": 2020, "digitalRelease": "2020-01-01"}]
            if endpoint == "wanted/missing":
                if self.complete:
                    return {"records": [], "totalRecords": 0}
                if self.app == "sonarr":
                    record.update(seriesId=2, seasonNumber=1, episodeNumber=2, airDateUtc="2020-01-01",
                                  series={"id": 2, "title": "Series", "monitored": True, "seriesType": "standard"})
                else:
                    record.update(title="Album", artistId=2, releaseDate="2020-01-01",
                                  artist={"id": 2, "artistName": "Artist", "monitored": True})
                return {"records": [record], "totalRecords": 1}
            if endpoint == "album/1":
                return {"releases": [{"id": 9, "monitored": True}]}
            if endpoint == "track":
                return [{"id": 3, "title": "Song", "hasFile": self.complete}]
            if endpoint == "manualimport":
                item = {"path": str(Path(query["folder"])/self.filename), "quality": {"quality": {"id": 1}}, "rejections": []}
                if self.app == "radarr":
                    item["movie"] = {"id": 1}
                elif self.app == "sonarr":
                    item.update(series={"id": 2}, episodes=[{"id": 1}])
                else:
                    item.update(artist={"id": 2}, album={"id": 1}, albumReleaseId=9, tracks=[{"id": 3}])
                return [item]
            if endpoint == "command":
                self.commands.append(data)
                self.complete = True
                return {"id": 123}
            raise AssertionError(endpoint)

    for app, filename, remote in (("lidarr", "01 Song.flac", "Artist\\Album\\01 Song.flac"),
                                  ("sonarr", "Series.S01E02.mkv", "Series.S01E02.mkv"),
                                  ("radarr", "Movie.2020.1080p.mkv", "Movie.2020.1080p.mkv")):
        library = Library(app, filename)
        now = [1700000000]
        # The Worker owns its SQLite connection on its executor thread; the
        # Nicotine plugin/core retains its own main-thread connection.
        with tempfile.TemporaryDirectory() as temporary, ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(worker_module.Worker, app, library,
                                 worker_module.SoulseekApi(base, plugin.api_key, "nicotine"),
                                 temporary, plugin.root, 0, "soulseek", False, lambda: now[0]).result()
            try:
                for _ in range(6):
                    now[0] += 301
                    pending = pool.submit(worker.tick)
                    until = time.monotonic()+15
                    while not pending.done():
                        if time.monotonic() > until:
                            raise RuntimeError("Worker/plugin integration timed out")
                        events.process_thread_events()
                        time.sleep(.01)
                    pending.result()
                    for token in list(plugin.tokens):
                        message = message_type(search_username=f"test-peer-{app}", token=token,
                                               shares=[(remote, 4, None, 180)], freeulslots=True, ulspeed=1024, inqueue=0)
                        parsed = message_type()
                        parsed.allowed_responses = {token}
                        parsed.parse_network_message(message.make_network_message())
                        parsed.username, parsed.addr = f"test-peer-{app}", ("127.0.0.1", 50300)
                        events.emit("file-search-response", parsed)
                        plugin.finish_search(token)
                    for transfer in core.downloads.transfers.values():
                        if f"arr-scripts{__import__('os').sep}{app}" in transfer.folder_path:
                            (Path(transfer.folder_path)/filename).write_bytes(b"test")
                            transfer.status = "Finished"
                state = pool.submit(lambda: worker.db.execute("SELECT state FROM jobs WHERE id=1").fetchone()[0]).result()
                assert state == "done", (app, state)
                assert len(library.commands) == 1 and library.commands[0]["importMode"] == "copy"
                print(f"{app}: real worker -> HTTP plugin -> actual Nicotine core -> simulated completed file -> validated Arr import payload passed.")
            finally:
                pool.submit(worker.db.close).result()


if __name__ == "__main__":
    main()
