"""环境与依赖自检（``nodule-eval doctor``）。

**装完先跑一遍 doctor**：报错指名到**具体的环境变量**，
不要让人猜「到底哪个没配」—— 院内机器上最费时间的往往就是这件事。

**这一层只报告，不替产品做决定**：它查的是「配置齐不齐、依赖在不在、
库能不能写、公开数据集缓存在不在」，**不发起任何出站调用**
（doctor 不碰 RIS / 推理 / 科研平台 / 模型端点 —— 自检程序去打真实端点
会消耗额度，也可能把对端日志搞脏）。

**替身在这里只被「提及」，不被「启用」**：替身开关仍然是显式的
（``--fixture`` 或 ``IMAGING_EVAL_FIXTURE=1``），doctor **永远不打开它**，
更不会因为「真实端点连不上」就悄悄改走替身 —— 那样自检就变成了
「假装一切正常」。默认路径下，doctor 的输出里**连「替身」两个字都不出现**：
院内装的时候看到的就是真实端点，不给「还有个本地假货可以用」的暗示。

**分级**：
``blocking`` 项不通过 → 退出码非零（院内脚本据此判断「装好了没有」）；
``advisory`` 项不通过只提示，不影响退出码（比如公开数据集缓存没下载 ——
那只影响跑对照实验，不影响跑评估）。

**doctor 同时是安装验证脚本**：它会真的打开一次运行库并做一次**写入探针**
（写完立刻回滚，不留残），这样「目录能写但文件写不进去」这类问题
在安装现场就能暴露，而不是等跑了两百例才发现。
"""

from __future__ import annotations

import importlib
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import REQUIRED_ENV_VARS, missing_env_vars
from .errors import ErrorCode, NoduleEvalError

#: 最低 Python 版本（3.10 起才有 ``X | Y`` 类型语法与 ``match``）。
#: 院内交付口径是 **Python 3.11**，doctor 只把 3.10 当最低线 ——
#: 拦得太紧会让「能跑」的机器被判「装不上」，反而没人信自检结果。
MIN_PYTHON: tuple[int, int] = (3, 10)

#: 院内交付口径推荐的 Python 版本（写进安装说明，不作为阻断条件）。
RECOMMENDED_PYTHON: tuple[int, int] = (3, 11)

#: 自检要确认装上了的第三方依赖（缺一个就跑不动主路径）。
REQUIRED_PACKAGES: tuple[str, ...] = ("requests", "yaml", "openpyxl")

#: 四个出站面（替身门按这四类路由）。
OUTBOUND_KINDS: tuple[str, ...] = ("ris", "inference", "research_platform", "model")

#: 出站面 → 环境变量里的基址变量名（**只指基址，不指 token**）。
_OUTBOUND_BASE_VARS: dict[str, str] = {
    "ris": "RIS_BASE_URL",
    "inference": "INFERENCE_BASE_URL",
    "research_platform": "RESEARCH_PLATFORM_BASE_URL",
    "model": "OPENAI_BASE_URL",
}

#: 写探针用的临时表名（写完回滚，不留残迹）。
_PROBE_TABLE = "doctor_write_probe"


@dataclass(frozen=True)
class CheckResult:
    """一项自检的结果。

    ``ok``
        这一项通没通过。
    ``blocking``
        不通过算不算**阻断**（阻断项让 ``doctor`` 退出码非零）。
    ``detail``
        中文说明 —— **指名**是哪个变量 / 哪个包 / 哪个目录出了问题。
    """

    name: str
    ok: bool
    detail: str
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class DoctorReport:
    """一次自检的完整结果。"""

    checks: tuple[CheckResult, ...] = ()

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if not check.ok)

    @property
    def blocking_failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if not check.ok and check.blocking)

    @property
    def advisories(self) -> tuple[CheckResult, ...]:
        """不通过但不阻断的提示项（公开数据集缓存、安装说明在不在之类）。"""

        return tuple(check for check in self.checks if not check.ok and not check.blocking)

    @property
    def ok(self) -> bool:
        """没有阻断项失败就算装好了。"""

        return not self.blocking_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
            "blocking_failures": [check.name for check in self.blocking_failures],
            "advisories": [check.name for check in self.advisories],
        }


