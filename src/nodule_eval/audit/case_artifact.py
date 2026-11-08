"""逐例产物归档：一例一目录，重跑不覆盖历史。

**源产品证据**：参考实现把上传的文件**另存一份**再拿去用 ——
``interface.py:40-46`` 的 ``saved_path = self.upload_dir / f"upload_{timestamp}{suffix}"``
（``shutil.copy2``，原件留着不动）。一份原件被反复覆盖，就看不出「上一次跑的是什么」。

本产品把这条语义搬到**逐例产物**上::

    runs/<run_id>/cases/<accession_no>/attempt-0001/case.json
    runs/<run_id>/cases/<accession_no>/attempt-0002/case.json   ← 重跑时另起一份

同一 ``run_id`` 重跑（换版复评、断点续跑、误操作重跑）**不覆盖**上一版：
两版都在，可以逐例比对「这一轮和上一轮差在哪」。

**为什么用序号而不是时间戳**：源产品用时间戳是因为它只关心「这份文件是哪天的」；
本产品要**逐例**比对两版产物，同一秒内的两次尝试用时间戳会撞名 ——
序号不会撞，且「第几次尝试」本身就是复核时要看的信息。

**产物里放什么**：入参（``study_uid`` / ``image_ref`` / 模型与版本）、
调用 id、归并出的标签、判定与判定依据、金标准原文的 **sha256**（不落原文）。
原文不进产物 —— 放射科写的那段话可能带患者可识别信息，
产物只留哈希与派生标签，脱敏扫描（任务 34）扫的就是这些目录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..caseset import safe_accession_component
from ..store import write_json_file

__all__ = [
    "ATTEMPT_PREFIX",
    "CASES_DIRNAME",
    "CaseArtifact",
    "case_attempts_dir",
    "write_case_artifact",
]

#: 逐例产物目录名（落在 ``runs/<run_id>/`` 下）。
CASES_DIRNAME = "cases"

#: 一次尝试的目录名前缀。
ATTEMPT_PREFIX = "attempt-"

#: 尝试目录名里的序号。
_ATTEMPT_RE = re.compile(r"^attempt-(\d+)$")

#: 逐例产物文件名。
CASE_ARTIFACT_FILENAME = "case.json"


@dataclass(frozen=True)
class CaseArtifact:
    """一次逐例尝试的产物位置。"""

    accession_no: str
    attempt: int
    directory: Path
    path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "attempt": self.attempt,
            "directory": str(self.directory),
            "path": str(self.path),
        }


def case_attempts_dir(run_dir: Path | str, accession_no: str) -> Path:
    """某一例的尝试目录（不创建）。"""

    component = safe_accession_component(accession_no)
    return Path(run_dir) / CASES_DIRNAME / component


def _next_attempt(case_dir: Path) -> int:
    """下一次尝试的序号（目录里已有的最大序号 + 1）。"""

    highest = 0
    if case_dir.is_dir():
        for child in case_dir.iterdir():
            match = _ATTEMPT_RE.match(child.name)
            if match is None:
                continue
            highest = max(highest, int(match.group(1)))
    return highest + 1


def write_case_artifact(
    run_dir: Path | str,
    accession_no: str,
    payload: dict[str, Any],
) -> CaseArtifact:
    """落一例的产物，返回它的位置。

    **绝不覆盖**：序号自增，历史版本原样留着。
    """

    case_dir = case_attempts_dir(run_dir, accession_no)
    attempt = _next_attempt(case_dir)
    attempt_dir = case_dir / f"{ATTEMPT_PREFIX}{attempt:04d}"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    body = dict(payload)
    body.setdefault("accession_no", accession_no)
    body["attempt"] = attempt

    path = write_json_file(attempt_dir / CASE_ARTIFACT_FILENAME, body)
    return CaseArtifact(
        accession_no=accession_no,
        attempt=attempt,
        directory=attempt_dir,
        path=path,
    )
