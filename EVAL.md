# Codex Orchestrator 评测

## Phase 2 工程化增强

在独立测试仓库和新 Codex 会话中执行。记录 CLI 和插件版本、运行 ID、关键任务及尝试 ID、宿主 Worker ID、验收命令和退出码。

| 用例 | 通过条件 |
| --- | --- |
| P2-1 DAG 与冲突 | 重复 ID、缺失依赖和循环图均零派发。三个无关节点的真实执行有重叠。依赖等待前置成功，重叠范围串行。 |
| P2-2 持久化与恢复 | 永久失败阻塞下游，独立任务成功。重新打开记录后，失败可定位且成功节点仍仅有一次派发。指定失败节点修复后只重试该节点并继续其下游。 |
| P2-3 超时与重试 | 到期后等待宿主终止及关闭证据。未确认时保留名额和范围。可重试故障使用新 Worker，最多自动重试一次。 |
| P2-4 Worktree 与集成 | 下游读到前置成果。越界改动可检出。独立 Worker 核验最终集成目录和补丁可在基线重放，原目录保持原状。 |
| P2-5 安装与回归 | Windows 临时项目中完成安装、Skill 发现、重复安装、升级及重复卸载。卸载保留运行记录，受影响的 Phase 1 用例通过。 |
| P2-6 Goal 与 Guard | 实测期间有活动 Goal，正式 `change` 激活已受信任的 Hook，宿主审计记录真实 Worker 的启动、等待和关闭。 |

核心状态转换、冲突判定、补丁操作和脚本行为用确定性测试覆盖。真实执行和 Hook 用例使用原生宿主证据。每项以「通过」「失败」或「待证」记录实际结果；离线模拟只算对应的确定性覆盖。全部通过且独立 Worker 已核验交付后关闭 Phase 2 Goal。

### Super-Agent 插件与 Phase 2 核验记录（2026-11-25）

在 Windows (`win32`)、Python `3.13.13`、pytest `9.1.1` 下由独立只读 QA 验收子代理执行核验。本机检测到的宿主与工具版本：`antigravity`（`ConversationService` 于本地可用）、`codex-cli 0.171.0`、`claude 2.1.332 (Claude Code)`、`opencode 1.4.6`。

- 全量确定性测试：`pytest -v` 退出码 `0`，**150 passed, 1 skipped, 0 failed**（Windows 跳过 1 项 POSIX 反斜杠文件名用例）。
- 零第三方运行时依赖：AST 扫描 `super_agent/**/*.py`（19 个文件）与兼容转发层 `skill/orchestrator/runtime/worktree.py`（1 个文件），共 20 个运行时文件仅使用 Python 3.10+ 标准库及包内模块，无 `spawn_agent` / `invoke_subagent` 调用。
- 实时 CLI 与 MCP 协议核验：`python -m super_agent doctor --json` 退出码 `0`；真实子进程 `python -m super_agent.mcp_server` 在换行 JSON-RPC 与 `Content-Length` 两种 stdio 帧格式下均完成 `initialize`、`tools/list`（12 个 `super_agent_*` 工具）与 `tools/call(super_agent_doctor)`（退出码 `0`）。
- 多宿主临时安装/卸载实测：在 `%TEMP%` 临时目录下对 `antigravity`、`codex`、`claude`、`opencode` 的 `project` / `user` 双作用域执行真实 CLI `install` → 重复升级（`upgraded: true`）→ 核验 `SUPER_AGENT_HOST` 与 `PYTHONPATH` 绑定 → `uninstall` → 重复卸载（`already_absent: true`），8 轮全通过，且模拟的 `~/.super-agent/runs/demo/state.json` 在每次卸载后保持完好。

| 用例 | 当前状态 | 证据与说明 |
| --- | --- | --- |
| P2-1 DAG 与冲突 | 确定性通过（真实多窗口待证） | `tests/test_dag.py` 与 `tests/test_engine.py` 覆盖重复 ID、缺失依赖、循环图零派发、目录包含/大小写路径冲突串行及并发上限；真实多窗口端到端执行待在目标宿主安装后实测。 |
| P2-2 持久化与恢复 | 确定性通过（真实多窗口待证） | `tests/test_store_protocol.py` 与 `tests/test_engine.py` 覆盖原子落盘、永久失败阻塞下游、新建引擎实例恢复不重派已成功节点及 `retry_task` 局部重跑。 |
| P2-3 超时与重试 | 确定性通过（真实多窗口待证） | `tests/test_engine.py` 与 `tests/test_adapters.py` 覆盖超时取消未确认前保留并发名额与写范围锁、确认后解锁，以及可重试故障至多自动重试一次。 |
| P2-4 Worktree 与集成 | 确定性通过（真实多窗口待证） | `tests/test_worktree.py`（14 passed, 1 skipped）与 `tests/test_engine.py` 使用真实 Git 仓库验证上游补丁重放、越界改动拦截、独立 `integration` 目录生成与 `worktree.py verify` 基线重放，以及显式 `apply_integration` 前主工作区保持原状。 |
| P2-5 安装与回归 | 通过（临时项目实测 + 回归） | `tests/test_installer.py`（30 passed）与独立 QA 在 `%TEMP%` 对 4 宿主×2 作用域实测安装、升级、单宿主绑定、重复卸载及运行记录保留；既有 `tests/test_worktree.py` 全量回归通过。 |
| P2-6 Goal 与 Guard | 待证（Phase 1 E5 已验） | 本次未在外部 Codex 桌面会话重跑 Goal + Hook 实测；Phase 1 E5 已记录原生 Hook 审计放行证据。 |

