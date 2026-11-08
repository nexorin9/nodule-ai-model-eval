"""run 版本台账：同一 ``run_id`` 的每一版产物各存一份，重跑不覆盖上一版。

**源产品证据**：参考实现下载影像时「已存在则跳过」
（``github_ref/MedRAX/data/figures.py:49-57`` 的 ``if output_path.exists(): return``），
保证重复跑不把已有文件覆盖 —— 但它**只保证文件不被覆盖，不留版本**：
跳过之后目录里剩下的就是「第一次跑的那份」，中间试过什么、再跑一遍差在哪，
全看不出来。

本产品要的是**可比对的两版**：同一轮评估重跑一次（换版复评、误操作重跑、
断点续跑），两版的逐例明细都得在 —— 复核的人会问「这一轮和上一轮差在哪」，
只留最新版答不上来。

落法::

    runs/<run_id>/
    ├── case_rows.csv              ← 最新一版（稳定路径：表内插件按这个路径回填）
    ├── result.json
    ├── tool_calls.jsonl           ← 跨版本累积的调用底账（只追加，不覆盖）
    └── versions/
        ├── version-0001/
        │   ├── version.json       ← 本版台账（起止时刻、处理/跳过例数、状态）
        │   ├── case_rows.csv      ← 第一版逐例明细快照
        │   └── result.json
        └── version-0002/ …

**为什么顶层留最新版、版本另存**：表内插件回填新增列需要一个**稳定路径**
（``runs/<run_id>/case_rows.csv``），路径每次都变的话宏里就得写版本号逻辑；
而版本对照是**事后核对**时才用的，不该由宏操心。所以顶层给「当前这一版」，
历史版本进 ``versions/``。

**跑崩了也留档**：一轮跑到一半断掉（断电、关窗、厂商端点挂掉）时，
本版台账照样落盘，状态写 ``interrupted`` —— 「这一版跑到哪儿崩的」
和「这一版跑完了」必须分得清，半截产物不能装作完整产物。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..store import write_json_file
from .toolcall import now_iso

__all__ = [
    "ATTEMPT_STATUS_COMPLETED",
    "ATTEMPT_STATUS_INTERRUPTED",
    "VERSION_DIR_PREFIX",
    "VERSION_FILENAME",
    "VERSIONS_DIRNAME",
    "RunVersion",
    "begin_version",
    "finish_version",
    "next_version_no",
    "versions_dir",
]

#: 版本目录名（落在 ``runs/<run_id>/`` 下）。
VERSIONS_DIRNAME = "versions"

#: 一个版本目录名的前缀（后面跟四位序号）。
VERSION_DIR_PREFIX = "version-"

#: 版本台账文件名。
VERSION_FILENAME = "version.json"

#: 版本跑完了。
ATTEMPT_STATUS_COMPLETED = "completed"
#: 版本中途断了（断电 / 关窗 / 端点挂掉）。
ATTEMPT_STATUS_INTERRUPTED = "interrupted"

_VERSION_DIR_RE = re.compile(rf"^{re.escape(VERSION_DIR_PREFIX)}(\d+)$")


@dataclass(frozen=True)
class RunVersion:
    """一次 run 版本（一次「跑一轮」就是一个版本）。"""

    version_no: int
    mode: str
    started_at: str
    directory: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_no": self.version_no,
            "mode": self.mode,
            "started_at": self.started_at,
            "directory": str(self.directory),
        }


def versions_dir(run_dir: Path | str) -> Path:
    """某一轮的版本目录（不创建）。"""

    return Path(run_dir) / VERSIONS_DIRNAME


def next_version_no(run_dir: Path | str) -> int:
    """下一个版本序号（目录里已有的最大序号 + 1）。

    序号而不是时间戳：同一秒内重跑两次用时间戳会撞名，
    而「第几版」本身就是复核时要看的信息。
    """

    highest = 0
    target = versions_dir(run_dir)
    if target.is_dir():
        for child in target.iterdir():
            match = _VERSION_DIR_RE.match(child.name)
            if match is None:
                continue
            highest = max(highest, int(match.group(1)))
    return highest + 1


def begin_version(run_dir: Path | str, mode: str) -> RunVersion:
    """开一个新版本：建目录、记起始时刻。

    ``mode`` 取 ``"run"``（正常跑一轮）或 ``"resume"``（断点续跑）——
    事后核对时一眼能看出这一版是重头跑的还是接着跑的。
    """

    version_no = next_version_no(run_dir)
    directory = versions_dir(run_dir) / f"{VERSION_DIR_PREFIX}{version_no:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    return RunVersion(
        version_no=version_no,
        mode=mode,
        started_at=now_iso(),
        directory=directory,
    )


def finish_version(
    version: RunVersion,
    *,
    status: str,
    detail: str = "",
    files: Iterable[Path | str] = (),
    summary: Mapping[str, Any] | None = None,
) -> Path:
    """收尾一个版本：把本版产物**抄一份**进版本目录，并写版本台账。

    **抄而不是移**：顶层 ``case_rows.csv`` 始终是「当前这一版」，
    表内插件按固定路径回填；历史版本进 ``versions/`` 供事后比对。

    **跑崩了也写台账**：``status="interrupted"`` 时同样落盘 ——
    半截产物必须说清自己是半截的。
    """

    version.directory.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    for path in files:
        source = Path(path)
        if not source.is_file():
            continue
        shutil.copy2(source, version.directory / source.name)
        archived.append(source.name)

    payload: dict[str, Any] = {
        "version_no": version.version_no,
        "mode": version.mode,
        "status": status,
        "started_at": version.started_at,
        "finished_at": now_iso(),
        "detail": detail,
        "archived_files": archived,
    }
    if summary:
        payload.update(summary)
    return write_json_file(version.directory / VERSION_FILENAME, payload)
