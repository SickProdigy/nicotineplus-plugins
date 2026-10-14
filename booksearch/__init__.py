"""Nicotine+ chat commands for searching lawful book catalogs."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin


USER_AGENT = "NicotinePlus-BookSearch/0.1"
OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
OPEN_LIBRARY_WORK_URL = "https://openlibrary.org"


@dataclass(frozen=True)
class SearchResult:
    """A provider-neutral result suitable for a one-line chat message."""

    title: str
    author: str
    year: Optional[int]
    url: str
    formats: tuple[str, ...] = ()


class SearchError(RuntimeError):
    """Raised when a provider cannot return usable search results."""


class OpenLibraryProvider:
    """Search Open Library's documented public Search API."""

    name = "Open Library"

    def search(self, query: str, limit: int, timeout: int) -> list[SearchResult]:
        params = urlencode({
            "q": query,
            "limit": limit,
            "fields": "key,title,author_name,first_publish_year,ebook_access",
        })
        request = Request(
            f"{OPEN_LIBRARY_SEARCH_URL}?{params}",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )

        try:
            with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS URL
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            raise SearchError(f"{self.name} search failed: {error}") from error

        results = []
        for document in payload.get("docs", []):
            key = document.get("key")
            title = _clean_text(document.get("title"))
            if not key or not title or not str(key).startswith("/works/"):
                continue

            authors = document.get("author_name") or []
            author = _clean_text(authors[0]) if authors else "Unknown author"
            access = _clean_text(document.get("ebook_access"))
            formats = (access.replace("_", " "),) if access and access != "no_ebook" else ()
            year = document.get("first_publish_year")
            if not isinstance(year, int):
                year = None

            results.append(SearchResult(
                title=title,
                author=author,
                year=year,
                url=f"{OPEN_LIBRARY_WORK_URL}{quote(str(key), safe='/')}",
                formats=formats,
            ))

        return results[:limit]


def _clean_text(value: object) -> str:
    """Keep provider text compact and safe for a single chat line."""

    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def format_result(result: SearchResult) -> str:
    details = result.author
    if result.year is not None:
        details += f", {result.year}"
    if result.formats:
        details += f"; {', '.join(result.formats)}"
    return f"{result.title} — {details} — {result.url}"


class Plugin(BasePlugin):
    settings = {
        "max_results": 3,
        "timeout_seconds": 10,
        "allow_remote_requests": False,
        "allowed_rooms": "",
        "remote_trigger": "!book",
        "remote_cooldown_seconds": 30,
    }
    metasettings = {
        "max_results": {"description": "Maximum results per search (1-5)", "type": "int"},
        "timeout_seconds": {"description": "Catalog request timeout in seconds (3-30)", "type": "int"},
        "allow_remote_requests": {"description": "Allow other users to search with the remote trigger", "type": "bool"},
        "allowed_rooms": {"description": "Comma-separated rooms for remote requests (empty allows joined rooms)", "type": "str"},
        "remote_trigger": {"description": "Prefix other users type in chat, for example !book", "type": "str"},
        "remote_cooldown_seconds": {"description": "Minimum seconds between remote replies per user (10-3600)", "type": "int"},
    }

    def __init__(self):
        self.providers = (OpenLibraryProvider(),)
        self._remote_last_used: dict[str, float] = {}
        self._stopping = False
        self.commands = {
            "booksearch": {
                "aliases": ["books"],
                "callback": self.booksearch_command,
                "parameters": ["<query>"],
                "description": "Search configured ebook and audiobook catalogs",
                "disable": ["cli"],
            }
        }

    def loaded_notification(self):
        self._stopping = False
        self.log("Ready. Use /booksearch <title, author, or ISBN>.")

    def disable(self):
        self._stopping = True

    def shutdown_notification(self):
        self._stopping = True

    def booksearch_command(self, args: str, room: Optional[str] = None, user: Optional[str] = None) -> bool:
        query = _clean_text(args)
        if not query:
            self.output("Usage: /booksearch <title, author, or ISBN>")
            return False

        self.echo_message(f'Searching book catalogs for "{query}"…')
        if room is not None:
            reply = lambda message: self.send_public(room, message)
        elif user is not None:
            reply = lambda message: self.send_private(user, message)
        else:
            return False

        self._start_search(query, reply)
        return True

    def incoming_public_chat_notification(self, room: str, user: str, line: str) -> None:
        if user == self.core.users.login_username:
            return
        query = self._remote_query(line)
        if query is None or not self._room_allowed(room) or not self._remote_allowed(user):
            return
        self._start_search(query, lambda message: self.send_public(room, f"{user}: {message}"))

    def incoming_private_chat_notification(self, user: str, line: str) -> None:
        if user == self.core.users.login_username:
            return
        query = self._remote_query(line)
        if query is None or not self._remote_allowed(user):
            return
        self._start_search(query, lambda message: self.send_private(user, message, show_ui=False, switch_page=False))

    def _remote_query(self, line: str) -> Optional[str]:
        if not bool(self.settings.get("allow_remote_requests", False)):
            return None
        trigger = _clean_text(self.settings.get("remote_trigger", "!book")) or "!book"
        text = str(line or "").strip()
        prefix = f"{trigger} "
        if not text.casefold().startswith(prefix.casefold()):
            return None
        query = _clean_text(text[len(prefix):])
        return query or None

    def _room_allowed(self, room: str) -> bool:
        configured = str(self.settings.get("allowed_rooms", ""))
        allowed = {item.strip().casefold() for item in configured.split(",") if item.strip()}
        return not allowed or room.casefold() in allowed

    def _remote_allowed(self, user: str) -> bool:
        now = time.monotonic()
        cooldown = _bounded_int(self.settings.get("remote_cooldown_seconds"), 30, 10, 3600)
        last_used = self._remote_last_used.get(user.casefold(), 0.0)
        if now - last_used < cooldown:
            return False
        self._remote_last_used[user.casefold()] = now
        return True

    def _start_search(self, query: str, reply: Callable[[str], None]) -> None:
        threading.Thread(target=self._search_worker, args=(query, reply), name="booksearch-provider", daemon=True).start()

    def _search_worker(self, query: str, reply: Callable[[str], None]) -> None:
        limit = _bounded_int(self.settings.get("max_results"), 3, 1, 5)
        timeout = _bounded_int(self.settings.get("timeout_seconds"), 10, 3, 30)
        results: list[SearchResult] = []
        errors = []

        for provider in self.providers:
            try:
                results.extend(provider.search(query, limit, timeout))
            except SearchError as error:
                errors.append(str(error))

        messages: Iterable[str]
        if results:
            messages = [format_result(result) for result in results[:limit]]
        elif errors:
            self.log("; ".join(errors))
            messages = ("Book search is temporarily unavailable.",)
        else:
            messages = (f'No catalog results found for "{query}".',)

        events.invoke_main_thread(self._deliver, reply, tuple(messages))

    def _deliver(self, reply: Callable[[str], None], messages: tuple[str, ...]) -> None:
        if self._stopping:
            return
        for message in messages:
            reply(message)


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))
