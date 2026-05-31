# Hermes Workflow 编排设计文档

> 版本：v1.3
> 日期：2026-05-30
> 目标读者：Tim / Hermes Agent / 后续部署 Agent  
> 文档目标：把“固定研发流程如何在 Hermes Kanban 上稳定流转”说明清楚，并给出可部署的目录、模板、Profile、Skill Bundle、Router 与 Instantiator 设计。
> v1.3 修订重点：新增 `architect` 角色，在需求澄清后拆解约 1K 代码影响范围的开发计划；代码开发阶段先收敛为单一 `dev-codex` worker 串行执行，降低并发与合并复杂度；保留“生成 -> 检查 -> 修订 -> 再检查 -> 提交”的自动闭环。

---

## 0. 一页结论

本设计的核心判断：

1. **Hermes Kanban 是运行时状态机，不是完整 Workflow Engine。**  
   它原生负责 task、assignee、status、parent-child dependency、dispatcher、worker handoff。

2. **Skill Bundle 只解决“角色会什么”，不解决“流程怎么流转”。**  
   例如 `ai-dev-codex` 可以加载 `codex`、`kanban-codex-lane`、`test-driven-development`、`github-pr-workflow`，但它不会天然知道“先实现、再检查、再修订、再提交”。

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
Profile Workers：orchestrator / architect / dev-codex / reviewer / shipper
  ↓
Handoff metadata：交付给下游角色
```

v1.3 落地约束：

```text
root task 是 Kanban DAG 的 dependency root
不再使用 dev-claude；所有开发任务由 dev-codex profile 执行
新增 architect：clarify 完成后由 architect 输出开发计划
architect 必须把开发任务拆到约 1K 代码影响范围
dev-codex 先只允许一个 worker 串行开发，降低合并复杂度
需求资料和每次任务记录必须落在持久化 dir workspace
scratch 只用于一次性分析、计划、临时检查，不保存最终证据
代码任务必须使用 worktree workspace
每个 dev_impl 必须进入 automated_check
automated_check 不通过时必须创建/释放 revise_impl，再回到 automated_check
automated_check PASS 后才能进入 commit
默认人工介入只发生在需求澄清；后续只在高风险或连续自动失败时升级人工
所有 assignee 必须先通过 profile discovery / registry 校验
output_contract 使用 JSON Schema 子集
Instantiator 维护创建事务和 per-node idempotency，但不维护运行状态
workspace 使用对象形式描述 worktree branch/path/base
```

### 0.1 v1.3 对 Swarm 实现的借鉴

`hermes_cli/kanban_swarm.py` 的价值不在于把研发流程也改成 swarm，而在于它展示了一个很适合生产化的实践：

1. **不引入第二个 scheduler。** Swarm 只是把 root、parallel workers、verifier、synthesizer 写进现有 Kanban kernel。Workflow 也必须这样做：Instantiator 只写 DAG，不运行 DAG。
2. **root 同时是 audit anchor 和 blackboard。** Swarm 把共享信息写成 root task 上的结构化 JSON comment。Workflow root 也应保存 `workflow_run_id`、需求快照、architect plan、检查结果索引和最终 commit/PR 信息。
3. **root 可立即 complete 释放后续节点。** 只要 DAG 已经创建完成，root 就可以 complete；后续由 dependency gate 控制实际执行顺序。
4. **用 dependency gate 汇合。** Swarm 的 verifier 等所有 worker 完成后才启动。Workflow 中对应 `architect_plan -> dev_impl -> automated_check -> commit`，而不是让 worker 自己判断整体完成。
5. **idempotency 优先。** Swarm 创建 root 时使用 idempotency key，重复调用时读取已有 topology。Workflow run 也必须支持重复触发、半失败恢复和节点级复用。
6. **共享状态用 comment / metadata，不用外部内存。** worker 间通信通过 Kanban comments、completion metadata、dir workspace 文件完成，避免上下文压缩或进程重启丢信息。

---

## 1. 背景与问题

当前希望构建一套基于 Hermes 的 AI 研发生产系统，典型流程如下：

```text
需求 / issue
  ↓
orchestrator 澄清需求；只有这里默认允许请求人工
  ↓
architect 拆解开发计划；每项约 1K 代码影响范围
  ↓
dev-codex 单 worker 串行实现一个开发项
  ↓
automated_check 自动检查
  ↓
revise_impl 自动修订，直到 automated_check PASS
  ↓
commit_impl 提交当前开发项
  ↓
下一开发项继续 dev_impl / check / revise / commit
  ↓
shipper 创建 PR / 检查 CI / 输出合并建议
```

已有认知：

- Hermes 有 Kanban。
- Hermes 有 Profile。
- Hermes 有 Skill / Skill Bundle。
- Hermes 有 `kanban-orchestrator`、`kanban-worker`、`codex`、`kanban-codex-lane`、`github-pr-workflow`、`requesting-code-review`、`test-driven-development` 等内置技能。

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
workflows/dev-feature-v3.yaml
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
  architect:
    role: technical planning and 1K-sized implementation task breakdown
  dev-codex:
    role: serial implementation, revision and commit through Codex worker
  reviewer:
    role: automated review, quality gate and risk check
  shipper:
    role: PR creation and CI tracking
```

### 4.2 Skill

Skill 是“怎么做”。

示例：

```text
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
每次必须检查 / 修订 / 提交 / PR
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
│ orchestrator / architect / dev-codex / reviewer │
│ shipper                                        │
│ dev-codex 先只允许一个 worker 串行开发           │
└───────────────────────────────────────────────┘
```

---

## 7. Workflow 与 Kanban 状态的关系

### 7.1 Workflow 只定义结构

Workflow Template 仍然只定义 DAG 结构，不定义运行状态。

```yaml
nodes:
  - id: run_record
    assignee: orchestrator
    parents: []

  - id: clarify
    assignee: orchestrator
    parents: [run_record]

  - id: architect_plan
    assignee: architect
    parents: [clarify]

  - id: dev_impl
    assignee: dev-codex
    parents: [architect_plan]

  - id: automated_check
    assignee: reviewer
    parents: [dev_impl]

  - id: commit_impl
    assignee: dev-codex
    parents: [automated_check]

  - id: pr
    assignee: shipper
    parents: [commit_impl]
```

### 7.2 Root task 是 Kanban dependency root

root task 不只是 workflow_run 的 metadata anchor，也是真实 Kanban DAG 的 dependency root。

规则：

```text
所有 parents: [] 或未声明 parents 的 workflow node，实例化时默认依赖 root_task。
```

示例：

```text
root_task
  ↓
run_record(dir)
  ↓
clarify
  ↓
architect_plan(约 1K 任务拆解)
  ↓
dev_impl(dev-codex 单 worker)
  ↓
automated_check
  ↳ FAIL: revise_impl(dev-codex) → automated_check
        ↓
commit_impl
        ↓
pr
```

这样可以避免 Instantiator 还在创建 DAG 时，parentless node 被 dispatcher 抢跑。root task 在全部节点和 parent links 创建完成后 complete，并在 metadata 中记录 `node_to_task_id`。

### 7.3 Kanban 负责运行状态

实例化后：

| Workflow Node | Kanban Task | 状态流转 |
|---|---|---|
| root | `t_root` | `ready → running → done` |
| run_record | `t_001` | `todo → ready → running → done` |
| clarify | `t_002` | `todo → ready → running → blocked/done` |
| architect_plan | `t_003` | `todo → ready → running → done` |
| dev_impl | `t_004` | `todo → ready → running → done` |
| automated_check | `t_005` | `todo → ready → running → blocked/done` |
| revise_impl | `t_fix_n` | `todo → ready → running → done` |
| commit_impl | `t_006` | `todo → ready → running → done` |
| pr | `t_007` | `todo → ready → running → blocked/done` |

### 7.4 映射规则

