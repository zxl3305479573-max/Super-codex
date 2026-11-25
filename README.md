# Super-Agent

`super-agent`（v0.2.0）是一个面向主流编码 Agent 的**单宿主内多对话窗口自编排插件**。

- **单宿主内多窗口（非子代理、非跨 Agent 混调）**：装在哪个 Agent，就只在该 Agent 内部由一个主对话窗口拆解模块、等待用户确认、自动拉起多个**同宿主的全新独立对话窗口**并行实现，并通过独立只读验收窗口实测后再汇总回主窗口确认落盘。
- **自动 Git Worktree 隔离与防越界**：默认在仓库外为每个模块尝试创建分离式 Git Worktree，自动校验写入范围、拦截越界改动，按 DAG 拓扑顺序合并生成 `final.patch` 并支持独立重放校验。
- **零外部运行时依赖**：`super_agent/` 全部基于 Python 3.10+ 标准库实现，同时提供标准 MCP Stdio Server（JSON-RPC 2.0）与 CLI（`python -m super_agent` / `super-agent`）两种入口。

## 核心架构与运行目录

### 单宿主绑定机制

安装时，安装器会在生成的 MCP 配置文件中自动写入 `PYTHONPATH` 与 `SUPER_AGENT_HOST=<host>`（若为 `custom` 且指定了 `--host-config`，还会写入 `SUPER_AGENT_HOST_CONFIG`）。一次运行中的所有模块实现窗口与独立验收窗口均严格绑定到该单一宿主，不降级为进程内子代理（如 `spawn_agent`），也不跨 Agent 混调。

### 仓库外运行目录

所有运行状态、尝试日志、隔离 Worktree 与合并补丁默认保存在仓库外目录，避免污染目标项目的工作区：

- 默认路径：`~/.super-agent/runs/<run_id>/`
- 环境变量覆盖：设置 `SUPER_AGENT_HOME=<目录>` 后，运行目录位于 `<SUPER_AGENT_HOME>/runs/<run_id>/`
- CLI 单次覆盖：传入 `--run-root <目录>`

单次运行目录布局：

```text
~/.super-agent/runs/<run_id>/
├── state.json                  # 原子写入的完整运行状态与尝试记录
├── events.jsonl                # 追加式结构化事件日志
├── attempts/<attempt_id>/      # 单次窗口尝试的提示词、结果、日志与补丁
│   ├── prompt.md
│   ├── result.json
│   ├── last_message.txt
│   ├── output.log
│   ├── exit_code.json
│   └── changes.patch
├── worktrees/                  # 各 Worker 与 integration-<n> 的分离式 Git Worktree
└── integration/
    └── final.patch             # 按 DAG 顺序合并生成的最终统一补丁
```

**卸载插件仅删除插件与 Skill 目录，完整保留 `~/.super-agent/runs/` 下的历史运行记录。**

## 支持的宿主与安装布局

内置 4 个主流宿主适配器与 1 个配置化自定义单宿主适配器：

| 宿主 (`--host`) | 独立窗口拉起方式 | `project` 级安装路径 | `user` 级安装路径 | MCP 配置文件 | 窗口恢复提示 (`resume_hint`) |
| --- | --- | --- | --- | --- | --- |
| `antigravity` | Connect HTTP `product.v1.ConversationService` 原生新对话窗口 | `.agents/plugins/super-agent` | `~/.gemini/config/plugins/super-agent` | `mcp_config.json` | `conversation://<id>` |
| `codex` | 独立 `codex exec --json` 线程（非 `spawn_agent`） | `.agents/plugins/super-agent`<br>`.agents/skills/super-agent` | `~/.codex/plugins/super-agent`<br>`~/.agents/skills/super-agent` | `.mcp.json` | `codex resume <thread_id>` |
| `claude` | 独立 `claude -p --session-id <uuid>` 会话 | `.claude/plugins/super-agent` | `~/.claude/plugins/super-agent` | `.mcp.json` | `claude --resume <session_id>` |
| `opencode` | 独立 `opencode run --format json` 会话 | `.opencode/plugins/super-agent`<br>`.agents/skills/super-agent` | `~/.config/opencode/plugins/super-agent`<br>`~/.agents/skills/super-agent` | `.mcp.json` | `opencode --session <session_id>` |
| `custom` | 由 `host.json` 驱动的单宿主 CLI 命令 | `.super-agent/plugin`<br>`.agents/skills/super-agent` | `~/.super-agent/plugin`<br>`~/.agents/skills/super-agent` | `.mcp.json` | 由 `resume_template` 定义 |

> **Antigravity 凭证与 Shell 回退**：Antigravity 的 `ANTIGRAVITY_LS_ADDRESS` 与 `ANTIGRAVITY_CSRF_TOKEN` 仅注入在工作区 Shell 进程中。若 MCP 子进程未继承这两个变量，`doctor` 会在 `hosts.antigravity.health.shell_fallback_recommended` 返回 `true`；此时主窗口在工作区 Shell 中执行 `python -m super_agent dispatch/wait`，直接继承环境变量而不经聊天文本或命令行参数传递凭证。

