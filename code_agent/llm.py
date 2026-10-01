"""LLM client: OpenAI-compatible chat completions over stdlib urllib.

Zero third-party dependencies on purpose, so the project runs anywhere
Python 3.9+ is available. Supports tool calling and exponential-backoff
retry on transient errors (429 / 5xx / network failure).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class LLMError(Exception):
    """Raised when the LLM call ultimately fails after all retries."""


class ChatClient:
    """Thin wrapper around an OpenAI-compatible /chat/completions endpoint.

    base_url / api_key / model can come from constructor arguments,
    environment variables (OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL)
    or a config.json file next to main.py.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: int = 4,
        timeout: int = 60,
    ) -> None:
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_MODEL", "deepseek-chat")
        self.max_retries = max_retries
        self.timeout = timeout
        if not self.base_url:
            raise LLMError(
                "未配置 LLM 服务地址：请设置 OPENAI_BASE_URL 或在 config.json 中填写。"
            )
        if not self.api_key:
            raise LLMError("未配置 API Key：请设置 OPENAI_API_KEY 或在 config.json 中填写。")

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        """Send one chat request; retry with exponential backoff on failures."""
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            wait = float(2 ** attempt)  # 1s, 2s, 4s, 8s
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 429 (rate limit) and 5xx are transient -> retry.
                transient = e.code == 429 or 500 <= e.code < 600
                last_err = LLMError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
                if not transient or attempt == self.max_retries:
                    raise last_err
                # Honor the server's Retry-After hint when present (capped).
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    try:
                        wait = min(float(retry_after), 30.0)
                    except ValueError:
                        pass
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = LLMError(f"网络错误: {e}")
                if attempt == self.max_retries:
                    raise last_err
            except json.JSONDecodeError as e:
                raise LLMError(f"LLM 返回了无法解析的响应: {e}")
            time.sleep(wait)

        raise LLMError(f"LLM 请求失败（已重试 {self.max_retries} 次）: {last_err}")