```text
workflow.nodes[*].id        → node_id
workflow.nodes[*].assignee  → profile registry 中解析后的 kanban.task.assignee
workflow.nodes[*].parents   → kanban.task_links parent → child
parentless nodes            → root_task_id → child
workflow.nodes[*].skills    → kanban.task.skills
workflow.nodes[*].workspace → 渲染后的 workspace object
workflow.nodes[*].body      → kanban.task.body
workflow.nodes[*].output_contract → kanban.task.metadata.output_contract
```

### 7.5 状态唯一事实源

原则不变：

```text
workflow_run 不保存独立运行状态。
workflow_run.status 只能从 Kanban tasks 聚合计算。
```

允许 Instantiator 保存“实例化事务状态”，例如 `creating / created / failed_partial`。这只是 DAG 创建事务状态，不是 workflow 运行状态。

聚合规则：

| Kanban 任务状态集合 | Workflow Run 状态 |
|---|---|
| 任一关键任务 `blocked` | `blocked` |
| 任一任务 `running` | `running` |
| 存在 `ready` 任务 | `ready` |
| 只有 `todo` 等待父任务 | `waiting` |
| 所有 leaf nodes `done` | `done` |
| root / leaf archived | `archived` |

### 7.6 Automated Check FAIL 不释放下游

Kanban dependency 只知道 parent task 是否 `done`，不知道 metadata 中的 `check_result`。

因此 `automated_check` 节点必须遵守：

```text
PASS:
  automated_check task 可以 complete
  下游 commit_impl task 被释放

FAIL:
  automated_check task 不得 complete
  reviewer 必须 block 当前 automated_check task
  reviewer 应创建 revise_impl task 并 link 回 automated_check
  revise_impl done 后 automated_check 被 unblock / rerun
  commit_impl 保持 todo，不被释放
```

## 8. 目录结构设计

推荐在一个独立 repo 管理：

```text
hermes-workflow-system/
├── README.md
├── workflows/
│   ├── dev-feature-v3.yaml
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
│               └── dev-feature-v3.yaml
│
├── skill-bundles/
│   ├── ai-architect.yaml
│   ├── ai-dev-codex.yaml
│   ├── ai-reviewer.yaml
│   ├── ai-shipper.yaml
│   └── ai-research.yaml
│
├── profiles/
│   ├── orchestrator.md
│   ├── architect.md
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
      - hand off clarified requirements to architect
      - never implement code directly
    allowed_skills:
      - kanban-orchestrator
      - workflow-orchestrator
    forbidden_actions:
      - write production code
      - merge PR
      - mark implementation as accepted

  architect:
    responsibility:
      - read clarified requirements and acceptance criteria
      - design implementation approach
      - split feature into ordered development tasks
      - keep each development task around 1000 changed lines or less
      - define files, interfaces, risks and check commands per task
      - write architecture_plan.md and development_plan.json to run_record dir
    allowed_skills:
      - kanban-worker
      - requesting-code-review
    default_workspace: dir
    forbidden_actions:
      - write production code
      - commit code
      - create PR

  dev-codex:
    responsibility:
      - serial implementation in one Codex worker
      - revision after automated checks fail
      - lint/test/typecheck fixes
      - create commit after each checked development task passes
    allowed_skills:
      - kanban-worker
      - codex
      - kanban-codex-lane
      - test-driven-development
      - requesting-code-review
    default_workspace: worktree
    concurrency:
      max_parallel_workers: 1
      note: "先只允许一个 dev-codex worker 串行开发，避免并发合并复杂度。"
    task_size_limit:
      changed_lines_hint: 1000
      note: "每个开发项由 architect 控制在约 1000 行代码影响范围；超过则拆成多个串行 dev_impl。"
    forbidden_actions:
      - use --yolo by default
      - modify secrets or credential stores
      - self-approve completion
      - commit before automated_check PASS

  reviewer:
    responsibility:
      - review parent handoff
      - inspect integration branch diff and tests
      - identify risks
      - decide PASS / PASS_WITH_CHANGES / FAIL
      - block automated_check instead of completing when FAIL
      - create revise_impl task when automated repair is possible
    allowed_skills:
      - kanban-worker
      - requesting-code-review

  shipper:
    responsibility:
      - create branch / PR
      - check CI status
      - prepare merge recommendation
      - never auto-merge unless project policy explicitly enables it
      - only escalate to human on risk-policy exceptions
    allowed_skills:
      - kanban-worker
      - github-pr-workflow
```

### 9.2 Profile Registry

Hermes profile 是用户本地配置，不是固定内置 roster。Dispatcher 不会自动纠正未知 assignee；未知 profile 可能导致任务卡在 ready 或 spawn failure。

因此 Instantiator 在创建任何 task 前必须执行 profile discovery：

```bash
hermes profile list
```

并用 registry 解析 workflow 里的逻辑角色：

```yaml
profiles:
  orchestrator:
    hermes_profile: orchestrator
    required: true
    capabilities:
      - workflow_orchestration
      - kanban_routing

  architect:
    hermes_profile: architect
    required: true
    capabilities:
      - architecture_planning
      - task_decomposition
      - risk_assessment

  dev-codex:
    hermes_profile: dev-codex
    required: true
    max_parallel_workers: 1
    capabilities:
      - implementation
      - revision
      - commit
      - batch_fix

  reviewer:
    hermes_profile: reviewer
    required: true
    capabilities:
      - code_review
      - risk_assessment

  shipper:
    hermes_profile: shipper
    required: true
    capabilities:
      - github_pr
      - ci_check
      - release_recommendation
```

并发上限应同时落到 Hermes Kanban dispatcher 配置：

```yaml
kanban:
  max_in_progress_per_profile: 1
```

这样 `dev-codex` 先只会串行处理一个开发 worker，后续如果要恢复并行，可以再把该值调回 2 并重新引入 lane 拓扑。

### 9.3 Assignee validation

规则：

```text
required=true 且 profile 不存在：
  拒绝实例化，root task block，返回缺失 profile 列表。

dev-codex profile 存在但已有一个 running worker：
  不创建第二个并行开发 worker；新的 revise / impl task 保持 todo，等待 dispatcher 后续释放。

dev-codex profile 不存在：
  拒绝实例化，root task block。v1.3 不再 fallback 到 dev-claude。
```

每个 task metadata 应记录：

```json
{
  "workflow_assignee": "dev-codex",
  "resolved_assignee": "dev-codex",
  "worker_mode": "serial",
  "assignee_resolution": "exact"
}
```

### 9.4 人工介入策略

v1.3 默认原则：

```text
只有需求澄清默认允许人工介入。
clarify 之后，生成、检查、修订、再检查、提交、PR 都应自动完成。
```

例外只允许以下几类：

```text
clarify_confidence 低于阈值
需求存在互相矛盾的 acceptance criteria
修改 protected_paths，例如 secrets、prod infra、payment/order/auth/permission
automated_check / revise_impl 连续失败超过 retry budget
外部系统需要人工凭证或二次确认
```

`manual_gate` 仍然是 workflow node 的特殊模式，但不再放在默认主路径里。Router 可以在触发例外时临时创建 blocked gate task，等待 comment 或 unblock。

## 10. Skill Bundle 设计

### 10.1 `ai-architect.yaml`

```yaml
name: ai-architect
description: Technical planner that decomposes clarified requirements into serial 1K-sized development tasks.
skills:
  - kanban-worker
  - requesting-code-review
instruction: |
  Always call kanban_show first and read clarify handoff.
  Do not write production code.
  Produce architecture_plan.md and development_plan.json in the run_record dir.
  Split work into ordered development_tasks.
  Keep each development task around 1000 changed lines or less.
  For each task include: id, title, scope, files_expected, dependencies, check_command, acceptance_checks, risk_notes.
  If a task is too large, split it before completing.
```

