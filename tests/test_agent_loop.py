"""Offline self-tests for the agent loop, tools, memory and LLM retry.

Run:  python tests/test_agent_loop.py
Uses scripted fakes / monkeypatching — no network or API key needed.
"""

import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from email.message import Message

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import code_agent.llm as llm_mod                            # noqa: E402
import code_agent.tools as tools_mod                        # noqa: E402
from code_agent.agent import CodeReviewAgent                # noqa: E402
from code_agent.llm import ChatClient, LLMError             # noqa: E402
from code_agent.memory import ConversationMemory            # noqa: E402
from code_agent.render import render_markdown               # noqa: E402
from code_agent.tools import call_tool, tool_schemas        # noqa: E402


class FakeLLM(ChatClient):
    """ChatClient that plays a scripted conversation instead of calling the API."""

    def __init__(self, script):
        self.script = list(script)
        super().__init__(base_url="http://fake", api_key="fake", model="fake")

    def chat(self, messages, tools=None, temperature=0.2):
        step = self.script.pop(0)
        return step


def make_agent(script, tmpdir, **kwargs):
    mem = ConversationMemory(history_file=os.path.join(tmpdir, "h.json"), max_messages=20)
    return CodeReviewAgent(FakeLLM(script), memory=mem, **kwargs)


# --------------------------------------------------------------------- #
# Agent loop
# --------------------------------------------------------------------- #
def test_tool_roundtrip(tmpdir):
    """Loop: model requests read_file -> tool result -> model answers."""
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file",
                     "arguments": '{"path": "samples/buggy_calculator.py"}'},
    }
    agent = make_agent([
        {"choices": [{"message": {"role": "assistant", "content": None,
                                  "tool_calls": [tool_call]}}]},
        {"choices": [{"message": {"role": "assistant",
                                  "content": "发现 4 个问题。"}}]},
    ], tmpdir)
    reply = agent.run("帮我审查 samples/buggy_calculator.py")
    assert reply == "发现 4 个问题。"
    # memory now holds: system, user, assistant(tool_calls), tool, assistant
    roles = [m["role"] for m in agent.memory.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"], roles
    assert "def divide" in agent.memory.messages[3]["content"]


def test_tool_call_callback(tmpdir):
    """on_tool_call callback fires once per tool call (UI decoupling)."""
    seen = []
    tc = {"id": "c1", "type": "function",
          "function": {"name": "list_files", "arguments": "{}"}}
    agent = make_agent([
        {"choices": [{"message": {"role": "assistant", "content": None,
                                  "tool_calls": [tc]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "done"}}]},
    ], tmpdir, on_tool_call=lambda name, args: seen.append(name))
    assert agent.run("看看目录") == "done"
    assert seen == ["list_files"], seen


def test_max_iterations_guard(tmpdir):
    """Loop must stop after max_iterations instead of hanging."""
    tc = {"id": "c", "type": "function",
          "function": {"name": "list_files", "arguments": "{}"}}
    script = [{"choices": [{"message": {"role": "assistant", "content": None,
                                        "tool_calls": [tc]}}]}] * 10
    agent = make_agent(script, tmpdir)
    agent.max_iterations = 3
    assert "最大迭代" in agent.run("死循环测试")


# --------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------- #
def test_tools_directly():
    assert tool_schemas() and all("function" in s for s in tool_schemas())
    names = [s["function"]["name"] for s in tool_schemas()]
    assert names == ["read_file", "list_files", "search_files", "write_file",
                     "run_python"], names

    out = call_tool("run_python", '{"code": "print(1+1)"}')
    assert "2" in out and "成功" in out

    old_timeout = tools_mod.EXEC_TIMEOUT
    tools_mod.EXEC_TIMEOUT = 2  # keep the timeout test fast
    try:
        out = call_tool("run_python", '{"code": "import time; time.sleep(99)"}')
    finally:
        tools_mod.EXEC_TIMEOUT = old_timeout
    assert "超时" in out

    out = call_tool("read_file", '{"path": "../outside.txt"}')
    assert "越界" in out or "错误" in out
    out = call_tool("read_file", '{"path": "不存在的文件.py"}')
    assert "不存在" in out
    out = call_tool("run_python", "not-json")
    assert "错误" in out
    out = call_tool("read_file", '[1, 2]')  # valid JSON, wrong type
    assert "错误" in out
    out = call_tool("no_such_tool", "{}")
    assert "未知工具" in out


def test_search_files():
    out = call_tool("search_files", '{"keyword": "def divide"}')
    assert "buggy_calculator.py" in out, out
    out = call_tool("search_files",
                    '{"keyword": "不存在的关键词xyz123", "directory": "samples"}')
    assert "未找到" in out
    out = call_tool("search_files", '{"keyword": "x", "directory": "../"}')
    assert "越界" in out or "错误" in out


def test_write_file():
    target = os.path.join(tools_mod.WORKSPACE_ROOT, ".tmp_write_test.txt")
    try:
        out = call_tool("write_file",
                        '{"path": ".tmp_write_test.txt", "content": "hello"}')
        assert "已写入" in out, out
        assert open(target, encoding="utf-8").read() == "hello"
        # overwrite guard
        out = call_tool("write_file",
                        '{"path": ".tmp_write_test.txt", "content": "x"}')
        assert "已存在" in out
        out = call_tool("write_file",
                        '{"path": ".tmp_write_test.txt", "content": "x",'
                        ' "overwrite": true}')
        assert "已写入" in out
        # confinement
        out = call_tool("write_file", '{"path": "../evil.txt", "content": "x"}')
        assert "越界" in out or "错误" in out
    finally:
        if os.path.exists(target):
            os.unlink(target)


# --------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------- #
def test_memory_persistence(tmpdir):
    mem = ConversationMemory(history_file=os.path.join(tmpdir, "h.json"))
    mem.add_user("hi")
    mem.add_tool_result("id1", "result")
    mem.save()
    mem2 = ConversationMemory(history_file=os.path.join(tmpdir, "h.json"))
    # sanitize keeps user msg; orphan tool msg (no matching tool_calls) dropped
    contents = [m.get("content") for m in mem2.messages]
    assert "hi" in contents, contents
    mem2.clear()
    assert not os.path.exists(os.path.join(tmpdir, "h.json"))


def test_memory_trim_keeps_tool_pairing(tmpdir):
    """Trimming deletes whole turns: no orphan tool messages, no dangling calls."""
    mem = ConversationMemory(history_file=os.path.join(tmpdir, "h.json"),
                             max_messages=6)
    mem.messages.append({"role": "system", "content": "s"})
    for k in range(5):
        mem.add_user(f"q{k}")
        mem.add_assistant({"role": "assistant",
                           "tool_calls": [{"id": f"c{k}"}]})
        mem.add_tool_result(f"c{k}", "r")
        mem.add_assistant({"role": "assistant", "content": f"a{k}"})
    assert len(mem.messages) <= 6, len(mem.messages)
    assert mem.messages[0]["role"] == "system"
    for i, m in enumerate(mem.messages):
        if m["role"] == "tool":
            prev = mem.messages[i - 1]
            assert prev["role"] == "assistant", mem.messages
            ids = [tc["id"] for tc in prev.get("tool_calls", [])]
            assert m["tool_call_id"] in ids, mem.messages
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = {tc["id"] for tc in m["tool_calls"]}
            following = mem.messages[i + 1:i + 1 + len(ids)]
            assert {t.get("tool_call_id") for t in following} == ids, mem.messages


def test_memory_load_sanitizes_orphans(tmpdir):
    """Old / corrupted history files must not break the API pairing rules."""
    path = os.path.join(tmpdir, "h.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([
            {"role": "system", "content": "s"},
            {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "c1"}]},  # unanswered
            {"role": "assistant", "content": "text",
             "tool_calls": [{"id": "c2"}]},                       # unanswered w/ text
            {"role": "user", "content": "again"},
        ], f)
    mem = ConversationMemory(history_file=path)
    roles = [m["role"] for m in mem.messages]
    assert "tool" not in roles, roles
    for m in mem.messages:
        assert not m.get("tool_calls"), m
    assert any(m.get("content") == "text" for m in mem.messages)


