# Hermes Workflow 编排设计文档

> 版本：v1.0  
> 日期：2026-05-27  
> 目标读者：Tim / Hermes Agent / 后续部署 Agent  
> 文档目标：把“固定研发流程如何在 Hermes Kanban 上稳定流转”说明清楚，并给出可部署的目录、模板、Profile、Skill Bundle、Router 与 Instantiator 设计。

---

## 0. 一页结论

本设计的核心判断：

1. **Hermes Kanban 是运行时状态机，不是完整 Workflow Engine。**  
   它原生负责 task、assignee、status、parent-child dependency、dispatcher、worker handoff。

2. **Skill Bundle 只解决“角色会什么”，不解决“流程怎么流转”。**  
   例如 `ai-dev-worker` 可以加载 `claude-code`、`codex`、`test-driven-development`、`github-pr-workflow`，但它不会天然知道“先实现、再 review、再 PR”。

3. **固定流程需要额外增加一层“确定性编排层”。**  
   这层负责把 `workflow_id + YAML 模板` 编译成 Hermes Kanban DAG。

4. **推荐最终方案：Trigger Router + Deterministic Instantiator + Hermes Kanban。**  
   Trigger Router 负责入口体验；Instantiator 负责确定性创建 Kanban DAG；Hermes Kanban 负责状态流转；Profile Worker 负责执行。

5. **不要另建一套 Workflow 状态机。**  
   Workflow 层只做“编译任务图”，不维护 `running/done/blocked`。运行状态必须以 Hermes Kanban 为唯一事实源。

一句话架构：

```text
用户入口 /wf、GitHub label、Feishu、Cron、Web UI
  ↓
Trigger Router：识别 workflow_id、校验参数、去重
  ↓
Instantiator：读取 YAML，创建 Kanban tasks + parent links
  ↓
Hermes Kanban：todo / ready / running / blocked / done
  ↓
Profile Workers：dev-claude / dev-codex / reviewer / shipper
  ↓
Handoff metadata：交付给下游角色
```

---

## 1. 背景与问题

当前希望构建一套基于 Hermes 的 AI 研发生产系统，典型流程如下：

```text
需求 / issue
  ↓
orchestrator 拆任务
  ↓
dev-claude 处理复杂模块
  ↓
dev-codex 处理清晰小任务 / 批量修复
  ↓
reviewer 审查
  ↓
shipper 创建 PR / 检查 CI
  ↓
人工批准 merge
```

已有认知：

- Hermes 有 Kanban。
- Hermes 有 Profile。
- Hermes 有 Skill / Skill Bundle。
- Hermes 有 `kanban-orchestrator`、`kanban-worker`、`claude-code`、`codex`、`kanban-codex-lane`、`github-pr-workflow`、`requesting-code-review`、`test-driven-development` 等内置技能。

但核心疑问是：

> 每个需求怎么指定给 orchestrator？  
> orchestrator 怎么知道固定流程？  
> 做完之后怎么传给下一个角色？  
> YAML Workflow 和 Kanban 状态到底是什么关系？  
> 是否必须自己加一层，还是 Hermes 原生就有更好的方式？

本文档回答这些问题，并形成可部署设计。

---

## 2. Hermes 原生能力边界

### 2.1 Hermes 原生有的能力

| 能力 | 是否原生 | 说明 |
|---|---:|---|
| Kanban task | 是 | 每个任务是持久化记录 |
| task status | 是 | `triage / todo / ready / running / blocked / done / archived` |
| assignee | 是 | 每个任务分配给一个 profile |
| parent-child dependency | 是 | parent done 后 child 才能 ready |
| dispatcher | 是 | 自动拉起对应 profile worker |
| `kanban_show()` | 是 | worker 读取当前任务、父任务 handoff、评论 |
| `kanban_complete()` | 是 | worker 写 summary + metadata |
| `kanban_create()` / `kanban_link()` | 是 | orchestrator 创建子任务和依赖 |
| Skill | 是 | 单个能力/方法说明 |
| Skill Bundle | 是 | 多个 skill 的组合入口 |
| Auto Decompose | 是 | 由 LLM 根据 profile roster 动态拆任务 |
| Cron / Webhook / API Server | 是 | 可作为入口或自动化触发能力 |

### 2.2 Hermes 原生没有的能力

| 能力 | 是否原生 | 说明 |
|---|---:|---|
| 固定 YAML Workflow Registry | 否 | 需要自己定义 |
| `workflow_id → YAML → Kanban DAG` 编译器 | 否 | 需要自己实现 |
| 多入口统一路由 `/wf` | 否 | 需要 Trigger Router |
| Workflow Run 聚合视图 | 否 | 可额外做轻量索引，但不能替代 Kanban 状态 |
| 固定流程版本治理 | 否 | 需要 repo + schema + 变更管理 |

结论：

```text
Hermes 原生提供状态机和多角色协作底座。
固定流程编排需要我们额外加一层，但不能重复造状态机。
```

---

## 3. 目标与非目标

### 3.1 目标

1. 支持用一个简单入口触发固定 workflow：

```text
/wf dev-feature repo=edn-agent issue=123
```

2. 支持 GitHub issue / label 自动触发：

```text
label: workflow:dev-feature
```

3. 支持 Feishu / Web UI / Cron 触发同一套 workflow。

4. 支持 workflow 模板版本化：

```text
workflows/dev-feature-v1.yaml
workflows/deep-research-v1.yaml
workflows/incident-rca-v1.yaml
```

5. 支持 deterministic DAG creation：

```text
YAML nodes → Kanban tasks
YAML parents → Kanban links
YAML assignee → Profile worker
YAML skills → task skills
```