## Phase 1 最小可用版本

以 [实施方案](PLAN.md) 为范围。按 Stop That Shit 的直接方案、具体缺口和相关验证原则选取下列用例。复用 Codex 原生 Worker 和已安装的 Guard，验收集中在可观察的行为。

## 验收环境

- Windows 和 Codex CLI 或桌面端。记录 CLI 版本、实际暴露的子 Agent 工具，以及本次使用的 Skill 安装路径。
- Guard 用例使用 Stop That Shit `0.2.5` 和 Node.js 18 及以上。常规运行使用已受信任的插件 Hook。隔离 CLI 运行须记录 Hook 审查和本次运行的信任方式。`$stop-that-shit` 指令位于用户消息第一行。独立运行可复现的 Skill 用例时，允许省略 Guard。
- 选择独立测试目录。三个 Worker 分别只写入 `alice.txt`、`bravo.txt`、`charlie.txt`，对应内容是单行 `ALICE`、`BRAVO`、`CHARLIE`。验证 Worker 读取这三个文件并逐一精确比较，允许正常的 Windows 或 Unix 行尾。

## 用例和通过条件

| 用例 | 执行 | 通过条件 |
| --- | --- | --- |
| E1 手动安装 | 按 Windows 说明安装到测试项目。新开会话，显式调用 `$orchestrator`，验收后按说明卸载。 | 新会话能发现 Skill。调用进入委派流程。卸载仅移除该 Skill。 |
| E2 原生并行和结果 | 在一个主会话中请求三个独立文件任务，再安排验证。 | 宿主记录三个不同的 Worker ID 和独立上下文启动。第三个 Worker 已启动时，至少还有一个先启动的 Worker 在执行。每个 Worker 的结构化结果按任务回到同一主会话。主会话的工具记录里没有对三个交付文件的写入或测试。 |
| E3 依赖和证据 | 把产物检查设为三个写入任务的下游。 | 验证 Worker 只在三项前置任务成功后启动，实际读取文件并返回比较结果。单独把一个测试文件内容改错时，产物检查能报错。 |
| E4 失败传播 | 增加一个会明确失败的前置任务，以及一个依赖它的任务。搭配一个可独立成功的任务。 | 保留失败原因，依赖任务进入阻塞状态，独立任务完成。成功任务只启动一次。失败不可重试时不重试。 |
| E5 Goal 和 Guard | 启用当前聊天的持久 Goal。通过已受信任的 Stop That Shit Hook 提交正式 `change` 指令，并在受控范围内运行实现或验收中的一个实际动作。 | Goal 有宿主返回的活动状态。Guard 有宿主日志、插件状态或诊断证据，表明正式指令激活了约束，且作用于实际工具路径。只看到插件配置、策略提示或离线适配器输出时，将宿主 Hook 验收记为待证。 |

E2 可用桌面子会话轨迹或 CLI 事件佐证。若所用宿主只提供按批启动和完成的证据，按实际证据报告批次执行，对执行时间重叠单独标注。E4 用安全、确定的失败即可，无需故障注入框架。

## 记录

每项记录 `通过`、`失败` 或 `待证`，附最短可复核证据。使用任务 ID、Worker ID、事件顺序、验证命令和退出码。无需保存聊天转录、终端长日志或敏感信息。实际产物内容由验证 Worker 检查，主会话保留检查结论。

以 E1–E5 的实际结果作为 Goal 的完成依据。遇到宿主限制时，保留已取得的证据，说明未覆盖的行为和复现方式。

## 本次实测结果

