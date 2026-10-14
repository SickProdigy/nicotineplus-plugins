"""Detect and optionally ignore high-volume public-chat spammers."""

from __future__ import annotations

import time
from collections import deque
from typing import Optional

from pynicotine.pluginsystem import BasePlugin


class Plugin(BasePlugin):
    settings = {
        "enforce_ignores": False,
        "message_threshold": 20,
        "window_seconds": 60,
        "scope": "per_room",
        "monitored_rooms": "",
        "exempt_users": "",
        "exempt_buddies": True,
        "exempt_private_room_staff": True,
        "blocked_countries": "",
        "blocked_phrases": "",
        "phrase_match_mode": "contains",
        "notify_local_room": True,
        "notify_desktop": True,
    }
    metasettings = {
        "enforce_ignores": {
            "description": "Add detected spammers to Nicotine+'s ignore list",
            "type": "bool",
        },
        "message_threshold": {
            "description": "Messages required to trigger (3-200)",
            "type": "int",
        },
        "window_seconds": {
            "description": "Rolling detection window in seconds (10-600)",
            "type": "int",
        },
        "scope": {
            "description": "Counting scope: per_room or all_rooms",
            "type": "str",
        },
        "monitored_rooms": {
            "description": "Comma-separated rooms to monitor (empty means every joined room)",
            "type": "str",
        },
        "exempt_users": {
            "description": "Comma-separated usernames that are never auto-ignored",
            "type": "str",
        },
        "exempt_buddies": {
            "description": "Never auto-ignore users on the buddy list",
            "type": "bool",
        },
        "exempt_private_room_staff": {
            "description": "Never auto-ignore private-room owners or operators",
            "type": "bool",
        },
        "blocked_countries": {
            "description": "Comma-separated two-letter country codes to ignore on sight",
            "type": "str",
        },
        "blocked_phrases": {
            "description": "Comma-separated phrases that trigger an ignore",
            "type": "str",
        },
        "phrase_match_mode": {
            "description": "Phrase matching: contains or exact",
            "type": "str",
        },
        "notify_local_room": {
            "description": "Echo detections locally in the affected room",
            "type": "bool",
        },
        "notify_desktop": {
            "description": "Show a desktop notification for detections",
            "type": "bool",
        },
    }

    def __init__(self):
        self._messages: dict[tuple[str, str], deque[float]] = {}
        self._triggered: set[tuple[str, str]] = set()
        self._rule_alerts: set[tuple[str, str, str]] = set()
        self._last_cleanup = 0.0
        self.commands = {
            "spamstatus": {
                "callback": self.spam_status_command,
                "description": "Show automatic spam-ignore settings and active counters",
                "disable": ["cli"],
            },
            "spamreset": {
                "callback": self.spam_reset_command,
                "parameters": ["[username]"],
                "description": "Clear spam counters for one user or everyone",
                "disable": ["cli"],
            },
        }

    def loaded_notification(self):
        mode = "enforcing" if bool(self.settings.get("enforce_ignores", False)) else "log-only"
        self.log("Loaded in %s mode. Use /spamstatus to review settings.", mode)

    def incoming_public_chat_notification(self, room: str, user: str, line: str) -> None:
        if not self._room_monitored(room) or self._user_exempt(room, user):
            return
        if self.core.network_filter.is_user_ignored(user):
            return

        country = str(self.core.users.countries.get(user, "") or "").upper()
        if country and country.casefold() in _casefold_csv(self.settings.get("blocked_countries", "")):
            self._handle_rule_detection(room, user, "country", f"country code {country}")
            return

        phrase = self._matching_phrase(line)
        if phrase is not None:
            self._handle_rule_detection(room, user, "phrase", f"blocked phrase: {phrase}")
            return

        now = time.monotonic()
        threshold = _bounded_int(self.settings.get("message_threshold"), 20, 3, 200)
        window = _bounded_int(self.settings.get("window_seconds"), 60, 10, 600)
        scope = str(self.settings.get("scope", "per_room")).strip().lower()
        scope_key = "*" if scope == "all_rooms" else room.casefold()
        key = (scope_key, user.casefold())
        timestamps = self._messages.setdefault(key, deque())
        cutoff = now - window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        timestamps.append(now)
        if len(timestamps) < threshold:
            self._triggered.discard(key)

        if len(timestamps) >= threshold and key not in self._triggered:
            self._triggered.add(key)
            self._handle_detection(room, user, len(timestamps), window)

        if now - self._last_cleanup >= max(window, 60):
            self._cleanup(now, window)

    def _handle_detection(self, room: str, user: str, count: int, window: int) -> None:
        reason = f"{count} messages in {window} seconds"
        self._take_action(room, user, reason)

    def _handle_rule_detection(self, room: str, user: str, rule: str, reason: str) -> None:
        alert_key = (rule, room.casefold(), user.casefold())
        enforcing = bool(self.settings.get("enforce_ignores", False))
        if alert_key in self._rule_alerts and not enforcing:
            return
        self._rule_alerts.add(alert_key)
        self._take_action(room, user, reason)

    def _take_action(self, room: str, user: str, reason: str) -> None:
        if bool(self.settings.get("enforce_ignores", False)):
            self.core.network_filter.ignore_user(user)
            message = f"Ignored {user} in {room}: {reason}."
        else:
            message = f"Would ignore {user} in {room}: {reason} (log-only mode)."
        self.log(message)
        if bool(self.settings.get("notify_local_room", True)):
            self.echo_public(room, f"[Auto Spam Ignore] {message}", message_type="local")
        if bool(self.settings.get("notify_desktop", True)):
            self.core.notifications.show_chatroom_notification(
                room,
                message,
                title="Auto Spam Ignore",
                high_priority=True,
            )

    def _matching_phrase(self, line: str) -> Optional[str]:
        phrases = [item.strip() for item in str(self.settings.get("blocked_phrases", "")).split(",") if item.strip()]
        if not phrases:
            return None
        message = _clean_text(line).casefold()
        mode = str(self.settings.get("phrase_match_mode", "contains")).strip().lower()
        for phrase in phrases:
            candidate = _clean_text(phrase).casefold()
            if mode == "exact" and message == candidate:
                return phrase
            if mode != "exact" and candidate in message:
                return phrase
        return None

    def _room_monitored(self, room: str) -> bool:
        configured = _casefold_csv(self.settings.get("monitored_rooms", ""))
        return not configured or room.casefold() in configured

    def _user_exempt(self, room: str, user: str) -> bool:
        username = user.casefold()
        if username == str(self.core.users.login_username).casefold():
            return True
        if username in _casefold_csv(self.settings.get("exempt_users", "")):
            return True
        if bool(self.settings.get("exempt_buddies", True)):
            if any(username == buddy.casefold() for buddy in self.core.buddies.users):
                return True
        if bool(self.settings.get("exempt_private_room_staff", True)):
            private_room = self.core.chatrooms.private_rooms.get(room)
            if private_room is not None:
                owner = str(private_room.owner or "").casefold()
                operators = {operator.casefold() for operator in private_room.operators}
                if username == owner or username in operators:
                    return True
        return False

    def _cleanup(self, now: float, window: int) -> None:
        cutoff = now - window
        for key, timestamps in tuple(self._messages.items()):
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if timestamps:
                continue
            del self._messages[key]
            self._triggered.discard(key)
        self._last_cleanup = now

    def spam_status_command(self, _args: str, **_context: object) -> bool:
        threshold = _bounded_int(self.settings.get("message_threshold"), 20, 3, 200)
        window = _bounded_int(self.settings.get("window_seconds"), 60, 10, 600)
        mode = "enforcing" if bool(self.settings.get("enforce_ignores", False)) else "log-only"
        scope = str(self.settings.get("scope", "per_room"))
        rooms = str(self.settings.get("monitored_rooms", "")).strip() or "all joined rooms"
        countries = str(self.settings.get("blocked_countries", "")).strip() or "none"
        phrase_count = len(_casefold_csv(self.settings.get("blocked_phrases", "")))
        notices = bool(self.settings.get("notify_local_room", True)) or bool(self.settings.get("notify_desktop", True))
        self.echo_message(
            f"Spam filter: {mode}; trigger: {threshold} messages/{window}s; "
            f"scope: {scope}; rooms: {rooms}; countries: {countries}; "
            f"phrases: {phrase_count}; notices: {'on' if notices else 'off'}; active counters: {len(self._messages)}"
        )
        return True

    def spam_reset_command(self, args: str, **_context: object) -> bool:
        username = args.strip().casefold()
        if not username:
            count = len(self._messages)
            self._messages.clear()
            self._triggered.clear()
            self._rule_alerts.clear()
            self.echo_message(f"Cleared {count} spam counters. Existing ignores were not changed.")
            return True

        keys = [key for key in self._messages if key[1] == username]
        for key in keys:
            del self._messages[key]
            self._triggered.discard(key)
        self._rule_alerts = {key for key in self._rule_alerts if key[2] != username}
        self.echo_message(
            f"Cleared {len(keys)} spam counters for {args.strip()}. Existing ignores were not changed."
        )
        return True


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _casefold_csv(value: object) -> set[str]:
    return {item.strip().casefold() for item in str(value or "").split(",") if item.strip()}


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