### 10.2 `ai-dev-codex.yaml`

```yaml
name: ai-dev-codex
description: Serial Codex implementation worker inside Hermes Kanban.
skills:
  - kanban-worker
  - codex
  - kanban-codex-lane
  - test-driven-development
  - requesting-code-review
instruction: |
  You are the only development worker in this workflow.
  Use Codex as a serial implementation worker.
  Hermes owns the task lifecycle.
  Never treat Codex self-report as completion.
  Always call kanban_show first.
  Work only inside the assigned workspace.
  For code changes, the workspace must be worktree.
  For requirement records and durable run evidence, write to the assigned dir workspace.
  For temporary analysis, use scratch only and copy durable outputs back to dir metadata.
  Review diffs and run tests before kanban_complete.
  If this is a commit task, commit only after automated_check has PASS metadata.
  Do not use dangerous bypass flags by default.
```

### 10.3 `ai-reviewer.yaml`

```yaml
name: ai-reviewer
description: Automated quality gate for implementation evidence, diff, tests and residual risk.
skills:
  - kanban-worker
  - requesting-code-review
instruction: |
  Always read all parent handoffs.
  Review changed files, verification evidence and residual risk.
  Return PASS, PASS_WITH_CHANGES, or FAIL.
  If FAIL, do not release commit.
  Create revise_impl tasks when repair is possible.
  Block only when retry budget is exhausted or human-risk policy requires it.
```

### 10.4 `ai-shipper.yaml`

```yaml
name: ai-shipper
description: GitHub PR workflow and CI tracking.
skills:
  - kanban-worker
  - github-pr-workflow
instruction: |
  Create PR only after commit_impl has committed verified code.
  Check CI status.
  Never merge automatically.
  Produce PR URL, CI status and merge recommendation.
```

---

## 11. Workflow Template Schema

### 11.1 顶层字段

```yaml
id: dev-feature-v3
version: 1.3.0
description: Codex-only feature workflow from issue to checked commit and PR.

inputs:
  repo:
    type: path
    required: true
  issue:
    type: string
    required: true
  base_branch:
    type: string
    required: false
    default: main
  branch_prefix:
    type: string
    required: false
    default: ai/dev-feature

runtime:
  board: "{board}"
  tenant: "{tenant}"
  idempotency:
    strategy: source_workflow_version

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
    workspace:
      type: dir
      repo: "{repo}"
      path: "{repo}"
    body: |
      ...
    output_contract:
      type: object
      required: []
      properties: {}
```

### 11.2 字段含义

| 字段 | 必填 | 说明 |
|---|---:|---|
| `id` | 是 | workflow 唯一 ID |
| `version` | 是 | 模板版本 |
| `inputs` | 是 | 运行所需参数 |
| `runtime.board` | 否 | Kanban board，Router 可注入 |
| `runtime.tenant` | 否 | Kanban tenant / namespace |
| `entry` | 是 | root task 默认 assignee/skills |
| `nodes` | 是 | DAG 节点列表 |
| `nodes[].id` | 是 | 节点 ID，模板内唯一 |
| `nodes[].assignee` | 是 | 逻辑角色，需经 Profile Registry 解析为真实 Hermes profile |
| `nodes[].parents` | 否 | 依赖节点 ID；为空时实例化为依赖 root task |
| `nodes[].skills` | 否 | task 需要加载的技能 |
| `nodes[].workspace` | 否 | workspace object |
| `nodes[].body` | 是 | 给 worker 的任务说明 |
| `nodes[].output_contract` | 否 | JSON Schema 子集，定义完成 metadata |
| `nodes[].mode` | 否 | `auto` / `manual_gate` / `auto_decompose` |
| `nodes[].review_policy` | 否 | review 节点 PASS/FAIL gate 规则 |
| `nodes[].task_size_limit` | 否 | 子任务规模约束 |

### 11.3 Workspace Object

不再推荐使用：

```yaml
workspace: "worktree:{repo}"
```

推荐使用对象：

```yaml
workspace:
  type: worktree
  repo: "{repo}"
  base: "{base_branch}"
  branch: "{branch_prefix}/issue-{issue}/{node_id}"
  path: ".hermes/worktrees/{workflow_run_id}/{node_id}"
  clean_required: true
  preserve_after_done: true
```

字段：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `type` | 是 | `dir` / `worktree` / `scratch` |
| `repo` | 条件 | repo 根目录或 repo identifier |
| `base` | worktree 时建议 | worktree 基础分支 |
| `branch` | worktree 时必填 | worker 操作分支 |
| `path` | worktree 时必填 | worktree 工作目录 |
| `clean_required` | 否 | 创建前是否要求 repo clean |
| `preserve_after_done` | 否 | 完成后是否保留用于审计 |

Workspace renderer 必须注入：

```text
workflow_run_id
workflow_id
workflow_version
node_id
repo
issue
base_branch
branch_prefix
```

安全规则：

```text
repo path 必须 canonicalize
branch name 必须 sanitize
path 必须位于允许目录内
worktree branch 不得指向 protected branch
串行 dev_impl / revise_impl / commit_impl 可以复用同一 feature branch，但必须在 handoff 中记录当前 plan_item_id
```

### 11.4 `dir` / `scratch` / `worktree` 使用规则

v1.3 要求每次需求任务都留下可审计的持久目录。这里把用户口径里的 “Direct 保存” 统一落为 Hermes 的 `dir` workspace。

| workspace 类型 | 用途 | 保存策略 |
|---|---|---|
| `dir` | 需求快照、澄清记录、计划、检查报告、最终 handoff、commit/PR 索引 | 必须持久化，路径建议为 `{repo}/.hermes/workflow-runs/{workflow_run_id}` |
| `scratch` | 临时分析、一次性探查、无须长期保存的中间材料 | 可清理；若产生决策依据，必须复制摘要到 `dir` 或 completion metadata |
| `worktree` | 代码生成、修订、集成、检查、提交 | 必须绑定唯一 branch/path，完成后按 `preserve_after_done` 策略保留或清理 |

每个 workflow run 最少包含：

```text
run_record(dir)        保存需求、澄清、DAG topology、检查和最终索引
architect(dir)         保存 architecture_plan.md 和 development_plan.json
dev_impl(worktree)     dev-codex 串行实现一个 plan item
check(scratch/dir)     临时检查可用 scratch，最终报告必须写回 dir
revise_impl(worktree)  检查失败后的串行修订
commit_impl(worktree)  只在 automated_check PASS 后提交当前 plan item
```

### 11.5 Output Contract

`output_contract` 使用 JSON Schema 子集，不再只用字符串数组。

```yaml
output_contract:
  type: object
  required:
    - changed_files
    - verification
    - residual_risk
  properties:
    changed_files:
      type: array
      items:
        type: string
    verification:
      type: array
      items:
        type: string
    residual_risk:
      type: array
      items:
        type: string
```

V1：作为 task body + metadata 的软约束，由 worker 自查、reviewer 复核。  
V2：由 completion validator 校验 `kanban_complete` metadata，缺字段时拒绝 complete 或自动 block。

### 11.6 Manual Gate

```yaml
mode: manual_gate
manual_gate:
  approvers:
    - tim
  approval_actions:
    - kanban_unblock
    - comment_contains: "/approve"
  timeout: "7d"
  on_timeout: block_workflow
```

`manual_gate` 不要求存在 `human` profile。v1.3 不把它放在默认主路径里，只在低置信度澄清、protected path、连续自动失败或外部权限缺失时动态创建。

### 11.7 Check Policy

```yaml
check_policy:
  pass_values:
    - PASS
  fail_behavior:
    action: block_current_task
    create_revise_task: true
    retry_until_pass: true
    max_revise_rounds: 3
    downstream_release: false
```

当 `check_result` 是 `FAIL` 时，reviewer/checker 不得 complete 当前 `automated_check` task；必须 block 它，创建 `revise_impl`，并在修订完成后重新检查。只有 `PASS` 才能释放 `commit_impl`。

