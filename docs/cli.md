# 命令行速查（`nodule-eval`）

> **CLI 是运维与排障通道，不是产品首要入口。**
> 放射科日常用**评估表内插件**（xlsm 宏 / WPS JSA 宏）—— 在表里点「开始评估」、
> 点「确认提交」。CLI 服务的是三类事：**后台跑批**（几百例无人值守跑一夜）、
> **没有宏可用时的退路**（宏信任目录受组策略限制、或机器上没装 Excel）、
> **排障**（这次跑的产物在哪、那一条调用发了什么、这轮写上去了没有）。

---

## 目录

- [子命令一览](#子命令一览)
- [通用约定](#通用约定)
- [run：跑一轮评估](#run跑一轮评估)
- [assemble：装配本院病例集](#assemble装配本院病例集)
- [submit：写回科研平台评估登记](#submit写回科研平台评估登记)
- [replay：回放写回回执](#replay回放写回回执)
- [inspect：按调用记录 ID 反查](#inspect按调用记录-id-反查)
- [redact-scan：产物脱敏扫描](#redact-scan产物脱敏扫描)
- [doctor：自检](#doctor自检)
- [export：导出结论表](#export导出结论表)
- [fill：把判定回填进评估表](#fill把判定回填进评估表)
- [产物落点](#产物落点)
- [退出码](#退出码)
- [典型流程](#典型流程)

---

## 子命令一览

| 子命令 | 什么时候用 | 得到什么 |
|--------|------------|----------|
| `run` | 跑一轮评估（后台跑批 / 无宏退路） | `runs/<run_id>/` 下的逐例明细、评估结论、漏诊清单、调用底账 |
| `assemble` | 按检查号把本院病例集装到本地 | `cases/<检查号>/` 目录 + `runs/<run_id>/caseset_manifest.json` 装配清单 |
| `submit` | 人工复核后把结论写回科研平台 | 科研平台的一条评估登记 + `runs/<run_id>/writeback_audit.jsonl` 写回底账 |
| `replay` | 「这轮到底写上去了没有」 | 本地回执台账里的回执（`--verify` 时另附对端核对结果） |
| `inspect` | 「这例凭什么算漏判」 | 那次调用的入参与返回 + 这一例的判定依据 + 可粘进工单的核查片段 |
| `redact-scan` | 产物要往外发之前 | 脱敏扫描报告（命中即非零退出） |
| `doctor` | 装完先自检；出故障先查环境 | 逐项自检结果（缺哪个变量指名哪个） |
| `export` | 要一份能带走的结论表（**不依赖宏**） | `评估结论_<run_id>.xlsx` + `漏诊清单_<run_id>.csv` + `逐例明细_<run_id>.csv` |
| `fill` | 把判定写回评估表（**不依赖宏**的回填通道） | 原表新增六列 + 汇总区 + 漏诊区 + 状态栏（既有列零改动） |

## 通用约定

### 启动方式

项目未做可安装打包，源码在 `src/` 下。两种等价写法：

```bash
# 方式一：设 PYTHONPATH（推荐，跨平台）
export PYTHONPATH=/opt/nodule-ai-model-eval/src
python3 -m nodule_eval.cli --help

# 方式二：进到 src/ 里直接跑
cd /opt/nodule-ai-model-eval/src && python3 -m nodule_eval.cli --help
```

### 凭据

全部凭据与模型参数**只从环境变量读**，缺任何一个都指名报错
（`CREDENTIAL_MISSING`），**绝不回落本地替身**。

| 环境变量 | 用途 |
|----------|------|
| `RIS_BASE_URL` / `RIS_API_TOKEN` | 院内 RIS 检查清单（读） |
| `INFERENCE_BASE_URL` / `INFERENCE_API_TOKEN` | 院内厂商肺结节 AI 推理服务（写） |
| `RESEARCH_PLATFORM_BASE_URL` / `RESEARCH_PLATFORM_TOKEN` | 院内科研平台评估登记（写） |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | 院内模型服务（OpenAI-compatible），用于语义归并 |
| `MODEL_TIMEOUT` / `MODEL_MAX_RETRIES` / `MODEL_TEMPERATURE` | 模型调用参数 |

### 通用参数

| 参数 | 含义 |
|------|------|
| `--runs-dir DIR` | 产物根目录，缺省 `runs/` |
| `--store PATH` | run store（SQLite）路径，缺省用平台默认目录 |
| `--fixture` | **仅测试用**：把出站目标改走替身。缺替身地址即报错，不回落真实端点 |
| `--run-id ID` | 本轮标识，缺省按时刻生成 |
| `--model-id ID` | 被评模型标识（对应 `models/<model_id>.yaml`） |
| `--concurrency N` | 工作线程数（同时处理几例） |

> **替身门是「全开或全关」**：一旦给了 `--fixture`（或 `IMAGING_EVAL_FIXTURE=1`），
> 四个出站面**全部**改走替身 —— 不会出现「RIS 走替身、推理走真实」的半开状态
> （那会让两轮评估结论失去可比性）。

---

## run：跑一轮评估

**用途**：逐例调用推理端点 → 回填判定与指标 → 落一整套产物。

**产物落点**：`runs/<run_id>/`（逐例明细、评估结论、漏诊清单、调用底账、
逐例产物、迁移留痕、耗时台账）。

### 参数

| 参数 | 用途 |
|------|------|
| `--cases FILE` | 病例输入：完整请求 JSON，**或**只含病例数组的 JSON |
| `--request FILE` | 完整请求 JSON（须为对象且含 `cases`）；**与 `--cases` 二选一** |
| `--model-id ID` | 被评模型标识（也可由请求文件里的 `model_id` 提供） |
| `--run-id ID` | 本轮标识（缺省按时刻生成） |
| `--dataset-ref REF` | 数据集引用（写进结论，幂等键要用它） |
| `--model-version VER` | 被评模型版本（缺省取模型目录里的声明） |
| `--resume` | 断点续跑：跳过已跑完的例，不重复调推理端点 |
| `--concurrency N` | 工作线程数（1 = 串行） |
| `--endpoint-limits inference=8,model=2` | 逐端点在途上限（超限排队**不丢弃**） |
| `--size-tolerance-mm MM` | 尺寸容差（缺省 2.0，闭区间，恰好等于阈值算一致） |
| `--merge rule\|model` | 金标准归并口径：`rule` 规则基线（纯本地）/ `model` 语义归并 |
| `--no-model-gateway` | 强制走纯规则基线：不建模型网关、不调模型端点 |
| `--modality DX` / `--from` / `--to` | 按检查号绑定影像时向 RIS 请求的模态与日期区间 |
| `--connect-timeout` / `--read-timeout` | 出站超时（连接与读取分开计时） |

### 典型调用

```bash
# ① 最小一轮：纯规则基线归并，不调模型端点
python3 -m nodule_eval.cli run \
  --cases 评估表病例.json \
  --model-id demo-model \
  --run-id eval-2026-03 \
  --no-model-gateway

# ② 走语义归并（模型端点不可达会自动降级为规则基线，该例仍计入指标但单列「规则降级」）
python3 -m nodule_eval.cli run \
  --cases 评估表病例.json \
  --model-id demo-model \
  --run-id eval-2026-03 \
  --merge model

# ③ 两百例跑批：并发 8，推理端点单独限到 4
python3 -m nodule_eval.cli run \
  --cases 评估表病例.json \
  --model-id demo-model \
  --run-id eval-2026-03 \
  --concurrency 8 \
  --endpoint-limits inference=4

# ④ 跑到一半断了，接着跑（已跑完的例不再调推理端点）
python3 -m nodule_eval.cli run \
  --cases 评估表病例.json \
  --model-id demo-model \
  --run-id eval-2026-03 \
  --resume
```

### 输出（终端）

```
run_id        : eval-2026-03
被评模型      : demo-model v1.0.0
归并口径      : 规则基线（--no-model-gateway：不建模型网关、不调模型端点）
病例数        : 200
原始计数      : TP 160 / FP 12 / FN 3 / TN 25（进分母 200 例）
敏感度        : 0.9815（TP/(TP+FN)）
特异度        : 0.6757（TN/(TN+FP)）
阳性预测值    : 0.9302（TP/(TP+FP)）
阴性预测值    : 0.8929（TN/(TN+FN)）
失败数        : 0
挂起数        : 2
不可比数      : 0
漏诊例数      : 3（与漏诊数 FN 相同）
run 状态      : evaluated
```

> 上面的样例走的是**规则基线**。用 `--merge model` 时，「归并口径」那一行下面
> 会多一行**归并口径版本**（版本号 + 提示词文件相对路径 + 内容哈希前 12 位），
> 并多出「模型归并 / 规则降级 / 降级说明」三行：

```
归并口径      : 模型归并（ModelGateway · OpenAI-compatible；不可达时降级为规则基线）
归并口径版本: v1（prompts/goldstd_merge.v1.md，sha256 16b42b355b10）
模型归并      : 1 例
规则降级      : 0 例
```

### 归并口径与提示词版本（`--merge model`）

`--merge model` 走语义归并时，用的 system prompt 是**外置的可版本化文件**：

| 项 | 值 | 说明 |
|----|----|------|
| 提示词文件 | `prompts/goldstd_merge.v1.md` | **版本号在文件名里**（`v1`）；改口径就另存一版（`v2`），老版留在仓库里，旧结论仍能对回它当时用的那一版 |
| 标签表 | 运行时从 `src/nodule_eval/goldstd/labels.yaml` 填进 `{{LOCATIONS}}` / `{{DENSITIES}}` | **标签表换了不用改提示词**（等价表是唯一真源） |
| 底账留痕 | `model_calls.jsonl` 每行的 `prompt_version` / `prompt_sha256` / `prompt_template` / `prompt_rendered_sha256` | `prompt_sha256` = **提示词文件的内容哈希**；凭它可反查回 `prompts/` 下的具体那一版 |
| 提示词与契约 | 提示词声明的字段必须与 `contracts.py` 的输出契约逐字一致 | 对不上（提示词教模型输出契约外的字段 / 契约必填字段提示词没声明）→ **载入即报错**，不回落内置兜底提示词 |
| 安全边界 | 提示词必须写明「不改写原文」「不输出诊断」 | 模型输出里真冒出 `diagnosis` / `impression` / `诊断` 一类字段 → **代码判解析失败** → 走规则降级（光靠提示词拦不住） |

核对这一版提示词是否与账上对得上：

```bash
# 账上这一轮用的是哪一版提示词
python3 -c "import json,sys;[print(json.loads(l).get('prompt_version'), json.loads(l).get('prompt_template')) for l in open('runs/eval-2026-03/model_calls.jsonl') if l.strip()]"

# 文件当前的内容哈希（应与账上 prompt_sha256 一致）
shasum -a 256 prompts/goldstd_merge.v1.md

# 一键核对（真 CLI 子进程跑一轮 → 逐条比对，退出码 0 即通过）
python3 scripts/verify_prompt_version.py
```

---

## assemble：装配本院病例集

**用途**：从 RIS 取检查清单，按检查号**幂等**落盘到 `cases/<检查号>/`。
二次装配**一次 RIS 都不打**。

**产物落点**：`cases/<检查号>/case.json` + `runs/<run_id>/caseset_manifest.json`。

### 参数

| 参数 | 用途 |
|------|------|
| `--cases FILE` | 评估表病例 JSON：用表里圈定的检查号作装配集合 |
| `--accession ACC` | 只装配指定检查号（可重复）；与 `--cases` 同时给时以本项为准 |
| `--cases-root DIR` | 病例集根目录，缺省 `cases/` |
| `--modality DX` / `--from` / `--to` | 向 RIS 取清单的模态与日期区间 |

### 典型调用

```bash
# 按评估表圈定的检查号装配（产品主路径）
python3 -m nodule_eval.cli assemble --cases 评估表病例.json --run-id asm-2026-03

# 手工补装某几例
python3 -m nodule_eval.cli assemble --accession ACC-0001 --accession ACC-0002
```

---

## submit：写回科研平台评估登记

**用途**：把这一轮的评估结论写回院内科研平台的评估登记 ——
**这是产品里唯一真正改变院内业务状态的动作**。

### 闸门（两条，都要满足）

1. 这一轮 run 已经**人工复核**过（状态 `reviewed`）；
2. 触发这次写回的是**人**，不是引擎。

**闸门不过 → 一次出站调用都不发**（不是「发了但被拒」—— 后者已经在对端
留下了痕迹，事后还得去解释）。未复核直接提交时报
`WRITEBACK_NOT_REVIEWED`，run 状态停在原处。

### 参数

| 参数 | 用途 |
|------|------|
| `--run-id ID` | 要写回的那一轮 |
| `--review` | 先把这一轮推进到「已人工复核」再写回（**无宏退路**时的人工复核动作） |
| `--dry-run` | 只组装提交体并打印口径，**不出站**（写回前核对内容用） |
| `--submitted-by NAME` | 提交人（缺省取当前系统账号） |

### 典型调用

```bash
# ① 写回前先核对：只组装提交体，一次出站都不发
python3 -m nodule_eval.cli submit --run-id eval-2026-03 --review --dry-run

# ② 确认无误后写回
python3 -m nodule_eval.cli submit --run-id eval-2026-03 --review

# ③ 表格里已经点过「确认提交」了，命令行只补写回这一步
python3 -m nodule_eval.cli submit --run-id eval-2026-03
```

### 输出（终端）

```
run_id        : eval-2026-03
当前 run 状态 : evaluated
写回闸门      : 放行 —— 已人工复核（reviewed），允许写回
写回结果      : 已写回
登记号        : REG-20260301-0001
幂等键        : 3f9c…（sha256(run_id|model_id|dataset_ref)）
回执状态      : registered（HTTP 200）
尝试次数      : 1
提交人        : zhangsan
```

> **重复提交只会有一条登记**：幂等键相同，本地已有「已登记」回执时
> **一次出站调用都不发**，直接把本地回执回放出去（终端会写「幂等回放」）。
> **写回失败不推进状态**：run 停在 `reviewed`，绝不标成「已提交」。

---

## replay：回放写回回执

**用途**：回答「这一轮到底写上去了没有」。

**默认零出站** —— 回放的是**本地回执台账**里的那张回执。
`--verify` 才去科研平台核对登记号（**一次 GET 查询，不发 POST**）。
**核对失败（连不上 / 5xx）不算「没登记」**，只报「查不到」。

### 参数

| 参数 | 用途 |
|------|------|
| `--run-id ID` | 要查的那一轮 |
| `--verify` | 去科研平台核对登记号（一次 GET） |
| `--json` | 以 JSON 输出（给脚本读） |

### 典型调用

```bash
# 本地回执（不发任何请求）
python3 -m nodule_eval.cli replay --run-id eval-2026-03

# 再去科研平台核对一次登记号
python3 -m nodule_eval.cli replay --run-id eval-2026-03 --verify

# 给脚本读
python3 -m nodule_eval.cli replay --run-id eval-2026-03 --json
```

### 输出（终端）

```
run_id        : eval-2026-03
run 状态      : submitted
本地回执状态  : registered
登记号        : REG-20260301-0001
幂等键        : 3f9c…
提交时间      : 2026-03-01T09:12:33+08:00
尝试次数      : 1
对端核对      : 对端已登记，登记号一致（REG-20260301-0001）
核对结论      : 一致 —— 登记确实生效了
```

---

## inspect：按调用记录 ID 反查

**用途**：表格「调用记录ID」列里那个 ID → 那次调用的**入参与返回**，
join 回**这一例的判定与判定依据**。

### 参数

| 参数 | 用途 |
|------|------|
| `--tool-call-id ID` | 调用记录 ID（必填） |
| `--run-id ID` | 限定在某一轮里查（缺省在所有 run 里找） |
| `--snippet` | 只输出可直接粘进工单 / 飞检说明的核查片段 |
| `--json` | 以 JSON 输出（给表格插件 / 脚本读） |

### 典型调用

```bash
# 查一次调用
python3 -m nodule_eval.cli inspect --tool-call-id tc-8f2a1c

# 只要能粘进工单的那一段
python3 -m nodule_eval.cli inspect --tool-call-id tc-8f2a1c --snippet
```

> 查不到就说查不到（`TOOL_CALL_NOT_FOUND`，并列出可用的 run_id），
> **绝不返回空结果** —— 返回空结果等于告诉复核的人「查过了、没问题」。
> 输出里给的是**金标准摘要 + 原文 sha256，不是原文**（原文可能带患者可识别信息）。

---

## redact-scan：产物脱敏扫描

**用途**：产物要往外发之前，扫一遍有没有患者可识别明细。
**命中即失败**（非零退出）。扫描器**只报不改** —— 底账必须原样留。

### 参数

| 参数 | 用途 |
|------|------|
| `--run-id ID` | 要扫的那一轮（必填） |
| `--json` | 以 JSON 输出 |

### 典型调用

```bash
python3 -m nodule_eval.cli redact-scan --run-id eval-2026-03
```

---

## doctor：自检

**用途**：装完先跑一遍；出故障先查环境。
**不发任何出站调用** —— 自检程序去打真实端点会消耗额度，也会把对端日志搞脏。

### 检查项

| 检查项 | 不通过的后果 | 查什么 |
|--------|--------------|--------|
| Python 版本 | 阻断（要求 ≥ 3.10，交付口径 3.11） | 当前版本号 |
| 依赖包（requests / yaml / openpyxl） | 阻断 | **带版本号**（排障先看版本） |
| 环境变量文件（`.env`） | 提示 | 装了 `.env` 却没装 `python-dotenv` → 值不会被读（这坑必须点出来） |
| 环境变量（12 项，逐个指名缺哪个） | 阻断 | 缺哪几项**逐个列名**，空串也算没配 |
| 出站目标（RIS / 推理 / 科研平台 / 模型） | 提示（仅替身模式下） | 默认只显示环境变量里的真实端点 |
| 被评模型目录（列出现在能用哪几个） | 阻断 | 已授权 / 未授权各几个 |
| 运行库 | 阻断 | **真写一次再回滚**（目录可写 ≠ 文件能写） |
| 产物目录可写 | 阻断 | 目录权限 |
| 公开数据集缓存（JSRT） | 提示 | 已缓存张数与 247 例对不对得上；**只读本地文件，不联网** |
| 院内安装说明 | 提示 | `docs/install.md` 在不在 |

### 参数

| 参数 | 用途 |
|------|------|
| `--json` | 以 JSON 输出（给院内安装脚本读，阻断项与提示项分开列） |
| `--store PATH` | 要检查的运行库路径 |
| `--models-dir DIR` | 被评模型目录 |
| `--runs-dir DIR` | 产物根目录 |
| `--dataset-dir DIR` | 公开数据集（JSRT）缓存目录（缺省 `data/jsrt`） |
| `--verify-dataset` | 对 JSRT 缓存做逐文件 SHA256 深度校验（耗时较久，缺省只查数量与元数据） |
| `--fixture` | 声明本次自检处于替身模式（**只影响说明文字**，doctor 本身不发起任何出站调用，也不打开替身门） |

### 典型调用

```bash
python3 -m nodule_eval.cli doctor
python3 -m nodule_eval.cli doctor --json
python3 -m nodule_eval.cli doctor --dataset-dir data/jsrt --verify-dataset
```

### 输出（终端）

```
[✓] Python 版本：当前 3.10.20（要求 ≥ 3.10）；院内交付口径是 3.11（低版本能跑，但建议按交付口径装）
[✓] 依赖包：requests 2.32.3、yaml 6.0.2、openpyxl 3.1.5
[✓] 环境变量文件（.env）：/opt/nodule-ai-model-eval/.env（已生效；系统环境变量优先）（提示项，不影响继续使用）
[✗] 环境变量：缺 2 项：RIS_API_TOKEN、OPENAI_API_KEY（在 .env 或系统环境变量里补齐；本产品不会回落本地替身）
[✓] 出站目标：四个出站面全部指向环境变量里的真实端点：ris=https://…；inference=https://…；…（doctor 不发起任何出站调用）
[✓] 被评模型目录：已授权 2 个：demo-model、second-model
[✓] 运行库：/opt/nodule-ai-model-eval/store/nodule_eval.sqlite3（已新建，写入探针通过且已回滚，无残迹）
[✓] 产物目录：runs（所在目录 /opt/nodule-ai-model-eval 可写）
[!] 公开数据集缓存（JSRT）：未下载（不影响跑评估；仅用于规则/模型归并对照实验）：data/jsrt。下载：python scripts/fetch_jsrt.py（提示项，不影响继续使用）
[✓] 院内安装说明：/opt/nodule-ai-model-eval/docs/install.md（提示项，不影响继续使用）

自检未通过：环境变量 —— 请按上面的提示逐项处理后重跑 doctor。
```

> **doctor 不替产品做决定**：它只报告「配置齐不齐、依赖在不在、库能不能写」。
> **替身在这里只被提及、不被启用** —— doctor 永远不会因为「真实端点连不上」
> 就悄悄改走替身，那会让自检变成「假装一切正常」。
> 默认路径下「出站目标」这一项**连「替身」两个字都不出现**；
> 只有显式 `--fixture` / `IMAGING_EVAL_FIXTURE=1` 时才提，并说清「仅测试用、非默认」。
> 有阻断项时**退出码非零**（`2`），院内脚本据此判断「装好了没有」；提示项不影响退出码。
>
> **doctor 同时是安装验证脚本**：它会真的打开一次运行库并做一次**写入探针**
> （写完立刻回滚，不留残），把「目录能写但文件写不进去」这类问题挡在安装现场。
> 完整安装步骤见 `docs/install.md`。

---

## export：导出结论表

**用途**：把一轮评估的结论导成科室能直接带走的文件 ——
**不依赖宏**（宏信任目录受组策略限制、或机器上根本没装 Excel 时，这是唯一出路）。

**内容全部取自 run store**（库里的逐例行是真源）：不从 CSV 再解析一遍 ——
二次解析会把「产物被改过」藏起来。

**导出件里不落金标准原文** —— 结论是要往外发的东西（招采、评审、飞检说明）。
逐例明细里的「金标准」一栏给的是**归并后的结构化标签摘要**（部位 / 尺寸 / 密度）。

### 参数

| 参数 | 用途 |
|------|------|
| `--run-id ID` | 要导出哪一轮 |
| `--out-dir DIR` | 导出目录，缺省 `runs/<run_id>/export/` |
| `--format xlsx\|csv\|both` | 导出格式（缺省 both） |

### 典型调用

```bash
# 导出到指定目录（给评审用）
python3 -m nodule_eval.cli export --run-id eval-2026-03 --out-dir D:/评估结论

# 只导 CSV（导入统计、贴进邮件）
python3 -m nodule_eval.cli export --run-id eval-2026-03 --format csv
```

### 产物

| 文件 | 内容 |
|------|------|
| `评估结论_<run_id>.xlsx` | 四页：评估结论（**带手算算式**）/ 逐例明细 / 漏诊清单 / 字段说明 |
| `漏诊清单_<run_id>.csv` | 漏诊例逐条（`utf-8-sig`，Excel 双击打开不乱码） |
| `逐例明细_<run_id>.csv` | 逐例判定（同样 `utf-8-sig`） |

「评估结论」页给的是**项目 / 值**两列，含四项指标的**原始计数**与**算式**
（`TP/(TP+FN) = 160/163 = 0.9815`）—— 拿计算器就能对一遍，不必信引擎。

---

## fill：把判定回填进评估表

**用途**：评估表里点不了「开始评估」（宏装不上、组策略禁宏、机器没装 Excel）时，
用这条命令把一轮评估的判定**回填进同一张评估表** —— 结果与宏回填完全一致。

**只写新增六列**（`verdict` / `model_findings` / `failure_reason` / `tool_call_id` /
`needs_review` / `normalize_source`），既有三列（检查号 / 金标准原文 / 影像引用）
**一个字都不动** —— 回填前后逐格比对，对不上就**放弃落盘**（表保持原样）。

**落盘是原子的**：整轮回填先在内存里改完、核对完，再一次性替换文件。
中途崩了 / 被 Ctrl-C 了 / 磁盘写满了 —— 原表**一个字节都没变**，
不会出现「前 80 例填好了、第 81 例填到一半」的半截状态。

### 参数

| 参数 | 用途 |
|------|------|
| `--run-id ID` | 要回填哪一轮（库里没有这一轮就报错） |
| `--sheet PATH` | 评估表路径（`.xlsm` / `.xlsx`；`.xlsm` 保留宏工程） |
| `--sheet-name NAME` | 明细页名（缺省自动找「评估表」或「逐例明细」） |
| `--store PATH` | 运行库路径（缺省用平台默认目录） |
| `--runs-dir DIR` | 产物根目录（缺省 `runs/`） |

### 典型调用

```bash
# 跑完一轮后回填进评估表
python3 -m nodule_eval.cli fill --sheet 评估表.xlsx --run-id eval-2026-03

# 指定运行库与明细页名
python3 -m nodule_eval.cli fill --sheet D:/评估/胸片评估表.xlsx \
    --sheet-name 评估表 --run-id eval-2026-03 --store D:/评估/nodule_eval.sqlite3
```

### 输出（终端）

```
run_id        : eval-2026-03
评估表        : D:/评估/胸片评估表.xlsx（明细页：评估表）
回填行数      : 200（其中失败例 2）
汇总区格数    : 15
漏诊清单行数  : 3
既有列核对    : 600 格逐格比对，未改动
状态栏        : 【运行状态】评估完成：run_id=eval-2026-03，共 200 例（失败 2 例）
```

**空态**：表里没圈定病例 → 状态栏写「本表未圈定病例」，一张格子都不动；
这一轮没漏诊 → 漏诊明细区写「本次未发现漏判」，不是一片空白。

---

## 产物落点

```
runs/<run_id>/
├── case_rows.csv            逐例判定（回填到评估表新增六列的数据源）
├── summary.json             run 级结论（含手算复核块 manual_check）
├── missed_list.csv          漏诊清单（条数 ≡ 漏诊数 FN）
├── tool_calls.jsonl         调用底账（按 tool_call_id 反查）
├── model_calls.jsonl        模型调用底账（只调过模型才有）
├── binding.json             三键绑定结果与差异记录
├── caseset_manifest.json    病例集装配清单
├── writeback_audit.jsonl    写回底账（真的写回过才有）
├── writeback_receipt.json   写回回执
├── transitions.jsonl        状态迁移留痕（谁在什么时候把状态推到哪一格）
├── case_timings.json        逐例耗时台账（含排队时长）
├── versions/                历史版本（重跑不覆盖上一版）
└── cases/<检查号>/attempt-XXXX/case.json   逐例产物（判定依据 + 原文 sha256）
```

---

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `2` | 可预期的产品错误（错误码 + 中文文案打在标准错误上），如 `CREDENTIAL_MISSING` / `CONTRACT_INVALID` / `WRITEBACK_NOT_REVIEWED` |
| `1` | 未预期异常 |

> `doctor` 在有**阻断项**时也返回 `2` —— 院内安装脚本据此判断「装好了没有」。

---

## 典型流程

### 后台跑批一轮评估（无人值守）

```bash
python3 -m nodule_eval.cli doctor                                   # 先自检
python3 -m nodule_eval.cli run \
  --cases 评估表病例.json --model-id demo-model --run-id eval-2026-03 \
  --concurrency 8 --endpoint-limits inference=4
python3 -m nodule_eval.cli export --run-id eval-2026-03 --out-dir D:/评估结论
```

### 评审前把结论核对一遍

```bash
# 漏诊清单里有几条？哪几例漏的？
python3 -m nodule_eval.cli inspect --tool-call-id <表格里的调用记录ID>

# 产物能不能往外发？
python3 -m nodule_eval.cli redact-scan --run-id eval-2026-03
```

### 写回科研平台（**须先人工复核**）

```bash
python3 -m nodule_eval.cli submit --run-id eval-2026-03 --review --dry-run   # 核对
python3 -m nodule_eval.cli submit --run-id eval-2026-03 --review            # 写回
python3 -m nodule_eval.cli replay --run-id eval-2026-03 --verify            # 核对生效
```
