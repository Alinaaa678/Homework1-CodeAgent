# Design.md — Code Review Agent 设计文档

## 1. 需求对照

| 作业要求 | 本项目的实现 | 位置 |
|---|---|---|
| Agent 基本循环：输入→推理→工具调用→输出 | ReAct 风格循环，LLM 决策何时调用工具，最多 8 轮迭代防护 | `code_agent/agent.py` |
| 至少一种工具 | 五种：`read_file` / `list_files` / `search_files` / `write_file` / `run_python` | `code_agent/tools.py` |
| 命令行或 Web 界面 | 交互式 CLI（`review` / 自由对话 / `tools` / `help` / `reset` / `exit`） | `main.py` |
| 上下文记忆 | 消息历史 + 会话持久化 JSON + 完整轮次裁剪 + 加载配对修复 | `code_agent/memory.py` |
| 错误处理与重试 | LLM 层指数退避重试（429/5xx/网络）+ 尊重 `Retry-After`；工具层全异常捕获；执行超时 | `code_agent/llm.py`、`tools.py` |
| 技术栈自由 | Python 3.9+，零第三方依赖，任意 OpenAI 兼容 LLM | 全部 |

## 2. 总体架构

```
┌──────────┐   用户输入    ┌──────────────────────────── Agent ───────────────────────────┐
│  CLI     │──────────────▶│ agent.py                                                   │
│ (main.py)│               │  ┌──────────┐   messages+tools   ┌──────────┐               │
│          │◀──────────────│  │memory.py │───────────────────▶│  llm.py  │──▶ LLM API    │
└──────────┘   最终回答     │  │(历史/持久)│◀───────────────────│(重试/超时)│               │
      ▲                     │  └──────────┘                    └──────────┘               │
      │ on_tool_call 回调    │        ▲                          │ tool_calls              │
      │（工具日志，UI 解耦）  │        │ tool results             ▼                         │
      └─────────────────────│  ┌─────────────────────────────────────┐                    │
                            │  │ tools.py：read_file / list_files /  │                    │
                            │  │   search_files / write_file /      │                    │
                            │  │   run_python（子进程隔离）          │                    │
                            │  └─────────────────────────────────────┘                    │
                            └──────────────────────────────────────────────────────────────┘
```

## 3. Agent 循环（agent.py）

每个用户请求执行一个 episode：

1. **输入**：用户消息加入 memory；
2. **推理**：把完整历史 + 工具 schema 发给 LLM（`tool_choice: auto`）；
3. **工具调用**：若响应含 `tool_calls`，逐条执行（通过 `on_tool_call` 回调
   把 `[tool] name(args)` 日志交给 CLI 层，核心类不直接 print，便于测试与复用），
   结果以 `role: tool` 追加回历史，回到第 2 步；
4. **输出**：LLM 返回纯文本时作为最终回答，episode 结束；
5. **防护**：超过 `max_iterations`（默认 8）仍未收敛则返回提示，防止死循环。

这是教科书式的 ReAct/Tool-Use 模式：模型负责"决定做什么"，运行时负责"安全地执行"。

## 4. 工具设计（tools.py）

- **统一契约**：每个工具 = JSON Schema（给 LLM 看）+ Python 函数（运行时执行），
  注册在 `TOOLS` 字典中，新增工具只需加一条注册项；
- **fail-safe**：`call_tool` 捕获一切异常（含非法 JSON、非对象参数）并返回错误字符串，
  LLM 读到错误后可自行修正参数重试，Agent 不会因工具崩溃而中断
  （对应"边界情况处理"评分点）；
- **五种工具的分工**：
  | 工具 | 用途 | 安全约束 |
  |---|---|---|
  | `read_file` | 读取待审查源码 | 限 200KB、限工作区内 |
  | `list_files` | 浏览目录定位代码 | 限 200 条、跳过 .git/__pycache__ 等 |
  | `search_files` | 关键词搜索定义/调用点 | 限 50 条匹配、跳过二进制与大文件 |
  | `write_file` | 落盘修复后的代码 | 限 200KB、限工作区内、默认不覆盖已有文件 |
  | `run_python` | 实际复现/验证 bug | 独立子进程、10s 超时、临时文件即删 |
