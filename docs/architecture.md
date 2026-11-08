# 架构与状态机

> README 的「架构与数据流」小节给的是**一张图**；这份文档给的是**图的细节**：
> 模块怎么分层、状态怎么迁移、审计链怎么串起来。
> 口径与指标的展开在 [`metrics.md`](metrics.md)，产物样例在 [`examples.md`](examples.md)。

---

## 一、模块分层

```
src/nodule_eval/
├── cli.py            命令行入口（运维与排障通道）：run / assemble / submit / inspect
│                     / replay / doctor / export / fill / redact-scan
├── contracts.py      三入口共用契约：请求 / 响应 dataclass、列契约、指标分母口径
├── config.py         环境变量配置面（缺凭据指名报错，不回落替身）
├── errors.py         统一错误码表（中文文案 + 可重试标记 + 是否进分母）
├── error_matrix.py   错误码 → 落地矩阵（生成 docs/error_matrix.md）
├── engine.py         逐例 fan-out 调度、并发限流、断点续跑、失败隔离
├── store.py          SQLite run store + run/case 双状态机
├── caseset.py        病例集装配、三键绑定、一对多歧义拒绝
├── catalog.py        被评模型目录装载与响应字段映射
├── gateway.py        单一 ModelGateway（OpenAI-compatible 协议）
├── structured.py     结构化输入输出契约与校验
├── limits.py         提交体上限等硬约束
├── export.py         结论表 / 漏诊清单导出
├── doctor.py         自检（零出站）
├── writeback.py      人工确认闸门、幂等写回、提交体组装
├── writeback_receipt.py 写回回执台账
├── fixture_gate.py   替身门（默认关闭，只认 --fixture / IMAGING_EVAL_FIXTURE=1）
├── adapters/         出站层：http / ris / inference / research_platform / vendor_error
├── datasets/         公开数据集取数与校验（jsrt）
├── goldstd/          金标准归并：labels 等价表 / rules 规则基线 / merge 语义归并
│                     / prompt 提示词版本化 / schema / benchmark / regression
├── metrics/          judge 判定 / attributes 属性三层 / strict 多标签最严
│                     / compute 指标 / report 漏诊清单与结论
├── audit/            toolcall 底账 / modelcall 元数据 / state_transition 迁移留痕
│                     / redact 脱敏扫描 / inspect 反查 / case_artifact 单例归档
│                     / version 版本台账
└── sheets/           表格层：columns 列契约 / template 模板 / read 读表
                      / fill 回填 / status 运行状态 / tokens 视觉 token / sections 版块
                      / macro_contract 宏↔引擎契约 / xlsm_vba / wps_jsa
```

**分层原则**：出站只有 `adapters/` 一层，界面上只暴露 `contracts.py` 里的字段；
业务代码里不出现厂商专有字段名（差异收在 `models/<model_id>.yaml` 的映射表），
也不出现任何 URL、令牌、状态码字面量。

---

## 二、run / case 双状态机

状态迁移表写死在 `src/nodule_eval/store.py` 的 `RUN_TRANSITIONS` / `CASE_TRANSITIONS`。

### run 状态

```
draft ──▶ assembled ──▶ running ──▶ called ──▶ evaluated ──▶ reviewed ──▶ submitted
  │           │            │           │           │            │
  └───────────┴───────────┴───────────┴───────────┴────────────┴──▶ failed
```

| 状态 | 含义 | 谁能推进到它 |
|------|------|--------------|
| `draft` | 已建 run，病例集还没装配 | 引擎（`actor=engine`） |
| `assembled` | 病例集已装配，绑定关系已落 `binding.json` | 引擎 |
| `running` | 逐例调用中 | 引擎 |
| `called` | 逐例调用完成，等归并与判定 | 引擎 |
| `evaluated` | 判定与指标已出，结论停在草稿层 | 引擎 |
| `reviewed` | **人工复核通过** | **只认 `actor=human-review`** |
| `submitted` | **人工确认提交，已写回科研平台** | **只认 `actor=human-submit`** |
| `failed` | 整轮致命错误 | 引擎 |

- `reviewed` 之后**只能提交，不能退回** —— 人工确认过的东西不该被机器推翻；
  `submitted` 没有后继边。
- `called → running` / `evaluated → running` 是**同一轮重跑**（复位重跑）；
  `failed → running` 是失败后重来。
- **引擎自推 `reviewed` / `submitted` 一律被拒**，错误码 `INVALID_STATE_TRANSITION`，
  文案写明「只能由人工复核 / 确认提交触发（当前触发方：engine）」。
- **被拒的迁移不留痕**：迁移日志只记成功的。

### case 状态

```
pending ──▶ called ──▶ normalized ──▶ judged
   │           │            │            │
   └───────────┴────────────┴────────────┴──▶ failed / needs_review
```