## 12. 样例：`dev-feature-v3.yaml`

### 12.1 v1.3 推荐模板

v1.3 的执行主线：

```text
run_record
  -> clarify
  -> architect_plan
  -> dev_impl[plan_item_001]
  -> automated_check[plan_item_001]
  -> revise_impl[plan_item_001] until PASS
  -> commit_impl[plan_item_001]
  -> dev_impl[plan_item_002]
  -> ...
  -> pr
```

`architect_plan` 不直接写代码。它只负责把需求拆成 `development_tasks[]`，每个任务控制在约 1K 代码影响范围。Instantiator 或 orchestrator 根据 `development_tasks[]` 生成串行子图；`dev-codex` 同一时间只跑一个 worker。

```yaml
id: dev-feature-v3
version: 1.3.0
description: Architect-planned, Codex-serial feature workflow with check/revise/commit per task.

inputs:
  repo:
    type: path
    required: true
  issue:
    type: string
    required: true
  base_branch:
    type: string
    required: false
    default: main
  branch_prefix:
    type: string
    required: false
    default: ai/dev-feature
  check_command:
    type: string
    required: false
    default: "auto"
  max_revise_rounds:
    type: integer
    required: false
    default: 3

runtime:
  run_record_dir: "{repo}/.hermes/workflow-runs/{workflow_run_id}"
  dev_profile: dev-codex
  max_parallel_dev_workers: 1
  idempotency:
    strategy: source_workflow_version

entry:
  assignee: orchestrator
  skills:
    - workflow-orchestrator
    - kanban-orchestrator

nodes:
  - id: run_record
    title: "Persist workflow run record for issue #{issue}"
    assignee: orchestrator
    parents: []
    workspace:
      type: dir
      repo: "{repo}"
      path: "{repo}/.hermes/workflow-runs/{workflow_run_id}"
      preserve_after_done: true
    body: |
      Create durable run files: requirement.md, source.json, topology.json,
      architecture_plan.md, development_plan.json, handoffs/, checks/, final.json.

  - id: clarify
    title: "Clarify issue #{issue}"
    assignee: orchestrator
    parents: [run_record]
    workspace:
      type: dir
      repo: "{repo}"
      path: "{repo}/.hermes/workflow-runs/{workflow_run_id}"
      preserve_after_done: true
    body: |
      Clarify goal, non-goals, acceptance criteria and risks.
      If clarify_confidence < 0.75 or criteria conflict, block and ask one concise question.
      Otherwise complete without human interruption.
      Write clarification.md to the run_record dir.
    output_contract:
      type: object
      required: [goal, acceptance_criteria, risk_points, clarify_confidence, human_needed]

  - id: architect_plan
    title: "Architect implementation plan for issue #{issue}"
    assignee: architect
    parents: [clarify]
    workspace:
      type: dir
      repo: "{repo}"
      path: "{repo}/.hermes/workflow-runs/{workflow_run_id}"
      preserve_after_done: true
    body: |
      Read clarification.md and produce architecture_plan.md plus development_plan.json.
      Split the feature into ordered development_tasks.
      Each development task should be around 1000 changed lines or less.
      Each task must include: id, title, scope, files_expected, dependencies,
      check_command, acceptance_checks, risk_notes.
      Do not write production code.
    output_contract:
      type: object
      required: [architecture_summary, development_tasks, risk_points]
      properties:
        development_tasks:
          type: array
          items:
            type: object
            required: [id, title, scope, estimated_changed_lines, check_command]

  - id: dev_impl
    title: "Implement plan item {plan_item_id}"
    assignee: dev-codex
    mode: dynamic_serial_template
    expand_from: architect_plan.development_tasks
    serial_group: development_tasks
    parents:
      - architect_plan
      - previous: commit_impl
    workspace:
      type: worktree
      repo: "{repo}"
      base: "{base_branch}"
      branch: "{branch_prefix}/issue-{issue}"
      path: ".hermes/worktrees/{workflow_run_id}/dev"
      clean_required: true
      preserve_after_done: true
    skills:
      - codex
      - kanban-codex-lane
      - test-driven-development
    body: |
      Implement exactly one architect plan item: {plan_item_id}.
      Keep scope within the plan item's 1K-ish budget.
      Do not commit.
      Write handoff to handoffs/{plan_item_id}/dev.json.

  - id: automated_check
    title: "Check plan item {plan_item_id}"
    assignee: reviewer
    mode: dynamic_serial_template
    expand_from: architect_plan.development_tasks
    parents: [dev_impl]
    workspace:
      type: scratch
      repo: "{repo}"
      preserve_after_done: false
    check_policy:
      pass_values: [PASS]
      fail_behavior:
        create_revise_task: true
        retry_until_pass: true
        max_revise_rounds: "{max_revise_rounds}"
        downstream_release: false
    body: |
      Run the plan item's check_command and acceptance_checks.
      Copy checks/{plan_item_id}/latest.json to the run_record dir.
      PASS releases commit_impl. FAIL creates revise_impl and blocks this check.

  - id: revise_impl
    title: "Revise plan item {plan_item_id}"
    assignee: dev-codex
    mode: dynamic_fix_template
    parents: [automated_check]
    workspace:
      type: worktree
      repo: "{repo}"
      base: "{base_branch}"
      branch: "{branch_prefix}/issue-{issue}"
      path: ".hermes/worktrees/{workflow_run_id}/dev"
      clean_required: true
      preserve_after_done: true
    body: |
      Fix only the failed checks for {plan_item_id}.
      Do not broaden scope.
      Do not commit.
      After completion, automated_check reruns.

  - id: commit_impl
    title: "Commit plan item {plan_item_id}"
    assignee: dev-codex
    mode: dynamic_serial_template
    expand_from: architect_plan.development_tasks
    parents: [automated_check]
    workspace:
      type: worktree
      repo: "{repo}"
      base: "{base_branch}"
      branch: "{branch_prefix}/issue-{issue}"
      path: ".hermes/worktrees/{workflow_run_id}/dev"
      clean_required: true
      preserve_after_done: true
    body: |
      Commit only if automated_check.check_result is PASS for {plan_item_id}.
      Create one commit for this plan item.
      Write commit metadata to handoffs/{plan_item_id}/commit.json.

  - id: pr
    title: "Create PR and check CI for issue #{issue}"
    assignee: shipper
    parents:
      - last: commit_impl
    workspace:
      type: dir
      repo: "{repo}"
      path: "{repo}/.hermes/workflow-runs/{workflow_run_id}"
      preserve_after_done: true
    skills:
      - github-pr-workflow
    body: |
      Push the verified branch and create a PR after all plan item commits are done.
      Check CI and write final.json.
      Do not merge automatically unless project policy explicitly enables it.
```

### 12.9 历史 v1.2 并行 lane 样例（废弃，不执行）

以下是 v1.2 的并行 lane 旧模板，仅保留为历史参考，不作为当前执行设计。主要变化：

- 不再使用 `dev-claude`。
- 使用同一个 `dev-codex` profile 启动两个逻辑 worker lane。
- `run_record` 使用 `dir` workspace 持久化每次需求任务。
- `lane_a_impl` / `lane_b_impl` / `integrate_impl` / `commit_impl` 使用 `worktree`。
- `automated_check` 先用 `scratch` 做临时执行，最终报告必须写回 `run_record_dir`。
- `automated_check` 只有 PASS 才能 complete；FAIL 时创建 `revise_impl` 并重新检查，直到 PASS 或超过 retry budget。
- 默认没有 `human_approval` 节点；人工只在 clarify 低置信度或风险策略命中时临时插入。

