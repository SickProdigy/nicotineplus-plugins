"""Controlled announcements to explicitly configured Nicotine+ rooms."""

from __future__ import annotations

import json
import time
import uuid
from typing import Callable, Optional

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin


class AnnouncementError(RuntimeError):
    """A safe announcement configuration or validation error."""


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _clean_message(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _csv_rooms(value: object) -> list[str]:
    rooms = []
    seen = set()
    for item in str(value or "").split(","):
        room = item.strip()
        key = room.casefold()
        if room and key not in seen:
            seen.add(key)
            rooms.append(room)
    return rooms


def _parse_groups(value: object) -> dict[str, list[str]]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError) as error:
        raise AnnouncementError("Room groups must be valid JSON.") from error
    if not isinstance(payload, dict):
        raise AnnouncementError("Room groups JSON must be an object.")

    groups = {}
    for name, rooms in payload.items():
        group_name = str(name).strip().casefold()
        if not group_name or not isinstance(rooms, list):
            raise AnnouncementError("Each room group must have a name and a JSON list of rooms.")
        normalized = _csv_rooms(",".join(str(room) for room in rooms))
        if not normalized:
            raise AnnouncementError(f"Room group '{name}' has no rooms.")
        groups[group_name] = normalized
    return groups


class Plugin(BasePlugin):
    settings = {
        "sending_enabled": False,
        "default_rooms": "",
        "room_groups_json": "{}",
        "message_prefix": "[Open Source] ",
        "interval_seconds": 5,
        "repeat_cooldown_seconds": 3600,
        "max_message_chars": 750,
    }
    metasettings = {
        "sending_enabled": {
            "description": "Actually send announcements (off means preview only)",
            "type": "bool",
        },
        "default_rooms": {
            "description": "Comma-separated rooms used by /announce",
            "type": "str",
        },
        "room_groups_json": {
            "description": "Named groups JSON, for example {\"opensource\":[\"Room One\",\"Room Two\"]}",
            "type": "str",
        },
        "message_prefix": {
            "description": "Text added before each announcement",
            "type": "str",
        },
        "interval_seconds": {
            "description": "Delay between room sends (2-300 seconds)",
            "type": "int",
        },
        "repeat_cooldown_seconds": {
            "description": "Block an identical broadcast for this long (60-86400 seconds)",
            "type": "int",
        },
        "max_message_chars": {
            "description": "Maximum final message length (100-900 characters)",
            "type": "int",
        },
    }

    def __init__(self):
        self._scheduled: dict[tuple[str, int], int] = {}
        self._job_replies: dict[str, Callable[[str], None]] = {}
        self._recent: dict[tuple[tuple[str, ...], str], float] = {}
        self._stopping = False
        self.commands = {
            "announce": {
                "callback": self.announce_command,
                "parameters": ["<message>"],
                "description": "Preview or send to configured default rooms",
                "disable": ["cli"],
            },
            "announcegroup": {
                "callback": self.announce_group_command,
                "parameters": ["<group>", "<message>"],
                "description": "Preview or send to one named room group",
                "disable": ["cli"],
            },
            "announceall": {
                "callback": self.announce_all_command,
                "parameters": ["<message>"],
                "description": "Preview or send to every explicitly configured room",
                "disable": ["cli"],
            },
            "announcecancel": {
                "callback": self.announce_cancel_command,
                "description": "Cancel all queued announcement sends",
                "disable": ["cli"],
            },
            "announcestatus": {
                "callback": self.announce_status_command,
                "description": "Show announcement mode, groups, rooms, and queued sends",
                "disable": ["cli"],
            },
        }

    def loaded_notification(self):
        self._stopping = False
        state = "sending enabled" if bool(self.settings.get("sending_enabled", False)) else "preview only"
        self.log("Loaded in %s mode.", state)

    def disable(self):
        self._cancel_all()
        self._stopping = True

    def shutdown_notification(self):
        self._cancel_all()
        self._stopping = True

    def announce_command(self, args: str, room: Optional[str] = None, user: Optional[str] = None) -> bool:
        return self._announce(_csv_rooms(self.settings.get("default_rooms", "")), args, room, user, "default rooms")

    def announce_group_command(self, args: str, room: Optional[str] = None, user: Optional[str] = None) -> bool:
        group, separator, message = args.strip().partition(" ")
        if not separator or not message.strip():
            self.output("Usage: /announcegroup <group> <message>")
            return False
        try:
            groups = _parse_groups(self.settings.get("room_groups_json", "{}"))
        except AnnouncementError as error:
            self.output(str(error))
            return False
        rooms = groups.get(group.casefold())
        if rooms is None:
            self.output(f"Unknown announcement group: {group}")
            return False
        return self._announce(rooms, message, room, user, f"group {group}")

    def announce_all_command(self, args: str, room: Optional[str] = None, user: Optional[str] = None) -> bool:
        try:
            groups = _parse_groups(self.settings.get("room_groups_json", "{}"))
        except AnnouncementError as error:
            self.output(str(error))
            return False
        rooms = _csv_rooms(self.settings.get("default_rooms", ""))
        for group_rooms in groups.values():
            rooms.extend(group_rooms)
        rooms = _csv_rooms(",".join(rooms))
        return self._announce(rooms, args, room, user, "all configured rooms")

    def announce_cancel_command(self, _args: str, **_context: object) -> bool:
        count = self._cancel_all()
        self.echo_message(f"Cancelled {count} queued announcement sends.")
        return True

    def announce_status_command(self, _args: str, **_context: object) -> bool:
        try:
            groups = _parse_groups(self.settings.get("room_groups_json", "{}"))
            group_names = ", ".join(sorted(groups)) or "none"
        except AnnouncementError:
            group_names = "invalid JSON"
        state = "enabled" if bool(self.settings.get("sending_enabled", False)) else "preview only"
        rooms = _csv_rooms(self.settings.get("default_rooms", ""))
        self.echo_message(
            f"Announcements: {state}; default rooms: {len(rooms)}; groups: {group_names}; "
            f"queued sends: {len(self._scheduled)}"
        )
        return True

    def _announce(
        self,
        rooms: list[str],
        raw_message: str,
        source_room: Optional[str],
        source_user: Optional[str],
        target_name: str,
    ) -> bool:
        reply = self._local_reply(source_room, source_user)
        if reply is None:
            return False
        try:
            message = self._validated_message(raw_message)
            joined_rooms = self._joined_rooms(rooms)
            if not rooms:
                raise AnnouncementError(f"No rooms are configured for {target_name}.")
            if not joined_rooms:
                raise AnnouncementError("None of the configured target rooms are currently joined.")
            self._check_repeat(joined_rooms, message)
        except AnnouncementError as error:
            reply(str(error))
            return False

        room_list = ", ".join(joined_rooms)
        if not bool(self.settings.get("sending_enabled", False)):
            reply(f"PREVIEW to {room_list}: {message}")
            return True

        job_id = uuid.uuid4().hex
        interval = _bounded_int(self.settings.get("interval_seconds"), 5, 2, 300)
        self._job_replies[job_id] = reply
        for index, target_room in enumerate(joined_rooms):
            event_id = events.schedule(
                (index + 1) * interval,
                self._send_scheduled,
                callback_args=(job_id, index, target_room, message),
            )
            self._scheduled[(job_id, index)] = event_id
        self._remember(joined_rooms, message)
        reply(f"Queued announcement to {len(joined_rooms)} room(s), spaced {interval} seconds apart.")
        return True

    def _validated_message(self, raw_message: str) -> str:
        body = _clean_message(raw_message)
        if not body:
            raise AnnouncementError("Announcement message cannot be empty.")
        prefix = _clean_message(self.settings.get("message_prefix", ""))
        message = f"{prefix} {body}".strip() if prefix else body
        maximum = _bounded_int(self.settings.get("max_message_chars"), 750, 100, 900)
        if len(message) > maximum:
            raise AnnouncementError(f"Announcement is too long (maximum {maximum} characters including prefix).")
        return message

    def _joined_rooms(self, rooms: list[str]) -> list[str]:
        joined = {name.casefold(): name for name in self.core.chatrooms.joined_rooms}
        return [joined[room.casefold()] for room in rooms if room.casefold() in joined]

    def _check_repeat(self, rooms: list[str], message: str) -> None:
        now = time.monotonic()
        cooldown = _bounded_int(self.settings.get("repeat_cooldown_seconds"), 3600, 60, 86400)
        key = (tuple(sorted(room.casefold() for room in rooms)), message)
        previous = self._recent.get(key)
        if previous is not None and now - previous < cooldown:
            remaining = int(cooldown - (now - previous))
            raise AnnouncementError(f"Identical announcement is on cooldown for about {remaining} more seconds.")

    def _remember(self, rooms: list[str], message: str) -> None:
        now = time.monotonic()
        cooldown = _bounded_int(self.settings.get("repeat_cooldown_seconds"), 3600, 60, 86400)
        cutoff = now - cooldown
        self._recent = {key: timestamp for key, timestamp in self._recent.items() if timestamp > cutoff}
        key = (tuple(sorted(room.casefold() for room in rooms)), message)
        self._recent[key] = now

    def _send_scheduled(self, job_id: str, index: int, room: str, message: str) -> None:
        self._scheduled.pop((job_id, index), None)
        reply = self._job_replies.get(job_id)
        if self._stopping:
            return
        if room not in self.core.chatrooms.joined_rooms:
            self.log("Skipped announcement because room is no longer joined: %s", room)
        else:
            self.send_public(room, message)
        if not any(key[0] == job_id for key in self._scheduled):
            self._job_replies.pop(job_id, None)
            if reply is not None:
                reply("Announcement job finished.")

    def _local_reply(self, room: Optional[str], user: Optional[str]) -> Optional[Callable[[str], None]]:
        if room is not None:
            return lambda message: self.echo_public(room, message, message_type="local")
        if user is not None:
            return lambda message: self.echo_private(user, message, message_type="local")
        return None

    def _cancel_all(self) -> int:
        count = len(self._scheduled)
        for event_id in tuple(self._scheduled.values()):
            events.cancel_scheduled(event_id)
        self._scheduled.clear()
        self._job_replies.clear()
        return count
