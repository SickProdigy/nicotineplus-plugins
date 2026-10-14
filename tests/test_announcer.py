import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "announcer" / "__init__.py"


class FakeEvents:
    def __init__(self):
        self.scheduled = []
        self.cancelled = []

    def schedule(self, delay, callback, callback_args=None, repeat=False):
        event_id = len(self.scheduled) + 1
        self.scheduled.append((event_id, delay, callback, callback_args, repeat))
        return event_id

    def cancel_scheduled(self, event_id):
        self.cancelled.append(event_id)


class BasePlugin:
    def __init__(self):
        pass


@pytest.fixture
def module(monkeypatch):
    fake_events = FakeEvents()
    events_module = types.ModuleType("pynicotine.events")
    events_module.events = fake_events
    plugin_module = types.ModuleType("pynicotine.pluginsystem")
    plugin_module.BasePlugin = BasePlugin
    package = types.ModuleType("pynicotine")
    monkeypatch.setitem(sys.modules, "pynicotine", package)
    monkeypatch.setitem(sys.modules, "pynicotine.events", events_module)
    monkeypatch.setitem(sys.modules, "pynicotine.pluginsystem", plugin_module)
    spec = importlib.util.spec_from_file_location("announcer_test_plugin", PLUGIN_PATH)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    loaded._fake_events = fake_events
    return loaded


def make_plugin(module, joined=("Linux", "Books")):
    plugin = module.Plugin()
    plugin.settings = dict(module.Plugin.settings)
    plugin.core = types.SimpleNamespace(chatrooms=types.SimpleNamespace(joined_rooms={name: object() for name in joined}))
    plugin.public_sent = []
    plugin.local_public = []
    plugin.local_private = []
    plugin.outputs = []
    plugin.logs = []
    plugin.echoes = []
    plugin.send_public = lambda room, message: plugin.public_sent.append((room, message))
    plugin.echo_public = lambda room, message, **kwargs: plugin.local_public.append((room, message, kwargs))
    plugin.echo_private = lambda user, message, **kwargs: plugin.local_private.append((user, message, kwargs))
    plugin.output = plugin.outputs.append
    plugin.log = lambda message, *args: plugin.logs.append(message % args if args else message)
    plugin.echo_message = plugin.echoes.append
    return plugin


def run_scheduled(fake_events):
    for _event_id, _delay, callback, args, _repeat in list(fake_events.scheduled):
        callback(*args)


def test_group_parser_normalizes_names_and_rooms(module):
    groups = module._parse_groups(json.dumps({"OpenSource": [" Linux ", "linux", "Books"]}))
    assert groups == {"opensource": ["Linux", "Books"]}
    with pytest.raises(module.AnnouncementError):
        module._parse_groups("[]")
    with pytest.raises(module.AnnouncementError):
        module._parse_groups("not-json")


def test_preview_is_local_and_does_not_schedule(module):
    plugin = make_plugin(module)
    plugin.settings["default_rooms"] = "linux, Missing"
    assert plugin.announce_command("Try this app", room="Lobby") is True
    assert plugin.public_sent == []
    assert module._fake_events.scheduled == []
    assert plugin.local_public[0][0] == "Lobby"
    assert "PREVIEW to Linux" in plugin.local_public[0][1]
    assert "[Open Source] Try this app" in plugin.local_public[0][1]


def test_enabled_group_schedules_only_joined_rooms_with_stagger(module):
    plugin = make_plugin(module)
    plugin.settings.update({
        "sending_enabled": True,
        "interval_seconds": 3,
        "room_groups_json": '{"media":["Books","Missing","Linux"]}',
    })
    assert plugin.announce_group_command("media Read freely", user="owner") is True
    assert [item[1] for item in module._fake_events.scheduled] == [3, 6]
    assert plugin.public_sent == []
    run_scheduled(module._fake_events)
    assert plugin.public_sent == [
        ("Books", "[Open Source] Read freely"),
        ("Linux", "[Open Source] Read freely"),
    ]
    assert plugin.local_private[-1][1] == "Announcement job finished."


def test_all_targets_are_deduplicated(module):
    plugin = make_plugin(module)
    plugin.settings.update({
        "default_rooms": "Linux",
        "room_groups_json": '{"one":["linux","Books"],"two":["BOOKS"]}',
    })
    assert plugin.announce_all_command("Hello", room="Lobby") is True
    assert "PREVIEW to Linux, Books" in plugin.local_public[-1][1]


def test_duplicate_actual_job_is_blocked(module):
    plugin = make_plugin(module)
    plugin.settings.update({"sending_enabled": True, "default_rooms": "Linux"})
    assert plugin.announce_command("Same", room="Lobby") is True
    assert plugin.announce_command("Same", room="Lobby") is False
    assert len(module._fake_events.scheduled) == 1
    assert "cooldown" in plugin.local_public[-1][1]


def test_cancel_clears_pending_events(module):
    plugin = make_plugin(module)
    plugin.settings.update({"sending_enabled": True, "default_rooms": "Linux, Books"})
    plugin.announce_command("Queued", room="Lobby")
    assert plugin.announce_cancel_command("") is True
    assert module._fake_events.cancelled == [1, 2]
    assert plugin._scheduled == {}
    assert plugin.echoes[-1] == "Cancelled 2 queued announcement sends."


def test_departed_room_is_skipped_at_send_time(module):
    plugin = make_plugin(module, joined=("Linux",))
    plugin.settings.update({"sending_enabled": True, "default_rooms": "Linux"})
    plugin.announce_command("Later", room="Lobby")
    plugin.core.chatrooms.joined_rooms.clear()
    run_scheduled(module._fake_events)
    assert plugin.public_sent == []
    assert "no longer joined" in plugin.logs[-1]


def test_empty_and_oversize_messages_are_rejected(module):
    plugin = make_plugin(module)
    plugin.settings.update({"default_rooms": "Linux", "max_message_chars": 100})
    assert plugin.announce_command("  ", room="Lobby") is False
    assert plugin.announce_command("x" * 100, room="Lobby") is False
    assert "too long" in plugin.local_public[-1][1]