6. 支持下游角色自动读取上游 handoff：

```text
parent done + metadata
  ↓
child ready
  ↓
child worker kanban_show()
```

7. 支持人工 gate：

```text
PR 创建完成后，不自动 merge，需要人工批准。
```

8. 支持审计：每个任务的输入、输出、状态、handoff、PR、CI 结果可追溯。

### 3.2 非目标

1. 不重新实现 Hermes Kanban 状态机。
2. 不绕过 Hermes dispatcher。
3. 不允许外部 coding agent 自己标记任务完成。
4. 不做无人值守自动 merge。
5. 不把所有任务都固定死；探索型任务仍允许 Hermes Auto Decompose。

---

## 4. 核心概念定义

### 4.1 Profile

Profile 是“谁来做”。

示例：

```yaml
profiles:
  orchestrator:
    role: workflow decomposition and routing
  dev-claude:
    role: complex implementation
  dev-codex:
    role: simple implementation and batch fixes
  reviewer:
    role: code review and risk check
  shipper:
    role: PR creation and CI tracking
  human:
    role: manual approval gate
```

### 4.2 Skill

Skill 是“怎么做”。

示例：

```text
claude-code
codex
kanban-codex-lane
test-driven-development
requesting-code-review
github-pr-workflow
```

### 4.3 Skill Bundle

Skill Bundle 是“一组常用技能的快捷组合”。

它解决的是：

```text
这个角色执行任务时需要加载哪些能力？
```

它不解决：

```text
这个角色什么时候执行？执行完之后交给谁？
```

### 4.4 Workflow Template

Workflow Template 是“固定工艺路线”。

它定义：

```text
节点有哪些
节点谁执行
节点依赖谁
节点需要哪些 skill
节点完成标准是什么
节点输出什么 handoff
```

### 4.5 Kanban DAG

Kanban DAG 是某次 workflow 执行时在 Hermes Kanban 里创建出来的真实任务图。

```text
Workflow Template = 蓝图
Kanban DAG        = 实例化后的工单图
Kanban Status     = 实时运行状态
```

### 4.6 Instantiator

Instantiator 是“把蓝图变成工单”的确定性编译器。

它只做：

```text
读取 YAML
校验 schema
创建 Kanban task
创建 parent link
记录 workflow_run 映射
退出
```

它不做：

```text
不维护 running/done/blocked
不调度 worker
不执行代码
不 review
不 merge
```

### 4.7 Trigger Router

Trigger Router 是“统一入口层”。

它负责：

```text
接收 /wf、GitHub Webhook、Feishu 命令、Web UI、Cron
识别 workflow_id
校验参数
生成 idempotency_key
调用 Instantiator
返回 run_id / root_task_id
```

---

## 5. 方案对比

### 5.1 方案 0：Hermes 原生 Auto Decompose

流程：

```text
用户创建 triage task
  ↓
Hermes decomposer 根据 profile 描述自动拆任务
  ↓
生成 Kanban 子任务和依赖
  ↓
dispatcher 执行
```

优点：

- 原生能力。
- 少写代码。
- 适合探索型任务。
- 对未知问题灵活。

缺点：

- 不确定。
- 每次拆分结果可能不同。
- 不适合固定研发生产线。
- 版本化、审计、复盘较弱。

适用场景：

```text
开放式研究
一次性分析
需求本身不清楚
希望 AI 自己判断怎么拆
```

不适用场景：

```text
固定研发流程
强 gate 流程
每次必须 review / PR / human approval
需要合规审计
```

---

### 5.2 方案 A：YAML Workflow → Kanban DAG

这里的方案 A 指：通过自定义 `workflow-orchestrator` Skill 或轻量脚本，让 orchestrator 根据 root task 里的 `workflow_id` 读取 YAML，然后创建 Kanban DAG。

流程：

```text
用户创建 root task
  ↓
root task assignee = orchestrator
  ↓
orchestrator 读取 workflow_id
  ↓
orchestrator 根据 Skill 规则读取 YAML
  ↓
orchestrator 调用 kanban_create / kanban_link
  ↓
Hermes Kanban 执行 DAG
```

优点：

- 上手快。
- 改动少。
- 与 Hermes 原生 Kanban 结合紧密。
- 适合快速验证 workflow 模型。
- 可以把模板放在 `~/.hermes/skills/workflow-orchestrator/references/workflows/`。

缺点：

- 如果由 LLM 读取 YAML 并展开，仍有一定不确定性。
- schema 校验能力弱。
- 入口体验仍可能依赖 CLI 或手工 root task。
- 不适合大量自动触发和团队化运营。

适用阶段：

```text
V1 验证阶段
个人使用
流程还在频繁调整
快速沉淀几个 workflow 模板
```

---

### 5.3 方案 B：Trigger Router + Deterministic Instantiator

流程：

```text
/wf、GitHub label、Feishu、Web UI、Cron
  ↓
Trigger Router
  ↓
校验 workflow_id + 参数
  ↓
Deterministic Instantiator 读取 YAML
  ↓
创建 Kanban DAG
  ↓
Hermes dispatcher 执行
```

优点：

- 用户体验最好。
- 确定性最高。
- 可测试、可审计、可回放。
- 支持 idempotency，避免重复触发。
- 支持多入口统一。
- 适合团队化、产品化。

缺点：

- 需要额外开发 Router / Instantiator。
- 需要维护 workflow schema。
- 初期实现成本高于方案 A。

适用阶段：

```text
V2 生产化阶段
多项目 / 多入口 / 多人使用
需要稳定研发流程
需要运行记录和审计
```

---

### 5.4 对比表