E1–E4 使用 Windows 桌面端随附的 Codex CLI `0.171.0-alpha.13`，通过原生 `spawn_agent` 启动 Worker，并使用宿主的原生等待工具接收回执。项目级安装目录为 `C:\Users\33054\AppData\Local\Temp\codex-orchestrator-live-01a20858\.agents\skills\orchestrator`。验证脚本为 `D:\33054\Documents\ChatGPT\super-codex\tests\native-smoke.ps1`，`-OutputDir` 指向供脚本读取现有产物的目录。早期用例分别读取该临时项目的 `e2e3` 和 `e4`。E5 使用 Windows 稳定版 Codex CLI `0.171.0`。实测日期为 2026-11-23，所列时间均为 UTC。

| 用例 | 结果 | 可复核证据 |
| --- | --- | --- |
| E1 手动安装 | 通过 | CLI 识别显式 `$orchestrator` 并进入委派。按 README 卸载前核对解析后的绝对路径与安装目录一致，目标、祖先及内容均无重解析点。卸载后目标已不存在，`.agents`、`.agents/skills`、`e2e3`、`e4` 均保留，四个产物的 SHA-256 和修改时间一致，`e4/blocked.txt` 仍不存在。 |
| E2 原生并行和结果 | 通过 | 更新版 Skill 的 CLI 主会话 `01a20885-0fd3-7341-92aa-809179cdf320` 启动三个不同的原生 Worker。宿主文件系统的六个标记证实三条命令的执行区间重叠。前三项原生等待回执按任务返回，均为 `succeeded`、命令退出 `0`。独立验证 Worker 在三项成功后启动，核验通过。四份原始 Worker JSON 回执的结构和字段类型均符合要求。见下方标记顺序及早期 E2E3 记录。 |
| E3 依赖和证据 | 通过 | 三个前置完成后，12:37:01.256Z 启动验证 Worker `01a20868-e16b-7e02-91d8-c644165a1793`。实际执行验证脚本 `-Case E2E3`，逐一读取并比对三份文件，全部通过，退出 `0`。独立复核的正确样本退出 `0`，新建的 `BRAVX` 错误样本退出 `1`。 |
| E4 失败传播 | 通过 | 更新版 Skill 主会话 `01a2088f-0049-76f2-9ff9-f0817f2ebf24` 中，`fail` Worker `01a2088f-66d7-7522-95db-6624981d7589` 启动一次，保留 `NATIVE_SMOKE_EXPECTED_FAILURE` 和退出 `23`，重试零次。`blocked` 启动零次，`independent` Worker `01a2088f-6c63-7f71-88f6-a6a8784dc942` 启动一次且成功。只读验证 Worker `01a20890-dbac-7312-9b16-186cd8276a9b` 和独立直接复核均确认 `independent.txt` 内容正确、`blocked.txt` 缺失，并退出 `0`。 |
| E5 Goal 和 Guard | 通过（进程级 Hook 信任） | 聊天 `01a20858-5d5d-7b43-8bd5-cdf9f0dcdd7b` 的原生 Goal 在测试期间为 `active`。稳定版 CLI 预检确认四个已审查的 Hook 均为 `trusted`。运行时审计确认正式 `change` 指令激活了 Guard，并放行原生 E4 产物验证 Worker 的启动、等待和关闭。Worker 验证成功，CLI 退出 `0`，用户配置 SHA 保持一致。 |

### E2 执行重叠

三个 Worker 各在一条命令内写入自己的 `.ready`，轮询直到三个 `.ready` 均存在，再写入自己的 `.done`。独立读取 `C:\Users\33054\AppData\Local\Temp\codex-orchestrator-eval-01a20858\overlap` 下的六个标记，每份内容均精确等于对应的小写任务名，宿主 `LastWriteTimeUtc` 如下。

| 任务 | 原生 Worker ID | `.ready` | `.done` |
| --- | --- | --- | --- |
| `alice` | `01a20885-c9b5-7bb3-b4cf-e8ce65be6a77` | 13:08:50.2823950Z | 13:08:53.1473042Z |
| `bravo` | `01a20885-ceeb-7933-b4fe-a9d28763f8bf` | 13:08:51.5842995Z | 13:08:53.1473042Z |
| `charlie` | `01a20885-d3c9-75c3-afb3-2190a6f120aa` | 13:08:52.9499631Z | 13:08:52.9619701Z |

最晚 `.ready`（Charlie，`2026-11-23T13:08:52.9499631Z`）≤ 最早 `.done`（Charlie，`2026-11-23T13:08:52.9619701Z`），相差 12.007 毫秒。结合命令内的等待条件，Charlie 写入 `.ready` 时，Alice 和 Bravo 的命令都尚未完成。主会话收到三项成功回执后，启动独立验证 Worker `01a20886-807b-7a82-8598-1c235b350283`。该 Worker 按区分大小写的方式核对六个标记的名称和内容，确认相同的 UTC 时间顺序，返回 `succeeded`。

### 早期 E2E3 与回执回归

