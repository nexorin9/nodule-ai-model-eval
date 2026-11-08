# 错误码落地矩阵

本文件由 `python scripts/gen_error_matrix.py` 生成，请勿手工编辑。

一个错误码在产品里要落到五个地方：**在哪儿触发**、**中文文案**、**能不能重试**、**进不进指标分母**、**用户在哪儿看到**。缺一处就会出现「同一个错误在不同层显示三种说法」。

文案模板的**唯一来源**是 `src/nodule_eval/errors.py`；表格 `failure_reason` 列、终端输出、`summary.json` 三处共用 `errors.user_text()`，因此同一错误码必然是同一句话。

## 落地矩阵

| 错误码 | 触发点 | 中文文案模板 | 可重试 | 进指标分母 | 用户可见位置 | 代码位置 |
|--------|--------|--------------|--------|------------|--------------|----------|
| `CREDENTIAL_MISSING` | config.require_env() 读环境变量时发现缺项 —— 指名缺哪个变量<br>CLI 启动即体检（doctor）时缺项 | `缺少必需的环境变量：{names}。请补齐后重试（不会自动回落到本地替身）` | 否 | 否 | 终端输出（doctor）<br>终端输出 | nodule_eval/config.py:202 |
| `CONTRACT_INVALID` | 模型目录字段映射不合法（catalog 装载时）<br>评估表既有列缺失 / 金标准为空 / 行号越界（sheets 读取时）<br>CLI 参数互斥或缺必填项（命令行解析时）<br>指标与结论自检对不上（落盘前一致性校验）<br>run store 打开失败或写入越界（存储层） | `不符合数据契约：{detail}` | 否 | 否 | 终端输出<br>表格 failure_reason 列<br>summary.json | nodule_eval/adapters/research_platform.py:197<br>nodule_eval/adapters/research_platform.py:162<br>nodule_eval/adapters/research_platform.py:324<br>nodule_eval/adapters/research_platform.py:329<br>…共 134 处 |
| `RIS_READ_FAILED` | adapters/ris.py 拉取检查清单时 HTTP 出错或分页中途失败<br>RIS 返回的记录缺 accession_no / study_uid 必填字段 | `读取院内 RIS 检查清单失败：{detail}` | 是 | 否 | 终端输出<br>表格 failure_reason 列<br>summary.json | nodule_eval/adapters/ris.py:289<br>nodule_eval/adapters/ris.py:300<br>nodule_eval/adapters/ris.py:306<br>nodule_eval/adapters/ris.py:313<br>…共 7 处 |
| `AMBIGUOUS_BINDING` | caseset.bind_cases() 一对多命中（同一检查号多条影像 / 多个时间点）<br>RIS 里查不到该检查号（unresolved 挂起）<br>表内检查号重复（duplicate 挂起） | `检查号 {accession_no} 命中多条影像，无法自动绑定，已挂起人工判定` | 否 | 否 | 表格 failure_reason 列<br>summary.json<br>终端输出 | nodule_eval/caseset.py:148<br>nodule_eval/caseset.py:916<br>nodule_eval/caseset.py:920 |
| `INFERENCE_4XX` | adapters/inference.py 收到 4xx（参数 / 鉴权 / 图像引用不合法）<br>**立刻判该例失败，不消耗退避预算**（重试必然还是被拒） | `推理服务拒绝了本次请求（HTTP {status}）：{detail}` | 否 | 否 | 表格 failure_reason 列<br>终端输出<br>tool_calls.jsonl<br>summary.json | nodule_eval/adapters/inference.py:133<br>nodule_eval/metrics/judge.py:193 |
| `INFERENCE_5XX` | adapters/inference.py 收到 5xx / 429（退避重试后仍失败）<br>连接被拒 / DNS 解析失败（adapters/inference.py） | `推理服务暂时不可用（HTTP {status}）：{detail}` | 是 | 否 | 表格 failure_reason 列<br>终端输出<br>tool_calls.jsonl<br>summary.json | nodule_eval/adapters/inference.py:127<br>nodule_eval/adapters/inference.py:130<br>nodule_eval/adapters/inference.py:136<br>nodule_eval/metrics/judge.py:194<br>…共 6 处 |
| `INFERENCE_TIMEOUT` | adapters/inference.py 连接阶段超时<br>adapters/inference.py 响应阶段超时 | `调用推理服务超时（{timeout}）：{detail}` | 是 | 否 | 表格 failure_reason 列<br>终端输出<br>tool_calls.jsonl<br>summary.json | nodule_eval/adapters/inference.py:121<br>nodule_eval/adapters/inference.py:124<br>nodule_eval/metrics/judge.py:195 |
| `STUDY_UID_MISMATCH` | adapters/inference.py 响应 study_uid 与请求不一致<br>adapters/inference.py 响应未回传 study_uid | `推理服务回传的检查标识与请求不一致（期望 {expected}，实得 {actual}），该例判失败` | 否 | 否 | 表格 failure_reason 列<br>终端输出<br>tool_calls.jsonl<br>summary.json | nodule_eval/adapters/inference.py:139<br>nodule_eval/adapters/inference.py:431<br>nodule_eval/metrics/judge.py:196 |
| `FIELD_MAPPING_MISSING` | 模型目录缺字段映射（catalog 装载时）<br>推理响应缺 candidates 或候选字段缺失（adapters/inference.py）<br>模型响应缺结构化字段（gateway 结构化解析时） | `字段缺失：{detail}` | 否 | 否 | 终端输出（doctor）<br>表格 failure_reason 列<br>终端输出<br>summary.json | nodule_eval/adapters/inference.py:143<br>nodule_eval/adapters/inference.py:602<br>nodule_eval/adapters/inference.py:419<br>nodule_eval/adapters/inference.py:442<br>…共 25 处 |
| `RESEARCH_WRITE_FAILED` | adapters/research_platform.py 写回登记时 HTTP 出错或连接失败<br>adapters/research_platform.py 回执核对缺 registration_id / run_id / status / received_at | `写回院内科研平台失败（{status}）：{detail}` | 是 | 否 | 终端输出<br>writeback_audit.jsonl | nodule_eval/adapters/research_platform.py:418<br>nodule_eval/adapters/research_platform.py:569<br>nodule_eval/adapters/research_platform.py:582<br>nodule_eval/adapters/research_platform.py:630<br>…共 11 处 |
| `RESEARCH_PAYLOAD_TOO_LARGE` | adapters/research_platform.py 提交体字节数超过对端上限 | `写回提交体超出对端上限：{detail}` | 否 | 否 | 终端输出<br>writeback_audit.jsonl | nodule_eval/adapters/research_platform.py:268 |
| `WRITEBACK_NOT_REVIEWED` | writeback 闸门校验：run 未经人工复核就请求确认提交<br>CLI submit 子命令直接跳过复核环节 | `确认闸门未通过：{detail}` | 否 | 否 | 终端输出 | nodule_eval/cli.py:1072<br>nodule_eval/writeback.py:198<br>nodule_eval/writeback.py:678 |
| `MODEL_UNAVAILABLE` | 模型目录未授权该模型（catalog 选型时）<br>模型目录文件缺失 / 端点路径缺失<br>gateway 连接阶段或响应阶段超时<br>gateway 端点不可达（降级路径入口） | `模型不可用：{detail}` | 否 | 否 | 终端输出（doctor） | nodule_eval/catalog.py:158<br>nodule_eval/catalog.py:237<br>nodule_eval/catalog.py:281<br>nodule_eval/catalog.py:297<br>…共 15 处 |
| `MODEL_REQUEST_REJECTED` | gateway 收到 4xx（请求被拒）<br>goldstd/merge.py 模型归并输出不合契约（缺 evidence_span / 改写原文） | `模型端点拒绝了本次请求（HTTP {status}）：{detail}` | 否 | 否 | 终端输出<br>表格 failure_reason 列<br>summary.json<br>model_calls.jsonl | nodule_eval/gateway.py:167<br>nodule_eval/goldstd/merge.py:230 |
| `RULE_DEGRADED` | goldstd/merge.py 模型不可达 / 输出不可解析 → 走规则基线归并<br>engine 逐例判定时标记该例为降级（仍进指标分母，单列降级数） | `模型归并未成功，已降级为规则归并：{detail}` | 否 | 是 | 表格 failure_reason 列<br>终端输出<br>summary.json<br>tool_calls.jsonl | nodule_eval/engine.py:1825<br>nodule_eval/goldstd/merge.py:893<br>nodule_eval/metrics/compute.py:144<br>nodule_eval/metrics/judge.py:410 |
| `INVALID_STATE_TRANSITION` | store.run/case 状态机遇到非法边（如 evaluated 未经 reviewed 直跳 submitted）<br>引擎内部循环试图自行推进到 reviewed / submitted<br>writeback_receipt 写回回执状态非法 | `状态迁移被拒：{detail}` | 否 | 否 | 终端输出<br>表格 failure_reason 列<br>summary.json<br>transitions.jsonl | nodule_eval/engine.py:1308<br>nodule_eval/engine.py:1336<br>nodule_eval/engine.py:1375<br>nodule_eval/engine.py:1536<br>…共 10 处 |
| `TOOL_CALL_NOT_FOUND` | audit/inspect.py 按 tool_call_id 反查未命中<br>audit/inspect.py 底账里缺该次调用 | `未找到该调用记录：{tool_call_id}。{detail}` | 否 | 否 | 终端输出（inspect） | nodule_eval/audit/inspect.py:425<br>nodule_eval/audit/inspect.py:129 |
| `INSPECT_ARTIFACT_INCOMPLETE` | audit/inspect.py 找不到逐例产物目录<br>audit/inspect.py 逐例产物缺 case.json / 底账缺行<br>audit/inspect.py 逐例产物与调用底账对不上 | `反查所需产物不完整：{detail}` | 否 | 否 | 终端输出（inspect）<br>runs/<run_id>/ 产物目录 | nodule_eval/audit/inspect.py:151<br>nodule_eval/audit/inspect.py:178<br>nodule_eval/audit/inspect.py:440<br>nodule_eval/audit/inspect.py:448<br>…共 6 处 |
| `ARTIFACT_PII_DETECTED` | audit/redact.py 扫产物命中身份证号 / 住院号 / 手机号 / 中文姓名 | `产物里出现患者可识别明细：{detail}` | 否 | 否 | 终端输出（redact-scan）<br>runs/<run_id>/ 产物目录 | nodule_eval/audit/redact.py:1455<br>nodule_eval/cli.py:983 |
| `ARTIFACT_FIELD_UNREGISTERED` | audit/redact.py 扫产物命中字段白名单之外的字段名 | `产物里出现未登记字段：{detail}` | 否 | 否 | 终端输出（redact-scan）<br>runs/<run_id>/ 产物目录 | nodule_eval/audit/redact.py:1462<br>nodule_eval/cli.py:985 |

## 用户可见的三个面

| 面 | 落点 | 谁产出文案 |
|----|------|------------|
| 表格 | `failure_reason` 列（评估表新增列） | `errors.user_text()` |
| 终端 | `run` 子命令输出里的「失败分布」 | `errors.user_text()` |
| 结论 | `summary.json` 的 `failures.by_code` | `errors.user_text()` |

三个面读的是**同一个函数**的返回值 —— 所以文案一致不是靠人工核对，是结构上不可能不一致。`tests/test_error_matrix.py` 每次都真跑一遍并逐字比对。

## 重试口径

4xx 参数类**不重试**：重试必然还是被拒，把退避预算浪费掉只会让这一例拖慢整轮。5xx / 429 与超时才退避重试（最多 3 次）。重试口径由 `errors.py` 的 `retryable` 字段声明，`adapters/http.py` 的实际行为由 `tests/test_error_matrix.py` 核对。
