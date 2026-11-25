# Super-Agent 测试与验收指南

本文档覆盖四类验证：离线确定性 `pytest` 套件、本机宿主 `doctor` 诊断、临时目录安装/卸载验证，以及保留的 Phase 1 Windows 原生冒烟验收。

## 1. 确定性单元测试套件（`pytest`）

运行时零第三方依赖；测试环境需 Python 3.10+、Git 与 `pytest`。在仓库根目录执行：

```powershell
pytest -v
```

套件包含 7 个确定性测试模块，均在隔离临时目录或本地 mock 服务中运行，不依赖外部网络或真实模型调用：

| 测试模块 | 覆盖范围 |
| --- | --- |
| `tests/test_dag.py` | DAG 拓扑排序、非法任务 ID/重复 ID/循环/缺失依赖拒绝、相对路径归一化与重叠检测（含大小写边界）、未确认清理槽位保留、失败阻塞传播与上下游遍历 |
| `tests/test_store_protocol.py` | `RunStore` 原子写入 `state.json`、`events.jsonl`、危险名称拦截、`SUPER_AGENT_HOME` 覆盖，以及 Worker/Verifier Prompt 构建与 `ResultContract` 解析、严格校验和安全归一化 |
| `tests/test_worktree.py` | 分离式 Git Worktree 准备、前置补丁叠加、写入越界拦截、二进制/删除/忽略文件捕获、`integrate` 与 `verify_integration` 独立重放校验，以及 `skill/orchestrator/runtime/worktree.py` 兼容层 |
| `tests/test_adapters.py` | `antigravity`（本地 mock Connect HTTP 服务与凭证脱敏）、`codex`、`claude`、`opencode`、`custom`（`host.json` 加载、占位符替换、正则提取会话 ID）单宿主适配器生命周期、进程树取消、`detect_host` 与 `doctor` |
| `tests/test_engine.py` | `SuperAgentEngine` 端到端状态机：审批闸门、并行调度、Worktree 隔离与越界失败、超时与未确认取消锁、自动重试与定向单任务重试、DAG 补丁合并、只读 Verifier 门禁与显式落盘 |
| `tests/test_mcp_server.py` | 零依赖 MCP JSON-RPC 2.0 Stdio Server（newline 与 `Content-Length` 双帧协议、协议版本协商、12 个工具参数校验与 `isError` 隔离）及 CLI / `python -m super_agent` 全部子命令 |
| `tests/test_installer.py` | 多宿主清单与 `$super-agent` Skill 校验、5 种宿主 × `project`/`user` 布局安装、重复升级、安全卸载（拒绝符号链接与 Windows Junction、防止覆盖源码目录、卸载保留运行记录） |

也可按模块单独执行，例如：

```powershell
pytest -v tests/test_engine.py tests/test_mcp_server.py
```

## 2. 本机宿主诊断（`doctor`）

在当前宿主或工作区 Shell 中执行：

```powershell
python -m super_agent doctor --json
```

核对以下关键字段：

- `detected_host` 与 `recommended_host`：确认与当前所在宿主一致（安装后 MCP 环境通过 `SUPER_AGENT_HOST` 固定）。
- `hosts.<host>.available` 与 `hosts.<host>.health`：确认目标宿主的 CLI 路径/版本或 Antigravity `ConversationService` 地址已就绪。
- `hosts.antigravity.health.shell_fallback_recommended`：在 Antigravity 宿主下，若 MCP 环境缺失 `ANTIGRAVITY_LS_ADDRESS` / `ANTIGRAVITY_CSRF_TOKEN`，该项为 `true`，提示主窗口改由工作区 Shell 执行 `python -m super_agent dispatch/wait`。
- `hosts.<host>.capabilities`：核对 `ui_native_window`、`resumable`、`supports_model`、`supports_cancel` 与 `transport`。

也可指定特定工作区与宿主覆盖进行检查：

```powershell
python -m super_agent doctor --workspace 'D:\33054\Documents\ChatGPT\super-codex' --host codex --json
```

## 3. 临时目录安装与卸载验证

以下 PowerShell 脚本在临时目录中端到端验证安装、重复升级、`SUPER_AGENT_HOST` 绑定、卸载幂等性，以及卸载后运行目录完整保留（不会改动本仓库或真实用户目录）：

