"""Compact Wolfram|Alpha text queries from Nicotine+ chat."""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin


API_URL = "https://api.wolframalpha.com/v2/query"
INPUT_URL = "https://www.wolframalpha.com/input"
USER_AGENT = "NicotinePlus-Wolfram/0.1"
NO_RESULT = "There is as yet insufficient data for a meaningful answer."


class Failure(Enum):
    MISSING_KEY = "missing_key"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    PROVIDER = "provider"
    INVALID_RESPONSE = "invalid_response"


class WolframError(RuntimeError):
    def __init__(self, failure: Failure):
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True)
class WolframAnswer:
    lines: tuple[str, ...]
    source_url: str


class WolframClient:
    def __init__(self, app_id: str):
        self.app_id = app_id.strip()
        if not self.app_id:
            raise WolframError(Failure.MISSING_KEY)

    def query(self, question: str, solve: bool, timeout: int, max_pods: int) -> WolframAnswer:
        params = {
            "input": question,
            "appid": self.app_id,
            "format": "plaintext",
        }
        if solve:
            params["podstate"] = "Step-by-step solution"
        request = Request(
            f"{API_URL}?{urlencode(params)}",
            headers={"Accept": "application/xml,text/xml", "User-Agent": USER_AGENT},
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed Wolfram HTTPS URL
                body = response.read()
        except HTTPError as error:
            if error.code in {401, 403, 501}:
                raise WolframError(Failure.AUTHENTICATION) from error
            if error.code == 429:
                raise WolframError(Failure.RATE_LIMIT) from error
            raise WolframError(Failure.PROVIDER) from error
        except TimeoutError as error:
            raise WolframError(Failure.TIMEOUT) from error
        except (URLError, OSError) as error:
            raise WolframError(Failure.NETWORK) from error

        lines = _parse_answers(body, solve=solve, max_pods=max_pods)
        source_url = f"{INPUT_URL}?{urlencode({'i': question})}"
        return WolframAnswer(tuple(lines), source_url)


def _parse_answers(body: bytes, solve: bool, max_pods: int) -> list[str]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise WolframError(Failure.INVALID_RESPONSE) from error

    lines = []
    for pod in root.findall(".//pod"):
        title = _clean_text(pod.attrib.get("title", "Result"))
        if solve and title.casefold() == "number line":
            continue
        values = []
        for plaintext in pod.findall(".//plaintext"):
            value = _clean_text(plaintext.text)
            if value and value not in values:
                values.append(value)
        if not values:
            continue
        text = "; ".join(values)
        line = f"{title}: {text}" if title else text
        if "current geoip location" in line.casefold():
            continue
        lines.append(line)
        if len(lines) >= max_pods:
            break
    return lines or [NO_RESULT]


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _chunk_text(text: str, size: int, maximum: int) -> list[str]:
    remaining = _clean_text(text)
    chunks = []
    while remaining and len(chunks) < maximum:
        if len(remaining) <= size:
            chunks.append(remaining)
            remaining = ""
            break
        split_at = remaining.rfind(" ", 0, size + 1)
        if split_at < size // 2:
            split_at = size
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining and chunks:
        chunks[-1] = chunks[-1].rstrip(" .") + "…"
    return chunks


class Plugin(BasePlugin):
    settings = {
        "app_id": "",
        "timeout_seconds": 20,
        "max_query_chars": 500,
        "max_answer_pods": 3,
        "reply_chunk_chars": 800,
        "max_reply_chunks": 3,
        "include_source_link": True,
        "allow_remote_queries": False,
        "approved_users": "",
        "allowed_rooms": "",
        "remote_trigger": "!wolfram",
        "remote_cooldown_seconds": 30,
        "max_concurrent_queries": 2,
    }
    metasettings = {
        "app_id": {"description": "Wolfram|Alpha AppID (never logged or sent to chat)", "type": "str"},
        "timeout_seconds": {"description": "Provider timeout (3-120 seconds)", "type": "int"},
        "max_query_chars": {"description": "Maximum question length (20-2000)", "type": "int"},
        "max_answer_pods": {"description": "Maximum result sections (1-8)", "type": "int"},
        "reply_chunk_chars": {"description": "Maximum characters per chat reply (200-1000)", "type": "int"},
        "max_reply_chunks": {"description": "Maximum reply messages (1-5)", "type": "int"},
        "include_source_link": {"description": "Include a Wolfram|Alpha input-page link", "type": "bool"},
        "allow_remote_queries": {"description": "Allow approved users to query Wolfram|Alpha", "type": "bool"},
        "approved_users": {"description": "Comma-separated Nicotine+ usernames allowed to query", "type": "str"},
        "allowed_rooms": {"description": "Comma-separated rooms for remote queries (empty allows joined rooms)", "type": "str"},
        "remote_trigger": {"description": "Remote query prefix, for example !wolfram", "type": "str"},
        "remote_cooldown_seconds": {"description": "Minimum seconds between remote queries per user (10-3600)", "type": "int"},
        "max_concurrent_queries": {"description": "Maximum simultaneous provider requests (1-4)", "type": "int"},
    }

    def __init__(self):
        self._remote_last_used: dict[str, float] = {}
        self._lock = threading.Lock()
        self._active_queries = 0
        self._stopping = False
        self.commands = {
            "wolfram": {
                "aliases": ["wa"],
                "callback": self.wolfram_command,
                "parameters": ["<question>"],
                "description": "Ask Wolfram|Alpha for a compact text answer",
                "disable": ["cli"],
            },
            "wolframsolve": {
                "aliases": ["wasolve"],
                "callback": self.wolfram_solve_command,
                "parameters": ["<question>"],
                "description": "Request step-by-step Wolfram|Alpha output",
                "disable": ["cli"],
            },
        }

    def loaded_notification(self):
        self._stopping = False
        self.log("Ready. Configure an AppID, then use /wolfram <question>.")

    def disable(self):
        self._stopping = True

    def shutdown_notification(self):
        self._stopping = True

    def wolfram_command(self, args: str, room: Optional[str] = None, user: Optional[str] = None) -> bool:
        return self._command(args, room, user, solve=False)

    def wolfram_solve_command(self, args: str, room: Optional[str] = None, user: Optional[str] = None) -> bool:
        return self._command(args, room, user, solve=True)

    def _command(self, args: str, room: Optional[str], user: Optional[str], solve: bool) -> bool:
        question = _clean_text(args)
        error = self._validate_question(question)
        if error:
            self.output(error)
            return False
        reply = self._command_reply(room, user)
        if reply is None:
            return False
        if not self._start_query(question, solve, reply):
            self.echo_message("The Wolfram query limit is currently reached.")
            return False
        self.echo_message(f"Asking Wolfram|Alpha: {question}")
        return True

    def incoming_public_chat_notification(self, room: str, user: str, line: str) -> None:
        question = self._remote_question(room, user, line)
        if question is None:
            return
        reply = lambda message: self.send_private(user, message, show_ui=False, switch_page=False)
        if not self._start_query(question, False, reply):
            reply("Wolfram|Alpha is busy; try again later.")

    def incoming_private_chat_notification(self, user: str, line: str) -> None:
        question = self._remote_question(None, user, line)
        if question is None:
            return
        reply = lambda message: self.send_private(user, message, show_ui=False, switch_page=False)
        if not self._start_query(question, False, reply):
            reply("Wolfram|Alpha is busy; try again later.")

    def _remote_question(self, room: Optional[str], user: str, line: str) -> Optional[str]:
        if user == self.core.users.login_username or not bool(self.settings.get("allow_remote_queries", False)):
            return None
        approved = {item.strip().casefold() for item in str(self.settings.get("approved_users", "")).split(",") if item.strip()}
        if user.casefold() not in approved:
            return None
        if room is not None:
            rooms = {item.strip().casefold() for item in str(self.settings.get("allowed_rooms", "")).split(",") if item.strip()}
            if rooms and room.casefold() not in rooms:
                return None
        trigger = _clean_text(self.settings.get("remote_trigger", "!wolfram")) or "!wolfram"
        text = str(line or "").strip()
        prefix = f"{trigger} "
        if not text.casefold().startswith(prefix.casefold()):
            return None
        question = _clean_text(text[len(prefix):])
        if self._validate_question(question):
            return None
        now = time.monotonic()
        cooldown = _bounded_int(self.settings.get("remote_cooldown_seconds"), 30, 10, 3600)
        key = user.casefold()
        if now - self._remote_last_used.get(key, 0.0) < cooldown:
            return None
        self._remote_last_used[key] = now
        return question

    def _validate_question(self, question: str) -> Optional[str]:
        if not str(self.settings.get("app_id", "")).strip():
            return "Configure a Wolfram|Alpha AppID in the plugin settings first."
        if not question:
            return "Usage: /wolfram <question>"
        maximum = _bounded_int(self.settings.get("max_query_chars"), 500, 20, 2000)
        if len(question) > maximum:
            return f"Question is too long (maximum {maximum} characters)."
        return None

    def _command_reply(self, room: Optional[str], user: Optional[str]) -> Optional[Callable[[str], None]]:
        if room is not None:
            return lambda message: self.send_public(room, message)
        if user is not None:
            return lambda message: self.send_private(user, message)
        return None

    def _start_query(self, question: str, solve: bool, reply: Callable[[str], None]) -> bool:
        maximum = _bounded_int(self.settings.get("max_concurrent_queries"), 2, 1, 4)
        with self._lock:
            if self._active_queries >= maximum:
                return False
            self._active_queries += 1
        threading.Thread(
            target=self._query_worker,
            args=(question, solve, reply),
            name="nicotine-wolfram",
            daemon=True,
        ).start()
        return True

    def _query_worker(self, question: str, solve: bool, reply: Callable[[str], None]) -> None:
        try:
            answer = WolframClient(str(self.settings.get("app_id", ""))).query(
                question=question,
                solve=solve,
                timeout=_bounded_int(self.settings.get("timeout_seconds"), 20, 3, 120),
                max_pods=_bounded_int(self.settings.get("max_answer_pods"), 3, 1, 8),
            )
            text = " | ".join(answer.lines)
            max_chunks = _bounded_int(self.settings.get("max_reply_chunks"), 3, 1, 5)
            messages = _chunk_text(
                text,
                _bounded_int(self.settings.get("reply_chunk_chars"), 800, 200, 1000),
                max_chunks,
            )
            if bool(self.settings.get("include_source_link", True)) and len(messages) < max_chunks:
                messages.append(f"Source: {answer.source_url}")
        except WolframError as error:
            self.log("Wolfram|Alpha query failed: %s", error.failure.value)
            messages = [_failure_message(error.failure)]
        except Exception as error:
            self.log("Unexpected Wolfram|Alpha failure: %s", type(error).__name__)
            messages = ["Wolfram|Alpha failed unexpectedly; check the Nicotine+ log."]
        finally:
            with self._lock:
                self._active_queries -= 1
        events.invoke_main_thread(self._deliver, reply, tuple(messages))

    def _deliver(self, reply: Callable[[str], None], messages: tuple[str, ...]) -> None:
        if self._stopping:
            return
        for message in messages:
            reply(message)


def _failure_message(failure: Failure) -> str:
    if failure is Failure.MISSING_KEY:
        return "No Wolfram|Alpha AppID is configured."
    if failure is Failure.AUTHENTICATION:
        return "Wolfram|Alpha rejected the configured AppID."
    if failure is Failure.RATE_LIMIT:
        return "Wolfram|Alpha is rate limiting requests; try again later."
    if failure is Failure.TIMEOUT:
        return "Wolfram|Alpha took too long to respond."
    if failure is Failure.NETWORK:
        return "Wolfram|Alpha could not be reached."
    if failure is Failure.INVALID_RESPONSE:
        return "Wolfram|Alpha returned an invalid response."
    return "Wolfram|Alpha is temporarily unavailable."
