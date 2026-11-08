"""并发写运行状态文件的回归测试（任务 48 采截图时撞出来的真问题）。

**问题**：``write_status`` 原来用**固定**的临时文件名（``engine_status.json.tmp``）。
并发跑（``--concurrency 4``）时几个线程同时刷进度：A 线程刚把临时文件替换成正式
文件，B 线程的 ``os.replace`` 就找不到源文件了 → ``FileNotFoundError`` →
状态栏停在半截、表格里看不到进度（终端会打一行 ``[WARN] 运行状态文件写不进去``）。

**修法**：临时文件名带进程号与线程号（唯一），再 ``os.replace`` 原子替换；
引擎侧把写盘也放进锁里，保证落到盘上的永远是最新的那份。

本测试断言三件事：

1. 并发写不抛错、正式文件始终是**合法 JSON**（读者不会读到半截）；
2. 临时文件名**唯一**（不同线程不共用一个）；
3. 不留残���的临时文件。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nodule_eval.sheets.status import (  # noqa: E402
    RunStatus,
    read_status,
    status_path,
    write_status,
)


def _status(processed: int, total: int = 200) -> RunStatus:
    return RunStatus(
        run_id="demo-status-concurrency",
        state="running",
        processed=processed,
        total=total,
        current_accession_no=f"DEMO-{processed:04d}",
    )


def test_concurrent_writes_keep_the_file_valid(tmp_path: Path) -> None:
    """并发写：正式文件始终是合法 JSON，且读得到进度。"""

    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for step in range(20):
                write_status(tmp_path, _status(index * 20 + step))
        except BaseException as exc:  # noqa: BLE001 - 测试要把异常记下来
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"并发写状态文件报错：{errors!r}"
    payload = json.loads(status_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["run_id"] == "demo-status-concurrency"
    assert 0 <= payload["processed"] <= payload["total"]
    assert read_status(status_path(tmp_path)) is not None


def test_temp_file_name_is_unique_per_writer(tmp_path: Path) -> None:
    """临时文件名带线程号 —— 两个**同时**在写的线程不会共用一个临时文件。"""

    seen: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    original = Path.write_text

    def spy(self: Path, *args: object, **kwargs: object) -> int:
        if self.name.endswith(".tmp"):
            with lock:
                seen.append(self.name)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    def writer(processed: int) -> None:
        barrier.wait(timeout=5)
        write_status(tmp_path, _status(processed))

    Path.write_text = spy  # type: ignore[method-assign]
    try:
        threads = [
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=writer, args=(2,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    finally:
        Path.write_text = original  # type: ignore[method-assign]

    assert len(seen) == 2, f"临时文件应当各写各的：{seen!r}"
    assert seen[0] != seen[1], f"临时文件名撞车了：{seen!r}"
    assert all(".tmp" in name for name in seen)


def test_no_temp_file_left_behind(tmp_path: Path) -> None:
    """写完不留残骸。"""

    write_status(tmp_path, _status(7))
    leftovers = [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == [], f"残留临时文件：{leftovers!r}"
