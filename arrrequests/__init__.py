"""Read-only Sonarr, Radarr, and Lidarr request lookups from Nicotine+ chat."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin


USER_AGENT = "NicotinePlus-ArrRequests/0.1"


class ArrError(RuntimeError):
    """A safe Arr connection or response failure."""


@dataclass(frozen=True)
class LookupResult:
    title: str
    year: Optional[int]
    external_id: str


class ArrClient:
    """Minimal read-only client for one Arr application."""

    ROUTES = {
        "tv": ("sonarr", "v3", "series/lookup"),
        "movie": ("radarr", "v3", "movie/lookup"),
        "music": ("lidarr", "v1", "artist/lookup"),
    }

    def __init__(self, media_type: str, base_url: str, api_key: str):
        if media_type not in self.ROUTES:
            raise ArrError("Media type must be tv, movie, or music.")
        self.media_type = media_type
        self.application, self.api_version, self.lookup_route = self.ROUTES[media_type]
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ArrError(f"Configure a complete HTTP(S) URL for {self.application}.")
        if not self.api_key:
            raise ArrError(f"Configure an API key for {self.application}.")

    def status(self, timeout: int) -> str:
        payload = self._get_json(f"/api/{self.api_version}/system/status", {}, timeout)
        name = _clean_text(payload.get("appName")) or self.application.title()
        version = _clean_text(payload.get("version")) or "unknown version"
        return f"{name} {version}"

    def lookup(self, term: str, limit: int, timeout: int) -> list[LookupResult]:
        payload = self._get_json(
            f"/api/{self.api_version}/{self.lookup_route}",
            {"term": term},
            timeout,
        )
        if not isinstance(payload, list):
            raise ArrError(f"{self.application} returned an unexpected lookup response.")

        results = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if self.media_type == "music":
                title = _clean_text(item.get("artistName") or item.get("title"))
                external_id = _clean_text(item.get("foreignArtistId"))
            elif self.media_type == "tv":
                title = _clean_text(item.get("title"))
                external_id = _identifier("tvdb", item.get("tvdbId"))
            else:
                title = _clean_text(item.get("title"))
                external_id = _identifier("tmdb", item.get("tmdbId"))
            year = item.get("year")
            if not isinstance(year, int):
                year = None
            if title:
                results.append(LookupResult(title, year, external_id))
        return results[:limit]

    def _get_json(self, path: str, params: dict, timeout: int):
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{self.base_url}{path}{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "X-Api-Key": self.api_key,
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # nosec B310 - configured Arr server
                return json.load(response)
        except HTTPError as error:
            raise ArrError(f"{self.application} returned HTTP {error.code}.") from error
        except (URLError, TimeoutError, ValueError, OSError) as error:
            raise ArrError(f"Could not reach {self.application}: {error}") from error


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _identifier(label: str, value: object) -> str:
    cleaned = _clean_text(value)
    return f"{label}:{cleaned}" if cleaned and cleaned != "0" else ""


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _format_result(result: LookupResult) -> str:
    details = []
    if result.year is not None:
        details.append(str(result.year))
    if result.external_id:
        details.append(result.external_id)
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{result.title}{suffix}"


class Plugin(BasePlugin):
    settings = {
        "allow_remote_requests": False,
        "approved_users": "",
        "remote_trigger": "!request",
        "remote_cooldown_seconds": 30,
        "sonarr_url": "http://127.0.0.1:8989",
        "sonarr_api_key": "",
        "radarr_url": "http://127.0.0.1:7878",
        "radarr_api_key": "",
        "lidarr_url": "http://127.0.0.1:8686",
        "lidarr_api_key": "",
        "timeout_seconds": 15,
        "max_results": 3,
        "max_concurrent_requests": 2,
    }
    metasettings = {
        "allow_remote_requests": {"description": "Allow approved users to run read-only lookups", "type": "bool"},
        "approved_users": {"description": "Comma-separated Nicotine+ usernames allowed to request", "type": "str"},
        "remote_trigger": {"description": "Request prefix, for example !request", "type": "str"},
        "remote_cooldown_seconds": {"description": "Minimum seconds between requests per user (10-3600)", "type": "int"},
        "sonarr_url": {"description": "Sonarr base URL", "type": "str"},
        "sonarr_api_key": {"description": "Sonarr API key (never logged)", "type": "str"},
        "radarr_url": {"description": "Radarr base URL", "type": "str"},
        "radarr_api_key": {"description": "Radarr API key (never logged)", "type": "str"},
        "lidarr_url": {"description": "Lidarr base URL", "type": "str"},
        "lidarr_api_key": {"description": "Lidarr API key (never logged)", "type": "str"},
        "timeout_seconds": {"description": "Arr request timeout (3-120 seconds)", "type": "int"},
        "max_results": {"description": "Maximum lookup results (1-5)", "type": "int"},
        "max_concurrent_requests": {"description": "Maximum simultaneous Arr lookups (1-4)", "type": "int"},
    }

    def __init__(self):
        self._remote_last_used: dict[str, float] = {}
        self._lock = threading.Lock()
        self._active_requests = 0
        self._stopping = False
        self.commands = {
            "arrlookup": {
                "callback": self.arr_lookup_command,
                "parameters": ["<tv|movie|music>", "<query>"],
                "description": "Look up a title without adding or downloading it",
                "disable": ["cli"],
            },
            "arrstatus": {
                "callback": self.arr_status_command,
                "description": "Check configured Sonarr, Radarr, and Lidarr connections",
                "disable": ["cli"],
            },
        }

    def loaded_notification(self):
        self._stopping = False
        self.log("Loaded in lookup-only mode. No titles can be added or downloaded.")

    def disable(self):
        self._stopping = True

    def shutdown_notification(self):
        self._stopping = True

    def arr_lookup_command(self, args: str, room: Optional[str] = None, user: Optional[str] = None) -> bool:
        parsed = _parse_request(args)
        if parsed is None:
            self.output("Usage: /arrlookup <tv|movie|music> <query>")
            return False
        reply = self._command_reply(room, user)
        if reply is None:
            return False
        if not self._start_worker(self._lookup_worker, parsed[0], parsed[1], reply):
            self.echo_message("The Arr request limit is currently reached.")
            return False
        self.echo_message(f"Looking up {parsed[0]}: {parsed[1]}")
        return True

    def arr_status_command(self, _args: str, room: Optional[str] = None, user: Optional[str] = None) -> bool:
        reply = self._command_reply(room, user)
        if reply is None:
            return False
        if not self._start_worker(self._status_worker, reply):
            self.echo_message("The Arr request limit is currently reached.")
            return False
        self.echo_message("Checking configured Arr connections…")
        return True

    def incoming_public_chat_notification(self, room: str, user: str, line: str) -> None:
        parsed = self._remote_request(user, line)
        if parsed is None:
            return
        reply = lambda message: self.send_private(user, message, show_ui=False, switch_page=False)
        if not self._start_worker(self._lookup_worker, parsed[0], parsed[1], reply):
            reply("The request service is busy; try again later.")

    def incoming_private_chat_notification(self, user: str, line: str) -> None:
        parsed = self._remote_request(user, line)
        if parsed is None:
            return
        reply = lambda message: self.send_private(user, message, show_ui=False, switch_page=False)
        if not self._start_worker(self._lookup_worker, parsed[0], parsed[1], reply):
            reply("The request service is busy; try again later.")

    def _remote_request(self, user: str, line: str) -> Optional[tuple[str, str]]:
        if user == self.core.users.login_username:
            return None
        if not bool(self.settings.get("allow_remote_requests", False)):
            return None
        approved = {item.strip().casefold() for item in str(self.settings.get("approved_users", "")).split(",") if item.strip()}
        if user.casefold() not in approved:
            return None
        trigger = _clean_text(self.settings.get("remote_trigger", "!request")) or "!request"
        text = str(line or "").strip()
        prefix = f"{trigger} "
        if not text.casefold().startswith(prefix.casefold()):
            return None
        parsed = _parse_request(text[len(prefix):])
        if parsed is None:
            return None
        now = time.monotonic()
        cooldown = _bounded_int(self.settings.get("remote_cooldown_seconds"), 30, 10, 3600)
        key = user.casefold()
        if now - self._remote_last_used.get(key, 0.0) < cooldown:
            return None
        self._remote_last_used[key] = now
        return parsed

    def _command_reply(self, room: Optional[str], user: Optional[str]) -> Optional[Callable[[str], None]]:
        if room is not None:
            return lambda message: self.send_public(room, message)
        if user is not None:
            return lambda message: self.send_private(user, message)
        return None

    def _client(self, media_type: str) -> ArrClient:
        application = ArrClient.ROUTES[media_type][0]
        return ArrClient(
            media_type,
            str(self.settings.get(f"{application}_url", "")),
            str(self.settings.get(f"{application}_api_key", "")),
        )

    def _lookup_worker(self, media_type: str, query: str, reply: Callable[[str], None]) -> None:
        try:
            results = self._client(media_type).lookup(
                query,
                _bounded_int(self.settings.get("max_results"), 3, 1, 5),
                _bounded_int(self.settings.get("timeout_seconds"), 15, 3, 120),
            )
            messages = [f"{media_type}: {_format_result(result)}" for result in results]
            if not messages:
                messages = [f'No {media_type} results found for "{query}".']
            messages.append("Lookup only—nothing was added or downloaded.")
        except ArrError as error:
            self.log("Arr lookup failed: %s", str(error))
            messages = ["The request lookup is unavailable; check the configured Arr connection."]
        except Exception as error:
            self.log("Unexpected Arr lookup failure: %s", str(error))
            messages = ["The request lookup failed unexpectedly; check the Nicotine+ log."]
        finally:
            self._release_worker()
        events.invoke_main_thread(self._deliver, reply, tuple(messages))

    def _status_worker(self, reply: Callable[[str], None]) -> None:
        messages = []
        timeout = _bounded_int(self.settings.get("timeout_seconds"), 15, 3, 120)
        for media_type in ("tv", "movie", "music"):
            application = ArrClient.ROUTES[media_type][0]
            if not str(self.settings.get(f"{application}_api_key", "")).strip():
                messages.append(f"{application}: not configured")
                continue
            try:
                messages.append(f"{application}: connected ({self._client(media_type).status(timeout)})")
            except ArrError as error:
                self.log("Arr status check failed for %s: %s", (application, str(error)))
                messages.append(f"{application}: unavailable")
            except Exception as error:
                self.log("Unexpected Arr status failure for %s: %s", (application, str(error)))
                messages.append(f"{application}: unavailable")
        self._release_worker()
        events.invoke_main_thread(self._deliver, reply, tuple(messages))

    def _start_worker(self, target: Callable, *args: object) -> bool:
        maximum = _bounded_int(self.settings.get("max_concurrent_requests"), 2, 1, 4)
        with self._lock:
            if self._active_requests >= maximum:
                return False
            self._active_requests += 1
        threading.Thread(target=target, args=args, name="nicotine-arr-request", daemon=True).start()
        return True

    def _release_worker(self) -> None:
        with self._lock:
            self._active_requests -= 1

    def _deliver(self, reply: Callable[[str], None], messages: tuple[str, ...]) -> None:
        if self._stopping:
            return
        for message in messages:
            reply(message)


def _parse_request(text: str) -> Optional[tuple[str, str]]:
    media_type, separator, query = _clean_text(text).partition(" ")
    media_type = media_type.casefold()
    query = query.strip()
    if not separator or media_type not in ArrClient.ROUTES or not query:
        return None
    return media_type, query
