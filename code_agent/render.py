"""Tiny Markdown -> terminal renderer (zero-dependency).

LLM 输出的审查报告是 Markdown；终端不认识 Markdown，会把
```、|---|---|、**粗体** 这类标记原样显示，看起来很乱。
这里做一个轻量转换：

- 去掉代码块栅栏，代码缩进显示；
- 标题加粗高亮；
- 管道表格按字符显示宽度对齐（兼容中文全角字符）；
- **粗体** / `行内代码` 转成 ANSI 样式（不支持时降级为纯文本）。
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import List

_BOLD = "\033[1m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def enable_ansi() -> None:
    """Enable VT100 escape processing on legacy Windows consoles."""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x4)
        except Exception:
            pass  # 失败也无妨：VS Code / Windows Terminal 本来就支持 ANSI


# --------------------------------------------------------------------- #
def _width(text: str) -> int:
    """Display width: CJK wide/full-width chars count as 2 columns."""
    w = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _inline(text: str, color: bool) -> str:
    """Convert **bold** and `code` to ANSI styles, or strip them."""
    if color:
        text = re.sub(r"\*\*(.+?)\*\*", _BOLD + r"\1" + _RESET, text)
        text = re.sub(r"`([^`]+)`", _CYAN + r"\1" + _RESET, text)
    else:
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


def _is_table_line(stripped: str) -> bool:
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _render_table(lines: List[str]) -> List[str]:
    """Align a pipe table by display width; drop the |---| separator row."""
    rows: List[List[str]] = []
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue  # Markdown separator row
        rows.append([_inline(c, color=False) for c in cells])
    if not rows:
        return []
    cols = max(len(r) for r in rows)
    for r in rows:
        r += [""] * (cols - len(r))
    widths = [max(_width(r[i]) for r in rows) for i in range(cols)]
    out: List[str] = []
    for idx, r in enumerate(rows):
        out.append("  " + "   ".join(_pad(c, widths[i]) for i, c in enumerate(r)).rstrip())
        if idx == 0 and len(rows) > 1:
            out.append("  " + "   ".join("─" * w for w in widths).rstrip())
    return out


def render_markdown(text: str, color: bool = True) -> str:
    """Render an LLM Markdown reply as terminal-friendly text."""
    if not text:
        return text
    out_lines: List[str] = []
    table_buf: List[str] = []
    in_code = False

    def flush_table() -> None:
        nonlocal table_buf
        if table_buf:
            out_lines.extend(_render_table(table_buf))
            table_buf = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            flush_table()
            out_lines.append("    " + line)
            continue
        if _is_table_line(stripped):
            table_buf.append(line)
            continue
        flush_table()
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            heading = _inline(m.group(2), color)
            out_lines.append("")
            out_lines.append((_BOLD + heading + _RESET) if color else heading)
            continue
        if re.fullmatch(r"-{3,}", stripped):
            out_lines.append("─" * 40)
            continue
        out_lines.append(_inline(line, color))
    flush_table()

    rendered = "\n".join(out_lines)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip("\n")