早期 E2E3 主会话 `01a20867-4e71-7110-bf6f-a0bbec28aed7` 先后启动 `alice`（12:35:57.956Z，`01a20867-e8fd-72a3-aea9-1ad8cc43a584`）、`bravo`（12:35:59.442Z，`01a20867-f036-7f82-b62e-99489fe797d6`）、`charlie`（12:36:01.178Z，`01a20867-f6ea-7a23-8bbe-32e27abe1304`）。首个完成由 12:36:32.569Z 的 `wait` 观察到，至 12:36:41.522Z 的等待三项均已完成，回执按任务返回。主会话的 PowerShell 只查看 Skill 和输出目录。按 Alice、Bravo、Charlie 顺序，三份交付文件的修改时间分别为 12:36:24.2537703Z、12:36:29.3520922Z、12:36:29.3086391Z，距对应的自报结束时间约 1 毫秒以内。约 5.05 秒的重叠来自 Worker 自报，早期宿主轨迹能确认批次启动和完成。下游产物验证见 E3。

E2–E4 原有回执曾出现可恢复的格式差异（`artifacts` 为对象，状态为 `passed` 或 `completed`）。更新已安装的 Skill 后，定向原生回归主会话 `01a2087e-ea30-7311-99a6-3428ae09ea38` 依次启动两个只读 Worker。`01a2087f-5e3c-77b0-b351-70860eca519c` 执行 `Test-Path`，返回 `True`、退出 `0`。`01a2087f-f2d1-7171-9541-0d0bf3a79980` 用 `Get-Item` 解析到准确的安装文件，返回 `PSIsContainer: false`、退出 `0`。两次原生等待回执均含 `succeeded`、`artifacts: []` 和预期字段及类型，无需归一化。

### E4 更新版完整复验

首次完整复跑时，PowerShell 包装层返回 `1`。修订夹具，让失败任务在外层 PowerShell 直接执行 `exit 23`。表中更新版主会话随后记录了 `fail` 的退出码 `23` 和失败标记、`blocked` 的依赖阻塞以及 `independent` 的成功。

首位只读验证 Worker 把 `-OutputDir` 理解为写入目标，因而未执行脚本。澄清该参数指向供脚本读取的现有产物目录后，验证 Worker `01a20890-dbac-7312-9b16-186cd8276a9b` 执行 `native-smoke.ps1 -Case E4`。该 Worker 和独立直接复核均返回 `PASS independent.txt`、`PASS blocked.txt absent`，退出 `0`。夹具提示已补充 `-OutputDir` 的输入目录语义。

### E5 原生 Hook 审计

Stop That Shit `0.2.5` 的 `HEAD` 和对应标签均指向 `8bc1ac558167121e7f28d9fb0bec05c039952206`。`.codex-plugin`、`hooks` 和 `src` 下均无已跟踪的修改或未跟踪的可执行内容。稳定版 CLI 通过 `-c hooks.state={...}` 内联表，将已核对的四个 Hook 定义哈希和内容哈希传给当前进程。预检中，宿主 app-server 的 `hooks/list` 将四个 Hook 均报告为 `trusted`。本次执行沿用该进程级信任配置，未使用 `--dangerously-bypass-hook-trust`。

消息首行为 `$stop-that-shit change agents=1 -- …`，次行显式调用 `$orchestrator`。稳定版 CLI 主会话 `01a20891-6ff7-7c10-a604-823f2d442554` 启动一个原生 Worker `01a20892-5f48-7af2-8cf5-413076d330d5`。该 Worker 独立执行只读检查，确认 `independent.txt` 内容为 `INDEPENDENT`、`blocked.txt` 缺失，检查器退出 `0`。原生等待回执为 `succeeded`，宿主确认关闭 Worker，CLI 发出 `turn.completed` 并退出 `0`。用户配置校验返回 `USER_CONFIG_SHA_UNCHANGED True`。

运行时审计的下列事件均记录 `mode=change`、`controlState=armed`、`agentBudget=1`，决策为 `allow`。

| 审计中的工具名 | 事件 ID |
| --- | --- |
| `spawn_agent` | `evt_4cf3e6cc-43f3-4d47-b3c1-e711b74b9e9c` |
| `multi_agent_v1wait_agent` | `evt_14106b70-fec6-446b-b918-7f83f44f4145` |
| `multi_agent_v1close_agent` | `evt_a6d7a5d5-e307-4d2f-afb3-e818bb662576` |

关闭事件的原因码为 `WITHIN_CONTRACT`。

不传入进程级信任配置时，基线 `hooks/list` 将四个已安装的 Hook 均报告为 `modified`。常规桌面运行的持久信任仍需由所有者在 `/hooks` 或桌面设置中审查。
