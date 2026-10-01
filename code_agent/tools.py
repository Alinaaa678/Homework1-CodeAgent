"""Tool registry for the agent.

Every tool follows the OpenAI function-calling contract:
  - a JSON schema (name / description / parameters)
  - a python callable invoked with parsed arguments

All filesystem access is confined to the current workspace directory,
and code execution runs in a subprocess with a hard timeout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Tuple

MAX_FILE_BYTES = 200_000          # ~200 KB per file read / write
MAX_LIST_ENTRIES = 200
MAX_SEARCH_MATCHES = 50           # max matches returned by search_files
EXEC_TIMEOUT = 10                 # seconds; tests may monkeypatch this
WORKSPACE_ROOT = os.path.abspath(os.getcwd())

SKIP_DIRS = (".git", "__pycache__", "node_modules", ".venv", "venv")


# --------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------- #
def read_file(path: str) -> str:
    """Read a source file (bounded size)."""
    abs_path = _safe_path(path)
    if not os.path.isfile(abs_path):
        return f"错误：文件不存在 — {path}"
    size = os.path.getsize(abs_path)
    if size > MAX_FILE_BYTES:
        return f"错误：文件过大（{size} 字节 > {MAX_FILE_BYTES} 字节上限），请分段审查。"
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        return f"错误：{path} 不是 UTF-8 文本文件。"
    except OSError as e:
        return f"错误：读取失败 — {e}"


def list_files(directory: str = ".") -> str:
    """List files/directories (bounded entries, relative paths)."""
    abs_dir = _safe_path(directory)
    if not os.path.isdir(abs_dir):
        return f"错误：目录不存在 — {directory}"
    entries: List[str] = []
    for root, dirs, files in os.walk(abs_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel_root = os.path.relpath(root, WORKSPACE_ROOT)
        for name in sorted(files):
            entries.append(os.path.join(rel_root, name) if rel_root != "." else name)
            if len(entries) >= MAX_LIST_ENTRIES:
                entries.append("...(已达条目上限，结果被截断)")
                return "\n".join(entries)
    return "\n".join(entries) if entries else "(空目录)"


def search_files(keyword: str, directory: str = ".") -> str:
    """Search *keyword* (plain substring, case-sensitive) across workspace files."""
    abs_dir = _safe_path(directory)
    if not os.path.isdir(abs_dir):
        return f"错误：目录不存在 — {directory}"
    matches: List[str] = []
    for root, dirs, files in os.walk(abs_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            fpath = os.path.join(root, name)
            rel = os.path.relpath(fpath, WORKSPACE_ROOT)
            try:
                if os.path.getsize(fpath) > MAX_FILE_BYTES:
                    continue
                with open(fpath, "r", encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        if keyword in line:
                            matches.append(f"{rel}:{lineno}: {line.strip()[:120]}")
                            if len(matches) >= MAX_SEARCH_MATCHES:
                                matches.append("...(已达匹配上限，结果被截断)")
                                return "\n".join(matches)
            except (UnicodeDecodeError, OSError):
                continue  # skip binary / unreadable files
    return "\n".join(matches) if matches else f"未找到包含 {keyword!r} 的内容。"


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    """Write text to a workspace file (bounded size, overwrite guarded)."""
    abs_path = _safe_path(path)
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return f"错误：内容超过 {MAX_FILE_BYTES} 字节上限。"
    if os.path.exists(abs_path) and not overwrite:
        return f"错误：文件已存在 — {path}。如需覆盖请设置 overwrite=true。"
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"已写入 {path}（{len(content)} 字符）。"
    except OSError as e:
        return f"错误：写入失败 — {e}"


def run_python(code: str) -> str:
    """Execute a Python snippet in a subprocess with timeout; return stdout/stderr."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp = f.name
        proc = subprocess.run(
            [sys.executable, tmp],
            capture_output=True,
            text=True,
            timeout=EXEC_TIMEOUT,
            cwd=WORKSPACE_ROOT,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        status = "成功" if proc.returncode == 0 else f"退出码 {proc.returncode}"
        return f"[执行{status}]\n{out.strip() or '(无输出)'}"
    except subprocess.TimeoutExpired:
        return f"错误：执行超时（>{EXEC_TIMEOUT}s）。"
    except OSError as e:
        return f"错误：无法启动子进程 — {e}"
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------- #
def _safe_path(path: str) -> str:
    """Resolve *path* and confine it inside the workspace root.

    Uses os.path.normcase so the check also works on case-insensitive
    filesystems (Windows / macOS).
    """
    abs_path = os.path.abspath(os.path.join(WORKSPACE_ROOT, path))
    root = os.path.normcase(WORKSPACE_ROOT)
    candidate = os.path.normcase(abs_path)
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError(f"路径越界，已拒绝访问：{path}")
    return abs_path


# name -> (schema, callable)
TOOLS: Dict[str, Tuple[Dict[str, Any], Callable[..., str]]] = {
    "read_file": (
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取工作区内的文本文件内容，用于获取待审查的源代码。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对于工作区的文件路径"}
                    },
                    "required": ["path"],
                },
            },
        },
        read_file,
    ),
    "list_files": (
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "列出工作区内某目录的文件结构，用于定位待审查代码。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string", "description": "目录路径，默认为当前目录"}
                    },
                },
            },
        },
        list_files,
    ),
    "search_files": (
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": "在工作区内按关键词搜索文本文件，返回 路径:行号: 内容，"
                               "用于定位函数定义、调用点或特定代码片段。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "要搜索的关键词（区分大小写）"},
                        "directory": {"type": "string", "description": "搜索范围目录，默认为整个工作区"},
                    },
                    "required": ["keyword"],
                },
            },
        },
        search_files,
    ),
    "write_file": (
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "把文本内容写入工作区内的文件（自动创建父目录），"
                               "用于在用户要求时落盘修复后的代码。默认不覆盖已有文件。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对于工作区的目标文件路径"},
                        "content": {"type": "string", "description": "要写入的完整文件内容"},
                        "overwrite": {"type": "boolean", "description": "是否允许覆盖已有文件，默认 false"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        write_file,
    ),
    "run_python": (
        {
            "type": "function",
            "function": {
                "name": "run_python",
                "description": "在隔离子进程中执行一段 Python 代码并返回输出，"
                               "用于验证 bug 复现、检查边界行为或测试修复方案。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "要执行的 Python 代码"}
                    },
                    "required": ["code"],
                },
            },
        },
        run_python,
    ),
}


def tool_schemas() -> List[Dict[str, Any]]:
    return [schema for schema, _ in TOOLS.values()]


def call_tool(name: str, arguments_json: str) -> str:
    """Dispatch a tool call; never raise — always return a string for the LLM."""
    if name not in TOOLS:
        return f"错误：未知工具 {name}"
    _, func = TOOLS[name]
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as e:
        return f"错误：工具参数不是合法 JSON — {e}"
    if not isinstance(args, dict):
        return "错误：工具参数必须是 JSON 对象。"
    try:
        return func(**args)
    except ValueError as e:                       # path confinement violation
        return f"错误：{e}"
    except TypeError as e:                        # wrong arguments
        return f"错误：工具参数不合法 — {e}"
    except Exception as e:                        # last-resort guard
        return f"错误：工具执行失败 — {e}"