| 维度 | Hermes Auto Decompose | YAML Workflow → Kanban DAG | Trigger Router + Instantiator |
|---|---|---|---|
| 是否 Hermes 原生 | 高 | 中 | 中 |
| 固定流程确定性 | 低 | 中高 | 高 |
| 用户体验 | 中 | 中 | 高 |
| 工程复杂度 | 低 | 中 | 中高 |
| 适合探索任务 | 高 | 中 | 中 |
| 适合固定研发流程 | 低 | 高 | 最高 |
| 可审计 | 中 | 高 | 高 |
| 可测试 | 低 | 中 | 高 |
| 多入口支持 | 中 | 中 | 高 |
| 重复触发去重 | 中 | 中 | 高 |
| 推荐阶段 | 探索任务常驻 | V1 | V2/V3 |

最终建议：

```text
V1：先做 YAML Workflow → Kanban DAG，快速验证。
V2：升级为 Trigger Router + Deterministic Instantiator。
长期：Auto Decompose 保留给探索型任务；固定流程走 Instantiator。
```

---

## 6. 推荐总体架构

```text
┌───────────────────────────────────────────────┐
│ 用户入口层                                      │
│ /wf CLI / Feishu / GitHub Label / Web UI / Cron │
└───────────────────────┬───────────────────────┘
                        ↓
┌───────────────────────────────────────────────┐
│ Trigger Router                                 │
│ - 识别 workflow_id                              │
│ - 校验参数                                      │
│ - 权限控制                                      │
│ - idempotency_key                               │
│ - 调用 Instantiator                             │
└───────────────────────┬───────────────────────┘
                        ↓
┌───────────────────────────────────────────────┐
│ Deterministic Instantiator                     │
│ - 读取 workflows/*.yaml                         │
│ - schema validation                            │
│ - variables rendering                           │
│ - create Kanban tasks                           │
│ - create parent links                           │
│ - write workflow_run mapping                    │
└───────────────────────┬───────────────────────┘
                        ↓
┌───────────────────────────────────────────────┐
│ Hermes Kanban                                  │
│ - task status                                   │
│ - assignee                                      │
│ - dependency                                    │
│ - dispatcher                                    │
│ - summary + metadata handoff                    │
└───────────────────────┬───────────────────────┘
                        ↓
┌───────────────────────────────────────────────┐
│ Profile Workers                                │
│ orchestrator / dev-claude / dev-codex / reviewer │
│ shipper / human                                 │
└───────────────────────────────────────────────┘
```

---

## 7. Workflow 与 Kanban 状态的关系

### 7.1 Workflow 只定义结构

```yaml
nodes:
  - id: clarify
    assignee: orchestrator

  - id: complex_impl
    assignee: dev-claude
    parents: [clarify]

  - id: review
    assignee: reviewer
    parents: [complex_impl]
```

### 7.2 Kanban 负责运行状态

实例化后：

| Workflow Node | Kanban Task | 状态流转 |
|---|---|---|
| clarify | `t_001` | `ready → running → done` |
| complex_impl | `t_002` | `todo → ready → running → done` |
| review | `t_003` | `todo → ready → running → blocked/done` |

### 7.3 映射规则

```text
workflow.nodes[*].id        → node_id
workflow.nodes[*].assignee  → kanban.task.assignee
workflow.nodes[*].parents   → kanban.task_links parent → child
workflow.nodes[*].skills    → kanban.task.skills
workflow.nodes[*].body      → kanban.task.body
workflow.nodes[*].workspace → kanban.task.workspace
```

### 7.4 状态唯一事实源

原则：

```text
workflow_run 不保存独立状态。
workflow_run.status 只能从 Kanban tasks 聚合计算。
```

聚合规则：

| Kanban 任务状态集合 | Workflow Run 状态 |
|---|---|
| 任一关键任务 `blocked` | `blocked` |
| 任一任务 `running` | `running` |
| 存在 `ready` 任务 | `ready` |
| 只有 `todo` 等待父任务 | `waiting` |
| 所有 leaf nodes `done` | `done` |
| root / leaf archived | `archived` |

---

## 8. 目录结构设计

推荐在一个独立 repo 管理：

```text
hermes-workflow-system/
├── README.md
├── workflows/
│   ├── dev-feature-v1.yaml
│   ├── deep-research-v1.yaml
│   ├── incident-rca-v1.yaml
│   └── competitor-tracking-v1.yaml
│
├── schemas/
│   ├── workflow.schema.json
│   ├── root-task.schema.json
│   └── handoff.schema.json
│
├── skills/
│   └── workflow-orchestrator/
│       ├── SKILL.md
│       └── references/
│           └── workflows/
│               └── dev-feature-v1.yaml
│
├── skill-bundles/
│   ├── ai-dev-claude.yaml
│   ├── ai-dev-codex.yaml
│   ├── ai-reviewer.yaml
│   ├── ai-shipper.yaml
│   └── ai-research.yaml
│
├── profiles/
│   ├── orchestrator.md
│   ├── dev-claude.md
│   ├── dev-codex.md
│   ├── reviewer.md
│   └── shipper.md
│
├── router/
│   ├── app.py
│   ├── routes/
│   │   ├── feishu.py
│   │   ├── github.py
│   │   ├── cli.py
│   │   └── web.py
│   └── config.yaml
│
├── instantiator/
│   ├── instantiate.py
│   ├── hermes_client.py
│   ├── renderer.py
│   ├── validator.py
│   └── state_index.py
│
├── tests/
│   ├── test_workflow_schema.py
│   ├── test_instantiation.py
│   ├── test_idempotency.py
│   └── fixtures/
│       └── dev-feature-sample.yaml
│
└── docs/
    ├── operation-runbook.md
    ├── profile-capability-map.md
    └── workflow-authoring-guide.md
```

