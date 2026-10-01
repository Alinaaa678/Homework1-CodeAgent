#!/usr/bin/env python3
"""Code Review Agent — CLI 入口（软件工程 Homework 1）。

用法：
    python main.py                    # 交互式 CLI
    python main.py --review a.py      # 直接审查一个文件
    python main.py --reset            # 清空历史记忆

配置文件（可选）：config.json
    {"base_url": "https://api.deepseek.com/v1",
     "api_key":  "sk-...",
     "model":    "deepseek-chat"}
也可以用环境变量 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from code_agent.agent import CodeReviewAgent
from code_agent.llm import ChatClient, LLMError
from code_agent.render import enable_ansi, render_markdown
from code_agent.tools import MAX_FILE_BYTES, tool_schemas


def load_config() -> dict:
    cfg = {}
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"⚠️ config.json 读取失败：{e}（将仅使用环境变量）")
    return cfg


def _print_tool_call(name: str, args: str) -> None:
    """CLI-side tool logger, injected into the agent as a callback."""
    print(f"  [tool] {name}({args[:80]})")


def _print_reply(text: str) -> None:
    """Render the agent's Markdown reply nicely in the terminal."""
    rendered = render_markdown(text, color=sys.stdout.isatty())
    print(f"\nAgent > {rendered}\n")


def build_agent() -> CodeReviewAgent:
    cfg = load_config()
    client = ChatClient(
        base_url=cfg.get("base_url"),
        api_key=cfg.get("api_key"),
        model=cfg.get("model"),
    )
    return CodeReviewAgent(client, on_tool_call=_print_tool_call)


BANNER = """\
============================================================
  Code Review Agent  v1.2  —  Homework 1
  命令:
    review <文件路径>   审查指定代码文件
    tools               查看 Agent 可用的工具
    help                重新显示本帮助
    reset               清空会话记忆
    exit / quit         退出
  其他输入视为与 Agent 自由对话（可追问上一轮审查）。
============================================================"""


def _read_code_file(path: str) -> str:
    """Read a code file for review, with the same size cap as the tools."""
    size = os.path.getsize(path)  # may raise OSError -> handled by caller
    if size > MAX_FILE_BYTES:
        raise LLMError(f"文件过大（{size} 字节 > {MAX_FILE_BYTES} 字节上限），请分段审查。")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def interactive(agent: CodeReviewAgent) -> None:
    print(BANNER)
    while True:
        try:
            user = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            return
        if not user:
            continue
        cmd, _, rest = user.partition(" ")
        if cmd in ("exit", "quit"):
            print("再见！")
            return
        if cmd == "help":
            print(BANNER)
            continue
        if cmd == "tools":
            for schema in tool_schemas():
                func = schema["function"]
                print(f"  {func['name']}: {func['description']}")
            continue
        if cmd == "reset":
            agent.memory.clear()
            print("✅ 会话记忆已清空。")
            continue
        try:
            if cmd == "review":
                if not rest:
                    print("用法：review <文件路径>")
                    continue
                code = _read_code_file(rest)
                language = os.path.splitext(rest)[1].lstrip(".") or "python"
                reply = agent.review_code(code, language=language)
            else:
                reply = agent.run(user)
            _print_reply(reply)
        except KeyboardInterrupt:
            print("\n⏹ 已中断当前请求。")
        except LLMError as e:
            print(f"\n❌ LLM 调用失败：{e}")
        except (OSError, UnicodeDecodeError) as e:
            print(f"\n❌ 文件错误：{e}")


def main() -> None:
    enable_ansi()
    parser = argparse.ArgumentParser(description="Code Review Agent CLI")
    parser.add_argument("--review", metavar="FILE", help="直接审查指定文件后退出")
    parser.add_argument("--reset", action="store_true", help="清空历史记忆后退出")
    args = parser.parse_args()

    if args.reset:
        from code_agent.memory import ConversationMemory
        ConversationMemory().clear()
        print("✅ 会话记忆已清空。")
        return

    try:
        agent = build_agent()
    except LLMError as e:
        print(f"❌ 初始化失败：{e}")
        print("提示：复制 config.example.json 为 config.json 并填入你的 API 信息，")
        print("或设置环境变量 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL。")
        sys.exit(1)

    if args.review:
        try:
            code = _read_code_file(args.review)
            language = os.path.splitext(args.review)[1].lstrip(".") or "python"
            _print_reply(agent.review_code(code, language=language))
        except (OSError, UnicodeDecodeError, LLMError) as e:
            print(f"❌ {e}")
            sys.exit(1)
    else:
        interactive(agent)


if __name__ == "__main__":
    main()