## Windows 安装、诊断与卸载

以下 PowerShell 命令在仓库根目录执行（需 Python 3.10+ 与 Git）。将 `<host>` 替换为 `antigravity`、`codex`、`claude`、`opencode` 或 `custom`。

### 安装 / 升级

```powershell
# 项目级安装（默认安装到当前目录；可用 --target 指定其他项目根目录）
python -m super_agent install --host codex --scope project
python -m super_agent install --host codex --scope project --target 'D:\path\to\your-project'

# 用户级安装（按宿主安装到当前用户配置目录）
python -m super_agent install --host antigravity --scope user
python -m super_agent install --host claude --scope user
python -m super_agent install --host opencode --scope user

# 自定义单宿主安装（可选绑定 host.json 与指定 Python 解释器）
python -m super_agent install --host custom --scope project `
  --host-config 'D:\path\to\host.json' `
  --python 'C:\Python313\python.exe'
```

重复执行 `install` 会通过暂存目录原子替换完成原地升级（输出 `upgraded: true`），并自动拒绝符号链接与 Windows Junction 目标路径。安装完成后重载对应宿主插件或重开会话即可加载 `$super-agent` 与 MCP 工具。

### 环境诊断

```powershell
# 以 JSON 输出当前宿主探测结果、各适配器可用性与能力矩阵
python -m super_agent doctor --json

# 指定工作区或覆盖待检测宿主
python -m super_agent doctor --workspace 'D:\path\to\your-project' --host codex --json
```

### 卸载

```powershell
# 卸载项目级安装
python -m super_agent uninstall --host codex --scope project --target 'D:\path\to\your-project'

# 卸载用户级安装
python -m super_agent uninstall --host antigravity --scope user
```

卸载操作具备幂等性（目标已不存在时返回 `already_absent: true`），只移除对应宿主的插件与 Skill 目录，不会删除 `~/.super-agent/runs/` 中的任何历史运行记录。

## 四阶段主窗口编排流程（`$super-agent`）

`skills/super-agent/agents/openai.yaml` 将 `allow_implicit_invocation` 设为 `false`，需在主对话窗口显式调用 `$super-agent`。

调用示例：

```text
$super-agent 先只读分析当前仓库，把需求拆成并行模块和独占写入范围，给出 DAG 计划表等我确认；批准后用同宿主独立对话窗口并行实现，在隔离 worktree 中防越界捕获并合并 final.patch，再拉起独立只读验收窗口实测后向我汇总。
```

主窗口严格遵循以下四个阶段（主窗口自身不直接修改目标交付文件）：

1. **阶段 1 · 规划与确认**
   调用 `super_agent_doctor` 检查当前宿主与能力，只读分析代码并设计无环模块 DAG（包含唯一 `id`、`depends_on`、并行互斥的 `write_paths`、`acceptance_checks`，以及默认自动追加的只读 `verifier` 任务）。调用 `super_agent_plan_create` 持久化为 `draft` 计划，向用户展示计划表并**等待显式批准**。
2. **阶段 2 · 批准与并行派发**
   用户批准后调用 `super_agent_plan_approve`，随后循环调用 `super_agent_dispatch_ready` 与 `super_agent_wait_step`（受 `max_concurrency` 与写入范围互斥约束）拉起同宿主独立对话窗口。
   - 若 `doctor` 提示 Antigravity 需要 Shell 回退，则改由工作区 Shell 执行：
     ```powershell
     python -m super_agent dispatch --run-id <run_id> --json
     python -m super_agent wait --run-id <run_id> --json
     ```
   - 每个 Worker 完成后，引擎校验结构化结果契约 `ResultContract`，并在 `worktree` 模式下自动捕获补丁与检测写越界；永久失败自动阻塞其下游，明确可重试的瞬时故障最多自动重试 1 次。若需人工恢复单个失败节点，调用 `super_agent_retry_task`（仅重跑失败任务并重置受阻下游，不重跑已成功任务）。
3. **阶段 3 · 合并补丁与独立只读验收**
   所有 Worker 成功后，在 `worktree` 模式下调用 `super_agent_integrate_run` 按 DAG 顺序在集成 Worktree 中重放所有 Worker 补丁并生成 `final.patch`。随后派发独立只读验收窗口（`role: "verifier"`, `write_paths: []`）直接检查真实产物、执行测试命令并运行 `python -m super_agent.worktree verify ...` 校验补丁重放。
4. **阶段 4 · 汇总与确认落盘**
   调用 `super_agent_summarize_run` 向用户呈现各模块状态、窗口 `session_id` 与恢复提示（`resume_hint`）、独立验收证据及 `final.patch` 路径和变更摘要。经用户**显式确认**后，再调用 `super_agent_apply_integration`（可先设 `check_only: true` 预检）将 `final.patch` 应用到主工作区。

## 12 个 MCP 工具与 CLI 对应

启动 MCP Stdio Server 可直接执行 `python -m super_agent.mcp_server` 或 `python -m super_agent mcp-serve`（同时支持换行分隔 JSON 与 `Content-Length` 帧协议）。全部 12 个工具与 CLI 子命令一一对应：

