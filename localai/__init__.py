"""Connect Nicotine+ chat to a local Ollama or llama.cpp server."""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin


USER_AGENT = "NicotinePlus-LocalAI/0.1"


class AIError(RuntimeError):
    """A safe, user-facing backend failure."""


class LocalAIClient:
    """Small standard-library client for Ollama and llama.cpp servers."""

    def __init__(self, backend: str, base_url: str, model: str, api_key: str = ""):
        self.backend = backend.strip().lower()
        self.base_url = base_url.strip().rstrip("/")
        self.model = model.strip()
        self.api_key = api_key.strip()

        if self.backend not in {"ollama", "llamacpp"}:
            raise AIError("Backend must be 'ollama' or 'llamacpp'.")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AIError("Server URL must be a complete http:// or https:// URL.")
        if not self.model:
            raise AIError("Configure a model name in the plugin settings.")

    def chat(self, prompt: str, system_prompt: str, max_tokens: int, timeout: int) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        if self.backend == "ollama":
            path = "/api/chat"
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {"num_predict": max_tokens},
            }
        else:
            path = "/v1/chat/completions"
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "max_tokens": max_tokens,
            }

        response = self._post_json(path, payload, timeout)
        try:
            if self.backend == "ollama":
                content = response["message"]["content"]
            else:
                content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise AIError("The AI server returned an unexpected response.") from error

        cleaned = _clean_text(content)
        if not cleaned:
            raise AIError("The AI server returned an empty response.")
        return cleaned

    def _post_json(self, path: str, payload: dict, timeout: int) -> dict:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urlopen(request, timeout=timeout) as response:  # nosec B310 - user-configured AI server
                return json.load(response)
        except HTTPError as error:
            detail = ""
            try:
                body = json.loads(error.read().decode("utf-8", "replace"))
                detail = _clean_text(body.get("error"))
            except (ValueError, AttributeError, OSError):
                pass
            suffix = f": {detail}" if detail else ""
            raise AIError(f"AI server returned HTTP {error.code}{suffix}") from error
        except (URLError, TimeoutError, ValueError, OSError) as error:
            raise AIError(f"Could not reach the AI server: {error}") from error


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _chunk_text(text: str, chunk_size: int, max_chunks: int) -> list[str]:
    remaining = _clean_text(text)
    chunks = []
    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            remaining = ""
            break
        split_at = remaining.rfind(" ", 0, chunk_size + 1)
        if split_at < chunk_size // 2:
            split_at = chunk_size
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining and chunks:
        chunks[-1] = chunks[-1].rstrip(" .") + "…"
    return chunks


