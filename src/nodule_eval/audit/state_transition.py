"""状态迁移日志：每次状态变化落 JSONL 一行。

**为什么要有这份底账**：飞检或科室复核问「这例凭什么算漏判」「这一轮
什么时候变成已复核的」，要能**按时间顺序把状态变化倒回去**。只留「现在
是什么状态」是不够的 —— 状态跳过的每一步、是谁推的、为什么推，都得留痕。

**源产品证据**：参考实现用 LangGraph 的 ``StateGraph`` 声明节点与条件边
（``github_ref/MedRAX/medrax/agent/agent.py:91-100``
的 ``add_conditional_edges`` / ``add_edge``）—— 走哪条边是**声明出来的**，
不是临场决定的。本产品沿用这条「边先声明」的做法：合法迁移边写死在
:data:`nodule_eval.store.RUN_TRANSITIONS` / :data:`nodule_eval.store.CASE_TRANSITIONS`，
不在边上的迁移一律拒绝。

源产品的状态**只活在进程内存**（``MemorySaver``），进程一退就没了 ——
「谁在什么时候把这一轮推到了哪一格」无从查起。本模块把每次状态变化
追加成 ``runs/<run_id>/transitions.jsonl``，一行一次迁移。

**字段语义**：``from`` / ``to`` 是迁移前后的状态，``actor`` 是**触发方**
（引擎 / 装配 / 人工复核 / 人工确认提交），``reason`` 是这一跳的原因，
``at`` 是发生时刻。四样凑齐，事后核对才不用猜。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 状态迁移日志文件名（落在本轮产物目录下）。
TRANSITIONS_FILENAME = "transitions.jsonl"


@dataclass(frozen=True)
class StateTransition:
    """一次状态迁移。

    ``scope`` 取 ``run`` / ``case``：run 是整轮的生命周期，
    case 是逐例的生命周期。``subject`` 在 run 作用域下是 ``run_id``，
    在 case 作用域下是 ``accession_no``。
    """

    scope: str
    run_id: str
    subject: str
    from_state: str
    to_state: str
    actor: str
    reason: str = ""
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "run_id": self.run_id,
            "subject": self.subject,
            "from": self.from_state,
            "to": self.to_state,
            "actor": self.actor,
            "reason": self.reason,
            "at": self.at,
        }


class TransitionRecorder:
    """把状态迁移追加到 ``runs/<run_id>/transitions.jsonl``。"""

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / TRANSITIONS_FILENAME

    def record(self, transition: StateTransition) -> StateTransition:
        """追加一行并立即落盘（flush + fsync），保证中断后已写的行不丢。"""

        line = json.dumps(transition.to_dict(), ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return transition

    def read_all(self) -> list[dict[str, Any]]:
        """读回全部迁移行（损坏行跳过，不让一行坏数据毁掉整份底账）。"""

        if not self.path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
        return rows