本地安装到 Hermes 的推荐同步方式：

```bash
# skills
mkdir -p ~/.hermes/skills/workflow-orchestrator
cp -r skills/workflow-orchestrator/* ~/.hermes/skills/workflow-orchestrator/

# skill bundles
mkdir -p ~/.hermes/skill-bundles
cp skill-bundles/*.yaml ~/.hermes/skill-bundles/

# workflows
mkdir -p ~/.hermes/workflows
cp workflows/*.yaml ~/.hermes/workflows/
```

---

## 9. Profile 设计

### 9.1 Profile Capability Map

```yaml
profiles:
  orchestrator:
    responsibility:
      - read root workflow task
      - validate intent
      - route work through Kanban
      - never implement code directly
    allowed_skills:
      - kanban-orchestrator
      - workflow-orchestrator
    forbidden_actions:
      - write production code
      - merge PR
      - mark implementation as accepted

  dev-claude:
    responsibility:
      - complex implementation
      - architecture-sensitive refactor
      - multi-file changes
      - test-first implementation
    allowed_skills:
      - kanban-worker
      - claude-code
      - test-driven-development
      - requesting-code-review
    default_workspace: worktree
    task_size_limit:
      changed_lines_hint: 1000
      note: "单个子任务默认控制在约 1000 行影响范围，超过则拆分。"

  dev-codex:
    responsibility:
      - simple bounded changes
      - batch fixes
      - lint/test fixes
      - low-risk implementation
    allowed_skills:
      - kanban-worker
      - codex
      - kanban-codex-lane
      - test-driven-development
    default_workspace: worktree
    forbidden_actions:
      - use --yolo by default
      - modify secrets or credential stores
      - self-approve completion

  reviewer:
    responsibility:
      - review parent handoff
      - inspect diff and tests
      - identify risks
      - decide PASS / PASS_WITH_CHANGES / FAIL
    allowed_skills:
      - kanban-worker
      - requesting-code-review

  shipper:
    responsibility:
      - create branch / PR
      - check CI status
      - prepare merge recommendation
      - never auto-merge without human approval
    allowed_skills:
      - kanban-worker
      - github-pr-workflow
```

---

## 10. Skill Bundle 设计

### 10.1 `ai-dev-claude.yaml`

```yaml
name: ai-dev-claude
description: Complex implementation worker using Claude Code, TDD and review discipline.
skills:
  - kanban-worker
  - claude-code
  - test-driven-development
  - requesting-code-review
instruction: |
  You are a Hermes Kanban worker for complex implementation tasks.
  Always call kanban_show first.
  Work only inside the assigned workspace.
  Prefer git worktree for code changes.
  Start with failing tests when feasible.
  Never mark done without verification evidence.
  Finish using kanban_complete with structured metadata.
```

### 10.2 `ai-dev-codex.yaml`

```yaml
name: ai-dev-codex
description: Bounded implementation worker using Codex lane inside Hermes Kanban.
skills:
  - kanban-worker
  - codex
  - kanban-codex-lane
  - test-driven-development
  - requesting-code-review
instruction: |
  Use Codex only as an isolated implementation lane.
  Hermes owns the task lifecycle.
  Never treat Codex self-report as completion.
  Review diffs and run tests before kanban_complete.
  Do not use dangerous bypass flags by default.
```

### 10.3 `ai-reviewer.yaml`

```yaml
name: ai-reviewer
description: Review implementation evidence, diff, tests and residual risk.
skills:
  - kanban-worker
  - requesting-code-review
instruction: |
  Always read all parent handoffs.
  Review changed files, verification evidence and residual risk.
  Return PASS, PASS_WITH_CHANGES, or FAIL.
  If FAIL, block or create requested-change tasks.
```

### 10.4 `ai-shipper.yaml`

```yaml
name: ai-shipper
description: GitHub PR workflow and CI tracking.
skills:
  - kanban-worker
  - github-pr-workflow
instruction: |
  Create PR only after reviewer has passed the work.
  Check CI status.
  Never merge automatically.
  Produce PR URL, CI status and merge recommendation.
```

---

## 11. Workflow Template Schema

### 11.1 顶层字段

```yaml
id: dev-feature-v1
version: 1.0.0
description: Feature development workflow from issue to PR.

inputs:
  repo:
    required: true
  issue:
    required: true
  branch_prefix:
    required: false

entry:
  assignee: orchestrator
  skills:
    - workflow-orchestrator
    - kanban-orchestrator

nodes:
  - id: clarify
    title: "Clarify issue #{issue}"
    assignee: orchestrator
    parents: []
    skills: []
    workspace: null
    body: |
      ...
    output_contract: []
```

### 11.2 字段含义

| 字段 | 必填 | 说明 |
|---|---:|---|
| `id` | 是 | workflow 唯一 ID |
| `version` | 是 | 模板版本 |
| `inputs` | 是 | 运行所需参数 |
| `entry` | 是 | root task 默认 assignee/skills |
| `nodes` | 是 | DAG 节点列表 |
| `nodes[].id` | 是 | 节点 ID，模板内唯一 |
| `nodes[].assignee` | 是 | 对应 Hermes profile |
| `nodes[].parents` | 否 | 依赖节点 ID |
| `nodes[].skills` | 否 | task 需要加载的技能 |
| `nodes[].workspace` | 否 | `scratch` / `dir:<path>` / `worktree:<path>` |
| `nodes[].body` | 是 | 给 worker 的任务说明 |
| `nodes[].output_contract` | 否 | 完成时必须输出的结构化字段 |
| `nodes[].mode` | 否 | `auto` / `manual_gate` / `auto_decompose` |
| `nodes[].task_size_limit` | 否 | 子任务规模约束 |