```yaml
id: dev-feature-v2
version: 1.2.0
description: Codex-only deterministic feature workflow with automated check/revise/commit loop.

inputs:
  repo:
    type: path
    required: true
    description: Local repo path.
  issue:
    type: string
    required: true
    description: GitHub issue number or issue URL.
  base_branch:
    type: string
    required: false
    default: main
  branch_prefix:
    type: string
    required: false
    default: ai/dev-feature
  board:
    type: string
    required: false
    default: default
  tenant:
    type: string
    required: false
    default: default
  check_command:
    type: string
    required: false
    default: "auto"
  max_revise_rounds:
    type: integer
    required: false
    default: 3

runtime:
  board: "{board}"
  tenant: "{tenant}"
  run_record_dir: "{repo}/.hermes/workflow-runs/{workflow_run_id}"
  dev_profile: dev-codex
  max_parallel_dev_workers: 2
  idempotency:
    strategy: source_workflow_version

entry:
  assignee: orchestrator
  skills:
    - workflow-orchestrator
    - kanban-orchestrator

nodes:
  - id: run_record
    title: "Persist workflow run record for issue #{issue}"
    assignee: orchestrator
    parents: []
    skills:
      - kanban-orchestrator
    workspace:
      type: dir
      repo: "{repo}"
      path: "{repo}/.hermes/workflow-runs/{workflow_run_id}"
      preserve_after_done: true
    body: |
      Create the durable run record directory.
      Persist:
      - requirement.md
      - source.json
      - topology.json
      - handoffs/
      - checks/
      - final.json
    output_contract:
      type: object
      required:
        - run_record_dir
        - requirement_snapshot
      properties:
        run_record_dir:
          type: string
        requirement_snapshot:
          type: string

  - id: clarify
    title: "Clarify issue #{issue}"
    assignee: orchestrator
    parents:
      - run_record
    skills:
      - kanban-orchestrator
    workspace:
      type: dir
      repo: "{repo}"
      path: "{repo}/.hermes/workflow-runs/{workflow_run_id}"
      preserve_after_done: true
    body: |
      Clarify the goal, non-goals, acceptance criteria, risks and task split for issue #{issue}.
      Repo: {repo}

      Default behavior:
      - If requirements are clear enough, do not ask the human.
      - If clarify_confidence < 0.75 or acceptance criteria conflict, block this task and ask one concise question.
      - Write clarification.md to the run_record dir.

      Output must include:
      - goal
      - non_goals
      - acceptance_criteria
      - risk_points
      - lane_a_scope
      - lane_b_scope
      - clarify_confidence
      - human_needed
    output_contract:
      type: object
      required:
        - goal
        - non_goals
        - acceptance_criteria
        - risk_points
        - lane_a_scope
        - lane_b_scope
        - clarify_confidence
        - human_needed
      properties:
        goal:
          type: string
        non_goals:
          type: array
          items:
            type: string
        acceptance_criteria:
          type: array
          items:
            type: string
        risk_points:
          type: array
          items:
            type: string
        lane_a_scope:
          type: array
          items:
            type: string
        lane_b_scope:
          type: array
          items:
            type: string
        clarify_confidence:
          type: number
        human_needed:
          type: boolean

  - id: lane_a_impl
    title: "Codex lane A implementation for issue #{issue}"
    assignee: dev-codex
    parents:
      - clarify
    worker_lane: lane-a
    workspace:
      type: worktree
      repo: "{repo}"
      base: "{base_branch}"
      branch: "{branch_prefix}/issue-{issue}/lane-a"
      path: ".hermes/worktrees/{workflow_run_id}/lane-a"
      clean_required: true
      preserve_after_done: true
    skills:
      - codex
      - kanban-codex-lane
      - test-driven-development
    body: |
      Implement lane_a_scope from clarify.
      Work only in the assigned worktree and branch.
      Write durable notes to {repo}/.hermes/workflow-runs/{workflow_run_id}/handoffs/lane-a.json.

      Completion requires:
      - implementation_branch
      - changed_files
      - verification
      - residual_risk
    output_contract:
      type: object
      required:
        - implementation_branch
        - changed_files
        - verification
        - residual_risk
      properties:
        implementation_branch:
          type: string
        changed_files:
          type: array
          items:
            type: string
        verification:
          type: array
          items:
            type: string
        residual_risk:
          type: array
          items:
            type: string
    task_size_limit:
      changed_lines_hint: 1000

  - id: lane_b_impl
    title: "Codex lane B implementation for issue #{issue}"
    assignee: dev-codex
    parents:
      - clarify
    worker_lane: lane-b
    workspace:
      type: worktree
      repo: "{repo}"
      base: "{base_branch}"
      branch: "{branch_prefix}/issue-{issue}/lane-b"
      path: ".hermes/worktrees/{workflow_run_id}/lane-b"
      clean_required: true
      preserve_after_done: true
    skills:
      - codex
      - kanban-codex-lane
      - test-driven-development
    body: |
      Implement lane_b_scope from clarify.
      Work only in the assigned worktree and branch.
      Write durable notes to {repo}/.hermes/workflow-runs/{workflow_run_id}/handoffs/lane-b.json.

      Completion requires:
      - implementation_branch
      - changed_files
      - verification
      - residual_risk
    output_contract:
      type: object
      required:
        - implementation_branch
        - changed_files
        - verification
        - residual_risk
      properties:
        implementation_branch:
          type: string
        changed_files:
          type: array
          items:
            type: string
        verification:
          type: array
          items:
            type: string
        residual_risk:
          type: array
          items:
            type: string
    task_size_limit:
      changed_lines_hint: 1000

  - id: integrate_impl
    title: "Integrate Codex lanes for issue #{issue}"
    assignee: dev-codex
    parents:
      - lane_a_impl
      - lane_b_impl
    workspace:
      type: worktree
      repo: "{repo}"
      base: "{base_branch}"
      branch: "{branch_prefix}/issue-{issue}"
      path: ".hermes/worktrees/{workflow_run_id}/integrate_impl"
      clean_required: true
      preserve_after_done: true
    skills:
      - codex
      - kanban-codex-lane
      - test-driven-development
      - requesting-code-review
    body: |
      Read lane A and lane B handoffs.
      Merge or cherry-pick both implementation branches into:
      {branch_prefix}/issue-{issue}

      Resolve conflicts, run relevant tests, and produce a unified diff.
      Write integration summary to {repo}/.hermes/workflow-runs/{workflow_run_id}/handoffs/integrate.json.

      Completion requires:
      - integration_branch
      - merged_branches
      - conflict_resolution
      - verification
      - residual_risk
    output_contract:
      type: object
      required:
        - integration_branch
        - merged_branches
        - verification
        - residual_risk
      properties:
        integration_branch:
          type: string
        merged_branches:
          type: array
          items:
            type: string
        conflict_resolution:
          type: array
          items:
            type: string
        verification:
          type: array
          items:
            type: string
        residual_risk:
          type: array
          items:
            type: string

  - id: automated_check
    title: "Automated check for issue #{issue}"
    assignee: reviewer
    parents:
      - integrate_impl
    workspace:
      type: scratch
      repo: "{repo}"
      preserve_after_done: false
    skills:
      - requesting-code-review
    check_policy:
      pass_values:
        - PASS
      fail_behavior:
        action: block_current_task
        create_revise_task: true
        retry_until_pass: true
        max_revise_rounds: "{max_revise_rounds}"
        downstream_release: false
    body: |
      Check the integration branch from integrate_impl.

      Check:
      - acceptance criteria from clarify
      - unified integration branch diff
      - test/lint/typecheck/build command: {check_command}
      - verification evidence
      - protected path changes
      - residual risks

      Decide:
      - PASS
      - FAIL

      If PASS:
      - write checks/latest.json in the run_record dir.
      - complete this task with structured metadata.

      If FAIL:
      - do NOT complete this task.
      - create a revise_impl task assigned to dev-codex.
      - link revise_impl back to this automated_check task before unblocking it.
      - block this task with exact failure reasons.
      - keep commit_impl unreleased.
    output_contract:
      type: object
      required:
        - check_result
        - failed_checks
        - verification
        - residual_risk
      properties:
        check_result:
          type: string
          enum:
            - PASS
            - FAIL
        failed_checks:
          type: array
          items:
            type: string
        verification:
          type: array
          items:
            type: string
        residual_risk:
          type: array
          items:
            type: string

  - id: commit_impl
    title: "Commit verified implementation for issue #{issue}"
    assignee: dev-codex
    parents:
      - automated_check
    workspace:
      type: worktree
      repo: "{repo}"
      base: "{base_branch}"
      branch: "{branch_prefix}/issue-{issue}"
      path: ".hermes/worktrees/{workflow_run_id}/commit"
      clean_required: true
      preserve_after_done: true
    skills:
      - codex
      - kanban-codex-lane
    body: |
      Commit only if automated_check.check_result is PASS.
      Use integration branch: {branch_prefix}/issue-{issue}.
      Create exactly one commit for this workflow run unless project policy requires split commits.
      Write commit metadata to {repo}/.hermes/workflow-runs/{workflow_run_id}/final.json.

      Completion requires:
      - branch
      - commit_sha
      - commit_message
      - verification
    output_contract:
      type: object
      required:
        - branch
        - commit_sha
        - commit_message
        - verification
      properties:
        branch:
          type: string
        commit_sha:
          type: string
        commit_message:
          type: string
        verification:
          type: array
          items:
            type: string

  - id: pr
    title: "Create PR and check CI for issue #{issue}"
    assignee: shipper
    parents:
      - commit_impl
    workspace:
      type: dir
      repo: "{repo}"
      path: "{repo}/.hermes/workflow-runs/{workflow_run_id}"
      preserve_after_done: true
    skills:
      - github-pr-workflow
    body: |
      Push the verified branch and create a GitHub PR.
      Check CI status and write PR metadata to final.json.
      Do not merge automatically unless project policy explicitly enables it.

      Completion requires:
      - branch
      - commit_sha
      - pr_url
      - ci_status
      - merge_recommendation
```

