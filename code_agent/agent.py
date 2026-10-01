"""The agent core: the input -> reasoning -> tool call -> output loop."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .llm import ChatClient
from .memory import ConversationMemory
from .prompts import REVIEW_TEMPLATE, SYSTEM_PROMPT
from .tools import call_tool, tool_schemas


class CodeReviewAgent:
    """A minimal ReAct-style agent built on an OpenAI-compatible LLM.

    Loop (per user request):
        1. send conversation + tool schemas to the LLM   (reasoning)
        2. if the model requests tool calls -> execute    (acting)
           tools and append results, goto 1
        3. when the model answers with plain text ->      (output)
           return the answer to the user

    UI concerns are decoupled through ``on_tool_call``: the core never
    prints directly, the CLI layer injects a logger callback instead.
    """

    def __init__(
        self,
        client: ChatClient,
        memory: Optional[ConversationMemory] = None,
        max_iterations: int = 8,
        on_tool_call: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.client = client
        self.memory = memory or ConversationMemory()
        self.max_iterations = max_iterations
        self.on_tool_call = on_tool_call
        if not any(m.get("role") == "system" for m in self.memory.messages):
            self.memory.messages.insert(
                0, {"role": "system", "content": SYSTEM_PROMPT}
            )

    # ------------------------------------------------------------------ #
    def run(self, user_input: str) -> str:
        """Drive one full agent episode for a user request."""
        self.memory.add_user(user_input)
        final_text = ""

        for _ in range(self.max_iterations):
            response = self.client.chat(
                messages=self.memory.messages,
                tools=tool_schemas(),
            )
            choice = response["choices"][0]
            message: Dict[str, Any] = choice["message"]

            # Plain-text answer -> episode finished.
            if not message.get("tool_calls"):
                final_text = message.get("content") or "(空回复)"
                self.memory.add_assistant(message)
                break

            # Tool call(s) requested -> execute and feed results back.
            self.memory.add_assistant(message)
            for tool_call in message["tool_calls"]:
                name = tool_call["function"]["name"]
                args = tool_call["function"].get("arguments", "")
                if self.on_tool_call:
                    self.on_tool_call(name, args)
                result = call_tool(name, args)
                self.memory.add_tool_result(tool_call["id"], result)
        else:
            final_text = "⚠️ 已达最大迭代次数，Agent 未能完成本轮任务。"

        self.memory.save()
        return final_text

    # ------------------------------------------------------------------ #
    def review_code(self, code: str, language: str = "python") -> str:
        """Convenience wrapper: build a review request and run the loop."""
        return self.run(REVIEW_TEMPLATE.format(language=language, code=code))