---

## 12. 样例：`dev-feature-v1.yaml`

```yaml
id: dev-feature-v1
version: 1.0.0
description: Deterministic feature development workflow from GitHub issue to PR.

inputs:
  repo:
    required: true
    description: Local repo path or repository name.
  issue:
    required: true
    description: GitHub issue number or issue URL.
  branch_prefix:
    required: false
    default: ai/dev-feature

entry:
  assignee: orchestrator
  skills:
    - workflow-orchestrator
    - kanban-orchestrator

nodes:
  - id: clarify
    title: "Clarify issue #{issue}"
    assignee: orchestrator
    skills:
      - kanban-orchestrator
    workspace: "dir:{repo}"
    body: |
      Clarify the goal, non-goals, acceptance criteria, risks and task split for issue #{issue}.
      Repo: {repo}

      Output must include:
      - goal
      - non_goals
      - acceptance_criteria
      - risk_points
      - implementation_split
    output_contract:
      - goal
      - non_goals
      - acceptance_criteria
      - risk_points
      - implementation_split

  - id: complex_impl
    title: "Complex implementation for issue #{issue}"
    assignee: dev-claude
    parents:
      - clarify
    workspace: "worktree:{repo}"
    skills:
      - claude-code
      - test-driven-development
    body: |
      Implement architecture-sensitive or multi-file changes for issue #{issue}.
      Read parent handoff from clarify first.
      Work only in the assigned git worktree.
      Start with tests where feasible.
      Keep the changed-line impact around 1000 lines; split if larger.

      Completion requires:
      - changed_files
      - tests_added
      - verification
      - residual_risk
    output_contract:
      - changed_files
      - tests_added
      - verification
      - residual_risk
    task_size_limit:
      changed_lines_hint: 1000

  - id: simple_impl
    title: "Simple bounded fixes for issue #{issue}"
    assignee: dev-codex
    parents:
      - clarify
    workspace: "worktree:{repo}"
    skills:
      - codex
      - kanban-codex-lane
      - test-driven-development
    body: |
      Handle simple, bounded or batch changes for issue #{issue}.
      Use Codex only as an isolated implementation lane.
      Do not modify security-sensitive files unless explicitly required by clarify handoff.
      Do not use dangerous bypass flags by default.

      Completion requires:
      - changed_files
      - verification
      - residual_risk
    output_contract:
      - changed_files
      - verification
      - residual_risk
    task_size_limit:
      changed_lines_hint: 1000

  - id: review
    title: "Review implementation for issue #{issue}"
    assignee: reviewer
    parents:
      - complex_impl
      - simple_impl
    workspace: "dir:{repo}"
    skills:
      - requesting-code-review
    body: |
      Review all parent handoffs, changed files, tests and residual risks.
      Decide one of:
      - PASS
      - PASS_WITH_CHANGES
      - FAIL

      If FAIL, create or request follow-up tasks instead of allowing PR.
    output_contract:
      - review_result
      - blocking_issues
      - requested_changes
      - residual_risk

  - id: pr
    title: "Create PR and check CI for issue #{issue}"
    assignee: shipper
    parents:
      - review
    workspace: "dir:{repo}"
    skills:
      - github-pr-workflow
    body: |
      Create a GitHub PR only if review is PASS or PASS_WITH_CHANGES.
      Check CI status.
      Do not merge automatically.

      Completion requires:
      - branch
      - pr_url
      - ci_status
      - merge_recommendation
    output_contract:
      - branch
      - pr_url
      - ci_status
      - merge_recommendation

  - id: human_approval
    title: "Human approval for issue #{issue}"
    assignee: human
    parents:
      - pr
    mode: manual_gate
    body: |
      Human approval required before merge.
      Review PR URL, CI status and merge recommendation.
```

---

## 13. Root Task 设计

### 13.1 Root Task Body

```yaml
workflow_id: dev-feature-v1
workflow_version: 1.0.0
repo: /home/tim/projects/edn-agent
issue: 123
source:
  type: github_issue
  repo: tim/edn-agent
  issue: 123
expected_output: pull_request
requested_by: tim
```

### 13.2 Root Task 创建规则

root task 必须：

```yaml
title: "[WF] dev-feature-v1: issue #123"
assignee: orchestrator
skills:
  - workflow-orchestrator
  - kanban-orchestrator
idempotency_key: "github:tim/edn-agent:issue:123:workflow:dev-feature-v1"
```

### 13.3 Root Task 的作用

root task 不是实际开发任务。它的作用是：

```text
记录 workflow 入口
触发 orchestrator 或 instantiator
作为 DAG 的根节点
承载 workflow_run 元数据
```

---

## 14. Handoff Contract

每个 worker 完成任务时，必须使用结构化 metadata。

### 14.1 通用结构

```json
{
  "summary": "Human-readable closeout.",
  "metadata": {
    "changed_files": [],
    "verification": [],
    "dependencies": [],
    "blocked_reason": null,
    "retry_notes": null,
    "residual_risk": [],
    "next_action": ""
  }
}
```

### 14.2 研发任务 metadata

```json
{
  "changed_files": [
    "src/auth/login.ts",
    "tests/auth/login.test.ts"
  ],
  "tests_added": [
    "tests/auth/login.test.ts"
  ],
  "verification": [
    "npm test tests/auth/login.test.ts",
    "npm run lint"
  ],
  "residual_risk": [
    "OAuth timeout scenario not covered"
  ],
  "pr_ready": false
}
```