- `judged` / `failed` 要重判，**必须先复位到 `pending`** 重新走一遍 —— 不允许直接改判。
- `needs_review`（人工澄清绑定歧义后）可以回到 `pending` 或直接 `called`。
- 逐例状态的**唯一推出口径**是 `contracts.py` 的 `case_status_for_row`，
  引擎、store、汇总行都从这一处取，**不各写一份**。

### 迁移留痕

每次成功迁移落两处：SQLite 的 `state_transition` 表 + `runs/<run_id>/transitions.jsonl`，
字段为 `scope` / `run_id` / `subject` / `from` / `to` / `actor` / `reason` / `at`。
飞检问「这例凭什么算漏判」「这一轮是谁批的」，看这一本账就够。

---

## 三、三级审计链

```
结论  case_rows.csv / summary.json
  │  tool_call_id
  ▼
工具调用  tool_calls.jsonl      ← 推理端点的入参与返回（失败例也留结构化记录）
  │  tool_call_id
  ▼
模型调用  model_calls.jsonl      ← 模型端点的调用元数据（只记哈希、不记原文）
```

| 账本 | 记什么 | 不记什么 |
|------|--------|----------|
| `tool_calls.jsonl` | 每次出站推理的 `args`（含 `accession_no` / `study_uid` / `image_ref` / `model_id` / `model_version`）、厂商返回原文、失败时的 `error_code` / `http_status` | 不记凭据 |
| `model_calls.jsonl` | `model` / `base_url_host` / `latency_ms` / `token 数` / `prompt_sha256` / `raw_response_sha256` / `prompt_version` / `fallback_used` / `attempts` | **不记提示词原文、不记模型返回原文**（只留哈希） |
| `case.json`（单例归档） | 归并后的标准标签、判定依据、逐层结论、覆盖不了的病灶 | **不记金标准原文**（只留 `gold_standard_sha256`） |

**三级链路一次走通**：`inspect --tool-call-id <ID>` 从结论一路 join 回那次推理的入参与返回。

---

## 四、并发、幂等与重放

| 机制 | 落点 | 语义 |
|------|------|------|
| 并发限流 | `engine.py` 的 `concurrency` + `endpoint_limits` | 逐端点独立在途上限，超限**排队不丢弃**；端点名写错当场报错 |
| 幂等键 | `writeback.py` | `sha256(run_id|model_id|dataset_ref)`，同一轮重复提交对端只产生一条登记 |
| 主键覆盖 | `store.py` 的 `case_row` 表 | 同一 `(run_id, accession_no)` 重复写按主键覆盖，不追加重复行；同一 `tool_call_id` 不重复登记 |
| 断点续跑 | `engine.py` 的 `--resume` | 从 SQLite 读已完成 case 跳过；中断点的例复位到 `pending` 重跑，不产生重复 `tool_call` |
| 版本台账 | `audit/version.py` | 同 `run_id` 重跑产生 `versions/version-XXXX/`，上一版一字不动，可两版比对 |
| 事务边界 | `store.py` | WAL + `BEGIN IMMEDIATE` + `busy_timeout`，锁冲突指数退避重试；不出现半行状态 |

---

## 五、失败与降级路径

| 位置 | 失败 | 降级 / 处置 |
|------|------|------------|
| 出站 HTTP | 4xx / 5xx / 429 / 超时 / 连接被拒 | 4xx 不重试；5xx 与 429 指数退避最多 3 次；超时区分连接阶段与响应阶段 |
| 响应校验 | `study_uid` 不回显或回传另一个检查 | 判 `STUDY_UID_MISMATCH`，该例判 failed，不当阴性 |
| 响应校验 | 缺字段 / `candidates` 为 null / 候选缺字段 | 判 `FIELD_MAPPING_MISSING`，写明缺哪一个，**不返回空候选冒充阴性** |
| 绑定 | 一对多 / RIS 查不到 / 表内检查号重复 | 判 `AMBIGUOUS_BINDING`，挂起为 `needs_review`，不进指标分母 |
| 归并 | 模型端点不可达 / 超时 / 不可解析 | 回退规则基线，该例标 `rule_degraded`，**仍进分母**，汇总行单列降级数 |
| 引擎 | 单例失败 | 该例判 failed，**其余例继续**，run 最终状态是 `evaluated` 而不是 `failed` |
| 引擎 | run 级致命错误 | 整轮判 `failed`，不留半截跑完了的产物，迁移底账记「整轮失败」 |
| 写回 | 5xx | 退避重试；仍失败则**状态不推进**、不标记已提交，错误码与状态码留痕 |
| 写回 | 提交体超上限 | 提交被拦下，**一个请求都没发**；报错写明实际字节与上限，逐例明细一条不截断 |

**一条红线**：失败例判 `failed`、**永不进指标分母**（`assert_failure_isolated` 自检）；
指标分母的口径真源在 `contracts.py` 的 `counts_toward_denominator()` 与 `partition_rows()`，
判定、指标、汇总行、写回体都从这一处取。
