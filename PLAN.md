# Super-Agent 实施方案与架构演进

## 从 Phase 1 到 Super-Agent 的演进

项目分为两个清晰阶段演进，原有 Phase 1 资产与评测记录完整保留在仓库中：

| 维度 | Phase 1（`$orchestrator` MVP） | 当前版本（`super-agent` v0.2.0） |
| --- | --- | --- |
| 适用宿主 | 仅 Codex | 所有主流编码 Agent（内置 `antigravity`、`codex`、`claude`、`opencode` 与配置化 `custom`） |
| 执行单元 | Codex 进程内原生子代理（`spawn_agent`） | **严格单宿主内多个全新独立对话窗口 / 独立会话** |
| 状态与恢复 | 主会话内存态 DAG，无持久化 | 仓库外 `~/.super-agent/runs/<run_id>/` 原子化 `state.json` + `events.jsonl`，支持定向单任务重试 |
| 隔离与防越界 | 共享工作区，靠提示词约定写入范围 | 自动分离式 Git Worktree 隔离、写入越界硬校验、DAG 顺序重放合并 `final.patch` |
| 集成接口 | 仅 Skill 提示词 | 零依赖 MCP JSON-RPC 2.0 Stdio Server（12 个工具）+ 完整 CLI + 显式 Skill `$super-agent` |
| 验收与落盘 | 验证 Worker 抽检共享目录产物 | 合并生成 `final.patch` 后拉起独立只读验收窗口实测与补丁重放校验，经用户确认才落盘 |

## 核心设计原则

1. **单宿主内多对话窗口自编排（非子代理、非跨 Agent 混调）**：装在哪个 Agent，就通过 `SUPER_AGENT_HOST=<host>` 绑定到哪个宿主；主对话窗口只负责只读规划、审批流转、调度轮询和汇总，所有模块实现与独立验收均在同宿主的新独立对话窗口内完成。
2. **零外部运行时依赖**：`super_agent/` 全部使用 Python 3.10+ 标准库实现（仅 `tests/` 使用 `pytest`），安装即自带完整运行时与 MCP 入口，无需额外守护进程或第三方包。
3. **仓库外状态与安全门禁**：运行状态、日志、隔离 Worktree 和补丁默认落在 `~/.super-agent/runs/<run_id>/`（可用 `SUPER_AGENT_HOME` 覆盖），主工作区在用户显式确认落盘前保持只读与干净。

## 模块与组件架构

```text
super_agent/
├── models.py          # 核心数据模型（RunState/TaskSpec/AttemptRecord/SessionHandle/ResultContract/IntegrationState）
├── dag.py             # DAG 无环校验、路径归一化与重叠检测、并发/未确认清理槽位追踪、失败阻塞传播
├── store.py           # RunStore 原子写 state.json、追加 events.jsonl、管理 attempts/worktrees/integration 路径
├── protocol.py        # Worker 与只读 Verifier 的自包含 Prompt 构建；ResultContract 严格解析与安全归一化
├── worktree.py        # 分离式 Git Worktree 准备、防越界捕获、DAG 顺序集成、独立重放校验与安全落盘
├── engine.py          # SuperAgentEngine 核心状态机（规划、审批、派发、轮询、有界等待、重试、取消、集成、汇总、落盘）
├── mcp_server.py      # 零依赖 MCP JSON-RPC 2.0 Stdio Server（newline + Content-Length 双帧协议，暴露 12 个工具）
├── cli.py             # 统一 CLI（install/uninstall/doctor 及全部编排子命令 + mcp-serve）
├── installer.py       # 多宿主插件安装/升级/安全卸载器（原子目录替换、防符号链接/junction、写入单宿主 MCP 配置）
└── adapters/
    ├── base.py        # HostSessionAdapter 抽象、能力/健康模型、跨平台托管子进程与进程树终止工具
    ├── __init__.py    # 宿主注册表、确定性 detect_host 探测顺序与 doctor 诊断汇总
    ├── antigravity.py # Connect HTTP ConversationService 适配器（原生 UI 独立对话窗口）
    ├── codex.py       # 独立 `codex exec --json` 线程适配器（明确不走 spawn_agent）
    ├── claude.py      # 独立 `claude -p --session-id` 会话适配器
    ├── opencode.py    # 独立 `opencode run --format json` 会话适配器
    └── custom.py      # 由 host.json 驱动的可配置单宿主 CLI 适配器
```

配套插件清单与引导资产：

- 多宿主插件清单：`plugin.json`、`mcp_config.json`、`.codex-plugin/plugin.json`、`.claude-plugin/plugin.json`、`.mcp.json`。
- 编排引导 Skill：`skills/super-agent/SKILL.md` 及 `skills/super-agent/agents/openai.yaml`（禁用隐式调用，要求显式 `$super-agent`）。
- 兼容层：`skill/orchestrator/runtime/worktree.py` 直接重导出 `super_agent/worktree.py`，保证原有路径导入与命令行用法持续兼容。