### 14.3 Review metadata

```json
{
  "review_result": "PASS_WITH_CHANGES",
  "blocking_issues": [],
  "requested_changes": [
    "Add timeout test for GitHub OAuth"
  ],
  "residual_risk": [
    "Need manual verification in staging"
  ]
}
```

### 14.4 PR metadata

```json
{
  "branch": "ai/dev-feature/issue-123",
  "pr_url": "https://github.com/org/repo/pull/456",
  "ci_status": "passing",
  "merge_recommendation": "ready_for_human_review"
}
```

---

## 15. Instantiator 设计

### 15.1 职责

```text
输入：workflow_id + inputs
输出：Kanban DAG + workflow_run mapping
```

### 15.2 伪代码

```python
def instantiate_workflow(workflow_id: str, inputs: dict) -> dict:
    workflow = load_yaml(f"workflows/{workflow_id}.yaml")
    validate_schema(workflow)
    validate_inputs(workflow["inputs"], inputs)
    validate_profiles_exist(workflow["nodes"])

    workflow_run_id = create_workflow_run_id()
    node_to_task_id = {}

    root_task_id = create_kanban_task(
        title=f"[WF] {workflow_id}: {inputs}",
        assignee=workflow["entry"]["assignee"],
        skills=workflow["entry"].get("skills", []),
        body=render_root_body(workflow_id, inputs),
        idempotency_key=make_idempotency_key(workflow_id, inputs),
    )

    for node in topological_sort(workflow["nodes"]):
        parent_task_ids = [node_to_task_id[p] for p in node.get("parents", [])]
        task_id = create_kanban_task(
            title=render(node["title"], inputs),
            assignee=node["assignee"],
            parents=parent_task_ids,
            skills=node.get("skills", []),
            workspace=render(node.get("workspace"), inputs),
            body=render(node["body"], inputs),
            metadata={
                "workflow_run_id": workflow_run_id,
                "workflow_id": workflow_id,
                "node_id": node["id"],
                "output_contract": node.get("output_contract", []),
            },
        )
        node_to_task_id[node["id"]] = task_id

    save_workflow_run_index(
        workflow_run_id=workflow_run_id,
        workflow_id=workflow_id,
        root_task_id=root_task_id,
        node_to_task_id=node_to_task_id,
        inputs=inputs,
    )

    return {
        "workflow_run_id": workflow_run_id,
        "root_task_id": root_task_id,
        "node_to_task_id": node_to_task_id,
    }
```

### 15.3 必须校验

```text
workflow_id 是否存在
workflow version 是否支持
required inputs 是否完整
node id 是否唯一
parents 是否存在
DAG 是否无环
assignee profile 是否存在
skills 是否存在
workspace 是否合法
manual_gate 是否有明确处理方式
```

---

## 16. Trigger Router 设计

### 16.1 支持入口

| 入口 | 示例 | 处理方式 |
|---|---|---|
| CLI | `/wf dev-feature issue=123` | parse command |
| Feishu | `@Hermes /wf dev-feature issue=123` | gateway/platform route |
| GitHub label | `workflow:dev-feature` | webhook route |
| GitHub comment | `/workflow dev-feature` | webhook route |
| Web UI | 选择 workflow + 填参数 | HTTP API |
| Cron | 每三天 competitor-tracking | scheduled trigger |

### 16.2 Router API

```http
POST /workflows/{workflow_id}/runs
Content-Type: application/json

{
  "inputs": {
    "repo": "/home/tim/projects/edn-agent",
    "issue": 123
  },
  "source": {
    "type": "github_issue",
    "repo": "tim/edn-agent",
    "issue": 123
  },
  "requested_by": "tim"
}
```

返回：

```json
{
  "workflow_run_id": "wf_20260527_001",
  "root_task_id": "t_root",
  "status": "created",
  "board": "edn-agent"
}
```

### 16.3 Idempotency Key

规则：

```text
{source.type}:{source.repo}:{source.issue}:workflow:{workflow_id}:version:{workflow_version}
```

示例：

```text
github_issue:tim/edn-agent:123:workflow:dev-feature-v1:version:1.0.0
```

用途：

```text
同一个 issue 重复打 label 不重复创建 DAG。
失败重试时返回已有 workflow_run_id。
```

---

## 17. 运行流程示例

### 17.1 GitHub Issue 触发

```text
1. 用户给 issue #123 打 label：workflow:dev-feature
2. GitHub webhook → Trigger Router
3. Router 识别 workflow_id = dev-feature-v1
4. Router 生成 idempotency_key
5. Instantiator 读取 dev-feature-v1.yaml
6. Instantiator 创建 Kanban DAG
7. Hermes dispatcher 拉起 orchestrator / dev / reviewer / shipper
8. 每个 worker 用 kanban_show 读取任务和父任务 handoff
9. 每个 worker 用 kanban_complete 写 summary + metadata
10. PR 任务完成后进入 human_approval
```

### 17.2 Feishu 命令触发

```text
@Hermes /wf dev-feature repo=edn-agent issue=123
```

返回：

```text
已创建 workflow run：wf_20260527_001
Root task：t_root
当前状态：clarify ready
看板：edn-agent
```

### 17.3 周期性研究触发

```text
/cron every 3 days /wf competitor-tracking topic="Cisco / HPE Aruba / Juniper Mist AI Ops updates"
```

建议：定时任务只负责触发 root workflow，不在 cron prompt 里塞完整研究逻辑。

---

## 18. V1 实施方案：Skill-Orchestrator 版

### 18.1 适用目的

