"""院内 RIS 检查清单的出站适配器。

RIS 是**权威数据源**：病例清单与图像引用只认它。本适配器落的是
``spec.md`` 里那条读接口的字段规范与凭据位 —— 不是「预留接口」::

    GET {RIS_BASE_URL}/api/v1/imaging-studies?modality=DX&from=..&to=..&cursor=..
    Authorization: Bearer $RIS_API_TOKEN
    -> {"items": [{"accession_no", "study_uid", "modality", "study_date",
                   "report_status", "image_ref"}], "next_cursor": ...}

**游标翻页**：``next_cursor`` 非空就带上 ``cursor`` 继续拉，直到取空为止。
只取一页就把剩下的病例丢掉，等于把「模型漏判」和「病例没进批次」混成一样。

**失败不静默返回空清单**：拉不到就是 ``RIS_READ_FAILED``，绝不拿空清单继续跑。
失败信息里带**页码与游标位置** —— 「第 3 页（cursor=xxx）读失败」才查得动，
只说「RIS 读失败」等于没说。

**字段级校验**（落在 :func:`nodule_eval.caseset.parse_ris_items`）：

- ``accession_no`` 与 ``study_uid`` 必填非空 —— 缺一条就是凭空少一例样本，
  直接判失败，**不静默丢行**；
- ``modality`` 明确不是请求模态的记录（CT / MR / US …）**直接过滤并计数** ——
  首版范围钉死在胸片，把 CT 混进来会把敏感度算歪；但过滤掉的条数必须看得见；
- ``modality`` 缺失（对端没给）**不丢**、计入「模态未知」计数 —— 缺字段不等于
  「不是胸片」，按缺字段把它丢掉才是真正的静默丢行；
- ``image_ref`` 缺失时**留空**，由后续按 ``accession_no`` 从 RIS 兜底取
  （本任务只负责如实记录，不在这里瞎编一个引用）。

**源产品证据**：参考实现的病例清单抓取是「翻页取号 + 每页条数对不上就告警」
（``github_ref/MedRAX/data/get_cases.py:29-38`` 的 ``total_pages`` 循环与
``len(numbers) != 9`` 告警）。本产品把同一机制改写到 RIS 的游标翻页上：
翻页到 ``next_cursor`` 为空为止，并**把过滤掉的条数显式计数**（原实现只打印一句
告警，本产品让计数进产物与汇总行）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping, Sequence

from ..caseset import RISItem, map_ris_failure, parse_ris_items
from ..config import Config, join_url
from ..errors import ErrorCode, NoduleEvalError
from .http import HttpClient, HttpFailure

#: RIS 检查清单端点路径（相对 ``RIS_BASE_URL``）。
DEFAULT_RIS_PATH = "/api/v1/imaging-studies"

#: 分页游标参数名。
CURSOR_PARAM = "cursor"

#: 单页最多拉多少条（防御对端返回超大页）。
DEFAULT_PAGE_LIMIT = 500

#: 日期参数格式（``spec.md`` 规定 ``from`` / ``to`` 形如 ``2026-03-01``）。
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 首版范围钉死在胸片，默认按 DX 过滤。
DEFAULT_MODALITY = "DX"


@dataclass(frozen=True)
class RISPage:
    """一页检查清单。

    ``items`` 保留**原始记录**（字段级校验前的形态），``parsed`` 是校验后的
    :class:`~nodule_eval.caseset.RISItem`。两者都留着是为了让「对端到底返回了什么」
    与「我们认了什么」可以分别核对 —— 静默丢行最怕的就是只留下后者。
    """

    items: tuple[Any, ...]
    next_cursor: str = ""
    parsed: tuple[RISItem, ...] = ()
    page_no: int = 1
    cursor: str = ""

    @property
    def has_more(self) -> bool:
        return bool(self.next_cursor)


@dataclass(frozen=True)
class RISReadResult:
    """一次 RIS 清单读取的结果（含过滤与计数）。

    计数不是装饰 —— 「这一批为什么少了 3 条」必须能从这里一眼看出来，
    否则只能回头翻 RIS 日志。
    """

    items: tuple[RISItem, ...]
    raw_count: int = 0
    filtered_non_modality: int = 0
    unknown_modality: int = 0
    missing_image_ref: int = 0
    pages: int = 0

    @property
    def total_count(self) -> int:
        """通过字段级校验、且模态符合要求的记录数。"""

        return len(self.items)

    def counts(self) -> dict[str, int]:
        """计数视图（落进装配清单与汇总行用）。"""

        return {
            "raw_count": self.raw_count,
            "total_count": self.total_count,
            "filtered_non_modality": self.filtered_non_modality,
            "unknown_modality": self.unknown_modality,
            "missing_image_ref": self.missing_image_ref,
            "pages": self.pages,
        }


def _page_prefix(page_no: int, cursor: str) -> str:
    """页位置前缀：``第 2 页（cursor=abc）``。"""

    where = f"第 {page_no} 页"
    if cursor:
        where += f"（cursor={cursor}）"
    return where


def _with_page_context(
    exc: NoduleEvalError,
    *,
    page_no: int,
    cursor: str,
) -> NoduleEvalError:
    """给已有错误补上页位置（错误码与原始状态码不变）。"""

    return NoduleEvalError(
        exc.code,
        detail=f"{_page_prefix(page_no, cursor)}：{exc.detail}",
        http_status=exc.http_status,
        **exc.fields,
    )


def _page_error_mapper(page_no: int, cursor: str) -> Callable[[HttpFailure], NoduleEvalError]:
    """带页位置的一次性错误映射（HTTP 层失败 → ``RIS_READ_FAILED``）。"""

    def mapper(failure: HttpFailure) -> NoduleEvalError:
        base = map_ris_failure(failure)
        return NoduleEvalError(
            base.code,
            detail=f"{_page_prefix(page_no, cursor)}：{base.detail}",
            http_status=base.http_status,
        )

    return mapper


def _validate_date(value: str, name: str) -> str:
    """校验日期参数（``from`` / ``to``），格式不对即指名报错。"""

    raw = (value or "").strip()
    if not raw:
        return ""
    if not DATE_PATTERN.match(raw):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"日期参数 {name} 须为 YYYY-MM-DD 格式（实得 {raw!r}）",
        )
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"日期参数 {name} 不是合法日期（实得 {raw!r}）",
        ) from None
    return raw


def _coerce_item(raw: Any) -> Any:
    """把 ``accession_no`` / ``study_uid`` 的数字形态归一成字符串。

    有些 RIS 把检查号与影像标识以 JSON 数字返回。直接按字符串校验会误判成
    「缺字段」，而这是**对端给对了、我们认错了** —— 必须归一而不是报错。
    """

    if not isinstance(raw, Mapping):
        return raw
    coerced = dict(raw)
    for key in ("accession_no", "study_uid"):
        value = coerced.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            coerced[key] = str(value)
        elif isinstance(value, float) and value.is_integer():
            coerced[key] = str(int(value))
    return coerced


class RISAdapter:
    """院内 RIS 检查清单的出站客户端。"""

    def __init__(
        self,
        config: Config,
        *,
        client: HttpClient | None = None,
        base_url: str | None = None,
        endpoint_path: str = DEFAULT_RIS_PATH,
    ) -> None:
        self._config = config
        self._client = client if client is not None else HttpClient()
        base = base_url if base_url is not None else config.ris_base_url
        self.url = join_url(base, endpoint_path)

    # ---- 单页 ----
    def fetch_page(
        self,
        *,
        modality: str = DEFAULT_MODALITY,
        date_from: str = "",
        date_to: str = "",
        cursor: str = "",
        page_no: int = 1,
    ) -> RISPage:
        """拉一页检查清单（原始记录 + 字段级校验后的记录）。

        出站失败一律抛 ``RIS_READ_FAILED``，并在错误信息里带上**页码与游标**；
        解析失败同样抛错并指明**缺哪一条的哪个字段**。
        """

        params: dict[str, Any] = {"modality": modality}
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to
        if cursor:
            params[CURSOR_PARAM] = cursor

        response = self._client.request(
            "GET",
            self.url,
            error_mapper=_page_error_mapper(page_no, cursor),
            headers=self._config.ris_headers,
            params=params,
        )

        try:
            payload = response.json()
        except ValueError as exc:
            raise NoduleEvalError(
                ErrorCode.RIS_READ_FAILED,
                detail=f"{_page_prefix(page_no, cursor)}：RIS 响应不是合法 JSON：{exc}",
                http_status=response.status_code,
            ) from exc

        return self.parse_response(
            payload,
            page_no=page_no,
            cursor=cursor,
            http_status=response.status_code,
        )

    # ---- 响应解析（独立成一处，出站与契约回放共用）----
    def parse_response(
        self,
        payload: Any,
        *,
        page_no: int = 1,
        cursor: str = "",
        http_status: int | None = None,
    ) -> RISPage:
        """按字段规范解析 RIS 响应（**出站与契约回放走的是同一段代码**）。

        ``spec.md`` 规定的响应体是 ``{"items": [...], "next_cursor": ...}``，
        两个字段**都必须在场**：

        - ``items`` 缺 → 少了一整批样本，报 ``RIS_READ_FAILED`` 并指名；
        - ``next_cursor`` 缺 → **判失败，不当「没有下一页」**。
          对端没给这个字段时，翻页会在这里停住 —— 后面几页的病例会被
          **静默丢掉**，而清单少几条 nobody 会察觉（尤其当少的那几例
          恰好是漏诊例时）。字段在场但值为空串/null 才是「没有下一页」。

        逐条记录的必填字段校验交给 :func:`nodule_eval.caseset.parse_ris_items`
        （缺 ``accession_no`` / ``study_uid`` 一律指名报错，不静默丢行）。
        """

        if not isinstance(payload, Mapping):
            raise NoduleEvalError(
                ErrorCode.RIS_READ_FAILED,
                detail=(
                    f"{_page_prefix(page_no, cursor)}："
                    f"RIS 响应顶层不是对象（实得 {type(payload).__name__}）"
                ),
                http_status=http_status,
            )

        items = payload.get("items")
        if items is None:
            raise NoduleEvalError(
                ErrorCode.RIS_READ_FAILED,
                detail=f"{_page_prefix(page_no, cursor)}：RIS 响应缺少字段 items",
                http_status=http_status,
            )
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise NoduleEvalError(
                ErrorCode.RIS_READ_FAILED,
                detail=f"{_page_prefix(page_no, cursor)}：RIS 响应的字段 items 不是数组",
                http_status=http_status,
            )

        if "next_cursor" not in payload:
            raise NoduleEvalError(
                ErrorCode.RIS_READ_FAILED,
                detail=(
                    f"{_page_prefix(page_no, cursor)}："
                    "RIS 响应缺少字段 next_cursor（不能当「没有下一页」处理 —— "
                    "后面几页的病例会被静默丢掉）"
                ),
                http_status=http_status,
            )
        next_cursor = payload.get("next_cursor") or ""
        raw_items = tuple(_coerce_item(item) for item in items)

        try:
            parsed = parse_ris_items({"items": list(raw_items), "next_cursor": next_cursor})
        except NoduleEvalError as exc:
            raise _with_page_context(exc, page_no=page_no, cursor=cursor) from exc

        return RISPage(
            items=raw_items,
            next_cursor=str(next_cursor),
            parsed=parsed,
            page_no=page_no,
            cursor=cursor,
        )

    # ---- 整份清单 ----
    def fetch_studies(
        self,
        *,
        modality: str = DEFAULT_MODALITY,
        date_from: str = "",
        date_to: str = "",
        max_pages: int = DEFAULT_PAGE_LIMIT,
    ) -> RISReadResult:
        """按游标翻页把整份清单取完，并做字段级校验与模态过滤。

        **失败一律抛错**（``RIS_READ_FAILED``），绝不返回空清单当作「没有病例」；
        **成功但为空**是合法答案（该区间确实没有符合条件的检查），返回 ``total_count=0``
        并把计数一并给出，由装配环节决定是否要人工确认。

        模态过滤口径：请求模态之外的记录（CT / MR …）**直接过滤并计数**；
        对端没给 ``modality`` 的记录**保留并计入「模态未知」** —— 缺字段不等于
        不是胸片，按缺字段丢掉才是真正的静默丢行。
        """

        date_from = _validate_date(date_from, "from")
        date_to = _validate_date(date_to, "to")
        if date_from and date_to and date_from > date_to:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"日期区间非法：from={date_from} 晚于 to={date_to}",
            )

        wanted = (modality or "").strip().upper()
        collected: list[RISItem] = []
        raw_count = 0
        filtered_non_modality = 0
        unknown_modality = 0
        missing_image_ref = 0
        cursor = ""
        pages = 0

        for page_no in range(1, max_pages + 1):
            page = self.fetch_page(
                modality=modality,
                date_from=date_from,
                date_to=date_to,
                cursor=cursor,
                page_no=page_no,
            )
            pages += 1
            raw_count += len(page.items)

            for item in page.parsed:
                declared = (item.modality or "").strip().upper()
                if declared and wanted and declared != wanted:
                    filtered_non_modality += 1
                    continue
                if not declared:
                    unknown_modality += 1
                collected.append(item)
                if not item.image_ref:
                    missing_image_ref += 1

            if not page.has_more:
                break
            cursor = page.next_cursor

        return RISReadResult(
            items=tuple(collected),
            raw_count=raw_count,
            filtered_non_modality=filtered_non_modality,
            unknown_modality=unknown_modality,
            missing_image_ref=missing_image_ref,
            pages=pages,
        )

    # ---- 兼容入口 ----
    def fetch_all(
        self,
        *,
        modality: str = DEFAULT_MODALITY,
        date_from: str = "",
        date_to: str = "",
        max_pages: int = DEFAULT_PAGE_LIMIT,
    ) -> tuple[Any, ...]:
        """按游标翻页把整个清单取完（**原始记录**，不做字段级校验）。

        保留此入口是为了让「对端到底返回了什么」可单独核对；
        产品主路径请用 :meth:`fetch_studies`。
        """

        collected: list[Any] = []
        cursor = ""
        for page_no in range(1, max_pages + 1):
            page = self.fetch_page(
                modality=modality,
                date_from=date_from,
                date_to=date_to,
                cursor=cursor,
                page_no=page_no,
            )
            collected.extend(page.items)
            if not page.has_more:
                break
            cursor = page.next_cursor
        return tuple(collected)
