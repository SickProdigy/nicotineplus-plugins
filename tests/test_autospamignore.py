import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_PATH = Path(__file__).parents[1] / "autospamignore" / "__init__.py"


class FakeNetworkFilter:
    def __init__(self):
        self.ignored = []

    def is_user_ignored(self, user):
        return user in self.ignored

    def ignore_user(self, user):
        self.ignored.append(user)


class FakeNotifications:
    def __init__(self):
        self.shown = []

    def show_chatroom_notification(self, room, message, **kwargs):
        self.shown.append((room, message, kwargs))


class FakeBasePlugin:
    core = types.SimpleNamespace(
        users=types.SimpleNamespace(login_username="owner"),
        buddies=types.SimpleNamespace(users={}),
        chatrooms=types.SimpleNamespace(private_rooms={}),
        network_filter=FakeNetworkFilter(),
        notifications=FakeNotifications(),
    )

    def log(self, message, args=None):
        self.logs.append((message, args))


def load_plugin_module():
    pynicotine = types.ModuleType("pynicotine")
    pluginsystem_module = types.ModuleType("pynicotine.pluginsystem")
    pluginsystem_module.BasePlugin = FakeBasePlugin
    module_name = "autospamignore_test_plugin"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "pynicotine": pynicotine,
        "pynicotine.pluginsystem": pluginsystem_module,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module


class AutoSpamIgnoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin_module()

    def setUp(self):
        network_filter = FakeNetworkFilter()
        self.plugin = self.module.Plugin()
        self.plugin.settings = dict(self.module.Plugin.settings)
        self.plugin.core = types.SimpleNamespace(
            users=types.SimpleNamespace(login_username="owner", countries={}),
            buddies=types.SimpleNamespace(users={}),
            chatrooms=types.SimpleNamespace(private_rooms={}),
            network_filter=network_filter,
            notifications=FakeNotifications(),
        )
        self.plugin.logs = []
        self.plugin.echoes = []
        self.plugin.echo_message = self.plugin.echoes.append
        self.plugin.local_room_messages = []
        self.plugin.echo_public = lambda room, message, **kwargs: self.plugin.local_room_messages.append((room, message, kwargs))
        self.network_filter = network_filter
        self.plugin.settings["message_threshold"] = 3

    def send(self, room="Books", user="spammer", timestamp=1.0):
        with patch.object(self.module.time, "monotonic", return_value=timestamp):
            self.plugin.incoming_public_chat_notification(room, user, "message")

    def test_log_only_mode_detects_without_ignoring(self):
        self.send(timestamp=1)
        self.send(timestamp=2)
        self.send(timestamp=3)
        self.send(timestamp=4)
        self.assertEqual(self.network_filter.ignored, [])
        self.assertEqual(len(self.plugin.logs), 1)
        self.assertIn("Would ignore", self.plugin.logs[0][0])

    def test_enforcement_ignores_on_threshold(self):
        self.plugin.settings["enforce_ignores"] = True
        self.send(timestamp=1)
        self.send(timestamp=2)
        self.send(timestamp=3)
        self.assertEqual(self.network_filter.ignored, ["spammer"])

    def test_rolling_window_discards_old_messages(self):
        self.plugin.settings["enforce_ignores"] = True
        self.send(timestamp=1)
        self.send(timestamp=2)
        self.send(timestamp=62)
        self.assertEqual(self.network_filter.ignored, [])
        self.send(timestamp=63)
        self.send(timestamp=64)
        self.assertEqual(self.network_filter.ignored, ["spammer"])

    def test_per_room_scope_counts_independently(self):
        self.plugin.settings["enforce_ignores"] = True
        self.send(room="Books", timestamp=1)
        self.send(room="Books", timestamp=2)
        self.send(room="Music", timestamp=3)
        self.assertEqual(self.network_filter.ignored, [])

    def test_all_rooms_scope_combines_counts(self):
        self.plugin.settings["enforce_ignores"] = True
        self.plugin.settings["scope"] = "all_rooms"
        self.send(room="Books", timestamp=1)
        self.send(room="Music", timestamp=2)
        self.send(room="Help", timestamp=3)
        self.assertEqual(self.network_filter.ignored, ["spammer"])

    def test_room_allowlist_and_explicit_exemption(self):
        self.plugin.settings["enforce_ignores"] = True
        self.plugin.settings["monitored_rooms"] = "Books"
        self.plugin.settings["exempt_users"] = "Trusted"
        for timestamp in range(1, 5):
            self.send(room="Music", timestamp=timestamp)
            self.send(room="Books", user="trusted", timestamp=timestamp)
        self.assertEqual(self.network_filter.ignored, [])
        self.assertEqual(self.plugin._messages, {})

    def test_owner_buddy_and_private_room_staff_are_exempt(self):
        private_room = types.SimpleNamespace(owner="roomowner", operators={"operator"})
        self.plugin.core.buddies.users = {"Buddy": object()}
        self.plugin.core.chatrooms.private_rooms = {"Private": private_room}
        for username in ("owner", "buddy", "roomowner", "operator"):
            self.assertTrue(self.plugin._user_exempt("Private", username))

    def test_reset_clears_user_without_unignoring(self):
        self.send(user="Spammer", timestamp=1)
        self.send(user="Other", timestamp=2)
        self.network_filter.ignored.append("Spammer")
        self.plugin.spam_reset_command("spammer")
        self.assertTrue(all(key[1] != "spammer" for key in self.plugin._messages))
        self.assertIn("Spammer", self.network_filter.ignored)
        self.assertIn("Existing ignores were not changed", self.plugin.echoes[-1])

    def test_status_reports_default_rule(self):
        self.plugin.settings["message_threshold"] = 20
        self.plugin.spam_status_command("")
        self.assertIn("20 messages/60s", self.plugin.echoes[-1])
        self.assertIn("log-only", self.plugin.echoes[-1])


    def test_detection_shows_local_and_desktop_notifications(self):
        self.send(timestamp=1)
        self.send(timestamp=2)
        self.send(timestamp=3)
        self.assertEqual(len(self.plugin.local_room_messages), 1)
        room, message, kwargs = self.plugin.local_room_messages[0]
        self.assertEqual(room, "Books")
        self.assertIn("Would ignore spammer", message)
        self.assertEqual(kwargs["message_type"], "local")
        self.assertEqual(len(self.plugin.core.notifications.shown), 1)
        self.assertTrue(self.plugin.core.notifications.shown[0][2]["high_priority"])

    def test_blocked_country_uses_observed_country_code(self):
        self.plugin.settings["blocked_countries"] = "RU, CN"
        self.plugin.core.users.countries["spammer"] = "RU"
        self.send(timestamp=1)
        self.assertEqual(len(self.plugin.logs), 1)
        self.assertIn("country code RU", self.plugin.logs[0][0])
        self.assertEqual(self.network_filter.ignored, [])
        self.plugin.settings["enforce_ignores"] = True
        self.send(timestamp=2)
        self.assertEqual(self.network_filter.ignored, ["spammer"])

    def test_phrase_contains_and_exact_modes(self):
        self.plugin.settings["blocked_phrases"] = "free money, click here"
        self.assertEqual(self.plugin._matching_phrase("Get FREE MONEY now"), "free money")
        self.plugin.settings["phrase_match_mode"] = "exact"
        self.assertIsNone(self.plugin._matching_phrase("Get free money now"))
        self.assertEqual(self.plugin._matching_phrase("  FREE   MONEY "), "free money")

    def test_exemptions_apply_before_country_and_phrase_rules(self):
        self.plugin.settings["enforce_ignores"] = True
        self.plugin.settings["blocked_countries"] = "US"
        self.plugin.settings["blocked_phrases"] = "blocked words"
        self.plugin.settings["exempt_users"] = "trusted"
        self.plugin.core.users.countries["trusted"] = "US"
        with patch.object(self.module.time, "monotonic", return_value=1):
            self.plugin.incoming_public_chat_notification("Books", "trusted", "blocked words")
        self.assertEqual(self.network_filter.ignored, [])
        self.assertEqual(self.plugin.logs, [])

    def test_notifications_can_be_disabled(self):
        self.plugin.settings["notify_local_room"] = False
        self.plugin.settings["notify_desktop"] = False
        self.send(timestamp=1)
        self.send(timestamp=2)
        self.send(timestamp=3)
        self.assertEqual(self.plugin.local_room_messages, [])
        self.assertEqual(self.plugin.core.notifications.shown, [])
        self.assertEqual(len(self.plugin.logs), 1)

if __name__ == "__main__":
    unittest.main()
