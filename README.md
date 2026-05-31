# Hermes Workflow System

Hermes Workflow System 是一个面向 Hermes 的工作流插件包，用来通过 Hermes Kanban 运行确定性的多 Agent 研发工作流。

当前版本的核心工作流是 `dev-feature-v3`：先由 architect 拆解方案，再由单个 `dev-codex` 串行实现，每个开发项都经过检查、修订、提交和 PR gate。Hermes Kanban 仍然是唯一的运行状态来源；本仓库只提供工作流模板、profile 映射、路由适配器、运维辅助能力和安装工具。

## 适合什么场景

- 把一个功能需求自动拆成架构计划和若干串行开发任务。
- 用 Hermes Kanban 调度不同 profile：orchestrator、architect、dev-codex、reviewer、shipper。
- 从 CLI、GitHub label、飞书命令、HTTP payload、Cron 或 Hermes dashboard triage card 触发同一套工作流。
- 保留可审计的 workflow run 记录、节点到 Kanban task 的映射、人工操作记录和 PR gate 状态。
- 在不引入第二套 workflow runtime 的前提下，为 Hermes 增加更完整的研发流程编排能力。

## 仓库内容

- `workflows/dev-feature-v3.yaml`：工作流模板源文件。
- `skills/workflow-orchestrator/`：Hermes orchestration skill，用于把 workflow root 展开成 Kanban DAG。
- `profiles/`：本地 Hermes profile 定义和 profile registry。
- `skill-bundles/`：architect、dev-codex、reviewer、shipper 等角色的技能包。
- `instantiator/`：把 workflow YAML 确定性编译成 Hermes Kanban tasks 的实例化器。
- `router/`：CLI、GitHub label、飞书命令、Cron、HTTP 风格请求和 dashboard triage intake 的路由适配器。
- `operations/`：workflow catalog、run history、operator controls、approval gates、metrics 和 PR merge-gate reconciliation。
- `scripts/`：安装、校验、动态开发图展开和 triage monitor systemd user service 安装脚本。
- `tests/`：覆盖 workflow artifact、instantiator、router、operations 和 graph expander 的测试。

## 安装

把本仓库的 Hermes workflow artifacts 安装到本地 Hermes home：

```bash
python3 scripts/install_hermes_v13_artifacts.py
```

默认安装位置是 `~/.hermes`。安装内容包括 workflow template、workflow-orchestrator skill、profile-local skill、skill bundles、脚本，以及 `instantiator`、`router`、`operations` 运行模块。

校验仓库内 artifacts：

```bash
python3 scripts/validate_hermes_v13_artifacts.py
```

校验已安装到 `~/.hermes` 的 artifacts：

```bash
python3 scripts/validate_hermes_v13_artifacts.py --installed
```

## 创建工作流运行

使用 Instantiator 创建一个本地确定性 workflow run：

```bash
python3 -m instantiator.instantiate \
  --workflow dev-feature-v3 \
  --repo /path/to/repo \
  --issue 123 \
  --board default
```

通过 `/wf` CLI 路由适配器触发：

```bash
python3 -m router.cli "/wf dev-feature repo=/path/to/repo issue=123 board=default"
```

也可以通过 GitHub label、飞书 `/wf` 命令、HTTP 风格 payload、Cron 或 dashboard triage monitor 接入；这些入口最终都会归一化为同一个 Instantiator 调用。

## Dashboard Triage Intake

如果希望 Hermes dashboard 里的 triage card 自动进入 `dev-feature-v3` 工作流，可以安装 systemd user service：

```bash
python3 scripts/install_hermes_triage_monitor_service.py \
  --board default \
  --repo /path/to/repo \
  --enable-now
```

该服务会持续扫描指定 board 的 triage cards，将符合条件的卡片路由到 workflow adapter。路由成功后，它会在原 triage card 上写入 workflow run/root task 信息并归档源卡片。

## 验证

运行测试：

```bash
python3 -m pytest -q
```

运行 artifact 校验：

```bash
python3 scripts/validate_hermes_v13_artifacts.py
```

当前发布版本的基础验证结果：

- `python3 -m pytest -q`：29 passed
- `python3 scripts/validate_hermes_v13_artifacts.py`：repo artifacts valid

## 设计原则

- Hermes Kanban 是唯一运行状态源；workflow run 不保存另一套 runtime status。
- Instantiator 只保存实例化事务、node/task 映射和幂等键。
- Router 只负责把不同入口归一化，不负责任务执行、状态轮询或 worker runtime。
- `dev-codex` 默认串行运行，避免并行 worktree 合并复杂度。
- `automated_check` 失败时不释放下游 commit task，而是阻塞当前检查并创建修订任务。
- 默认只在澄清不足、保护路径、重复自动失败或外部权限 gate 时请求人工介入。

## 发布形态

这个项目按 Hermes plugin/workflow pack 发布，而不是独立应用。它依赖 Hermes 的 profile、Kanban task、dispatcher 和本地 `~/.hermes` 布局；本仓库负责把研发工作流、路由和运维辅助能力打包成可安装、可验证、可版本化的插件系统。