```text
快速验证 workflow 模板
不用先开发 Router 服务
先让 Hermes 自己读模板并创建 DAG
```

### 18.2 文件位置

```text
~/.hermes/skills/workflow-orchestrator/
├── SKILL.md
└── references/
    └── workflows/
        └── dev-feature-v1.yaml
```

### 18.3 `SKILL.md` 草案

```markdown
---
name: workflow-orchestrator
description: Expand named workflow templates into Hermes Kanban DAG tasks.
version: 1.0.0
---

# Workflow Orchestrator

## Purpose

Expand a root workflow task into deterministic Hermes Kanban child tasks.

## Trigger

Use this skill when the current Kanban task body contains:

- workflow_id
- repo or project
- issue or input
- expected_output

## Procedure

1. Call `kanban_show()` first.
2. Parse `workflow_id` from the root task body.
3. Load the matching YAML template from `references/workflows/{workflow_id}.yaml`.
4. Validate required inputs are present.
5. For each node, create a Kanban task using `kanban_create`.
6. Translate `parents` into parent task IDs.
7. Attach workflow metadata to every task body.
8. Complete the root task with a summary containing all created task IDs.

## Rules

- Do not implement code.
- Do not review code.
- Do not create PR.
- Only create and link tasks.
- If template is missing or invalid, call `kanban_block()` with the reason.
```

### 18.4 Root Task 创建示例

```bash
hermes kanban create "[WF] dev-feature-v1: issue #123" \
  --assignee orchestrator \
  --skill workflow-orchestrator \
  --skill kanban-orchestrator \
  --idempotency-key "github:edn-agent:123:dev-feature-v1" \
  --body "
workflow_id: dev-feature-v1
repo: /home/tim/projects/edn-agent
issue: 123
expected_output: pull_request
"
```

### 18.5 V1 风险

| 风险 | 控制措施 |
|---|---|
| LLM 读 YAML 时误解 | 模板尽量短，节点明确 |
| 漏建任务 | root complete metadata 必须列出 node/task mapping |
| profile 不存在 | 先让 orchestrator 检查 profile roster |
| 模板格式错误 | invalid 时 block root task |

---

## 19. V2 实施方案：Trigger Router + Instantiator

### 19.1 适用目的

```text
生产化
多入口触发
确定性创建 DAG
可测试、可审计、可回放
```

### 19.2 服务组成

```text
router/app.py
  - FastAPI app
  - auth middleware
  - GitHub webhook route
  - Feishu command route
  - workflow run route

instantiator/instantiate.py
  - load workflow YAML
  - validate schema
  - render variables
  - call Hermes Kanban CLI/API
  - save workflow_run index
```

### 19.3 推荐接口

```bash
python -m instantiator.instantiate \
  --workflow dev-feature-v1 \
  --repo /home/tim/projects/edn-agent \
  --issue 123 \
  --board edn-agent
```

或：

```http
POST /workflows/dev-feature-v1/runs
```

### 19.4 Router 不应该做的事

```text
不写代码
不 review
不 merge
不直接改 Hermes DB
不维护 task status
```

### 19.5 Instantiator 不应该做的事

```text
不执行 worker
不轮询每个任务状态作为主流程
不绕过 Hermes dispatcher
不替代 kanban_complete
```

---

## 20. Workflow Run Index

可选做一个轻量索引文件或 SQLite 表：

```json
{
  "workflow_run_id": "wf_20260527_001",
  "workflow_id": "dev-feature-v1",
  "workflow_version": "1.0.0",
  "board": "edn-agent",
  "root_task_id": "t_root",
  "node_to_task_id": {
    "clarify": "t_001",
    "complex_impl": "t_002",
    "simple_impl": "t_003",
    "review": "t_004",
    "pr": "t_005",
    "human_approval": "t_006"
  },
  "inputs": {
    "repo": "/home/tim/projects/edn-agent",
    "issue": 123
  },
  "created_at": "2026-05-27T00:00:00Z"
}
```

注意：

```text
这个 index 只做映射，不保存真实运行状态。
真实状态必须从 Kanban task status 聚合。
```

---

## 21. 安全与质量策略

### 21.1 Worktree 隔离

默认所有 coding task 使用：

```text
workspace: worktree:{repo}
```

避免不同 worker 在同一目录互相踩踏。

### 21.2 禁止危险参数默认开启

默认禁止：

```text
codex --yolo
claude --dangerously-skip-permissions
无人值守 auto merge
```

### 21.3 任务规模限制

原则：

```text
单个开发子任务默认控制在约 1000 行影响范围。
超过则由 orchestrator 拆分。
```

### 21.4 Review Gate

所有实现任务必须经过 reviewer：

```text
dev task done ≠ feature accepted
reviewer PASS 才能进入 PR
```

### 21.5 Human Gate

PR 创建后必须进入人工批准：

```text
PR ready ≠ auto merge
human approval required
```

### 21.6 Secret / Credential 保护

dev-codex / dev-claude 默认不得修改：

```text
.env
secrets
credential stores
production config
payment/order systems
```

如必须修改，需人工 gate。

---

## 22. 验收测试

### 22.1 Workflow 创建测试

输入：

```bash
/wf dev-feature repo=edn-agent issue=123
```

期望：

```text
创建 workflow_run_id
创建 root_task
创建 clarify / complex_impl / simple_impl / review / pr / human_approval tasks
parents 正确
assignee 正确
skills 正确
```

### 22.2 Parent Gate 测试

期望：

```text
complex_impl 和 simple_impl 在 clarify done 前保持 todo。
clarify done 后进入 ready。
review 在两个 implementation 都 done 前保持 todo。
```