# --------------------------------------------------------------------------
# 逐项检查
# --------------------------------------------------------------------------


def _check_python() -> CheckResult:
    """Python 版本。"""

    current = sys.version_info[:2]
    text = platform.python_version()
    if current >= MIN_PYTHON:
        suffix = ""
        if current < RECOMMENDED_PYTHON:
            suffix = (
                f"；院内交付口径是 {RECOMMENDED_PYTHON[0]}.{RECOMMENDED_PYTHON[1]}"
                "（低版本能跑，但建议按交付口径装）"
            )
        return CheckResult(
            "Python 版本",
            True,
            f"当前 {text}（要求 ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）{suffix}",
        )
    return CheckResult(
        "Python 版本",
        False,
        f"当前 {text}，低于要求的 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
    )


def _check_packages() -> CheckResult:
    """第三方依赖是否装上了（**带版本号** —— 版本对不上时排障要看它）。"""

    missing: list[str] = []
    versions: list[str] = []
    for name in REQUIRED_PACKAGES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            missing.append(name)
            continue
        versions.append(f"{name} {getattr(module, '__version__', '未知版本')}")
    if missing:
        return CheckResult(
            "依赖包",
            False,
            f"缺 {'、'.join(missing)}（安装：pip install -r requirements.txt）",
        )
    return CheckResult("依赖包", True, "、".join(versions))


def _check_env(env: Mapping[str, str]) -> CheckResult:
    """环境变量齐备性 —— **逐个指名缺哪几个**。

    缺变量是院内安装最常见的失败原因，报一句「配置有误」等于没报。
    """

    missing = missing_env_vars(env)
    if missing:
        return CheckResult(
            "环境变量",
            False,
            f"缺 {len(missing)} 项：{'、'.join(missing)}"
            "（在 .env 或系统环境变量里补齐；本产品不会回落本地替身）",
        )
    return CheckResult(
        "环境变量",
        True,
        f"{len(REQUIRED_ENV_VARS)} 项齐备（RIS / 推理 / 科研平台 / 模型四组凭据）",
    )


def _check_outbound_target(
    env: Mapping[str, str],
    *,
    fixture_explicit: bool,
) -> CheckResult:
    """出站目标确认 —— **默认全部是环境变量里的真实端点**。

    替身只在这里被**提及一次**，且只在**显式开启**时提：
    显式开启说明「这条是测试路径，不是默认路径」；默认情况下连提都不提 ——
    院内安装时看到的应该只有真实端点，不留「还能拿本地假货顶一下」的暗示。

    **任何情况下 doctor 都不发起出站调用** —— 这一行只是把配置读出来核对。
    """

    if not fixture_explicit:
        configured = []
        for kind in OUTBOUND_KINDS:
            var = _OUTBOUND_BASE_VARS[kind]
            value = str(env.get(var, "")).strip()
            if value:
                configured.append(f"{kind}={value}")
        if configured:
            return CheckResult(
                "出站目标",
                True,
                "四个出站面全部指向环境变量里的真实端点："
                + "；".join(configured)
                + "（doctor 不发起任何出站调用）",
            )
        return CheckResult(
            "出站目标",
            True,
            "四个出站面（RIS / 推理 / 科研平台 / 模型）均以环境变量里的地址为准"
            "（地址尚未配置，见「环境变量」一项）",
        )

    # 显式开启替身：只提及、说明非默认，并指出地址是否给全。
    from .fixture_gate import FIXTURE_BASE_URL_ENV, FIXTURE_ENV, FIXTURE_KIND_ENV

    missing: list[str] = []
    for kind in OUTBOUND_KINDS:
        if str(env.get(FIXTURE_KIND_ENV.get(kind, ""), "")).strip():
            continue
        if str(env.get(FIXTURE_BASE_URL_ENV, "")).strip():
            continue
        missing.append(FIXTURE_KIND_ENV.get(kind, FIXTURE_BASE_URL_ENV))
    if missing:
        return CheckResult(
            "出站目标",
            False,
            f"已显式开启替身模式（{FIXTURE_ENV}=1 或 --fixture）："
            f"仅测试用，非默认路径，正式评估结论不采信替身返回；"
            f"且替身地址未给全（缺 {len(missing)} 项分端点地址：{'、'.join(missing)}；"
            f"也可以只给统一的 {FIXTURE_BASE_URL_ENV}）",
            blocking=False,
        )
    return CheckResult(
        "出站目标",
        True,
        "已显式开启替身模式（--fixture 或 IMAGING_EVAL_FIXTURE=1）："
        "仅测试用，非默认路径，正式评估结论不采信替身返回；"
        "doctor 本身不发起任何出站调用",
    )


