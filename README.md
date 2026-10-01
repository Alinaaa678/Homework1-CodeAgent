# Code Review Agent — 软件工程 Homework 1

一个简单的**代码审查 Agent**：读取工作区代码、实际运行可疑片段验证 bug、输出分级审查报告，并支持多轮追问与落盘修复。

## 功能特性

- **Agent 循环**：输入 → 推理 → 工具调用 → 输出（ReAct 风格，最多 8 轮迭代防护）
- **内置 5 种工具**：
  | 工具 | 说明 |
  |---|---|
  | `read_file` | 读取工作区内文本文件（限 200KB） |
  | `list_files` | 浏览目录结构，定位待审查代码 |
  | `search_files` | 按关键词搜索代码，返回 `路径:行号: 内容`，定位定义与调用点 |
  | `write_file` | 落盘修复后的代码（自动建目录，默认不覆盖已有文件） |
  | `run_python` | 在隔离子进程中执行 Python 代码（10s 超时），用于复现/验证 bug |
- **上下文记忆**：会话历史持久化到 `.code_agent_history.json`，支持多轮追问与 `reset` 清空；
  按"完整轮次"裁剪历史、加载时自动修复孤儿消息，保证长会话不会触发 API 的 tool 配对报错
- **错误处理与重试**：LLM 调用对 429/5xx/网络错误指数退避重试（1s→2s→4s→8s），
  并尊重服务端的 `Retry-After` 提示；工具调用全部包裹异常，绝不向 LLM 抛出
- **安全边界**：文件读写限制在工作区内（防路径穿越，兼容 Windows 大小写不敏感），
  代码执行限时限量，覆盖写入需显式确认
- **架构解耦**：Agent 核心不直接打印，通过 `on_tool_call` 回调把工具日志交给 CLI 层
- **零第三方依赖**：仅使用 Python 3.9+ 标准库

## 快速开始

```bash
# 1. 配置（三选一）
cp config.example.json config.json   # 填入 base_url / api_key / model
# 或设置环境变量：
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=deepseek-chat

# 2. 交互式使用
python main.py

# 3. 或直接审查一个文件
python main.py --review samples/buggy_calculator.py

# 4. 离线自测（无需 API Key）
python tests/test_agent_loop.py
```

### CLI 命令

```
你 > review src/main.py      # 审查指定文件
你 > 上面第 2 个问题怎么改？   # 基于记忆自由追问
你 > 把修复后的版本写入 fixed.py   # Agent 调用 write_file 落盘
你 > tools                   # 查看可用工具
你 > reset                   # 清空会话记忆
你 > exit                    # 退出
```

## 支持任意 OpenAI 兼容服务

DeepSeek、通义千问、月之暗面、本地 Ollama / vLLM 等均可，只要改 `base_url` / `model`。

> ⚠️ `config.json` 含有 API Key，已加入 `.gitignore`，提交仓库时不会泄露。

## 项目结构

```
├── main.py               # CLI 入口（review / tools / help / reset / exit）
├── code_agent/
│   ├── agent.py          # Agent 核心循环（ReAct，UI 回调解耦）
│   ├── llm.py            # OpenAI 兼容客户端 + 指数退避/Retry-After 重试
│   ├── tools.py          # 工具注册表与实现（5 种工具 + 路径安全围栏）
│   ├── memory.py         # 上下文记忆（持久化 + 轮次裁剪 + 加载修复）
│   ├── prompts.py        # 系统提示词
│   └── render.py         # Markdown → 终端渲染（表格对齐 / 标题加粗）
├── samples/              # 待审查示例代码（故意埋 bug）
├── tests/                # 离线自测（FakeLLM + monkeypatch，无需 Key）
├── .gitignore            # 屏蔽 config.json / 历史文件 / __pycache__
└── Design.md             # 设计文档
```

## 示例对话

```
你 > review samples/buggy_calculator.py
  [tool] read_file({"path": "samples/buggy_calculator.py"})
  [tool] run_python({"code": "from samples.buggy_calculator import divide..."})
Agent > ## 审查报告
🔴 严重：divide() 未处理 b=0，将抛出 ZeroDivisionError（已用 run_python 复现）
🟡 中等：average() 对空列表产生 ZeroDivisionError……

你 > 把 divide 的修复版写入 samples/fixed_calculator.py
  [tool] write_file({"path": "samples/fixed_calculator.py", ...})
Agent > 已写入修复版本，并用 run_python 验证了 b=0 时返回 None 不再崩溃。
```

设计细节见 [Design.md](Design.md)。
