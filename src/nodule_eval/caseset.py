"""病例集装配与实体绑定的最小切片。

RIS 是病例与图像引用的**权威数据源**：检查号（``accession_no``）→ 影像标识
（``study_uid``）的绑定关系只认 RIS。同一个检查号命中多条影像 / 多个时间点时
**绝不自动取第一条**，而是挂起人工判定（``AMBIGUOUS_BINDING``）——
自动取第一条会让「模型判漏了」这件事被归到一条根本不是本次检查的影像上，
直接把敏感度算歪。

本文件先落「解析 + 绑定」这一层：替身场景断言（任务 5）需要它才能证明
「一对多 → 挂起而不是自动绑定」。RIS 的 HTTP 读取与游标翻页在任务 7 落，
病例集按检查号幂等落盘在任务 9 落，绑定关系落盘与指标隔离在任务 10 落。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .adapters.http import HttpFailure
from .contracts import CaseInput
from .errors import ErrorCode, NoduleEvalError, error_text

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注，避免与 adapters.ris 的循环导入
    from .adapters.ris import RISAdapter

#: RIS 检查清单里每条记录必填的字段。
RIS_REQUIRED_FIELDS: tuple[str, ...] = ("accession_no", "study_uid")


def map_ris_failure(failure: HttpFailure) -> NoduleEvalError:
    """把 RIS 读取失败翻成产品错误码。

    RIS 读失败**一律**是 ``RIS_READ_FAILED``（可重试）：清单拉不到就是没拉到，
    绝不用空清单继续跑 —— 那样会静默地把一批病例当成「模型全漏判」。
    """

    return NoduleEvalError(
        ErrorCode.RIS_READ_FAILED,
        detail=failure.detail(),
        http_status=failure.status,
    )


@dataclass(frozen=True)
class RISItem:
    """RIS 检查清单里的一条影像记录。"""

    accession_no: str
    study_uid: str
    modality: str = ""
    study_date: str = ""
    report_status: str = ""
    image_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "study_uid": self.study_uid,
            "modality": self.modality,
            "study_date": self.study_date,
            "report_status": self.report_status,
            "image_ref": self.image_ref,
        }


def _ris_error(detail: str) -> NoduleEvalError:
    return NoduleEvalError(ErrorCode.RIS_READ_FAILED, detail=detail)


def parse_ris_items(payload: Any) -> tuple[RISItem, ...]:
    """解析 RIS 检查清单响应 ``{"items": [...], "next_cursor": ...}``。

    ``accession_no`` 与 ``study_uid`` 必填非空；缺字段即报 ``RIS_READ_FAILED``
    并写明缺哪一条的哪个字段 —— **不静默丢行**，丢一条就等于凭空少一例样本。
    """

    if not isinstance(payload, Mapping):
        raise _ris_error(f"响应顶层不是对象（实得 {type(payload).__name__}）")

    raw_items = payload.get("items")
    if raw_items is None:
        raise _ris_error("响应缺少字段 items")
    if isinstance(raw_items, (str, bytes)) or not isinstance(raw_items, Sequence):
        raise _ris_error("响应的字段 items 不是数组")

    items: list[RISItem] = []
    for index, item in enumerate(raw_items):
        where = f"第 {index + 1} 条"
        if not isinstance(item, Mapping):
            raise _ris_error(f"{where}不是对象（实得 {type(item).__name__}）")
        values: dict[str, str] = {}
        for field in RIS_REQUIRED_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _ris_error(f"{where}缺少字段 {field}（必填且不得为空）")
            values[field] = value.strip()

        image_ref_raw = item.get("image_ref")
        image_ref: str | None = None
        if isinstance(image_ref_raw, str) and image_ref_raw.strip():
            image_ref = image_ref_raw.strip()

        items.append(
            RISItem(
                accession_no=values["accession_no"],
                study_uid=values["study_uid"],
                modality=str(item.get("modality") or ""),
                study_date=str(item.get("study_date") or ""),
                report_status=str(item.get("report_status") or ""),
                image_ref=image_ref,
            )
        )

    return tuple(items)


def bind_accession(
    items: Sequence[RISItem],
    accession_no: str,
    *,
    modality: str = "DX",
) -> RISItem:
    """把检查号绑到唯一一条影像上；命中多条即挂起人工判定。

    - 命中零条：报 ``CONTRACT_INVALID``（表里写了这个检查号，RIS 里却没有 —— 契约不符）
    - 命中多条且 ``study_uid`` 不唯一：报 ``AMBIGUOUS_BINDING``，
      **绝不自动取第一条**，该例由调用方挂起为 ``needs_review``、不进指标分母
    - 命中多条但 ``study_uid`` 相同（同一影像的重复行）：算唯一绑定
    """

    matches = [
        item
        for item in items
        if item.accession_no == accession_no and (not modality or item.modality == modality)
    ]

    if not matches:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"RIS 清单里找不到检查号 {accession_no}（modality={modality or '不限'}）",
        )

    distinct_uids = {item.study_uid for item in matches}
    if len(distinct_uids) > 1:
        raise NoduleEvalError(
            ErrorCode.AMBIGUOUS_BINDING,
            accession_no=accession_no,
            detail=(
                f"命中 {len(matches)} 条影像（{len(distinct_uids)} 个 study_uid），"
                f"需人工判定"
            ),
        )

    return matches[0]


# --------------------------------------------------------------------------
# 病例集装配（任务 9）
# --------------------------------------------------------------------------
#
# **源产品证据**：参考实现按 ``case_id`` 建目录落图，已存在则跳过
# （``github_ref/MedRAX/data/figures.py:49-57`` 的 ``save_figure``：
# ``if output_path.exists(): return``），病例号本身则来自翻页抓取
# （``github_ref/MedRAX/data/get_cases.py:29-41``）。
#
# 本产品把同一机制改写到院内 RIS 上：
#
# - **来源**从 Eurorad 换成院内 RIS（``adapters/ris.py`` 的游标翻页）；
# - **落盘单位**从「一张图」换成「一例」——``cases/<accession_no>/`` 目录里放
#   ``study_uid`` / ``image_ref`` / 装配时间；
# - **幂等语义原样保留**：已存在的检查号**跳过**，既不重写目录，也不再为它
#   多打一次 RIS。一次装配里所有检查号都已装配过时，**一次 RIS 都不打**。
#
# 幂等不是洁癖：装配是「上游 → 下游」的一次性固化，重复装配会把已经评审过的
# 影像引用改掉，事后对不上「这批结论跑的是哪张片」。

#: 病例集根目录的缺省名（相对当前工作目录）。
DEFAULT_CASES_ROOT = "cases"

#: 单例装配目录内的文件名。
CASE_FILE_FILENAME = "case.json"

#: 装配清单文件名（落在 ``runs/<run_id>/`` 下）。
CASESET_MANIFEST_FILENAME = "caseset_manifest.json"

#: 装配来源标记：来自院内 RIS。
SOURCE_RIS = "ris"

#: 引用来源标记：评估表自带。
SOURCE_SHEET = "sheet"

#: 引用来源标记：从已装配的病例集兜底取。
SOURCE_CASESET = "caseset"

#: 引用来源标记：两处都取不到。
SOURCE_UNRESOLVED = "unresolved"


def _now_iso() -> str:
    from .audit.toolcall import now_iso

    return now_iso()


def safe_accession_component(accession_no: str) -> str:
    """把检查号校验成**可安全用作目录名**的一段路径。

    检查号来自 RIS 与评估表，是外部输入。直接拿它拼路径，一个 ``../..``
    就能把装配目录写到病例集根之外去 —— 那等于把院内产物落到任意位置。
    非法就报错并**指名该检查号**，不静默改写、不悄悄跳过。
    """

    raw = (accession_no or "").strip()
    if not raw:
        raise NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail="检查号为空")
    if raw in {".", ".."} or "/" in raw or "\\" in raw or "\x00" in raw:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"检查号 {raw!r} 含非法路径字符（/ \\ . .. 等），"
                f"不能作为病例集目录名"
            ),
        )
    return raw


@dataclass(frozen=True)
class AssembledCase:
    """一例已装配的病例（``cases/<accession_no>/case.json`` 的内容）。"""

    accession_no: str
    study_uid: str
    image_ref: str | None = None
    modality: str = ""
    study_date: str = ""
    report_status: str = ""
    assembled_at: str = ""
    source: str = SOURCE_RIS

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "study_uid": self.study_uid,
            "image_ref": self.image_ref,
            "modality": self.modality,
            "study_date": self.study_date,
            "report_status": self.report_status,
            "assembled_at": self.assembled_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AssembledCase | None":
        """从 ``case.json`` 还原；缺关键字段返回 ``None``（当作未装配）。"""

        accession_no = payload.get("accession_no")
        study_uid = payload.get("study_uid")
        if not isinstance(accession_no, str) or not accession_no.strip():
            return None
        if not isinstance(study_uid, str) or not study_uid.strip():
            return None
        image_ref = payload.get("image_ref")
        return cls(
            accession_no=accession_no.strip(),
            study_uid=study_uid.strip(),
            image_ref=image_ref.strip() if isinstance(image_ref, str) and image_ref.strip() else None,
            modality=str(payload.get("modality") or ""),
            study_date=str(payload.get("study_date") or ""),
            report_status=str(payload.get("report_status") or ""),
            assembled_at=str(payload.get("assembled_at") or ""),
            source=str(payload.get("source") or SOURCE_RIS),
        )


@dataclass(frozen=True)
class CasesetAssembly:
    """一次装配的结果与计数。"""

    run_id: str
    cases_root: str
    source: str
    requested: tuple[str, ...] = ()
    assembled: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    ambiguous: tuple[str, ...] = ()
    damaged_dirs: tuple[str, ...] = ()
    ris_counts: dict[str, int] = field(default_factory=dict)
    manifest_path: str = ""
    generated_at: str = ""

    def counts(self) -> dict[str, int]:
        """计数视图（装配清单与终端输出共用）。"""

        return {
            "requested": len(self.requested),
            "assembled": len(self.assembled),
            "skipped": len(self.skipped),
            "unresolved": len(self.unresolved),
            "ambiguous": len(self.ambiguous),
            "damaged_dirs": len(self.damaged_dirs),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "cases_root": self.cases_root,
            "source": self.source,
            "generated_at": self.generated_at,
            "requested": list(self.requested),
            "assembled": list(self.assembled),
            "skipped": list(self.skipped),
            "unresolved": list(self.unresolved),
            "ambiguous": list(self.ambiguous),
            "damaged_dirs": list(self.damaged_dirs),
            "counts": self.counts(),
            "ris_counts": dict(self.ris_counts),
            "note": (
                "requested = 本轮纳入的检查号（未指定时为 RIS 清单里的全部）；"
                "unresolved = RIS 清单（按 modality 过滤后）里找不到该检查号；"
                "ambiguous = 同一检查号命中多条影像，留给绑定层按人工判定处理"
            ),
        }


@dataclass(frozen=True)
class CaseResolution:
    """评估表病例的引用解析结果。"""

    cases: tuple[CaseInput, ...] = ()
    resolved_from_caseset: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.cases),
            "resolved_from_caseset": len(self.resolved_from_caseset),
            "unresolved": len(self.unresolved),
        }


def index_assembled_cases(cases_root: Path | str) -> tuple[dict[str, AssembledCase], list[str]]:
    """扫一遍病例集根目录，返回（已装配的检查号 → 病例, 损坏的目录名）。

    **目录存在但内容读不出来**（上一次装配被中断、或文件被手改坏）时，
    该目录**不算已装配** —— 下一次装配会把它补齐，而不是因为「目录在」
    就跳过、留一个永远缺引用的空壳。这份损坏清单会进装配清单，
    不静默吞掉。
    """

    root = Path(cases_root)
    assembled: dict[str, AssembledCase] = {}
    damaged: list[str] = []
    if not root.is_dir():
        return assembled, damaged

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        case_file = entry / CASE_FILE_FILENAME
        if not case_file.is_file():
            damaged.append(entry.name)
            continue
        try:
            payload = json.loads(case_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            damaged.append(entry.name)
            continue
        if not isinstance(payload, Mapping):
            damaged.append(entry.name)
            continue
        case = AssembledCase.from_dict(payload)
        if case is None:
            damaged.append(entry.name)
            continue
        assembled[case.accession_no] = case

    return assembled, damaged


def _write_case(cases_root: Path, case: AssembledCase) -> Path:
    """把一例落盘到 ``cases/<accession_no>/case.json``。"""

    target_dir = cases_root / case.accession_no
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / CASE_FILE_FILENAME
    target.write_text(
        json.dumps(case.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def _write_manifest(
    runs_dir: Path | str,
    assembly: CasesetAssembly,
) -> Path:
    """落装配清单 ``runs/<run_id>/caseset_manifest.json``。"""

    run_dir = Path(runs_dir) / assembly.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / CASESET_MANIFEST_FILENAME
    target.write_text(
        json.dumps(assembly.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def assemble_caseset(
    adapter: "RISAdapter",
    cases_root: Path | str,
    *,
    run_id: str,
    requested: Sequence[str] | None = None,
    modality: str = "DX",
    date_from: str = "",
    date_to: str = "",
    runs_dir: Path | str = "runs",
) -> CasesetAssembly:
    """装配本院病例集：RIS 取检查清单 → 按 ``accession_no`` 幂等落盘。

    **幂等**：已存在的检查号**跳过** —— 既不重写它的装配目录，也不为它多打
    一次 RIS。二次装配同一批检查号时，**一次 RIS 都不发**（没有待装配项
    就直接收工），目录数量与内容一字不变。

    **「已装配」与「未解析」是两回事**：RIS 里找不到的检查号（``unresolved``）
    每次装配都会**再问一次 RIS** —— 它可能只是还没进 RIS（比如报告还没出），
    悄悄跳过等于让这一例永远装不上。所以零请求的保证只覆盖**已装配**的检查号。

    ``requested`` 给出时只装配这几例；缺省装配 RIS 清单里的全部。

    **一对多不自动取第一条**：同一检查号命中多条影像（多时间点）时，
    该例**不装配**，进 ``ambiguous`` 清单交给绑定层按人工判定处理 ——
    自动取第一条会让「模型判漏了」归到一条根本不是本次检查的影像上。

    **RIS 里没有的检查号**进 ``unresolved``，不静默丢弃（少一例样本 =
    凭空少一个分母）。
    """

    root = Path(cases_root)
    root.mkdir(parents=True, exist_ok=True)

    requested_list: tuple[str, ...] | None = None
    if requested is not None:
        requested_list = tuple(safe_accession_component(item) for item in requested)

    existing, damaged = index_assembled_cases(root)

    assembled: list[str] = []
    skipped: list[str] = []
    unresolved: list[str] = []
    ambiguous: list[str] = []
    ris_counts: dict[str, int] = {}

    # ---- 二次装配：全部已存在 → 一次 RIS 都不打 ----
    if requested_list is not None and not [
        acc for acc in requested_list if acc not in existing
    ]:
        skipped.extend(requested_list)
        assembly = CasesetAssembly(
            run_id=run_id,
            cases_root=str(root),
            source=SOURCE_RIS,
            requested=requested_list,
            assembled=tuple(assembled),
            skipped=tuple(skipped),
            unresolved=tuple(unresolved),
            ambiguous=tuple(ambiguous),
            damaged_dirs=tuple(damaged),
            ris_counts=ris_counts,
            generated_at=_now_iso(),
        )
        manifest_path = _write_manifest(runs_dir, assembly)
        return _with_manifest(assembly, manifest_path)

    # ---- 需要 RIS 才能补齐的部分 ----
    result = adapter.fetch_studies(
        modality=modality, date_from=date_from, date_to=date_to
    )
    ris_counts = result.counts()

    by_accession: dict[str, list[RISItem]] = {}
    for item in result.items:
        by_accession.setdefault(item.accession_no, []).append(item)

    targets = list(requested_list) if requested_list is not None else list(by_accession)
    assembled_at = _now_iso()

    for accession_no in targets:
        if accession_no in existing:
            skipped.append(accession_no)
            continue

        matches = by_accession.get(accession_no, [])
        if not matches:
            unresolved.append(accession_no)
            continue

        distinct_uids = {match.study_uid for match in matches}
        if len(distinct_uids) > 1:
            ambiguous.append(accession_no)
            continue

        item = matches[0]
        _write_case(
            root,
            AssembledCase(
                accession_no=accession_no,
                study_uid=item.study_uid,
                image_ref=item.image_ref,
                modality=item.modality,
                study_date=item.study_date,
                report_status=item.report_status,
                assembled_at=assembled_at,
                source=SOURCE_RIS,
            ),
        )
        assembled.append(accession_no)

    assembly = CasesetAssembly(
        run_id=run_id,
        cases_root=str(root),
        source=SOURCE_RIS,
        requested=requested_list if requested_list is not None else tuple(targets),
        assembled=tuple(assembled),
        skipped=tuple(skipped),
        unresolved=tuple(unresolved),
        ambiguous=tuple(ambiguous),
        damaged_dirs=tuple(damaged),
        ris_counts=ris_counts,
        generated_at=assembled_at,
    )
    manifest_path = _write_manifest(runs_dir, assembly)
    return _with_manifest(assembly, manifest_path)


def _with_manifest(assembly: CasesetAssembly, manifest_path: Path) -> CasesetAssembly:
    """给装配结果补上装配清单的落点。"""

    return CasesetAssembly(
        run_id=assembly.run_id,
        cases_root=assembly.cases_root,
        source=assembly.source,
        requested=assembly.requested,
        assembled=assembly.assembled,
        skipped=assembly.skipped,
        unresolved=assembly.unresolved,
        ambiguous=assembly.ambiguous,
        damaged_dirs=assembly.damaged_dirs,
        ris_counts=assembly.ris_counts,
        manifest_path=str(manifest_path),
        generated_at=assembly.generated_at,
    )


def resolve_case_inputs(
    cases: Sequence[CaseInput],
    cases_root: Path | str,
) -> CaseResolution:
    """把评估表里的病例解析成「可调用的病例」：补齐 ``image_ref`` 与 ``study_uid``。

    口径（与 ``spec.md`` 的最小数据格式一致）：

    - **表格给了 ``image_ref``** —— 以表格为准（来源 ``sheet``）；
    - **表格没给** —— 按 ``accession_no`` 从**已装配的病例集**取（来源 ``caseset``）；
    - **两处都取不到** —— 该例标 ``needs_review``，**不静默用空值继续**：
      一个没有影像引用的病例进了推理调用，要么白跑一次，要么被当成「未检出」
      混进指标分母。

    ``study_uid`` 同样只认病例集（评估表没有这一列）；取不到时该例一并
    标 ``needs_review``。

    **本入口是「兜底」口径**：它的权威源是**本地病例集快照**，快照可能过期，
    所以表格与快照不一致时**以表格为准**（只记一笔差异）。

    真源口径在 :func:`bind_cases`：权威源是**实时 RIS** 时，不一致一律
    **以 RIS 为准并挂起人工复核**。两者是同一套绑定规则的两个权威级别，
    不是两份口径 —— 规则只写在 :func:`bind_cases` 里，本函数只是把快照
    当权威源调它。
    """

    index, _ = index_assembled_cases(cases_root)

    snapshot_items = tuple(
        RISItem(
            accession_no=case.accession_no,
            study_uid=case.study_uid,
            modality=case.modality,
            study_date=case.study_date,
            report_status=case.report_status,
            image_ref=case.image_ref,
        )
        for case in index.values()
    )

    result = bind_cases(
        cases,
        snapshot_items,
        authority=AUTHORITY_SNAPSHOT,
        modality="",
        write=False,
    )

    resolved: list[CaseInput] = []
    from_caseset: list[str] = []
    unresolved: list[str] = []
    sources: dict[str, str] = {}

    for case in result.cases:
        entry = result.entry_for(case.accession_no)
        image_ref_source = entry.image_ref_source if entry is not None else SOURCE_UNRESOLVED
        if image_ref_source == SOURCE_CASESET:
            from_caseset.append(case.accession_no)
        if not case.image_ref or not case.study_uid:
            unresolved.append(case.accession_no)
        sources[case.accession_no] = image_ref_source or SOURCE_UNRESOLVED
        resolved.append(case)

    return CaseResolution(
        cases=tuple(resolved),
        resolved_from_caseset=tuple(from_caseset),
        unresolved=tuple(unresolved),
        sources=sources,
    )


# --------------------------------------------------------------------------
# 三键实体绑定（任务 10）
# --------------------------------------------------------------------------
#
# **为什么绑定要单独一层**：装配解决的是「把病例集固化下来」，绑定解决的是
# 「这一行表格到底对应哪一张影像」。三键各有分工：
#
# - ``accession_no``（表格行 → 业务检查号）：**唯一由科室给**的一键；
# - ``study_uid``（影像标识）：**只认 RIS**。评估表没有这一列，也不该有；
# - ``row_no``（表格行号）：回填与人工复核时的定位锚点。
#
# **一一对应关系是硬要求**：一个检查号对多张影像、一行表里同一个检查号出现两次、
# 一张影像被两行表引用 —— 三者都会把「模型判漏了」记到错的例上。碰上就挂起
# （``needs_review``），**绝不自动取第一条**。
#
# **权威级别**：实时 RIS 是真源（``AUTHORITY_RIS``）；本地病例集快照只是兜底
# （``AUTHORITY_SNAPSHOT``，可能过期）。两者对「表格与权威源不一致」的处理不同：
#
# - **真源冲突** → 以真源为准、**记差异并挂起**（这一例要人看过才算数）；
# - **快照冲突** → 以表格为准、**只记差异不挂起**（表格可能是刚更新的）。
#
# 规则只写在这里；``resolve_case_inputs`` 以快照为权威源调同一套规则。

#: 绑定关系落盘的文件名（``runs/<run_id>/binding.json``）。
BINDING_FILENAME = "binding.json"

#: 绑定状态：唯一绑定成功。
BINDING_BOUND = "bound"
#: 绑定状态：检查号命中多条影像 —— 挂起人工判定，不自动取第一条。
BINDING_AMBIGUOUS = "ambiguous"
#: 绑定状态：RIS 里找不到该检查号，或引用解析不出来。
BINDING_UNRESOLVED = "unresolved"
#: 绑定状态：表格里同一个检查号出现多次 —— 一一对应关系不成立。
BINDING_DUPLICATE = "duplicate"

#: 权威级别：实时 RIS（真源）。
AUTHORITY_RIS = "ris"
#: 权威级别：本地病例集快照（兜底，可能过期）。
AUTHORITY_SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class BindingCandidate:
    """一对多时的候选影像 —— 供人工判定挑哪一条。"""

    study_uid: str
    study_date: str = ""
    image_ref: str | None = None
    modality: str = ""
    report_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_uid": self.study_uid,
            "study_date": self.study_date,
            "image_ref": self.image_ref,
            "modality": self.modality,
            "report_status": self.report_status,
        }


@dataclass(frozen=True)
class BindingDifference:
    """表格与权威源不一致的一处（表里写了什么、权威源是什么、以谁为准）。"""

    field: str
    sheet_value: str
    authority_value: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "sheet_value": self.sheet_value,
            "authority_value": self.authority_value,
            "note": self.note,
        }


@dataclass(frozen=True)
class BindingEntry:
    """一行的绑定结论 —— 落进 ``binding.json`` 的每一项。"""

    accession_no: str
    row_no: int | None = None
    status: str = BINDING_BOUND
    study_uid: str | None = None
    image_ref: str | None = None
    study_uid_source: str = ""
    image_ref_source: str = ""
    needs_review: bool = False
    error_code: str = ""
    message: str = ""
    sheet_image_ref: str | None = None
    sheet_study_uid: str | None = None
    candidates: tuple[BindingCandidate, ...] = ()
    differences: tuple[BindingDifference, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "row_no": self.row_no,
            "status": self.status,
            "study_uid": self.study_uid,
            "image_ref": self.image_ref,
            "study_uid_source": self.study_uid_source,
            "image_ref_source": self.image_ref_source,
            "needs_review": self.needs_review,
            "error_code": self.error_code,
            "message": self.message,
            "sheet_image_ref": self.sheet_image_ref,
            "sheet_study_uid": self.sheet_study_uid,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "differences": [difference.to_dict() for difference in self.differences],
        }


@dataclass(frozen=True)
class BindingResult:
    """一次绑定的结果与落点。"""

    run_id: str = ""
    authority: str = AUTHORITY_RIS
    entries: tuple[BindingEntry, ...] = ()
    cases: tuple[CaseInput, ...] = ()
    binding_path: str = ""
    generated_at: str = ""

    def counts(self) -> dict[str, int]:
        """计数视图（绑定清单与终端输出共用）。"""

        counts = {
            "total": len(self.entries),
            "bound": 0,
            "ambiguous": 0,
            "unresolved": 0,
            "duplicate": 0,
            "with_difference": 0,
            "needs_review": 0,
        }
        for entry in self.entries:
            counts[entry.status] = counts.get(entry.status, 0) + 1
            if entry.differences:
                counts["with_difference"] += 1
            if entry.needs_review:
                counts["needs_review"] += 1
        return counts

    def entry_for(self, accession_no: str) -> BindingEntry | None:
        """取某一行的绑定结论（按检查号）。"""

        for entry in self.entries:
            if entry.accession_no == accession_no:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "authority": self.authority,
            "generated_at": self.generated_at,
            "counts": self.counts(),
            "entries": [entry.to_dict() for entry in self.entries],
            "note": (
                "accession_no ↔ study_uid ↔ 表格行号 的一一对应关系。"
                "bound = 唯一绑定；ambiguous = 命中多条影像（挂起，不自动取第一条）；"
                "unresolved = 权威源里找不到；duplicate = 表格里检查号重复。"
                "needs_review 的例不进入指标分母，也不调用推理服务。"
            ),
        }


def _group_by_accession(
    items: Sequence[RISItem],
    modality: str,
) -> dict[str, list[RISItem]]:
    """按检查号分组，并按请求模态过滤。

    口径与 RIS 适配器一致：**明确不是请求模态**的记录过滤掉（首版范围钉死胸片）；
    **没给模态**的记录保留（缺字段不等于「不是胸片」）。
    """

    wanted = (modality or "").strip().upper()
    grouped: dict[str, list[RISItem]] = {}
    for item in items:
        declared = (item.modality or "").strip().upper()
        if declared and wanted and declared != wanted:
            continue
        grouped.setdefault(item.accession_no, []).append(item)
    return grouped


def _hold(
    case: CaseInput,
    *,
    status: str,
    error_code: str,
    message: str,
    sheet_image_ref: str | None,
    sheet_study_uid: str | None,
    candidates: tuple[BindingCandidate, ...] = (),
    differences: tuple[BindingDifference, ...] = (),
) -> tuple[BindingEntry, CaseInput]:
    """造一个「挂起」的绑定结论。

    挂起的例**不带 ``study_uid`` 也不带 ``image_ref``** —— 没有可信的影像引用
    就不能发起推理调用，否则「模型判漏了」会被记到一条根本不是本次检查的
    影像上。表格给的原值原样留在 ``sheet_*`` 字段里，供人工判定时对照。
    """

    entry = BindingEntry(
        accession_no=case.accession_no,
        row_no=case.row_no,
        status=status,
        study_uid=None,
        image_ref=None,
        study_uid_source="",
        image_ref_source=SOURCE_UNRESOLVED,
        needs_review=True,
        error_code=error_code,
        message=message,
        sheet_image_ref=sheet_image_ref,
        sheet_study_uid=sheet_study_uid,
        candidates=candidates,
        differences=differences,
    )
    resolved = CaseInput(
        accession_no=case.accession_no,
        gold_standard_text=case.gold_standard_text,
        image_ref=None,
        row_no=case.row_no,
        study_uid=None,
        needs_review=True,
        hold_reason=message,
    )
    return entry, resolved


def _bind_one(
    case: CaseInput,
    grouped: Mapping[str, list[RISItem]],
    occurrences: Mapping[str, int],
    *,
    authority: str,
) -> tuple[BindingEntry, CaseInput]:
    """绑定一行表格 —— 返回（绑定结论, 绑定后的病例输入）。"""

    accession_no = case.accession_no
    sheet_image_ref = case.image_ref
    sheet_study_uid = case.study_uid

    # ---- 1. 表格里同一个检查号出现多次 → 一一对应关系已破 ----
    occurrences_of = occurrences.get(accession_no, 0)
    if occurrences_of > 1:
        message = error_text(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"评估表里检查号 {accession_no} 出现了 {occurrences_of} 行，"
                f"一行一个检查号的一一对应关系不成立"
            ),
        )
        return _hold(
            case,
            status=BINDING_DUPLICATE,
            error_code=ErrorCode.CONTRACT_INVALID.value,
            message=message,
            sheet_image_ref=sheet_image_ref,
            sheet_study_uid=sheet_study_uid,
        )

    matches = grouped.get(accession_no, [])
    distinct_uids = {item.study_uid for item in matches}

    # ---- 2. 一对多 → 挂起人工判定，**绝不自动取第一条** ----
    if len(distinct_uids) > 1:
        candidates = tuple(
            BindingCandidate(
                study_uid=item.study_uid,
                study_date=item.study_date,
                image_ref=item.image_ref,
                modality=item.modality,
                report_status=item.report_status,
            )
            for item in matches
        )
        message = error_text(ErrorCode.AMBIGUOUS_BINDING, accession_no=accession_no)
        return _hold(
            case,
            status=BINDING_AMBIGUOUS,
            error_code=ErrorCode.AMBIGUOUS_BINDING.value,
            message=message,
            sheet_image_ref=sheet_image_ref,
            sheet_study_uid=sheet_study_uid,
            candidates=candidates,
        )

    # ---- 3. 权威源里找不到这个检查号 ----
    if not matches:
        message = error_text(
            ErrorCode.CONTRACT_INVALID,
            detail=f"RIS 清单里找不到检查号 {accession_no}",
        )
        return _hold(
            case,
            status=BINDING_UNRESOLVED,
            error_code=ErrorCode.CONTRACT_INVALID.value,
            message=message,
            sheet_image_ref=sheet_image_ref,
            sheet_study_uid=sheet_study_uid,
        )

    # ---- 4. 唯一绑定 ----
    item = matches[0]
    differences: list[BindingDifference] = []
    needs_review = case.needs_review

    # 4a. 影像标识：表格里若带了，与权威源不一致要记一笔
    if sheet_study_uid and sheet_study_uid != item.study_uid:
        differences.append(
            BindingDifference(
                field="study_uid",
                sheet_value=sheet_study_uid,
                authority_value=item.study_uid,
                note=(
                    "表格里的影像标识与权威源不一致，以权威源为准"
                    if authority == AUTHORITY_RIS
                    else "表格里的影像标识与本地快照不一致，沿用表格值（快照可能过期）"
                ),
            )
        )
        if authority == AUTHORITY_RIS:
            needs_review = True

    # 4b. 影像引用：真源优先，快照次之
    if authority == AUTHORITY_RIS:
        if item.image_ref:
            image_ref = item.image_ref
            image_ref_source = SOURCE_RIS
            if sheet_image_ref and sheet_image_ref != item.image_ref:
                differences.append(
                    BindingDifference(
                        field="image_ref",
                        sheet_value=sheet_image_ref,
                        authority_value=item.image_ref,
                        note="表格里的影像引用与 RIS 不一致，以 RIS 为准",
                    )
                )
                needs_review = True
        else:
            image_ref = sheet_image_ref
            image_ref_source = SOURCE_SHEET if sheet_image_ref else SOURCE_UNRESOLVED
            if sheet_image_ref:
                differences.append(
                    BindingDifference(
                        field="image_ref",
                        sheet_value=sheet_image_ref,
                        authority_value="",
                        note="RIS 未提供影像引用，沿用评估表给出的引用",
                    )
                )
    else:
        if sheet_image_ref:
            image_ref = sheet_image_ref
            image_ref_source = SOURCE_SHEET
            if item.image_ref and sheet_image_ref != item.image_ref:
                differences.append(
                    BindingDifference(
                        field="image_ref",
                        sheet_value=sheet_image_ref,
                        authority_value=item.image_ref,
                        note="评估表给出的引用与本地快照不一致，沿用表格值（快照可能过期）",
                    )
                )
        elif item.image_ref:
            image_ref = item.image_ref
            image_ref_source = SOURCE_CASESET
        else:
            image_ref = None
            image_ref_source = SOURCE_UNRESOLVED

    study_uid = item.study_uid
    study_uid_source = SOURCE_RIS
    if authority != AUTHORITY_RIS:
        # 快照口径：快照可能过期，表格给的标识优先（与影像引用同一口径）。
        if sheet_study_uid:
            study_uid = sheet_study_uid
            study_uid_source = SOURCE_SHEET
        else:
            study_uid_source = SOURCE_CASESET

    if not image_ref or not study_uid:
        needs_review = True
        if not image_ref:
            message = error_text(
                ErrorCode.CONTRACT_INVALID,
                detail=f"检查号 {accession_no} 没能解析出影像引用",
            )
        else:
            message = error_text(
                ErrorCode.CONTRACT_INVALID,
                detail=f"检查号 {accession_no} 没能解析出影像标识",
            )
        entry = BindingEntry(
            accession_no=accession_no,
            row_no=case.row_no,
            status=BINDING_UNRESOLVED,
            study_uid=study_uid,
            image_ref=image_ref,
            study_uid_source=study_uid_source,
            image_ref_source=image_ref_source,
            needs_review=True,
            error_code=ErrorCode.CONTRACT_INVALID.value,
            message=message,
            sheet_image_ref=sheet_image_ref,
            sheet_study_uid=sheet_study_uid,
            differences=tuple(differences),
        )
        resolved = CaseInput(
            accession_no=accession_no,
            gold_standard_text=case.gold_standard_text,
            image_ref=image_ref,
            row_no=case.row_no,
            study_uid=study_uid,
            needs_review=True,
            hold_reason=message,
        )
        return entry, resolved

    entry = BindingEntry(
        accession_no=accession_no,
        row_no=case.row_no,
        status=BINDING_BOUND,
        study_uid=study_uid,
        image_ref=image_ref,
        study_uid_source=study_uid_source,
        image_ref_source=image_ref_source,
        needs_review=needs_review,
        error_code="",
        message="",
        sheet_image_ref=sheet_image_ref,
        sheet_study_uid=sheet_study_uid,
        differences=tuple(differences),
    )
    resolved = CaseInput(
        accession_no=accession_no,
        gold_standard_text=case.gold_standard_text,
        image_ref=image_ref,
        row_no=case.row_no,
        study_uid=study_uid,
        needs_review=needs_review,
        hold_reason="",
    )
    return entry, resolved


def bind_cases(
    cases: Sequence[CaseInput],
    ris_items: Sequence[RISItem],
    *,
    run_id: str = "",
    runs_dir: Path | str = "runs",
    modality: str = "DX",
    authority: str = AUTHORITY_RIS,
    write: bool = True,
) -> BindingResult:
    """三键实体绑定：``accession_no`` ↔ ``study_uid`` ↔ 表格行。

    **一一对应关系**落进 ``runs/<run_id>/binding.json`` —— 每一行表格都能
    回答三个问题：绑到了哪张影像、凭什么这么绑、有没有留了差异。

    **一对多绝不自动取第一条**：同一检查号命中多条影像（多时间点 / 复查）
    时该例挂起（``needs_review``），``study_uid`` 与 ``image_ref`` 都留空 ——
    没有可信引用就不发起推理调用。候选影像原样记在 ``candidates`` 里，
    等人工在科务会上挑。

    **权威级别**：

    - ``AUTHORITY_RIS``（默认，真源）—— 实时 RIS。不一致一律**以 RIS 为准**、
      **记差异并挂起**；
    - ``AUTHORITY_SNAPSHOT``（兜底）—— 本地病例集快照。不一致时**以表格为准**、
      只记差异不挂起（快照可能过期，表格可能是刚更新的）。

    金标准原文（``gold_standard_text``）在这一层**逐字保留**，不参与任何改写。
    """

    occurrences: dict[str, int] = {}
    for case in cases:
        occurrences[case.accession_no] = occurrences.get(case.accession_no, 0) + 1

    grouped = _group_by_accession(ris_items, modality)

    entries: list[BindingEntry] = []
    resolved_cases: list[CaseInput] = []
    for case in cases:
        entry, resolved = _bind_one(case, grouped, occurrences, authority=authority)
        entries.append(entry)
        resolved_cases.append(resolved)

    generated_at = _now_iso()
    result = BindingResult(
        run_id=run_id,
        authority=authority,
        entries=tuple(entries),
        cases=tuple(resolved_cases),
        generated_at=generated_at,
    )

    if write and run_id:
        result = _with_binding_path(result, _write_binding(runs_dir, result))

    return result


def _write_binding(runs_dir: Path | str, result: BindingResult) -> Path:
    """落绑定清单 ``runs/<run_id>/binding.json``。"""

    run_dir = Path(runs_dir) / result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / BINDING_FILENAME
    target.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def _with_binding_path(result: BindingResult, binding_path: Path) -> BindingResult:
    """给绑定结果补上绑定清单的落点。"""

    return BindingResult(
        run_id=result.run_id,
        authority=result.authority,
        entries=result.entries,
        cases=result.cases,
        binding_path=str(binding_path),
        generated_at=result.generated_at,
    )