def _check_models(models_dir: Path | str | None) -> CheckResult:
    """被评模型目录 —— 列出现在能用哪几个。"""

    from .catalog import default_models_dir, load_catalog

    target = Path(models_dir) if models_dir is not None else default_models_dir()
    if not target.is_dir():
        return CheckResult(
            "被评模型目录",
            False,
            f"目录不存在：{target}（模型目录是产品的一部分，须随部署一起带上）",
        )
    try:
        catalog = load_catalog(target)
    except NoduleEvalError as exc:
        return CheckResult("被评模型目录", False, f"目录解析失败：{exc.message}")
    authorized = catalog.authorized_ids()
    if not authorized:
        detail = "目录里没有任何已授权模型"
        if catalog.issues:
            detail += "；未通过的原因：" + "；".join(issue.reason for issue in catalog.issues)
        return CheckResult("被评模型目录", False, detail)
    detail = f"已授权 {len(authorized)} 个：{'、'.join(authorized)}"
    if catalog.issues:
        detail += f"；另有 {len(catalog.issues)} 个未授权（不参与评估）"
    return CheckResult("被评模型目录", True, detail)


def _probe_store_write(target: Path) -> tuple[bool, str]:
    """真的写一次再回滚 —— 验证运行库**可写**，不只是目录可写。

    ``目录能写`` 与 ``文件能写`` 是两件事：目录权限对、但文件被占用、
    磁盘满、配额限制，都只会在真写的时候才暴露。这里开一次事务、
    建一张临时表、插一行、立刻回滚 —— 库文件里不留任何痕迹。
    """

    import sqlite3

    created = not target.exists()
    try:
        conn = sqlite3.connect(str(target), timeout=5.0, isolation_level=None)
    except sqlite3.Error as exc:
        return False, f"运行库打不开：{target}（{exc}）"
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_PROBE_TABLE} "
            "(id INTEGER PRIMARY KEY, at TEXT NOT NULL)"
        )
        conn.execute(f"INSERT INTO {_PROBE_TABLE} (at) VALUES (?)", ("probe",))
        conn.execute("ROLLBACK")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - 事务已断，回滚也失败
            pass
        return False, f"运行库不可写：{target}（{exc}）"
    finally:
        conn.close()

    # 探针必须不留残迹：回滚后表应当不存在。
    try:
        conn = sqlite3.connect(str(target), timeout=5.0, isolation_level=None)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (_PROBE_TABLE,),
        ).fetchall()
        conn.close()
        if rows:
            return False, f"写入探针留下残迹：{target}（表 {_PROBE_TABLE} 未回滚）"
    except sqlite3.Error as exc:  # pragma: no cover - 复查失败按写失败处理
        return False, f"运行库不可写：{target}（复查失败：{exc}）"

    prefix = "已新建" if created else "已存在"
    return True, f"{target}（{prefix}，写入探针通过且已回滚，无残迹）"


def _check_store(store_path: Path | str | None) -> CheckResult:
    """运行库（SQLite）能不能落盘 —— **探一次真实写入**。"""

    from .store import default_store_path, resolve_store_path

    try:
        target = resolve_store_path(store_path)
    except NoduleEvalError as exc:
        return CheckResult("运行库", False, exc.message)
    parent = target.parent
    if not parent.exists():
        return CheckResult(
            "运行库",
            False,
            f"目录不存在：{parent}（先建好目录，或用 --store 指定别的位置）",
        )
    if not os.access(parent, os.W_OK):
        return CheckResult(
            "运行库",
            False,
            f"目录不可写：{parent}（换一个可写目录，或给当前账号写权限）",
        )
    ok, detail = _probe_store_write(target)
    return CheckResult("运行库", ok, detail)


