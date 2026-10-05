from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple
import json
import threading
import time
import os

import requests


def _env_optional_bool(raw: Optional[str]) -> Optional[bool]:
    """Parse a tri-state boolean.

    Returns None when the flag should be omitted from the request entirely
    (so we stay compatible with old Ollama builds / models without thinking),
    True/False otherwise.
    """
    if raw is None:
        return None
    val = str(raw).strip().lower()
    if val in {"", "auto", "unset", "default"}:
        return None
    if val in {"1", "true", "yes", "on"}:
        return True
    return False


class OllamaClient:
    """Minimal Ollama chat client with optional streaming.

    Uses:
      POST {base_url}/api/chat

    Notes:
    - keep_alive must be an integer (e.g. -1) for "keep model warm" behavior.
    - In streaming mode, we keep a reference to the active response so another
      thread (e.g. barge-in) can cancel generation by closing the socket.
    """

    def __init__(self, base_url: Optional[str] = None, timeout_s: int = 120) -> None:
        # Default to 127.0.0.1 (NOT "localhost"): on Windows, Python's requests
        # tries IPv6 ::1 first for "localhost" and stalls ~2.3s before falling
        # back to IPv4 — a fixed per-request tax. Override with OLLAMA_HOST
        # (e.g. "ollama:11434" in Docker).
        if base_url is None:
            base_url = os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434"
        if not base_url.startswith(("http://", "https://")):
            base_url = "http://" + base_url
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

        self._stream_lock = threading.Lock()
        self._active_resp: Optional[requests.Response] = None
        self._active_cancel: Optional[threading.Event] = None
        # Keep models warm; default from env or 900s.
        try:
            self.default_keep_alive = int(os.getenv("OLLAMA_KEEP_ALIVE", "900"))
        except Exception:
            self.default_keep_alive = 900

        # Thinking models (e.g. gemma4) otherwise burn the entire token budget
        # inside the `thinking` field and return an EMPTY `content`, so the voice
        # agent receives no reply at all. Default to disabling thinking.
        # OLLAMA_THINK=1/true re-enables it; OLLAMA_THINK=auto omits the flag.
        self.think = _env_optional_bool(os.getenv("OLLAMA_THINK", "false"))

    @staticmethod
    def _close_response(resp) -> None:
        # requests.close can wait for an in-flight read; never do it on the RTC loop.
        if resp is not None:
            def close():
                try:
                    resp.close()
                except Exception:
                    pass
            threading.Thread(target=close, daemon=True).start()

    def cancel_active_stream(self) -> None:
        with self._stream_lock:
            resp = self._active_resp
            event = self._active_cancel
            self._active_resp = None
            self._active_cancel = None
            if event is not None:
                event.set()
        self._close_response(resp)

    def chat(
        self,
        model: str,
        messages: List[Dict[str, str]],
        options: Optional[Dict[str, Any]] = None,
        keep_alive: Optional[int] = None,
    ) -> str:
        url = f"{self.base_url}/api/chat"

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.default_keep_alive if keep_alive is None else keep_alive,
        }
        if options:
            payload["options"] = options
        if self.think is not None:
            payload["think"] = self.think

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout_s)
                if resp.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"{resp.status_code} {resp.text}")
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                if attempt >= max_retries:
                    raise
                sleep = min(2 ** (attempt - 1), 5)
                print(f"[OLLAMA] chat retry {attempt}/{max_retries} after error: {e}")
                time.sleep(sleep)

        data = resp.json()
        message = data.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise RuntimeError(f"Unexpected Ollama response shape: {data}")
        return content

    def chat_stream(
        self, model: str, messages: List[Dict[str, str]],
        options: Optional[Dict[str, Any]] = None, keep_alive: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
        read_timeout_s: Optional[float] = None, max_stall_s: float = 30.0,
    ) -> Iterator[str]:
        """Stream with per-request cancellation, including before response headers arrive."""
        stop = stop_event if stop_event is not None else threading.Event()
        if stop.is_set():
            return
        payload = {"model": model, "messages": messages, "stream": True,
                   "keep_alive": self.default_keep_alive if keep_alive is None else keep_alive}
        if options:
            payload["options"] = options
        if self.think is not None:
            payload["think"] = self.think
        resp = None
        started = time.monotonic()
        first = True
        with self._stream_lock:
            self._active_cancel = stop
        try:
            timeout = (min(5.0, self.timeout_s), read_timeout_s or max_stall_s or 30.0)
            resp = requests.post(f"{self.base_url}/api/chat", json=payload, stream=True, timeout=timeout)
            resp.raise_for_status()
            with self._stream_lock:
                if stop.is_set() or self._active_cancel is not stop:
                    return
                self._active_resp = resp
            # A small chunk prevents requests from buffering the first few tokens.
            for line in resp.iter_lines(chunk_size=1, decode_unicode=True):
                if stop.is_set():
                    return
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("error"):
                    raise RuntimeError(obj["error"])
                token = (obj.get("message") or {}).get("content") or ""
                if token:
                    if first:
                        first = False
                        print(f"[OLLAMA_PROBE] post_to_first_token_ms={(time.monotonic()-started)*1000:.0f}", flush=True)
                    yield token
                if obj.get("done"):
                    print(f"[OLLAMA_DONE] prompt_eval_ms={obj.get('prompt_eval_duration',0)/1e6:.0f} "
                          f"eval_ms={obj.get('eval_duration',0)/1e6:.0f} prompt_tokens={obj.get('prompt_eval_count',0)}", flush=True)
                    return
        except Exception:
            if not stop.is_set():
                raise
        finally:
            with self._stream_lock:
                if self._active_cancel is stop:
                    self._active_resp = None
                    self._active_cancel = None
            if resp is not None:
                resp.close()