| MCP 工具名 | 对应 CLI 子命令 | 作用与关键参数 |
| --- | --- | --- |
| `super_agent_doctor` | `doctor` | 探测当前宿主、各适配器健康状态、能力与 Shell 回退建议（`workspace?`） |
| `super_agent_plan_create` | `plan-create` | 校验模块 DAG 并创建 `draft` 运行（`workspace`, `tasks`, `max_concurrency?=3`, `host?`, `isolation_mode?="auto"`, `auto_add_verifier?=true`, `run_id?`） |
| `super_agent_plan_approve` | `plan-approve` | 用户确认后将 `draft` 运行批准为可执行状态（`run_id`） |
| `super_agent_dispatch_ready` | `dispatch` | 按并发上限与写入范围互斥拉起就绪任务的独立窗口（`run_id`） |
| `super_agent_poll_run` | `poll` | 单次轮询活跃窗口并回收已完成的结果与补丁（`run_id`） |
| `super_agent_wait_step` | `wait` | 在有界时间内轮询等待状态变化或任务结束（`run_id`, `timeout_sec?=30`, `poll_interval_sec?=1`） |
| `super_agent_retry_task` | `retry` | 仅重跑失败/超时/取消的单个任务，可选重置受阻下游（`run_id`, `task_id`, `reset_dependents?=true`） |
| `super_agent_cancel_run` | `cancel` | 请求中止活跃窗口并取消运行，返回未确认终止的窗口列表（`run_id`） |
| `super_agent_integrate_run` | `integrate` | 在集成 Worktree 中按 DAG 顺序合并 Worker 补丁并生成 `final.patch`（`run_id`） |
| `super_agent_summarize_run` | `summarize` | 汇总运行状态、模块结果、窗口会话 ID、验收证据与补丁差异统计（`run_id`） |
| `super_agent_apply_integration` | `apply` | 预检或将已通过验收的 `final.patch` 显式落盘到主工作区（`run_id`, `check_only?=false`） |
| `super_agent_list_runs` | `list-runs` | 按最近更新时间倒序列出已持久化的运行记录（无参数） |

## 自定义单宿主适配器（`custom` / `host.json`）

对于未内置或自建的 CLI 编码 Agent，可通过 `--host custom` 与单个 `host.json` 接入。配置查找优先级依次为：

1. 显式传入的 `config_path` / 安装时绑定的环境变量 `SUPER_AGENT_HOST_CONFIG`
2. `<workspace>/.super-agent/host.json`
3. `~/.super-agent/host.json`

`host.json` 示例：

```json
{
  "name": "my-agent",
  "command": [
    "my-agent-cli",
    "run",
    "--cwd", "{workspace}",
    "--prompt-file", "{prompt_file}",
    "--result-file", "{result_file}",
    "--role", "{role}"
  ],
  "stdin_prompt": false,
  "env": {
    "MY_AGENT_NON_INTERACTIVE": "1"
  },
  "session_id_regex": "session_id=(?P<session_id>[A-Za-z0-9_-]+)",
  "resume_template": "my-agent-cli resume {session_id}"
}
```

字段与占位符说明：

- `command`（必填）：非空字符串数组，支持以下 8 个模板占位符：`{workspace}`、`{prompt_file}`、`{result_file}`、`{last_message_file}`、`{task_id}`、`{title}`、`{role}`、`{model}`。
- `stdin_prompt`（可选，默认 `false`）：设为 `true` 时将 `prompt.md` 内容直接重定向至子进程 `stdin`。
- `env`（可选）：追加到子进程的键值对环境变量。
- `session_id_regex` 与 `resume_template`（均可选）：前者从 `output.log` 中提取会话 ID（优先取命名捕获组 `(?P<session_id>...)`，其次取第 1 个捕获组），后者将 `{session_id}` 替换为恢复命令提示。

## 测试与验收

在仓库根目录可直接运行全部确定性测试（7 个测试模块）：

```powershell
pytest -v
```

完整的测试模块说明、本机 `doctor` 诊断、临时目录安装/卸载验证及 Phase 1 冒烟步骤见 [tests/README.md](tests/README.md)，工程架构与演进边界见 [PLAN.md](PLAN.md)，历史与阶段评测标准见 [EVAL.md](EVAL.md)。

## 历史 Phase 1 资产与兼容层

本仓库保留了 Phase 1（仅限 Codex 原生子代理的 `$orchestrator` 最小可用版本）的历史资产以便回归和对照：

- `skill/orchestrator/`：原 Phase 1 手动调用的 Codex Skill 目录（按原有说明复制至 `.agents/skills/orchestrator` 仍可独立运行）。
- `skill/orchestrator/runtime/worktree.py`：已保留为向后兼容导入层（shim），内部直接转发并复用 `super_agent/worktree.py` 的全部公开接口与 CLI。
- `tests/native-smoke.ps1` 与 [EVAL.md](EVAL.md)：保留 Phase 1 的 Windows 冒烟脚本及 E1–E5 实测记录（含 [E5 原生 Hook 审计](EVAL.md#e5-原生-hook-审计)）。