## 四阶段执行闭环与安全不变量

1. **阶段 1 · 规划与审批闸门**：`create_plan` 先校验任务 ID、无环依赖、Verifier 只读约束与路径合法性；未通过校验时零持久化、零派发。合法计划以 `draft` 状态保存，未经 `approve_plan` 禁止派发任何窗口。
2. **阶段 2 · 并发派发与隔离执行**：`dispatch_ready` 按拓扑顺序选取就绪任务，同时满足 `max_concurrency` 上限与写入路径互斥（Windows 下不区分大小写）。在 `worktree` 模式下为每次尝试创建独立分离式 Worktree 并叠加前置已成功任务的补丁；窗口退出后自动校验 `ResultContract` 并通过 `worktree.capture` 检出越界修改。
   - **失败传播与定向重试**：永久失败立即阻塞全部下游任务，独立分支继续完成；明确标记 `retryable` 或命中瞬时网络/限流错误的故障最多自动重试 1 次（新窗口、新 Worktree）。人工可通过 `retry_task` 仅重跑失败任务并重置受阻下游，不重跑已成功模块。
   - **未确认终止锁**：任务超时或运行取消后，若宿主尚未确认窗口终止（`cleanup_confirmed: false`），引擎持续保留该任务的并发名额与写入路径锁，防止并发覆写。
3. **阶段 3 · 补丁集成与独立只读验收**：全部 Worker 完成后，`integrate_run` 在新的 `integration-<n>` Worktree 中按 DAG 顺序重放补丁并生成 `final.patch`；随后派发 `role="verifier"`、`write_paths=[]` 的独立只读窗口，要求其核验真实交付文件、执行验收命令并通过 `python -m super_agent.worktree verify` 独立验证补丁重放。
4. **阶段 4 · 汇总与确认落盘**：`summarize_run` 输出结构化与 Markdown 报告（含各窗口 `session_id`、`resume_hint`、验收结果与 `final.patch` 统计）。`apply_integration` 在存在 Verifier 时强制要求所有 Verifier 已 `succeeded`，且由用户确认后再执行 `git apply --check` 与正式落盘。

## 明确边界与宿主限制

1. **严格单宿主与禁止子代理替代**：单次安装与单次运行仅绑定一个 `SUPER_AGENT_HOST`；不支持单次运行内混用多个不同编码 Agent，也禁止用进程内子代理代替独立对话窗口。
2. **Antigravity 会话凭证与取消语义**：
   - Antigravity 的 `ANTIGRAVITY_LS_ADDRESS` 与 `ANTIGRAVITY_CSRF_TOKEN` 仅存在于工作区 Shell 环境中，若 MCP Server 进程未继承这两个变量，`doctor` 会将 `hosts.antigravity.health.shell_fallback_recommended` 置为 `true`。此时必须由主窗口通过工作区 Shell 调用 `python -m super_agent dispatch/wait` 直接继承环境变量，严禁把 CSRF Token 写入聊天消息或命令行参数（日志与错误信息中亦做自动脱敏）。
   - `product.v1.ConversationService` 当前不提供强制中止 RPC，因此 `AntigravityAdapter.cancel_window` 在会话尚未进入 `RUN_STATE_IDLE` 时仅追加 `super-agent:cancel-requested` 标签并返回 `confirmed=False`，引擎据此保留并发槽位与写范围锁直至窗口真正空闲。
3. **无 CLI / API 的封闭 GUI Agent**：对于不暴露命令行非交互模式、也不提供本地会话创建 API 的封闭桌面/网页 GUI Agent，插件无法原生拉起独立窗口；需由使用者提供外部包装脚本，再通过 `custom` 适配器的 `host.json` 接入。
4. **非 Git 仓库回退**：当目标工作区不是具备有效 `HEAD` 提交的 Git 仓库根目录时，`isolation_mode="auto"` 会自动降级为 `shared` 共享工作区模式（仍受 DAG 依赖与 `write_paths` 互斥保护，但不创建 Worktree 也不产出 `final.patch`）；显式指定 `isolation_mode="worktree"` 时则直接报错拒绝。
5. **Phase 1 历史资产与评测边界**：原 Phase 1 `$orchestrator`（`skill/orchestrator/`）及其原生冒烟脚本（`tests/native-smoke.ps1`）继续保留。确定性逻辑由 `pytest` 套件覆盖，真实宿主端到端验收标准与实测记录维护在 [EVAL.md](EVAL.md)。