```powershell
$repo = 'D:\33054\Documents\ChatGPT\super-codex'
$tmpRoot = Join-Path ([IO.Path]::GetTempPath()) ('super-agent-verify-' + [guid]::NewGuid().ToString('N'))
$tmpProj = [IO.Directory]::CreateDirectory((Join-Path $tmpRoot 'project')).FullName
$tmpHome = [IO.Directory]::CreateDirectory((Join-Path $tmpRoot 'sa-home')).FullName
$sentinelRun = [IO.Directory]::CreateDirectory((Join-Path $tmpHome 'runs\run-keep-me')).FullName
[IO.File]::WriteAllText((Join-Path $sentinelRun 'state.json'), '{"run_id":"run-keep-me"}')

$prevHome = $env:SUPER_AGENT_HOME
$env:SUPER_AGENT_HOME = $tmpHome
Push-Location $repo
try {
    # 1. 首次安装（可把 codex 替换为 antigravity / claude / opencode / custom）
    $install1 = python -m super_agent install --host codex --scope project --target $tmpProj --json | ConvertFrom-Json
    Write-Host "Install1 upgraded=$($install1.upgraded) plugin=$($install1.plugin_dir) mcp=$($install1.mcp_config_path)"

    # 2. 校验生成的 MCP 配置绑定了 SUPER_AGENT_HOST
    $mcp = Get-Content -LiteralPath $install1.mcp_config_path -Raw | ConvertFrom-Json
    Write-Host "Bound host=$($mcp.mcpServers.'super-agent'.env.SUPER_AGENT_HOST)"

    # 3. 重复安装验证原地原子升级
    $install2 = python -m super_agent install --host codex --scope project --target $tmpProj --json | ConvertFrom-Json
    Write-Host "Install2 upgraded=$($install2.upgraded)"

    # 4. 卸载并验证运行目录完整保留
    $uninstall1 = python -m super_agent uninstall --host codex --scope project --target $tmpProj --json | ConvertFrom-Json
    Write-Host "Uninstall1 already_absent=$($uninstall1.already_absent) removed=$($uninstall1.removed_paths -join ', ')"
    Write-Host "Plugin removed=$(-not (Test-Path -LiteralPath $install1.plugin_dir))"
    Write-Host "Runs preserved=$(Test-Path -LiteralPath (Join-Path $sentinelRun 'state.json'))"

    # 5. 重复卸载验证幂等性
    $uninstall2 = python -m super_agent uninstall --host codex --scope project --target $tmpProj --json | ConvertFrom-Json
    Write-Host "Uninstall2 already_absent=$($uninstall2.already_absent)"
}
finally {
    Pop-Location
    if ($null -eq $prevHome) { Remove-Item Env:SUPER_AGENT_HOME -ErrorAction SilentlyContinue } else { $env:SUPER_AGENT_HOME = $prevHome }
    Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
}
```

预期结果：`Install1 upgraded=False`、`Bound host=codex`、`Install2 upgraded=True`、`Uninstall1 already_absent=False`、`Plugin removed=True`、`Runs preserved=True`、`Uninstall2 already_absent=True`。

## 4. 保留的 Phase 1 Windows 原生冒烟验收（`native-smoke.ps1`）

如需对保留的 Phase 1 `$orchestrator` Skill（`skill/orchestrator/`）做回归冒烟验收，沿用 `tests/native-smoke.ps1`。E1 先将 `skill/orchestrator` 复制到独立测试项目的 `.agents/skills/orchestrator`，再在新 Codex 会话中执行 E2–E4，验收结束后仅删除该 Skill 目录。

在 PowerShell 中准备两个独立输出目录：

```powershell
$repo = 'D:\33054\Documents\ChatGPT\super-codex'
$smoke = Join-Path $repo 'tests\native-smoke.ps1'
$run = Join-Path $repo ('tests\native-smoke-' + [guid]::NewGuid().ToString('N'))
$e23 = [IO.Directory]::CreateDirectory((Join-Path $run 'e2e3')).FullName
$e4 = [IO.Directory]::CreateDirectory((Join-Path $run 'e4')).FullName
```

桌面端将下列命令生成的提示（首行为 `$orchestrator`）分别粘贴到新会话：

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $smoke -Prompt -OutputDir $e23 -Case E2E3
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $smoke -Prompt -OutputDir $e4 -Case E4
```

使用 Codex CLI 验收时可保留 JSON 事件轨迹供核对：

```powershell
codex --version
$traceE23 = powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $smoke -Prompt -OutputDir $e23 -Case E2E3 | codex exec -C $repo --sandbox workspace-write --json -
$traceE4 = powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $smoke -Prompt -OutputDir $e4 -Case E4 | codex exec -C $repo --sandbox workspace-write --json -
```

完成后用独立进程核验产物（正常退出码均为 `0`）：

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $smoke -OutputDir $e23 -Case E2E3
$LASTEXITCODE
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $smoke -OutputDir $e4 -Case E4
$LASTEXITCODE
```

负向验证（将 `bravo.txt` 临时改为 `WRONG`，预期退出码为 `1` 并指出内容错误）：

```powershell
$bravo = Join-Path $e23 'bravo.txt'
$original = [IO.File]::ReadAllBytes($bravo)
try {
    [IO.File]::WriteAllText($bravo, 'WRONG')
    powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $smoke -OutputDir $e23 -Case E2E3
    $LASTEXITCODE
}
finally {
    [IO.File]::WriteAllBytes($bravo, $original)
}
```

判定标准见 [../EVAL.md](../EVAL.md)：

- **E2**：核对 3 个不同的原生 Worker ID、执行区间重叠，以及主会话未直接写入目标文件。
- **E3**：核对验证 Worker 在三项写入成功后才启动并实际运行检查命令，记录正常（`0`）与篡改样本（`1`）退出码。
- **E4**：核对 `fail` 启动一次并返回退出码 `23`，`blocked` 零启动且呈阻塞状态，`independent` 成功且 `blocked.txt` 缺失。