## 13. Root Task 设计

### 13.1 Root Task Body

```yaml
workflow_id: dev-feature-v3
workflow_version: 1.3.0
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
title: "[WF] dev-feature-v3: issue #123"
assignee: orchestrator
skills:
  - workflow-orchestrator
  - kanban-orchestrator
idempotency_key: "github:tim/edn-agent:issue:123:workflow:dev-feature-v3"
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

### 14.3 Automated Check metadata

```json
{
  "check_result": "PASS",
  "failed_checks": [],
  "verification": [
    "npm test",
    "npm run lint"
  ],
  "residual_risk": [
    "OAuth timeout scenario not covered"
  ]
}
```

### 14.4 Commit metadata

```json
{
  "branch": "ai/dev-feature/issue-123",
  "commit_sha": "abc1234",
  "commit_message": "Implement issue 123 workflow",
  "verification": [
    "automated_check PASS"
  ]
}
```

### 14.5 PR metadata

```json
{
  "branch": "ai/dev-feature/issue-123",
  "commit_sha": "abc1234",
  "pr_url": "https://github.com/org/repo/pull/456",
  "ci_status": "passing",
  "merge_recommendation": "ready"
}
```

---

## 15. Instantiator 设计

### 15.1 职责

```text
输入：workflow_id + inputs
输出：Kanban DAG + workflow_run mapping
```

Instantiator 只负责确定性创建 DAG，不执行 worker，不维护 workflow 运行状态。

允许 Instantiator 维护“实例化事务状态”：

```text
creating / created / failed_partial
```

这只是 DAG 创建事务状态，不是 workflow runtime status。真实运行状态仍由 Kanban task status 聚合。

### 15.2 必须校验

```text
workflow_id 是否存在
workflow version 是否支持
required inputs 是否完整
node id 是否唯一
parents 是否存在
DAG 是否无环
profile registry 是否存在
assignee profile 是否存在或 fallback 可用
skills 是否存在
workspace object 是否合法
workspace repo/path 是否安全
branch name 是否安全
manual_gate 是否有明确处理方式
output_contract 是否是合法 JSON Schema 子集
```

### 15.3 Idempotency Key

Root run key：

```text
{source.type}:{source.repo}:{source.issue}:workflow:{workflow_id}:version:{workflow_version}
```

Node task key：

```text
{root_idempotency_key}:node:{node_id}
```

示例：

```text
github_issue:tim/edn-agent:123:workflow:dev-feature-v3:version:1.3.0:node:clarify
```

### 15.4 Transaction 流程

```text
1. acquire idempotency lock
2. if run exists and instantiation_status=created:
     return existing workflow_run_id / root_task_id
3. if run exists and instantiation_status=creating and lock not expired:
     return conflict / retry later
4. if run exists and instantiation_status in failed_partial or expired creating:
     resume partial creation
5. discover profiles with `hermes profile list`
6. validate / resolve all assignees through Profile Registry
7. create or reuse root task by root idempotency key
8. for each node in topological order:
     create or reuse task by node idempotency key
     parentless nodes depend on root_task_id
     repair parent links if missing
9. write node_to_task_id mapping
10. mark instantiation_status=created
11. complete root task with mapping metadata
12. release lock
```

### 15.5 伪代码

```python
def instantiate_workflow(workflow_id: str, inputs: dict, source: dict) -> dict:
    workflow = load_yaml(f"workflows/{workflow_id}.yaml")
    validate_schema(workflow)
    validate_inputs(workflow["inputs"], inputs)
    validate_dag(workflow["nodes"])

    available_profiles = discover_profiles()  # hermes profile list
    profile_registry = load_profile_registry()
    assignee_map = resolve_assignees(workflow["nodes"], profile_registry, available_profiles)

    root_idempotency_key = make_root_idempotency_key(workflow, inputs, source)

    with acquire_lock(root_idempotency_key):
        existing = load_workflow_run_by_idempotency(root_idempotency_key)
        if existing and existing["instantiation_status"] == "created":
            return existing

        workflow_run_id = existing["workflow_run_id"] if existing else create_workflow_run_id()
        mark_instantiation_status(workflow_run_id, "creating")

        context = {
            **inputs,
            "workflow_run_id": workflow_run_id,
            "workflow_id": workflow_id,
            "workflow_version": workflow["version"],
        }

        root_task_id = create_or_reuse_kanban_task(
            title=f"[WF] {workflow_id}: {inputs}",
            assignee=assignee_map[workflow["entry"]["assignee"]],
            skills=workflow["entry"].get("skills", []),
            body=render_root_body(workflow_id, workflow["version"], inputs, source),
            idempotency_key=root_idempotency_key,
            metadata={
                "workflow_run_id": workflow_run_id,
                "workflow_id": workflow_id,
                "workflow_version": workflow["version"],
            },
        )

        node_to_task_id = {}
        for node in topological_sort(workflow["nodes"]):
            declared_parents = node.get("parents", [])
            if declared_parents:
                parent_task_ids = [node_to_task_id[p] for p in declared_parents]
            else:
                parent_task_ids = [root_task_id]

            context["node_id"] = node["id"]
            task_id = create_or_reuse_kanban_task(
                title=render(node["title"], context),
                assignee=assignee_map[node["assignee"]],
                parents=parent_task_ids,
                skills=node.get("skills", []),
                workspace=render_workspace(node.get("workspace"), context),
                body=render(node["body"], context),
                idempotency_key=make_node_idempotency_key(root_idempotency_key, node["id"]),
                metadata={
                    "workflow_run_id": workflow_run_id,
                    "workflow_id": workflow_id,
                    "workflow_version": workflow["version"],
                    "node_id": node["id"],
                    "workflow_assignee": node["assignee"],
                    "resolved_assignee": assignee_map[node["assignee"]],
                    "output_contract": node.get("output_contract", {}),
                    "mode": node.get("mode", "auto"),
                    "manual_gate": node.get("manual_gate"),
                    "review_policy": node.get("review_policy"),
                },
            )
            repair_parent_links(task_id, parent_task_ids)
            node_to_task_id[node["id"]] = task_id

        save_workflow_run_index(
            workflow_run_id=workflow_run_id,
            workflow_id=workflow_id,
            workflow_version=workflow["version"],
            root_task_id=root_task_id,
            node_to_task_id=node_to_task_id,
            inputs=inputs,
            idempotency_key=root_idempotency_key,
            instantiation_status="created",
        )

        complete_kanban_task(
            root_task_id,
            summary="Workflow DAG instantiated successfully.",
            metadata={
                "workflow_run_id": workflow_run_id,
                "node_to_task_id": node_to_task_id,
            },
        )

        return {
            "workflow_run_id": workflow_run_id,
            "root_task_id": root_task_id,
            "node_to_task_id": node_to_task_id,
        }
