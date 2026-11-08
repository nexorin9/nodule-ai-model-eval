"""出站端点限流：每个端点一个独立的上限，超限的请求**排队等待**，不丢弃。

**为什么要有这一层**：一次评估是几百例的批量 fan-out（任务 14），
但院内推理服务与院内模型服务各有各的承载 —— 推理端点通常按「同时几例」限流，
模型端点更贵更慢、上限更低。**一个总闸门管两个端点**会出现这种局面：
模型端点已经打满，推理端点的请求还在排队等一个根本轮不到的额度。

所以这里按**端点**分闸门：

- ``inference``：推理端点（逐例调用被评模型）
- ``model``：模型端点（金标准语义归并，任务 21 / 24 走这里）
- ``ris``：RIS 读端点
- ``research``：科研平台写回端点

每个闸门**各自**计数、各自限流、各自记录峰值 ——
「推理端点峰值 8、模型端点峰值 2」是两条独立的事实，不是同一条。

**排队不是丢弃**：超限的请求阻塞在信号量上，等前面的腾出位置；
一个都不丢。落空的请求会让这批评估凭空少几例 ——
「跑完了但少了 40 例」比「跑崩了」更难查。

**每次等待都留痕**：等过久的请求数与累计等待秒数进限流统计，
压测报告与运行日志据此说明「这轮慢在哪」。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

#: 推理端点（逐例调用被评模型）。
ENDPOINT_INFERENCE = "inference"
#: 模型端点（金标准语义归并）。
ENDPOINT_MODEL = "model"
#: RIS 读端点。
ENDPOINT_RIS = "ris"
#: 科研平台写回端点。
ENDPOINT_RESEARCH = "research"

#: 本产品认识的全部出站端点。
KNOWN_ENDPOINTS: tuple[str, ...] = (
    ENDPOINT_INFERENCE,
    ENDPOINT_MODEL,
    ENDPOINT_RIS,
    ENDPOINT_RESEARCH,
)


@dataclass
class Slot:
    """一次占用的句柄：让调用方知道这一等等了多久。"""

    endpoint: str
    waited_seconds: float = 0.0


@dataclass
class EndpointLimiter:
    """单个端点的并发闸门。

    参数
    ----
    name
        端点名（``inference`` / ``model`` / ``ris`` / ``research``）。
    max_concurrency
        这个端点允许的同时在途请求数。**至少 1**。
    """

    name: str
    max_concurrency: int
    _semaphore: threading.Semaphore = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False)
    _in_flight: int = field(init=False, default=0, repr=False)
    _acquisitions: int = field(init=False, default=0, repr=False)
    _queued: int = field(init=False, default=0, repr=False)
    _waited_seconds: float = field(init=False, default=0.0, repr=False)
    _peak_in_flight: int = field(init=False, default=0, repr=False)

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError(f"端点 {self.name} 的并发上限至少为 1")
        self._semaphore = threading.Semaphore(self.max_concurrency)
        self._lock = threading.Lock()

    # ---- 占位 ----
    @contextmanager
    def slot(self) -> Iterator[Slot]:
        """占一个额度；退出时**一定**归还（异常也还）。"""

        waited = False
        started = time.perf_counter()
        if not self._semaphore.acquire(blocking=False):
            # 额度用尽 → 排队等，不丢。
            waited = True
            self._semaphore.acquire()
        waited_seconds = time.perf_counter() - started

        with self._lock:
            self._in_flight += 1
            self._acquisitions += 1
            if waited:
                self._queued += 1
                self._waited_seconds += waited_seconds
            if self._in_flight > self._peak_in_flight:
                self._peak_in_flight = self._in_flight

        handle = Slot(endpoint=self.name, waited_seconds=waited_seconds if waited else 0.0)
        try:
            yield handle
        finally:
            with self._lock:
                self._in_flight -= 1
            self._semaphore.release()

    # ---- 统计 ----
    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def peak_in_flight(self) -> int:
        with self._lock:
            return self._peak_in_flight

    def stats(self) -> dict[str, Any]:
        """这个端点的限流统计（进压测报告与运行日志）。"""

        with self._lock:
            return {
                "endpoint": self.name,
                "max_concurrency": self.max_concurrency,
                "acquisitions": self._acquisitions,
                "queued": self._queued,
                "waited_seconds": round(self._waited_seconds, 4),
                "peak_in_flight": self._peak_in_flight,
                "in_flight": self._in_flight,
            }


class LimiterSet:
    """一批端点闸门（每个端点一个独立的上限）。"""

    def __init__(self, limiters: Mapping[str, EndpointLimiter]) -> None:
        self._limiters = dict(limiters)

    @classmethod
    def build(
        cls,
        *,
        concurrency: int,
        endpoint_limits: Mapping[str, int] | None = None,
    ) -> "LimiterSet":
        """按并发数建一套闸门。

        ``endpoint_limits`` 给出**逐端点**的上限覆盖（缺省每个端点都取
        ``concurrency``）。覆盖项只认已知的端点名 —— 写错端点名要报错，
        不能默默不生效（限流写错名字 = 限流没开）。
        """

        if concurrency < 1:
            raise ValueError("并发数至少为 1")
        overrides = dict(endpoint_limits or {})
        unknown = sorted(set(overrides) - set(KNOWN_ENDPOINTS))
        if unknown:
            raise ValueError(
                f"未知的出站端点：{'/'.join(unknown)}（可用：{'/'.join(KNOWN_ENDPOINTS)}）"
            )
        limiters: dict[str, EndpointLimiter] = {}
        for name in KNOWN_ENDPOINTS:
            cap = int(overrides.get(name, concurrency))
            if cap < 1:
                raise ValueError(f"端点 {name} 的并发上限至少为 1")
            limiters[name] = EndpointLimiter(name=name, max_concurrency=cap)
        return cls(limiters)

    def limiter(self, name: str) -> EndpointLimiter:
        """取某个端点的闸门。"""

        try:
            return self._limiters[name]
        except KeyError as exc:  # pragma: no cover - 由 build 保证不出现
            raise KeyError(f"没有这个出站端点的闸门：{name}") from exc

    def stats(self) -> dict[str, dict[str, Any]]:
        """全部端点的限流统计。"""

        return {name: limiter.stats() for name, limiter in self._limiters.items()}

    def to_dict(self) -> dict[str, Any]:
        return self.stats()
