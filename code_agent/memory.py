"""Conversation memory: in-context history + on-disk persistence.

Keeps the running message list within the model context window by
dropping the oldest *complete turns* once the budget is exceeded, and
persists the session to a JSON file so context survives restarts.

The OpenAI tool-calling contract requires that:
  - every `tool` message immediately follows an assistant message that
    declared the matching `tool_call_id`, and
  - every assistant `tool_calls` entry is answered by a `tool` message.
Both `_trim` (group deletion) and `_sanitize` (load-time repair) exist
to preserve this invariant — a single orphan message makes the API
reject the whole request with a 400 error.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

HISTORY_FILE = os.path.join(os.getcwd(), ".code_agent_history.json")
# Rough budget: keep at most this many messages in context.
MAX_CONTEXT_MESSAGES = 40


class ConversationMemory:
    def __init__(self, history_file: str = HISTORY_FILE,
                 max_messages: int = MAX_CONTEXT_MESSAGES) -> None:
        self.history_file = history_file
        self.max_messages = max_messages
        self.messages: List[Dict[str, Any]] = []
        self.load()

    # ----------------------------------------------------------------- #
    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})
        self._trim()

    def add_assistant(self, message: Dict[str, Any]) -> None:
        """Store an assistant message as returned by the API."""
        kept: Dict[str, Any] = {"role": "assistant"}
        if message.get("content"):
            kept["content"] = message["content"]
        if message.get("tool_calls"):
            kept["tool_calls"] = message["tool_calls"]
        self.messages.append(kept)
        self._trim()

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )
        self._trim()

    def _trim(self) -> None:
        """Bound context length by dropping the oldest *complete turns*.

        A turn = one user message plus all following assistant / tool
        messages up to (but excluding) the next user message. Deleting
        whole turns guarantees no orphan `tool` message and no dangling
        `tool_calls` is left behind; the system prompt at index 0 is
        always kept.
        """
        while len(self.messages) > self.max_messages:
            end = 1
            for i in range(1, len(self.messages)):
                if i > 1 and self.messages[i]["role"] == "user":
                    break
                end = i + 1
            if end <= 1:  # nothing deletable left (system prompt only)
                break
            del self.messages[1:end]

    # ----------------------------------------------------------------- #
    def save(self) -> None:
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.messages, f, ensure_ascii=False, indent=1)
        except OSError:
            pass  # persistence is best-effort

    def load(self) -> None:
        if not os.path.exists(self.history_file):
            return
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.messages = self._sanitize(
                    [m for m in data if isinstance(m, dict)]
                )
        except (OSError, json.JSONDecodeError):
            pass

    @staticmethod
    def _sanitize(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Repair loaded history so it satisfies the tool-call pairing rules.

        - orphan `tool` messages (no preceding assistant tool_calls) are dropped;
        - an assistant message whose `tool_calls` lack matching tool results
          keeps only its text content (or is dropped if empty).
        """
        clean: List[Dict[str, Any]] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                ids = [tc.get("id") for tc in m["tool_calls"] if isinstance(tc, dict)]
                results: Dict[Any, Dict[str, Any]] = {}
                j = i + 1
                while j < len(messages) and messages[j].get("role") == "tool":
                    results[messages[j].get("tool_call_id")] = messages[j]
                    j += 1
                if ids and all(call_id in results for call_id in ids):
                    clean.append(m)
                    clean.extend(results[call_id] for call_id in ids)
                elif m.get("content"):
                    clean.append({"role": "assistant", "content": m["content"]})
                i = j
                continue
            if m.get("role") == "tool":
                i += 1  # orphan tool message: drop
                continue
            clean.append(m)
            i += 1
        return clean

    def clear(self) -> None:
        self.messages = []
        if os.path.exists(self.history_file):
            try:
                os.unlink(self.history_file)
            except OSError:
                pass