- **安全性**：`_safe_path` 用 `os.path.normcase` 归一化后比较前缀，
  在 Windows/macOS 大小写不敏感文件系统上也不会被 `../` 或大小写变体绕过。

## 5. LLM 客户端（llm.py）

- 直接以标准库 `urllib` 调用 OpenAI 兼容的 `/chat/completions`，**零依赖**；
- 配置优先级：构造参数 > `config.json` > 环境变量；
- 重试策略：429 / 5xx / 网络错误指数退避（1s, 2s, 4s, 8s，默认最多 4 次），
  若服务端返回 `Retry-After` 头则按服务端要求等待（封顶 30s）；
  4xx（非 429）不重试直接报错；HTTP 200 但响应非 JSON 时抛出明确错误。

## 6. 记忆（memory.py）

- **会话内**：完整消息列表（system / user / assistant / tool）。
- **轮次裁剪**：超过 40 条时按"完整轮次"删除最旧历史——一轮 = 一条 user 消息
  及其后所有 assistant / tool 消息。OpenAI 工具调用协议要求 `tool` 消息必须紧跟
  声明了对应 `tool_call_id` 的 assistant 消息，逐条删除会产生孤儿 `tool` 消息
  或悬空的 `tool_calls`，导致 API 直接 400 拒绝整个请求；按轮次删除从结构上
  避免了这个问题。
- **加载修复**：从磁盘加载历史时 `_sanitize` 自动丢弃孤儿 `tool` 消息、
  把缺少工具结果的 assistant `tool_calls` 降级为纯文本，保证旧版本或损坏的
  历史文件不会让会话无法继续。
- **会话间**：保存为 `.code_agent_history.json`，重启 CLI 可继续追问；
  提供 `clear()` 与 CLI `reset` 命令。

## 7. 关键取舍

| 决策 | 理由 |
|---|---|
| 不用 LangChain 等框架 | 作业评分含"架构清晰度"：裸实现能逐行对应 Agent 设计模式，且无依赖、易运行、易评审 |
| 选代码审查方向 + run_python 工具 | 让 Agent "读代码→实际复现 bug→给报告"，完整展示工具增强的 Agent 价值 |
| 增加 write_file 工具 | 让"追问→修复"闭环可以真正落盘，而不只是输出建议；覆盖保护防止误写 |
| UI 日志用回调注入 | Agent 核心保持纯库形态，测试静默，CLI 自由控制输出 |
| 中文报告 + 分级 emoji | 输出可读性高，演示效果好 |

## 8. 测试

`tests/test_agent_loop.py` 共 11 项离线测试（FakeLLM + monkeypatch，**无需 API Key**）：

1. 工具调用回环与消息入历史顺序；
2. `on_tool_call` 回调触发；
3. 最大迭代防护（死循环收敛）；
4. 工具直通：执行成功/超时/路径越界/文件不存在/坏 JSON/非对象参数/未知工具；
5. `search_files`：命中、未命中、目录越界；
6. `write_file`：写入、覆盖保护、显式覆盖、路径越界；
7. 记忆持久化与清空；
8. 轮次裁剪后 tool 配对完整性（无孤儿/悬空）；
9. 加载时修复孤儿消息与未应答的 tool_calls；
10. LLM 重试：429 + `Retry-After` 等待时长、400 不重试；
11. 终端渲染：代码栅栏剥离、表格按显示宽度对齐（含中文全角）、空输入。

另：`code_agent/render.py` 负责把 LLM 的 Markdown 报告转成终端可读文本
（去代码栅栏、管道表格对齐、标题/粗体 ANSI 高亮，非 TTY 时自动降级为纯文本），
CLI 通过 `render_markdown()` 统一输出 Agent 回复。