# --------------------------------------------------------------------- #
# LLM retry
# --------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code, retry_after=None):
    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://fake/chat/completions", code, "err",
                                  hdrs, io.BytesIO(b"server says no"))


def test_llm_retry_with_retry_after():
    """429 -> retry honoring Retry-After; success on 3rd attempt."""
    client = ChatClient(base_url="http://fake", api_key="k", model="m")
    calls = {"n": 0}
    waits = []
    real_urlopen = urllib.request.urlopen
    real_sleep = llm_mod.time.sleep

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429, retry_after=7)
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    urllib.request.urlopen = fake_urlopen
    llm_mod.time.sleep = waits.append
    try:
        resp = client.chat(messages=[{"role": "user", "content": "hi"}])
    finally:
        urllib.request.urlopen = real_urlopen
        llm_mod.time.sleep = real_sleep
    assert resp["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 3
    assert waits == [7.0, 7.0], waits  # Retry-After respected, not 1s/2s


def test_llm_no_retry_on_400():
    """Non-transient 4xx must fail immediately without retrying."""
    client = ChatClient(base_url="http://fake", api_key="k", model="m")
    calls = {"n": 0}
    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(400)

    urllib.request.urlopen = fake_urlopen
    try:
        try:
            client.chat(messages=[{"role": "user", "content": "hi"}])
            raise AssertionError("should have raised LLMError")
        except LLMError as e:
            assert "HTTP 400" in str(e)
    finally:
        urllib.request.urlopen = real_urlopen
    assert calls["n"] == 1


# --------------------------------------------------------------------- #
# Terminal rendering
# --------------------------------------------------------------------- #
def test_render_markdown():
    md = ("## 标题\n\n```py\ndef f():\n    pass\n```\n\n"
          "| 用例 | 结果 |\n|---|---|\n| a([]) | 中文报错 ✅ |\n\n"
          "**重点** 和 `code`\n---\n尾")
    out = render_markdown(md, color=False)
    assert "```" not in out          # code fences stripped
    assert "##" not in out           # heading markers stripped
    assert "|---|" not in out and "---" not in out  # separator row / hr gone
    assert "def f():" in out and "中文报错" in out
    assert "**" not in out and "`code`" not in out  # inline markers stripped
    assert "|" not in out            # pipes replaced by aligned columns
    assert render_markdown("", color=False) == ""


# --------------------------------------------------------------------- #
if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        test_tool_roundtrip(d)
    with tempfile.TemporaryDirectory() as d:
        test_tool_call_callback(d)
    with tempfile.TemporaryDirectory() as d:
        test_max_iterations_guard(d)
    test_tools_directly()
    test_search_files()
    test_write_file()
    with tempfile.TemporaryDirectory() as d:
        test_memory_persistence(d)
    with tempfile.TemporaryDirectory() as d:
        test_memory_trim_keeps_tool_pairing(d)
    with tempfile.TemporaryDirectory() as d:
        test_memory_load_sanitizes_orphans(d)
    test_llm_retry_with_retry_after()
    test_llm_no_retry_on_400()
    test_render_markdown()
    print("✅ 全部 11 项测试通过：Agent 循环 / 工具×5 / 记忆裁剪与修复 / 重试与 Retry-After / 终端渲染")
