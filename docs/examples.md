# 产物样例

> 下面每段都取自**真实运行**的产物文件（本仓库 `runs/` 目录下的实际落盘内容），
> 检查号、站点地址一律是脱敏占位符。字段名与真实产物**逐字一致** ——
> 改字段名的改动必须同时改这里，否则测试对不上。

产物落点：`runs/<run_id>/`。

| 文件 | 内容 | 谁消费它 |
|------|------|----------|
| `case_rows.csv` | 逐例判定明细（回填评估表的数据源） | 表格回填、指标反算 |
| `summary.json` | run 级结论（原始计数 + 四项指标 + 手算复核块） | 汇总区、导出结论表、写回提交体 |
| `result.json` | 引擎给表格宏读的结果 JSON（`case_rows` / `metrics` / `missed_list` / `summary`） | 表格内宏 |
| `missed_list.csv` | 漏诊清单（条数 ≡ FN） | 评审、写招采说明 |
| `tool_calls.jsonl` | 推理调用底账（入参 + 返回 + 时间戳） | `inspect` 反查、事后核对 |
| `model_calls.jsonl` | 模型端点调用底账（**只记哈希、不记原文**） | 归并口径追溯 |
| `binding.json` | 三键绑定关系与差异记录 | 一对多歧义核对 |
| `cases/<检查号>/attempt-XXXX/case.json` | 单例归档（归并标签 / 判定依据 / 逐层结论） | 单例争议复核 |
| `transitions.jsonl` | 状态迁移留痕（from / to / 触发方 / 原因） | 飞检问询「这例凭什么这么判」 |
| `case_timings.json` | 逐例耗时与排队时长 | 规模评估 |

---

## 逐例判定（`case_rows.csv`）

```csv
accession_no,verdict,model_findings,failure_reason,tool_call_id,needs_review,normalize_source,row_no
DEMO-0001,hit,右下肺 | 8.0mm | 磨玻璃,,tc_0ed385e48b5a4b3482e0ef963f6b8e65,False,rule,1
DEMO-0002,true_negative,未检出候选病灶,,tc_a8d9b982a3624ed2aeaebb193597ae38,False,rule,2
DEMO-0003,miss,未检出候选病灶,,tc_a1c0f47b6d9e4c2f8a0b3d5e7f9012ab,False,rule,3
DEMO-0004,failed,,推理服务暂时不可用（HTTP 500）：重试 3 次后仍失败,tc_b2e1f38c7a0d5e6f1b2c3d4e5f60718,True,rule,4
```

- `verdict` 五个互斥值：`hit`（命中）· `miss`（漏判）· `false_positive`（误报）·
  `true_negative`（真阴性）· `failed`（失败）。
- `normalize_source` 是归并口径：`rule`（规则基线）· `model`（语义归并）。
- **产物里不落金标准原文** —— 金标准只在评估表里，逐例产物只存内容哈希。

## 汇总结论（`summary.json`）

```json
{
  "run_id": "demo-200-dx-001",
  "model_id": "demo-model",
  "model_version": "v1.0.0",
  "dataset_ref": "demo-chest-xray-200",
  "total_cases": 200,
  "evaluated_cases": 195,
  "counts": {
    "tp": 120, "fp": 40, "fn": 20, "tn": 15,
    "counted": 195, "failed": 5, "needs_review": 0,
    "incomparable": 0, "rule_degraded": 0, "model_normalized": 0
  },
  "metrics": {
    "sensitivity": 0.8571, "sensitivity_reason": "",
    "specificity": 0.2727, "specificity_reason": "",
    "ppv": 0.75, "ppv_reason": "",
    "npv": 0.4286, "npv_reason": ""
  },
  "verdict_distribution": {"hit": 120, "miss": 20, "false_positive": 40, "true_negative": 15, "failed": 5},
  "manual_check": {
    "consistent": true,
    "formulas": {
      "sensitivity": "TP/(TP+FN) = 120/140 = 0.8571",
      "specificity": "TN/(TN+FP) = 15/55 = 0.2727",
      "ppv": "TP/(TP+FP) = 120/160 = 0.7500",
      "npv": "TN/(TN+FN) = 15/35 = 0.4286"
    }
  }
}
```

- 四项指标与原始计数**同出一处**（`metrics/compute.py` 的 `compute_metrics()`），不各写一份。
- 分母为 0 的指标输出 `null` 并写明原因（「本轮没有金标准阳性的样本（TP+FN=0），分母为 0，敏感度算不出（不是 0）」）——
  **「算不出」与「算出来是 0」是两件事**。
- 取整用 `Decimal` + `ROUND_HALF_EVEN`（保留四位小数），不用二进制浮点的 `round()` —— 这一栏是要给人拿计算器手算复核的。
- `manual_check` 是手算复核块：给出四个算式（含中间值）与从逐例明细反算出来的计数。

## 漏诊清单（`missed_list.csv`）

```csv
accession_no,gold_standard_summary,model_findings,missed_reason,tool_call_id,rule_degraded
DEMO-0003,右肺下叶 8.0mm 磨玻璃,未检出候选病灶,金标准写明了结节，模型这次没有报出对应的病灶；差在「存在性」这一层,tc_a1c0f47b6d9e4c2f8a0b3d5e7f9012ab,False
```

- 「金标准摘要」由**归并后的标准标签**（部位 / 尺寸 / 密度）拼成，不是原文 —— 原文可能带姓名、年龄、住院号。
- 判了漏诊但因属性不可比没进分母的例单列在 `summary.json` 的 `missed.not_counted`，**不静默丢行**。

## 调用底账（`tool_calls.jsonl`）

一行一次出站调用：