class Plugin(BasePlugin):
    settings = {
        "backend": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "llama3.2",
        "api_key": "",
        "system_prompt": "Answer helpfully and concisely for a public chat. Do not claim to have searched Soulseek or the internet.",
        "timeout_seconds": 120,
        "max_tokens": 256,
        "max_prompt_chars": 1000,
        "reply_chunk_chars": 800,
        "max_reply_chunks": 3,
        "max_concurrent_requests": 1,
        "allow_remote_requests": False,
        "allowed_rooms": "",
        "remote_trigger": "!ai",
        "remote_cooldown_seconds": 60,
    }
    metasettings = {
        "backend": {"description": "Backend: ollama or llamacpp", "type": "str"},
        "base_url": {"description": "AI server base URL", "type": "str"},
        "model": {"description": "Model name exposed by the AI server", "type": "str"},
        "api_key": {"description": "Optional bearer API key (never logged)", "type": "str"},
        "system_prompt": {"description": "System instruction sent with each stateless request", "type": "str"},
        "timeout_seconds": {"description": "Request timeout in seconds (5-600)", "type": "int"},
        "max_tokens": {"description": "Maximum generated tokens (16-2048)", "type": "int"},
        "max_prompt_chars": {"description": "Maximum prompt length (50-4000 characters)", "type": "int"},
        "reply_chunk_chars": {"description": "Maximum characters per chat reply (200-1000)", "type": "int"},
        "max_reply_chunks": {"description": "Maximum reply messages per request (1-5)", "type": "int"},
        "max_concurrent_requests": {"description": "Maximum simultaneous generations (1-4)", "type": "int"},
        "allow_remote_requests": {"description": "Allow other users to prompt the model", "type": "bool"},
        "allowed_rooms": {"description": "Comma-separated rooms for remote prompts (empty allows joined rooms)", "type": "str"},
        "remote_trigger": {"description": "Prefix other users type, for example !ai", "type": "str"},
        "remote_cooldown_seconds": {"description": "Minimum seconds between remote prompts per user (10-3600)", "type": "int"},
    }

    def __init__(self):
        self._remote_last_used: dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._active_requests = 0
        self._stopping = False
        self.commands = {
            "ai": {
                "aliases": ["askai"],
                "callback": self.ai_command,
                "parameters": ["<prompt>"],
                "description": "Ask the configured local Ollama or llama.cpp model",
                "disable": ["cli"],
            }
        }

    def loaded_notification(self):
        self._stopping = False
        self.log("Ready. Use /ai <prompt>. Remote !ai prompts are disabled by default.")

    def disable(self):
        self._stopping = True

    def shutdown_notification(self):
        self._stopping = True

    def ai_command(self, args: str, room: Optional[str] = None, user: Optional[str] = None) -> bool:
        prompt = _clean_text(args)
        error = self._validate_prompt(prompt)
        if error:
            self.output(error)
            return False

        if room is not None:
            reply = lambda message: self.send_public(room, message)
        elif user is not None:
            reply = lambda message: self.send_private(user, message)
        else:
            return False

        if not self._start_request(prompt, reply):
            self.echo_message("The local AI request limit is currently reached.")
            return False
        self.echo_message("Waiting for the local AI model…")
        return True

    def incoming_public_chat_notification(self, room: str, user: str, line: str) -> None:
        if user == self.core.users.login_username:
            return
        prompt = self._remote_prompt(line)
        if prompt is None or not self._room_allowed(room) or not self._remote_allowed(user):
            return
        reply = lambda message: self.send_public(room, f"{user}: {message}")
        if not self._start_request(prompt, reply):
            self.send_public(room, f"{user}: local AI is busy; try again later.")

    def incoming_private_chat_notification(self, user: str, line: str) -> None:
        if user == self.core.users.login_username:
            return
        prompt = self._remote_prompt(line)
        if prompt is None or not self._remote_allowed(user):
            return
        reply = lambda message: self.send_private(user, message, show_ui=False, switch_page=False)
        if not self._start_request(prompt, reply):
            reply("Local AI is busy; try again later.")

    def _validate_prompt(self, prompt: str) -> Optional[str]:
        if not prompt:
            return "Usage: /ai <prompt>"
        maximum = _bounded_int(self.settings.get("max_prompt_chars"), 1000, 50, 4000)
        if len(prompt) > maximum:
            return f"Prompt is too long (maximum {maximum} characters)."
        return None

    def _remote_prompt(self, line: str) -> Optional[str]:
        if not bool(self.settings.get("allow_remote_requests", False)):
            return None
        trigger = _clean_text(self.settings.get("remote_trigger", "!ai")) or "!ai"
        text = str(line or "").strip()
        prefix = f"{trigger} "
        if not text.casefold().startswith(prefix.casefold()):
            return None
        prompt = _clean_text(text[len(prefix):])
        return None if self._validate_prompt(prompt) else prompt

    def _room_allowed(self, room: str) -> bool:
        configured = str(self.settings.get("allowed_rooms", ""))
        allowed = {item.strip().casefold() for item in configured.split(",") if item.strip()}
        return not allowed or room.casefold() in allowed

    def _remote_allowed(self, user: str) -> bool:
        now = time.monotonic()
        cooldown = _bounded_int(self.settings.get("remote_cooldown_seconds"), 60, 10, 3600)
        key = user.casefold()
        if now - self._remote_last_used.get(key, 0.0) < cooldown:
            return False
        self._remote_last_used[key] = now
        return True

    def _start_request(self, prompt: str, reply: Callable[[str], None]) -> bool:
        maximum = _bounded_int(self.settings.get("max_concurrent_requests"), 1, 1, 4)
        with self._state_lock:
            if self._active_requests >= maximum:
                return False
            self._active_requests += 1
        threading.Thread(
            target=self._request_worker,
            args=(prompt, reply),
            name="nicotine-local-ai",
            daemon=True,
        ).start()
        return True

    def _request_worker(self, prompt: str, reply: Callable[[str], None]) -> None:
        try:
            client = LocalAIClient(
                backend=str(self.settings.get("backend", "ollama")),
                base_url=str(self.settings.get("base_url", "http://127.0.0.1:11434")),
                model=str(self.settings.get("model", "")),
                api_key=str(self.settings.get("api_key", "")),
            )
            answer = client.chat(
                prompt=prompt,
                system_prompt=_clean_text(self.settings.get("system_prompt", "")),
                max_tokens=_bounded_int(self.settings.get("max_tokens"), 256, 16, 2048),
                timeout=_bounded_int(self.settings.get("timeout_seconds"), 120, 5, 600),
            )
            messages = _chunk_text(
                answer,
                _bounded_int(self.settings.get("reply_chunk_chars"), 800, 200, 1000),
                _bounded_int(self.settings.get("max_reply_chunks"), 3, 1, 5),
            )
        except AIError as error:
            self.log("AI request failed: %s", str(error))
            messages = ["Local AI is unavailable. Check the plugin server, backend, and model settings."]
        except Exception as error:
            self.log("Unexpected AI request failure: %s", str(error))
            messages = ["Local AI failed unexpectedly. Check the Nicotine+ log."]
        finally:
            with self._state_lock:
                self._active_requests -= 1

        events.invoke_main_thread(self._deliver, reply, tuple(messages))

    def _deliver(self, reply: Callable[[str], None], messages: tuple[str, ...]) -> None:
        if self._stopping:
            return
        for message in messages:
            reply(message)