def _check_runs_dir(runs_dir: Path | str | None) -> CheckResult:
    """产物目录能不能落盘。"""

    target = Path(runs_dir) if runs_dir is not None else Path("runs")
    parent = target if target.exists() else target.parent
    if not parent.exists():
        return CheckResult(
            "产物目录",
            False,
            f"目录不存在：{parent}（先建好目录）",
        )
    if not os.access(parent, os.W_OK):
        return CheckResult("产物目录", False, f"目录不可写：{parent}")
    return CheckResult("产物目录", True, f"{target}（所在目录 {parent} 可写）")


def _check_jsrt_cache(
    dataset_dir: Path | str | None,
    *,
    verify: bool,
) -> CheckResult:
    """公开数据集（JSRT）缓存状态 —— **提示项**，不阻断。

    JSRT 只用于「规则基线 vs 模型归并」的对照实验，**不参与院内评估主路径**，
    所以没下载只是提示，不该把「装好了」判成「没装好」。

    **只读本地文件，不联网**：自检程序不该在安装现场发外网请求。
    """

    from .datasets.jsrt import (
        DEFAULT_CACHE_DIR,
        METADATA_MEMBER,
        Manifest,
        default_manifest_path,
        verify_cached,
    )

    cache = Path(dataset_dir) if dataset_dir is not None else DEFAULT_CACHE_DIR
    if not cache.is_dir():
        return CheckResult(
            "公开数据集缓存（JSRT）",
            False,
            f"未下载（不影响跑评估；仅用于规则/模型归并对照实验）：{cache}。"
            "下载：python scripts/fetch_jsrt.py",
            blocking=False,
        )

    images_dir = cache / "images"
    pngs = sorted(images_dir.glob("*.png")) if images_dir.is_dir() else []
    expected = 0
    try:
        manifest = Manifest.load(default_manifest_path())
        expected = manifest.case_count
    except Exception:  # noqa: BLE001 - 清单缺失不该让自检崩掉
        expected = 0

    detail_parts: list[str] = [f"已缓存 {len(pngs)} 张"]
    if expected:
        detail_parts.append(f"（应为 {expected} 张）")
    if not (cache / METADATA_MEMBER).is_file():
        detail_parts.append("元数据文件缺失")
    archive = cache / "jsrt-archive.zip"
    if archive.is_file():
        detail_parts.append(f"归档 {archive.stat().st_size} 字节")
    else:
        detail_parts.append("归档未保留（重跑取数脚本会重新下载）")

    ok = True
    if expected and len(pngs) != expected:
        ok = False
        detail_parts.append(f"数量不符：实得 {len(pngs)} 张")

    if verify:
        try:
            result = verify_cached(cache)
        except Exception as exc:  # noqa: BLE001 - 校验失败按提示项处理
            return CheckResult(
                "公开数据集缓存（JSRT）",
                False,
                f"深度校验失败：{exc}；缓存：{cache}",
                blocking=False,
            )
        detail_parts.append(
            f"逐文件 SHA256 校验 {result.checked} 张"
            f"（不符 {len(result.hash_mismatches)} 张）"
        )
        if not result.ok:
            ok = False
            if result.hash_mismatches:
                detail_parts.append(f"哈希不符 {len(result.hash_mismatches)} 张")
            if result.missing:
                detail_parts.append(f"缺 {len(result.missing)} 张")
            if result.unexpected:
                detail_parts.append(f"多出 {len(result.unexpected)} 张")
    else:
        detail_parts.append("未做逐文件哈希校验（--verify-dataset 可开）")

    return CheckResult(
        "公开数据集缓存（JSRT）",
        ok,
        "；".join(detail_parts),
        blocking=False,
    )


