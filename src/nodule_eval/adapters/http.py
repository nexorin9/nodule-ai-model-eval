"""出站 HTTP 会话层：超时分离、退避重试与错误码映射。

**源产品证据**：参考实现的评测脚本把重试装饰在「一次模型请求」函数上 ——
``@retry(wait=wait_exponential(multiplier=1, min=4, max=10), stop=stop_after_attempt(3))``
（``github_ref/MedRAX/quickstart.py:410``）。重试语义只服务模型调用这一个出口，
且由装饰器隐式生效、无法断言「到底试了几次」。

本产品有**三个**出站出口（RIS 读、推理写、科研平台写），退避量级必须一致且**可测**，
因此把它下沉成**统一会话层**：

- **连接超时与读取超时分离**（``timeout=(connect, read)``）。院内推理服务单例可能几十秒
  才返回，用同一个超时值会把「模型慢」误判成「连不上」。
- **5xx 与 429 退避重试**（指数退避，最小 4 秒、最多 3 次尝试）；**4xx 不重试** ——
  参数错误重试没有意义，只会把退避预算浪费在必然失败的请求上。
- 每次失败都归一成 :class:`HttpFailure`，再由**调用方给的映射函数**翻成产品错误码。
  本层不掺任何业务语义：RIS 读失败与推理失败在这里长得一样，差别只在映射函数。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests

from ..errors import NoduleEvalError

#: 退避重试的 HTTP 状态码：服务端错误与限流。
RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: 退避参数（与源产品 tenacity 的 ``wait_exponential(multiplier=1, min=4, max=10)`` 同量级）。
DEFAULT_BACKOFF_BASE = 4.0
DEFAULT_BACKOFF_MAX = 10.0

#: 最多尝试次数（含首次）。3 次仍失败才判该例 failed。
DEFAULT_MAX_ATTEMPTS = 3

#: 连接超时与读取超时的缺省值（秒）。
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 60.0

#: 错误响应体的摘录长度（避免把整段 HTML 写进日志与失败原因）。
BODY_SNIPPET_LIMIT = 500

#: 连接池的**最小**尺寸。``requests`` 的默认连接池只有 10 条连接，
#: 并发超过 10 时 urllib3 会把挤不下的连接**丢掉重建**
#: （``Connection pool is full, discarding connection``）——
#: 一次评估是几百上千例，每例都重新握手会把时间全花在 TCP 与 TLS 上，
#: 压测报告里看到的「慢」其实是连接池在漏。
MIN_POOL_SIZE = 10

#: 失败种类。
KIND_CONNECT_TIMEOUT = "connect_timeout"
KIND_READ_TIMEOUT = "read_timeout"
KIND_CONNECTION_ERROR = "connection_error"
KIND_REQUEST_ERROR = "request_error"
KIND_HTTP_STATUS = "http_status"

#: 把一次失败翻成产品错误码的映射函数。
ErrorMapper = Callable[["HttpFailure"], NoduleEvalError]


@dataclass(frozen=True)
class HttpFailure:
    """一次出站失败的归一形态。

    ``kind`` 区分「连不上」「读超时」「服务端报错」三类；``status`` 只在 HTTP 状态码
    类的失败里有值；``attempts`` 记录**已经试了几次**（供底账与错误文案核对）；
    ``connect_timeout`` / ``read_timeout`` 带上当时的超时设置 ——
    「模型慢」与「连不上」在失败原因里必须**说得清是哪一种**，
    否则信息科会去查网络，而问题其实在厂商服务。
    ``connect_timeouts`` / ``read_timeouts`` 是**两个阶段的超时次数**，
    分开计数：一次 5 次尝试里若连接阶段超时 1 次、响应阶段超时 2 次，
    光看 ``kind`` 只知道「最后一次是哪种」，看不出这轮到底卡在哪一段。
    """

    kind: str
    url: str = ""
    status: int | None = None
    body: str = ""
    attempts: int = 1
    connect_timeout: float = 0.0
    read_timeout: float = 0.0
    connect_timeouts: int = 0
    read_timeouts: int = 0

    def detail(self) -> str:
        """给错误文案用的补充说明（不含患者可识别明细）。"""

        if self.kind == KIND_CONNECT_TIMEOUT:
            return f"连接超时（{self.connect_timeout:g} 秒未建立连接）：{self.body}"
        if self.kind == KIND_READ_TIMEOUT:
            return f"读取响应超时（{self.read_timeout:g} 秒未返回）：{self.body}"
        if self.kind == KIND_CONNECTION_ERROR:
            return f"无法连接（连接被拒或域名解析失败）：{self.body}"
        if self.kind == KIND_REQUEST_ERROR:
            return f"请求层异常（TLS 握手或代理）：{self.body}"
        return f"服务端返回 HTTP {self.status}：{self.body}"


def _body_snippet(response: requests.Response) -> str:
    try:
        text = response.text or ""
    except Exception:  # pragma: no cover - 极端响应体的兜底
        return ""
    return text[:BODY_SNIPPET_LIMIT]


class HttpClient:
    """带超时分离与退避重试的出站会话。

    用 ``requests.Session`` 复用连接；**不**使用 ``urllib3.Retry``，
    因为我们需要精确控制「试了几次、退避了几秒、最后一次失败是什么」，
    这些都要落到调用底账里被事后反查。
    """

    def __init__(
        self,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.max_attempts = int(max_attempts)
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self.session = session if session is not None else requests.Session()
        self._sleep = sleep
        #: 当前连接池尺寸（``resize_pool`` 改过之后才是并发数）。
        self.pool_maxsize = MIN_POOL_SIZE

    def resize_pool(self, max_connections: int) -> int:
        """按并发数调整连接池尺寸，返回调整后的尺寸。

        **为什么引擎要调它**：``requests`` 的连接池默认只有 10 条连接，
        ``pool_block=False`` 时挤不下的连接会被 urllib3 直接丢掉重建 ——
        并发 16 跑两千例，每例重握手一次，测出来的「耗时」大半是握手的开销，
        不是推理服务慢。调大之后 ``pool_block=True``：池满了就**等**，
        而不是丢掉重来。

        **线程安全的说明**：``requests.Session`` 官方未承诺线程安全，
        但它在这一层只被当作「连接池 + 默认头」的容器使用，
        请求过程中不改它的属性（Cookie 罐本产品不用）；
        urllib3 的连接池自身是线程安全的。真正需要串行化的地方
        （调用底账追加、run 状态推进）由引擎各自的锁守住。
        """

        size = max(int(max_connections), MIN_POOL_SIZE)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=size,
            pool_maxsize=size,
            pool_block=True,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.pool_maxsize = size
        return size

    def backoff_delay(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的退避秒数（指数退避，封顶 ``backoff_max``）。"""

        return min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))

    def request(
        self,
        method: str,
        url: str,
        *,
        error_mapper: ErrorMapper,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        params: Mapping[str, Any] | None = None,
        stats: dict[str, int] | None = None,
    ) -> requests.Response:
        """发一次出站请求，失败按策略重试，仍失败则抛映射后的产品错误。

        - 2xx / 3xx：直接返回响应
        - 4xx：**不重试**，立即抛映射后的错误（参数错误重试没有意义）
        - 5xx / 429：指数退避重试，最多 ``max_attempts`` 次尝试
        - 连接超时 / 读取超时 / 连接失败：按可重试处理，退避后重试

        ``stats``
            可选的**计数回收口**。成功时把 ``attempts`` / ``connect_timeouts`` /
            ``read_timeouts`` 写进去 —— **失败时这些计数本来就在异常里**
            （``HttpFailure``），成功路径却没人说得清「这一轮打出去几次」：
            前两次 5xx、第三次才成功，光看返回值只知道「成功了」，
            审计时会以为只调了一次。给了 ``stats`` 就如实报出去。
        """

        last_failure: HttpFailure | None = None
        connect_timeouts = 0
        read_timeouts = 0

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=dict(headers) if headers else None,
                    json=json_body,
                    params=dict(params) if params else None,
                    timeout=(self.connect_timeout, self.read_timeout),
                )
            except requests.exceptions.ConnectTimeout as exc:
                connect_timeouts += 1
                last_failure = HttpFailure(
                    KIND_CONNECT_TIMEOUT,
                    url=url,
                    body=str(exc),
                    attempts=attempt,
                    connect_timeout=self.connect_timeout,
                    read_timeout=self.read_timeout,
                    connect_timeouts=connect_timeouts,
                    read_timeouts=read_timeouts,
                )
            except requests.exceptions.ReadTimeout as exc:
                read_timeouts += 1
                last_failure = HttpFailure(
                    KIND_READ_TIMEOUT,
                    url=url,
                    body=str(exc),
                    attempts=attempt,
                    connect_timeout=self.connect_timeout,
                    read_timeout=self.read_timeout,
                    connect_timeouts=connect_timeouts,
                    read_timeouts=read_timeouts,
                )
            except requests.exceptions.ConnectionError as exc:
                last_failure = HttpFailure(
                    KIND_CONNECTION_ERROR,
                    url=url,
                    body=str(exc),
                    attempts=attempt,
                    connect_timeout=self.connect_timeout,
                    read_timeout=self.read_timeout,
                    connect_timeouts=connect_timeouts,
                    read_timeouts=read_timeouts,
                )
            except requests.exceptions.RequestException as exc:
                # TLS 握手失败、代理异常等：重试没有意义，直接失败。
                raise error_mapper(
                    HttpFailure(
                        KIND_REQUEST_ERROR,
                        url=url,
                        body=str(exc),
                        attempts=attempt,
                        connect_timeout=self.connect_timeout,
                        read_timeout=self.read_timeout,
                        connect_timeouts=connect_timeouts,
                        read_timeouts=read_timeouts,
                    )
                ) from exc
            else:
                if response.status_code in RETRYABLE_STATUSES:
                    last_failure = HttpFailure(
                        KIND_HTTP_STATUS,
                        url=url,
                        status=response.status_code,
                        body=_body_snippet(response),
                        attempts=attempt,
                        connect_timeout=self.connect_timeout,
                        read_timeout=self.read_timeout,
                        connect_timeouts=connect_timeouts,
                        read_timeouts=read_timeouts,
                    )
                elif response.status_code >= 400:
                    # 4xx 不重试。
                    raise error_mapper(
                        HttpFailure(
                            KIND_HTTP_STATUS,
                            url=url,
                            status=response.status_code,
                            body=_body_snippet(response),
                            attempts=attempt,
                            connect_timeout=self.connect_timeout,
                            read_timeout=self.read_timeout,
                            connect_timeouts=connect_timeouts,
                            read_timeouts=read_timeouts,
                        )
                    ) from None
                else:
                    if stats is not None:
                        stats["attempts"] = attempt
                        stats["connect_timeouts"] = connect_timeouts
                        stats["read_timeouts"] = read_timeouts
                    return response

            if attempt >= self.max_attempts:
                break
            self._sleep(self.backoff_delay(attempt))

        assert last_failure is not None  # 循环至少执行一次
        raise error_mapper(last_failure) from None