```json
{"tool_call_id":"tc_0ed385e48b5a4b3482e0ef963f6b8e65","name":"demo-model.detect","args":{"accession_no":"DEMO-0001","study_uid":"STUDY-0001","image_ref":"dx://local/0001","model_id":"demo-model","model_version":"v1.0.0"},"content":"{\"study_uid\":\"STUDY-0001\",\"model_id\":\"demo-model\",\"model_version\":\"v1.0.0\",\"candidates\":[{\"position\":\"右下肺\",\"size_mm\":8.0,\"density\":\"磨玻璃\",\"confidence\":0.87}]}","timestamp":"2026-11-07T22:29:58"}
```

- `content` 是**厂商返回体的原文**（字符串，不解析、不重排），失败时是结构化失败记录
  （`error_code` / `http_status` / `counts_toward_metrics`）。
- `args` 里带 `accession_no`，逐例行与底账靠 `tool_call_id` 一一对应。
- 失败例**照样留底账** —— 「这例凭什么算失败」看这一行就够。

## 模型调用底账（`model_calls.jsonl`）

走语义归并（`--merge model`）时另有一本账，记的是**模型端点**那几次调用：

```json
{"model_call_id":"mc_6f1c2a9b4d7e8f0a3b5c7d9e1f2a4b6","tool_call_id":"tc_0ed385e48b5a4b3482e0ef963f6b8e65","accession_no":"DEMO-0001","model":"<MODEL_NAME>","base_url_host":"<ENDPOINT_HOST>","started_at":"2026-11-07T22:29:58","latency_ms":812,"prompt_tokens":128,"completion_tokens":64,"prompt_sha256":"<SHA256>","raw_response_sha256":"<SHA256>","fallback_used":false,"prompt_template":"prompts/goldstd_merge.v1.md","prompt_template_sha256":"<SHA256>","prompt_version":"v1","prompt_rendered_sha256":"<SHA256>","status":"ok","error_code":"","attempts":1,"connect_timeouts":0,"read_timeouts":0}
```

- 与 `tool_calls.jsonl` **按 `tool_call_id` 关联**：结论 → 工具调用 → 模型调用，三级链路一次走通。
- **只记哈希、不记原文**：提示词与模型返回里带金标准原文，账上只留 `prompt_sha256` / `raw_response_sha256`。
- **失败的调用照样记账**（`status=failed` + 原因码），便于查「这轮为什么降级」。
- **这批结论是哪版口径跑的**：`prompt_version` 是归并口径的版本号（提示词文件名里的 `v1`），
  `prompt_sha256` 是**提示词文件的内容哈希**，`prompt_template` 是那个文件的相对路径 ——
  三个凑一起就能反查回「这一轮用的是哪一版提示词」。改口径就另存一版
  （`prompts/goldstd_merge.v2.md`），老版留在仓库里，旧结论仍能对回它当时用的那一版。
- 走规则基线（`--merge rule`）时**没有这本账** —— 没调模型就没有账。

## 三键绑定（`binding.json`）

```json
{
  "bindings": [
    {"accession_no": "DEMO-0001", "study_uid": "STUDY-0001", "row_no": 2, "status": "bound"},
    {"accession_no": "DEMO-0002", "study_uid": null, "row_no": 3, "status": "ambiguous", "error_code": "AMBIGUOUS_BINDING", "candidates": ["STUDY-0002A", "STUDY-0002B"]}
  ],
  "differences": [
    {"accession_no": "DEMO-0003", "field": "image_ref", "sheet_value": "<SHEET_REF>", "authority_value": "<RIS_REF>", "note": "以 RIS 为真源，该例挂起人工复核"}
  ]
}
```

- `status`：`bound`（一一对应）· `ambiguous`（一对多，拒绝自动取第一条）·
  `unresolved`（RIS 里查不到）· `duplicate`（表内检查号重复）。
- 后三类一律挂起为 `needs_review`，**不进指标分母**。

## 状态迁移留痕（`transitions.jsonl`）

```json
{"scope":"run","run_id":"demo-200-dx-001","subject":"demo-200-dx-001","from":"running","to":"called","actor":"engine","reason":"逐例调用完成","at":"2026-11-07T22:29:58"}
{"scope":"run","run_id":"demo-200-dx-001","subject":"demo-200-dx-001","from":"evaluated","to":"reviewed","actor":"human-review","reason":"放射科与信息科复核通过","at":"2026-11-07T23:10:12"}
```

- `actor` 只有 `engine` / `human-review` / `human-submit` 三种。
- **引擎自推 `reviewed` / `submitted` 一律被拒** —— 只有人工复核与人工确认能推进。
- 被拒的迁移**不留痕**。

## 单例归档（`cases/<检查号>/attempt-XXXX/case.json`）

```json
{
  "accession_no": "DEMO-0001",
  "attempt": 1,
  "gold_standard_sha256": "<SHA256>",
  "labels": {"presence": true, "location": "右下肺", "size_mm": 8.0, "density": "磨玻璃"},
  "judgement": {"verdict": "hit", "basis": "存在性一致，部位一致，尺寸在容差内，密度一致", "uncovered_labels": [], "incomparable": false},
  "failure": null
}
```

- **只落金标准的哈希，不落原文**。
- 重跑不覆盖历史，另起 `attempt-0002`。

---

## 导出件（`export` 子命令）

| 文件 | 内容 |
|------|------|
| `评估结论_<run_id>.xlsx` | 三页：评估结论（含手算复核算式）/ 逐例明细 / 漏诊清单 |
| `逐例明细_<run_id>.csv` | 与 `case_rows.csv` 同列 |
| `漏诊清单_<run_id>.csv` | 与 `missed_list.csv` 同列 |

导出件**不落金标准原文**，可直接贴进邮件或评审材料。