def _check_install_doc(docs_dir: Path | str | None = None) -> CheckResult:
    """院内安装说明在不在 —— **提示项**。

    装不上的时候，科室多半是翻这份说明而不是翻代码；
    它不在现场，说明多半已经被人删了或没随部署带上。
    """

    base = Path(docs_dir) if docs_dir is not None else Path(__file__).resolve().parents[2] / "docs"
    target = base / "install.md"
    if target.is_file():
        return CheckResult(
            "院内安装说明",
            True,
            f"{target}",
            blocking=False,
        )
    return CheckResult(
        "院内安装说明",
        False,
        f"缺 {target}（安装步骤、离线安装与常见报错处置都在这份文件里）",
        blocking=False,
    )


def _check_env_file(env_file: Path | str | None = None) -> CheckResult:
    """``.env`` 配置能不能被读到 —— **提示项**。

    装了 ``.env`` 却没装 ``python-dotenv`` 时，``.env`` 里的值**不会被读** ——
    这时 doctor 会说「变量齐备」，实际跑起来却是缺凭据。
    这种「看起来装好了」的坑必须在这里点出来。
    """

    from .config import default_env_file_path

    target = Path(env_file) if env_file is not None else default_env_file_path()
    if not target.is_file():
        return CheckResult(
            "环境变量文件（.env）",
            True,
            f"未使用 .env（{target} 不存在）：凭据全部取自进程环境变量",
            blocking=False,
        )
    try:
        importlib.import_module("dotenv")
    except ImportError:
        return CheckResult(
            "环境变量文件（.env）",
            False,
            f"有 {target} 但没装 python-dotenv —— .env 里的值不会被读"
            "（pip install python-dotenv，或改用系统环境变量）",
            blocking=False,
        )
    return CheckResult(
        "环境变量文件（.env）",
        True,
        f"{target}（已生效；系统环境变量优先）",
        blocking=False,
    )


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def run_doctor(
    *,
    env: Mapping[str, str] | None = None,
    store_path: Path | str | None = None,
    models_dir: Path | str | None = None,
    runs_dir: Path | str | None = None,
    fixture_explicit: bool = False,
    dataset_dir: Path | str | None = None,
    verify_dataset: bool = False,
) -> DoctorReport:
    """跑一遍自检 —— **不发任何出站调用**。

    每一项都**指名**到具体的变量 / 包 / 目录；
    替身在这一层**只被提及、不被启用**（且只在显式开启时才提）。
    """

    source = os.environ if env is None else env
    if env is None:
        # 院内凭据多数配在 .env 里 —— 自检必须按同一口径读，否则会误判「缺变量」。
        from .config import load_env_file

        load_env_file()
        source = os.environ
    checks: list[CheckResult] = [
        _check_python(),
        _check_packages(),
        _check_env_file(),
        _check_env(source),
        _check_outbound_target(source, fixture_explicit=fixture_explicit),
        _check_models(models_dir),
        _check_store(store_path),
        _check_runs_dir(runs_dir),
        _check_jsrt_cache(dataset_dir, verify=verify_dataset),
        _check_install_doc(),
    ]
    return DoctorReport(checks=tuple(checks))


def render_report(report: DoctorReport) -> str:
    """把自检结果渲染成给人看的中文文本。"""

    lines: list[str] = []
    for check in report.checks:
        mark = "✓" if check.ok else ("✗" if check.blocking else "!")
        suffix = "" if check.blocking else "（提示项，不影响继续使用）"
        lines.append(f"[{mark}] {check.name}：{check.detail}{suffix}")
    lines.append("")
    if report.ok:
        lines.append("自检通过：配置齐备，可以开始评估。")
        if report.advisories:
            names = "、".join(check.name for check in report.advisories)
            lines.append(f"另有 {len(report.advisories)} 项提示（{names}）—— 不阻断，按需处理。")
        return "\n".join(lines)

    names = "、".join(check.name for check in report.blocking_failures)
    lines.append(f"自检未通过：{names} —— 请按上面的提示逐项处理后重跑 doctor。")
    if report.advisories:
        advisory_names = "、".join(check.name for check in report.advisories)
        lines.append(f"另有 {len(report.advisories)} 项提示（{advisory_names}）—— 不阻断。")
    return "\n".join(lines)