```

### 15.6 Partial failure resume

如果 Instantiator 创建到一半失败，下一次重试必须：

```text
- 根据 root idempotency key 找到已有 root task
- 根据 per-node idempotency key 找到已创建 node task
- 补创建缺失 task
- 补修 parent links
- 重新写入 workflow_run_index
- 不重复创建已有 task
```

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
github_issue:tim/edn-agent:123:workflow:dev-feature-v3:version:1.3.0
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
3. Router 识别 workflow_id = dev-feature-v3
4. Router 生成 idempotency_key
5. Instantiator 读取 dev-feature-v3.yaml
6. Instantiator 创建 Kanban DAG
7. Hermes dispatcher 拉起 orchestrator / architect / dev-codex / reviewer / shipper
8. 每个 worker 用 kanban_show 读取任务和父任务 handoff
9. 每个 worker 用 kanban_complete 写 summary + metadata
10. automated_check PASS 后 commit_impl 提交，再由 shipper 创建 PR
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
        └── dev-feature-v3.yaml
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
hermes kanban create "[WF] dev-feature-v3: issue #123" \
  --assignee orchestrator \
  --skill workflow-orchestrator \
  --skill kanban-orchestrator \
  --idempotency-key "github:edn-agent:123:dev-feature-v3" \
  --body "
workflow_id: dev-feature-v3
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
  --workflow dev-feature-v3 \
  --repo /home/tim/projects/edn-agent \
  --issue 123 \
  --board edn-agent
```

或：

```http
POST /workflows/dev-feature-v3/runs
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

可选做一个轻量索引文件或 SQLite 表。

注意：这个 index 只做映射和实例化事务记录，不保存真实运行状态。真实状态必须从 Kanban task status 聚合。

```json
{
  "workflow_run_id": "wf_20260527_001",
  "workflow_id": "dev-feature-v3",
  "workflow_version": "1.3.0",
  "board": "edn-agent",
  "tenant": "github:tim/edn-agent",
  "root_task_id": "t_root",
  "node_to_task_id": {
    "run_record": "t_001",
    "clarify": "t_002",
    "architect_plan": "t_003",
    "dev_impl:plan_item_001": "t_004",
    "automated_check:plan_item_001": "t_005",
    "commit_impl:plan_item_001": "t_006",
    "pr": "t_007"
  },
  "inputs": {
    "repo": "/home/tim/projects/edn-agent",
    "issue": 123,
    "base_branch": "main",
    "branch_prefix": "ai/dev-feature"
  },
  "idempotency_key": "github_issue:tim/edn-agent:123:workflow:dev-feature-v3:version:1.3.0",
  "instantiation_status": "created",
  "created_at": "2026-05-27T00:00:00Z",
  "updated_at": "2026-05-27T00:00:05Z"
}
```

允许的 `instantiation_status`：

```text
creating
created
failed_partial
```

`instantiation_status` 只描述 DAG 创建事务，不描述 workflow 是否 running / done / blocked。

聚合 workflow run 运行状态时，必须实时读取 Kanban task status。

## 21. 安全与质量策略

### 21.1 Workspace 隔离与持久化

默认所有需求 run 必须同时使用三类 workspace：

```text
dir       保存需求快照、澄清记录、检查报告、最终 commit/PR 索引
scratch   临时分析和检查执行缓存
worktree  所有代码写入、修订、集成和提交
```

默认所有 coding task 使用对象化 worktree：

```yaml
workspace:
  type: worktree
  repo: "{repo}"
  base: "{base_branch}"
  branch: "{branch_prefix}/issue-{issue}/{node_id}"
  path: ".hermes/worktrees/{workflow_run_id}/{node_id}"
  clean_required: true
  preserve_after_done: true
```

v1.3 先不做并行开发。`dev_impl`、`revise_impl`、`commit_impl` 复用同一个 feature branch 和 worktree，按 `plan_item_id` 串行推进：

```text
dev_impl:{plan_item_001} → automated_check:{plan_item_001} → commit_impl:{plan_item_001}
dev_impl:{plan_item_002} → automated_check:{plan_item_002} → commit_impl:{plan_item_002}
```

统一提交分支由第一个 `dev_impl` 从 base branch 创建，后续计划项沿同一分支连续提交：

```text
{branch_prefix}/issue-{issue}
```

### 21.2 Architect Gate

所有开发任务必须先经过 `architect_plan`：

```text
clarify → architect_plan → serial dev_impl/check/revise/commit tasks → pr
```

architect 必须把工作拆成约 1K 代码影响范围的计划项。dev-codex 每次只领取一个计划项。

### 21.3 禁止危险参数默认开启

默认禁止：

```text
codex --yolo
无人值守 auto merge
gh pr merge
gh pr merge --auto
```

### 21.4 任务规模限制

原则：

```text
单个开发子任务默认控制在约 1000 行影响范围。
超过则由 architect 拆分为多个串行 plan item。
```

### 21.5 自动检查与修订闭环

所有实现任务必须经过 `automated_check`：

```text
dev task done ≠ feature accepted
architect_plan done ≠ feature implemented
automated_check PASS 才能进入 commit_impl
```

如果 `automated_check` 判定 `FAIL`：

```text
reviewer 不得 complete automated_check task
reviewer 必须 block automated_check task
reviewer 应创建 revise_impl task，分配给 dev-codex
revise_impl 完成后 automated_check 自动重跑
commit_impl 不得 ready
超过 max_revise_rounds 后才升级人工
```

### 21.6 Commit Gate

代码提交必须在检查通过之后：

```text
automated_check PASS
  → commit_impl
  → git commit
  → pr
```

`commit_impl` 必须记录 `commit_sha`、`commit_message`、`branch` 和 `verification`。不得让实现 worker 在 lane 内提前提交。

### 21.7 Human Gate

默认没有最终人工批准节点。人工介入只允许：

```text
需求澄清低置信度
protected_paths 命中
连续自动修订失败
外部凭证/权限缺失
项目策略明确要求最终人工确认
```

`manual_gate` 不假设存在 `human` profile。它只由 Router 在风险例外时动态创建，或由 task block/comment 承载。

### 21.8 Secret / Credential 保护

coding worker 默认不得修改：

```text
.env
.env.*
secrets
credential stores
production config
payment/order systems
```

推荐在 workflow 或 profile registry 中配置：

```yaml
guardrails:
  protected_paths:
    - ".env"
    - ".env.*"
    - "**/secrets/**"
    - "**/credentials/**"
  require_human_gate_for_paths:
    - "infra/prod/**"
    - "payments/**"
    - "orders/**"