### 22.3 Handoff 测试

期望：

```text
reviewer 启动后能看到 complex_impl 和 simple_impl 的 metadata。
shipper 启动后能看到 reviewer 的 review_result。
```

### 22.4 Idempotency 测试

同一个 GitHub issue 重复触发：

```text
不重复创建 DAG。
返回已有 workflow_run_id。
```

### 22.5 Failure / Block 测试

当缺少 repo 参数：

```text
workflow 不创建子任务。
root task blocked。
blocked_reason 说明缺少 repo。
```

当 CI 失败：

```text
shipper task blocked 或创建 follow-up fix task。
不进入 human_approval done。
```

---

## 23. 部署执行清单

### Phase 0：前置检查

```bash
hermes --version
hermes kanban init
hermes gateway start
hermes skills list
hermes bundles list
```

确认存在以下 bundled skills：

```text
kanban-orchestrator
kanban-worker
claude-code
codex
kanban-codex-lane
github-pr-workflow
requesting-code-review
test-driven-development
```

### Phase 1：创建目录

```bash
mkdir -p ~/.hermes/workflows
mkdir -p ~/.hermes/skill-bundles
mkdir -p ~/.hermes/skills/workflow-orchestrator/references/workflows
```

### Phase 2：安装 V1 模板

```bash
cp workflows/dev-feature-v1.yaml ~/.hermes/workflows/
cp workflows/dev-feature-v1.yaml ~/.hermes/skills/workflow-orchestrator/references/workflows/
cp skills/workflow-orchestrator/SKILL.md ~/.hermes/skills/workflow-orchestrator/SKILL.md
```

### Phase 3：安装 Skill Bundles

```bash
cp skill-bundles/*.yaml ~/.hermes/skill-bundles/
hermes bundles reload
hermes bundles list
```

### Phase 4：创建测试 root task

```bash
hermes kanban create "[WF] dev-feature-v1: smoke test" \
  --assignee orchestrator \
  --skill workflow-orchestrator \
  --skill kanban-orchestrator \
  --body "
workflow_id: dev-feature-v1
repo: /home/tim/projects/edn-agent
issue: 123
expected_output: pull_request
"
```

### Phase 5：观察执行

```bash
hermes kanban watch
hermes kanban list
hermes kanban stats
```

### Phase 6：实现 V2 Router / Instantiator

```bash
cd hermes-workflow-system
python -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn pyyaml jsonschema typer
```

启动：

```bash
uvicorn router.app:app --host 0.0.0.0 --port 8787
```

测试：

```bash
curl -X POST http://localhost:8787/workflows/dev-feature-v1/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "inputs": {
      "repo": "/home/tim/projects/edn-agent",
      "issue": 123
    },
    "requested_by": "tim"
  }'
```

---

## 24. 给 Hermes / 部署 Agent 的任务说明

请按以下顺序执行，不要跳步：

```text
1. 阅读本文档第 0-7 节，理解设计边界。
2. 不要实现新的 workflow 状态机。
3. 以 Hermes Kanban task status 为唯一事实源。
4. 先完成 V1：workflow-orchestrator skill + dev-feature-v1.yaml。
5. 验证 root task 能展开为 Kanban DAG。
6. 验证 parent-child dependency 正常推进。
7. 验证 worker 能读取 parent handoff。
8. 再实现 V2：Trigger Router + Deterministic Instantiator。
9. 为 Instantiator 增加 schema validation 和 idempotency。
10. 为 GitHub label / Feishu / CLI 增加入口适配。
11. 所有 coding worker 必须使用 worktree。
12. 所有实现任务必须经过 reviewer。
13. PR 创建后必须等待 human approval。
```

禁止事项：

```text
不要让 dev worker 直接 merge。
不要让 Codex/Claude Code 自报完成后直接关闭整体 workflow。
不要复制一套独立状态机。
不要默认使用危险权限绕过参数。
不要把大任务一次性塞给单个 worker。
```

---

## 25. 推荐路线图

### V1：可用闭环

目标：1 天内跑通。

```text
workflow-orchestrator skill
+ dev-feature-v1.yaml
+ root task
+ Kanban DAG
+ dev/reviewer/shipper profiles
```

验收：

```text
一个 issue 能自动展开成 5-6 个 Kanban tasks。
父子依赖正确。
下游能看到上游 metadata。
```

### V2：产品化入口

目标：把手动 root task 变成 `/wf`。

```text
Trigger Router
+ CLI / Feishu / GitHub webhook
+ deterministic instantiator
+ workflow_run index
```

验收：

```text
/wf dev-feature issue=123 能创建完整 DAG。
同一个 issue 重复触发不会重复创建。
```

### V3：工程化运营

目标：适合多个项目长期使用。

```text
Workflow Catalog
Run History Dashboard
Cancel / Retry / Re-run
Schema versioning
Approval gate
Metrics
```

验收：

```text
可查看所有 workflow runs。
可按项目隔离 board。
可回放每次执行的 node/task mapping。
```

---

## 26. 最终架构判断

最推荐的长期形态：

```text
Hermes 原生 Kanban = 状态机
Auto Decompose = 探索型任务拆解器
Workflow YAML = 固定工艺路线
Instantiator = 工艺路线编译器
Trigger Router = 产品化入口
Skill Bundle = 角色能力包
Profile = 角色身份
```

最终原则：

```text
固定流程走 Instantiator。
探索任务走 Auto Decompose。
运行状态归 Kanban。
质量 gate 归 Reviewer。
上线决策归 Human。
```

这套设计不是为了把 Hermes 变重，而是为了把“聪明的 Agent”变成“稳定的生产系统”。