```

如必须修改，需人工 gate。

### 21.9 Profile validation

Instantiator 创建任务前必须校验 assignee profile 存在。v1.3 必须存在 `orchestrator`、`architect`、`dev-codex`、`reviewer`、`shipper`。不得 fallback 到 `dev-claude`。

### 21.10 Output Contract validation

V1：worker 自查，reviewer 检查 parent handoff。  
V2：completion validator 校验 JSON Schema 子集，缺失 required 字段时拒绝 complete 或 block。

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
创建 run_record / clarify / architect_plan / dev_impl / automated_check / commit_impl / pr tasks
根据 architect_plan.development_tasks 动态创建串行 plan item 子图
parentless run_record 依赖 root_task
parents 正确
assignee 正确且经过 profile registry 解析
skills 正确
workspace branch/path 正确
run_record 使用 dir workspace
architect_plan 使用 dir workspace
dev_impl/revise_impl/commit_impl 使用 worktree workspace
automated_check 使用 scratch 并把最终报告写回 dir
```

### 22.2 Parent Gate 测试

期望：

```text
run_record 在 root done 前保持 todo。
clarify 在 run_record done 前保持 todo。
architect_plan 在 clarify done 前保持 todo。
dev_impl:{plan_item_001} 在 architect_plan done 前保持 todo。
dev_impl 同一时间最多一个 running。
automated_check:{plan_item_id} 在对应 dev_impl done 前保持 todo。
commit_impl:{plan_item_id} 在对应 automated_check PASS/done 前保持 todo。
下一 plan item 的 dev_impl 在上一 plan item commit_impl done 前保持 todo。
pr 在最后一个 commit_impl done 前保持 todo。
```

### 22.3 Architect Plan 测试

期望：

```text
architect_plan 输出 architecture_plan.md。
architect_plan 输出 development_plan.json。
development_tasks 每项包含 id/title/scope/files_expected/check_command/acceptance_checks/risk_notes。
每个 development_task 估算代码影响范围约 1000 行以内。
超过 1000 行的任务必须继续拆分。
```

### 22.4 Handoff 测试

期望：

```text
reviewer 启动后能看到对应 dev_impl 的 metadata 和 plan_item_id。
dev-codex commit_impl 启动后能看到 automated_check 的 check_result=PASS。
shipper 启动后能看到 commit_sha、branch、verification。
run_record dir 能看到 requirement、architecture_plan、development_plan、handoffs、checks、final 索引。
```

### 22.5 Idempotency 测试

同一个 GitHub issue 重复触发：

```text
不重复创建 DAG。
返回已有 workflow_run_id。
per-node idempotency key 不重复创建节点 task。
```

### 22.6 Partial Failure / Resume 测试

模拟 Instantiator 创建到 `architect_plan` 或第一个 `dev_impl` 后崩溃。

期望：

```text
workflow_run_index 标记 failed_partial 或 creating expired。
重试时复用已有 root / run_record / clarify / architect_plan。
如果 development_plan.json 已存在，则按 plan 补创建缺失的串行 dev_impl / automated_check / commit_impl / pr。
补修 parent links。
不重复创建已有 task。
最终 instantiation_status=created。
```

### 22.7 Missing Profile 测试

当 required profile 不存在：

```text
workflow 不创建子任务或 root task blocked。
blocked_reason 说明缺失 profile。
不得 fallback 到 dev-claude。
```

### 22.8 Automated Check FAIL 测试

当 reviewer/checker 判定 FAIL：

```text
automated_check task blocked，而不是 done。
reviewer 创建 revise_impl task，assignee=dev-codex。
revise_impl done 后 automated_check 被 unblock/rerun。
commit_impl 保持 todo，不进入 ready。
超过 max_revise_rounds 后才升级人工。
```

### 22.9 Failure / Block 测试

当缺少 repo 参数：

```text
workflow 不创建子任务。
root task blocked。
blocked_reason 说明缺少 repo。
```

当 CI 失败：

```text
shipper task blocked 或创建 follow-up fix task。
不执行 merge。
```

### 22.10 低人工介入测试

期望：

```text
需求清晰时不会创建 manual_gate。
clarify_confidence 低于阈值时 clarify block 并只问一个问题。
protected_paths 命中时 Router 动态创建 manual_gate 或 block 当前任务。
clarify 之后默认自动执行到 commit/pr，不要求人工批准。
```

## 23. 部署执行清单

### Phase 0：前置检查

```bash
hermes --version
hermes kanban init
hermes gateway start
hermes skills list
hermes bundles list
```

建议配置：

```yaml
kanban:
  max_in_progress_per_profile: 1
```

确认存在以下 bundled skills：

```text
kanban-orchestrator
kanban-worker
codex
kanban-codex-lane
github-pr-workflow
requesting-code-review
test-driven-development
```

确认存在以下 profiles：

```text
orchestrator
architect
dev-codex
reviewer
shipper
```

确认存在以下 skill bundles：

```text
ai-architect
ai-dev-codex
ai-reviewer
ai-shipper
```

### Phase 1：创建目录

```bash
mkdir -p ~/.hermes/workflows
mkdir -p ~/.hermes/skill-bundles
mkdir -p ~/.hermes/skills/workflow-orchestrator/references/workflows
```

### Phase 2：安装 V1.3 模板

```bash
cp workflows/dev-feature-v3.yaml ~/.hermes/workflows/
cp workflows/dev-feature-v3.yaml ~/.hermes/skills/workflow-orchestrator/references/workflows/
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
hermes kanban create "[WF] dev-feature-v3: smoke test" \
  --assignee orchestrator \
  --skill workflow-orchestrator \
  --skill kanban-orchestrator \
  --body "
workflow_id: dev-feature-v3
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
curl -X POST http://localhost:8787/workflows/dev-feature-v3/runs \
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
4. 先完成 V1.3：workflow-orchestrator skill + dev-feature-v3.yaml。
5. 验证 root task 能展开为 Kanban DAG，且 parentless nodes 依赖 root task。
6. 验证 parent-child dependency 正常推进。
7. 验证 worker 能读取 parent handoff。
8. 增加 architect_plan，确保需求先拆成约 1K 代码影响范围的开发项。
9. manual_gate 不要放在默认主路径，只在 clarify 低置信度或风险例外时动态创建。
10. Instantiator 必须做 profile discovery / assignee validation。
11. automated_check FAIL 时 block check task，不得 complete 释放 commit_impl。
12. output_contract 使用 JSON Schema 子集。
13. Instantiator 必须支持 per-node idempotency 和 partial failure resume。
14. workspace 必须使用对象形式描述 repo/base/branch/path，并强制使用 dir/scratch/worktree 三类 workspace。
15. 再实现 V2：Trigger Router + Deterministic Instantiator。
16. 为 GitHub label / Feishu / CLI 增加入口适配。
17. 所有 coding worker 必须使用 worktree。
18. 所有实现任务必须经过 automated_check。
19. automated_check PASS 后才能 commit，commit 后才能 PR。
```

禁止事项：

```text
不要让 dev worker 直接 merge。
不要让 Codex 自报完成后直接关闭整体 workflow。
不要复制一套独立状态机。
不要默认使用危险权限绕过参数。
不要把大任务一次性塞给单个 worker。
不要在 dev_impl / revise_impl 中提前 git commit。
```

---

## 25. 推荐路线图

### V1：可用闭环

目标：1 天内跑通。

```text
workflow-orchestrator skill
+ dev-feature-v3.yaml
+ root task
+ Kanban DAG
+ architect/dev-codex/reviewer/shipper profiles
```

验收：

```text
一个 issue 能自动展开成 root + run_record / clarify / architect_plan / dev_impl / automated_check / commit_impl / pr tasks。
父子依赖正确。
下游能看到上游 metadata。
每次需求有持久 dir 记录。
检查失败会自动生成 revise_impl 并重跑 automated_check。
检查通过后自动提交 commit。
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
可通过在hermes的dashboard中的kanban中，在triage中增加一条冒泡排序的需求，会自动扩展需求并完成开发
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
