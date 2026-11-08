"""JSRT 公开数据集的获取与完整性校验。

**源产品证据**：参考实现用两个脚本把公开病例集拉下来 ——
``data/get_cases.py`` 翻页抓病例号、``data/figures.py`` 逐张下载图像，
后者的 ``save_figure`` 有个关键语义：**目标文件已存在就直接返回**（不重复下载），
而抓取失败时只 ``print`` 一句就继续往下走（``except Exception: print(...)``）。

本模块把这两条语义都改掉：

- **幂等保留**：已存在且校验通过的文件不重复下载（对齐 ``save_figure`` 的「已存在则跳过」）。
- **失败不再吞掉**：抓取失败**不静默跳过** —— 明确记失败、末尾统一报告，
  并以非零退出码结束。源实现那句 ``print`` 之后继续跑，最终会在「少了一半病例」
  的数据集上得出一个看起来正常的评估结论 —— 这正是本产品最不能出的错。

**校验口径**：逐文件 SHA256 + 数量必须**恰好 247 例**。
数量与哈希都由 ``scripts/jsrt_manifest.json`` 里的钉死清单给出，
不靠「下载下来看着像 247 张」这种肉眼判断。

**公开数据的定位**：JSRT 只进 ``tests/fixtures/`` 与本模块的缓存目录，
**不是**产品的对接面；院内真实数据一律走 RIS 适配器。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

#: 清单文件名（相对项目根）。
MANIFEST_FILENAME = "jsrt_manifest.json"

#: 缓存目录的缺省名（相对项目根）。已在 ``.gitignore`` 的 ``data/`` 之下。
DEFAULT_CACHE_DIR = Path("data") / "jsrt"

#: 镜像站请求头。**必须带 User-Agent** —— 镜像站对无 UA 的请求返回 404，
#: 带上 UA 才返回归档本体（实测：无 UA → 404 空体，有 UA → 200 application/zip）。
DEFAULT_USER_AGENT = "nodule-ai-model-eval/1.0 (+JSRT dataset fetch script)"

#: 下载归档的超时（连接, 读取）秒。归档约 390MB，读取超时给足。
DOWNLOAD_TIMEOUT: tuple[float, float] = (15.0, 120.0)

#: 归档内图像目录前缀。
IMAGE_PREFIX = "images/images/"

#: 元数据 CSV 在归档内的路径。
METADATA_MEMBER = "jsrt_metadata.csv"


class DatasetFetchError(Exception):
    """取数失败（网络不可达、归档损坏、数量或哈希不符）。

    与产品主链路的错误码表分开：这是**取数脚本**的失败，
    不参与评估指标的分子分母。
    """


@dataclass(frozen=True)
class Manifest:
    """钉死的清单：镜像地址、归档哈希、逐文件哈希与例数。"""

    dataset: str
    version: str
    source: str
    mirror_urls: tuple[str, ...]
    case_count: int
    archive_sha256: str
    archive_bytes: int
    metadata_sha256: str
    images: Mapping[str, str]
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "Manifest":
        archive = payload.get("archive") or {}
        metadata = payload.get("metadata_file") or {}
        images = payload.get("images") or {}
        if not isinstance(images, Mapping) or not images:
            raise DatasetFetchError("清单里的 images 必须是非空映射（文件名 → SHA256）")
        return cls(
            dataset=str(payload.get("dataset", "")),
            version=str(payload.get("version", "")),
            source=str(payload.get("source", "")),
            mirror_urls=tuple(str(url) for url in payload.get("mirror_urls") or ()),
            case_count=int(payload.get("case_count", 0)),
            archive_sha256=str(archive.get("sha256", "")),
            archive_bytes=int(archive.get("bytes", 0)),
            metadata_sha256=str(metadata.get("sha256", "")),
            images={str(k): str(v) for k, v in images.items()},
            raw=dict(payload),
        )

    @classmethod
    def load(cls, path: Path | str) -> "Manifest":
        target = Path(path)
        if not target.is_file():
            raise DatasetFetchError(f"找不到清单文件：{target}")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DatasetFetchError(f"清单文件不是合法 JSON：{target}（{exc}）") from exc
        if not isinstance(payload, Mapping):
            raise DatasetFetchError(f"清单文件顶层须为 JSON 对象：{target}")
        return cls.from_json(payload)


@dataclass
class FetchReport:
    """一次取数的完整结果 —— 成功与失败都要落到这里，不靠返回值暗示。"""

    cache_dir: Path
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    verified: int = 0
    metadata_rows: int = 0

    @property
    def ok(self) -> bool:
        """是否**全部成功** —— 有任何失败就不算成功。"""

        return not self.failures

    def summary(self) -> str:
        """给人看的中文摘要（脚本的终端输出）。"""

        return (
            f"下载 {len(self.downloaded)} 例 · 跳过已存在 {len(self.skipped)} 例 · "
            f"校验通过 {self.verified} 例 · 失败 {len(self.failures)} 例"
        )


def sha256_bytes(payload: bytes) -> str:
    """字节流的 SHA256。"""

    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path | str) -> str:
    """文件的 SHA256（分块读，不把 400MB 归档整个读进内存）。"""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_manifest_path() -> Path:
    """项目根下的清单路径。"""

    return Path(__file__).resolve().parents[3] / "scripts" / MANIFEST_FILENAME


# --------------------------------------------------------------------------
# 归档下载
# --------------------------------------------------------------------------


def _resolve_mirror(urls: Sequence[str], manifest: Manifest) -> str:
    """取第一个非空的镜像地址。"""

    candidates = [url for url in urls if url.strip()] or list(manifest.mirror_urls)
    if not candidates:
        raise DatasetFetchError("没有可用的镜像地址（清单里的 mirror_urls 为空）")
    return candidates[0]


def download_archive(
    url: str,
    target: Path | str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: tuple[float, float] = DOWNLOAD_TIMEOUT,
    session: Any | None = None,
    max_attempts: int = 3,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """把归档下载到 ``target``。

    - **断点续传**：目标已存在且长度小于预期时带 ``Range`` 头续下，
      避免 400MB 下到一半重来。
    - **失败不吞**：重试耗尽后抛 :class:`DatasetFetchError`，由调用方统一报告。
    - **不静默替换**：已存在且哈希匹配时直接复用（幂等重跑）。
    """

    import requests  # 局部导入：让「只读清单」这类轻用途不必装 requests

    dest = Path(target)
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected_bytes = 0

    if dest.is_file():
        existing = dest.stat().st_size
        if existing > 0:
            # 已下完的不重复下。
            if on_progress is not None:
                on_progress(existing, existing)
            return dest
        dest.unlink()

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        headers = {"User-Agent": user_agent}
        offset = dest.stat().st_size if dest.is_file() else 0
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=timeout,
                allow_redirects=True,
            ) as response:
                if response.status_code not in (200, 206):
                    raise DatasetFetchError(
                        f"镜像站返回 HTTP {response.status_code}（请求 {url}）"
                    )
                # 服务端忽略了 Range 而从头发：重置文件，避免拼出坏归档。
                mode = "ab" if (offset and response.status_code == 206) else "wb"
                total_header = response.headers.get("Content-Length")
                expected_bytes = (
                    offset + int(total_header) if total_header and mode == "ab" else
                    int(total_header) if total_header else 0
                )
                with open(dest, mode) as handle:
                    for chunk in response.iter_content(1 << 20):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        if on_progress is not None:
                            handle.flush()
                            on_progress(dest.stat().st_size, expected_bytes or None)
            return dest
        except (OSError, Exception) as exc:  # noqa: BLE001 - 网络异常种类多，统一收口
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_attempts:
                continue
    raise DatasetFetchError(
        f"下载归档失败（已尝试 {max_attempts} 次）：{url} —— {last_error}"
    )


# --------------------------------------------------------------------------
# 解包与校验
# --------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """校验结果。"""

    checked: int = 0
    hash_mismatches: list[tuple[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    count_ok: bool = True
    metadata_ok: bool = True

    @property
    def ok(self) -> bool:
        return (
            self.count_ok
            and self.metadata_ok
            and not self.hash_mismatches
            and not self.missing
            and not self.unexpected
        )


def verify_directory(
    images_dir: Path | str,
    manifest: Manifest,
    *,
    metadata_path: Path | str | None = None,
) -> VerificationResult:
    """校验已落盘的图像目录：逐文件 SHA256 + 数量恰好 ``case_count``。

    **数量必须是恰好 247**：少一例说明下载被截断，多一例说明镜像站塞了别的东西 ——
    两种情况都会让「敏感度 / 特异度」算在错误的样本集上。
    """

    directory = Path(images_dir)
    result = VerificationResult()

    if not directory.is_dir():
        result.count_ok = False
        result.missing = sorted(manifest.images)
        return result

    actual = {path.name for path in directory.glob("*.png")}
    expected = set(manifest.images)

    result.missing = sorted(expected - actual)
    result.unexpected = sorted(actual - expected)

    for name in sorted(expected & actual):
        path = directory / name
        result.checked += 1
        digest = sha256_file(path)
        if digest != manifest.images[name]:
            result.hash_mismatches.append((name, digest))

    result.count_ok = len(actual) == manifest.case_count

    if metadata_path is not None:
        meta = Path(metadata_path)
        if not meta.is_file():
            result.metadata_ok = False
        else:
            result.metadata_ok = sha256_file(meta) == manifest.metadata_sha256

    return result


def extract_archive(
    archive_path: Path | str,
    dest_dir: Path | str,
    manifest: Manifest,
) -> tuple[Path, Path]:
    """解包归档到 ``dest_dir``，返回 (图像目录, 元数据 CSV 路径)。

    **先验归档哈希再解包**：归档损坏时解出来的是一堆错字节，
    逐文件验出来只是「247 个不匹配」，看不出根因；先验归档能一次说清。
    """

    archive = Path(archive_path)
    if not archive.is_file():
        raise DatasetFetchError(f"找不到归档文件：{archive}")

    actual = sha256_file(archive)
    if actual != manifest.archive_sha256:
        raise DatasetFetchError(
            f"归档 SHA256 不符：期望 {manifest.archive_sha256}，实得 {actual}"
            f"（文件 {archive}）"
        )

    dest = Path(dest_dir)
    images_dir = dest / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(archive) as zf:
            members = set(zf.namelist())
            for name in manifest.images:
                member = f"{IMAGE_PREFIX}{name}"
                if member not in members:
                    raise DatasetFetchError(f"归档里缺图像：{member}")
                target = images_dir / name
                if target.is_file() and sha256_file(target) == manifest.images[name]:
                    continue
                target.write_bytes(zf.read(member))

            if METADATA_MEMBER not in members:
                raise DatasetFetchError(f"归档里缺元数据文件：{METADATA_MEMBER}")
            metadata_path = dest / METADATA_MEMBER
            metadata_path.write_bytes(zf.read(METADATA_MEMBER))
    except zipfile.BadZipFile as exc:
        raise DatasetFetchError(f"归档不是合法 ZIP：{archive}（{exc}）") from exc

    return images_dir, metadata_path


def read_metadata_rows(metadata_path: Path | str) -> list[dict[str, str]]:
    """读元数据 CSV 的全部数据行（表头不算）。"""

    import csv

    path = Path(metadata_path)
    if not path.is_file():
        raise DatasetFetchError(f"找不到元数据文件：{path}")
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def fetch_dataset(
    dest_dir: Path | str = DEFAULT_CACHE_DIR,
    *,
    manifest_path: Path | str | None = None,
    urls: Sequence[str] | None = None,
    session: Any | None = None,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> FetchReport:
    """取全量 JSRT：下载 → 解包 → 逐文件校验 → 数量校验。

    **断网时抛 :class:`DatasetFetchError`** —— 脚本据此以非零退出码结束并给出
    明确的降级提示，**不静默跳过**、不产出「看起来成功」的半份数据集。
    """

    manifest = Manifest.load(manifest_path or default_manifest_path())
    dest = Path(dest_dir)
    images_dir = dest / "images"
    report = FetchReport(cache_dir=dest)

    if images_dir.is_dir():
        existing = {path.name for path in images_dir.glob("*.png")}
        report.skipped = sorted(existing & set(manifest.images))
        report.verified = len(report.skipped)

    archive_path = dest / "jsrt-archive.zip"
    url = _resolve_mirror(list(urls or []), manifest)

    if not (archive_path.is_file() and archive_path.stat().st_size > 0):
        download_archive(
            url,
            archive_path,
            session=session,
            on_progress=on_progress,
        )

    images_dir, metadata_path = extract_archive(archive_path, dest, manifest)

    result = verify_directory(images_dir, manifest, metadata_path=metadata_path)
    report.verified = result.checked
    report.metadata_rows = len(read_metadata_rows(metadata_path))

    if not result.count_ok:
        report.failures.append(
            f"数量校验不通过：实得 {result.checked + len(result.unexpected)} 例，"
            f"应为 {manifest.case_count} 例"
            + (f"（多出 {len(result.unexpected)} 例）" if result.unexpected else "")
            + (f"（缺 {len(result.missing)} 例）" if result.missing else "")
        )
    if result.missing:
        report.failures.append(f"缺 {len(result.missing)} 例，例如 {result.missing[:3]}")
    if result.unexpected:
        report.failures.append(
            f"多出 {len(result.unexpected)} 例（例如 {result.unexpected[:3]}），不在清单内"
        )
    if result.hash_mismatches:
        report.failures.append(
            f"{len(result.hash_mismatches)} 例哈希不符，例如 "
            f"{[name for name, _ in result.hash_mismatches[:3]]}"
        )
    if not result.metadata_ok:
        report.failures.append("元数据文件缺失或哈希不符")

    report.downloaded = sorted(set(manifest.images) - set(report.skipped))
    return report


def verify_cached(
    dest_dir: Path | str = DEFAULT_CACHE_DIR,
    *,
    manifest_path: Path | str | None = None,
) -> VerificationResult:
    """只校验已缓存的数据集，不联网。"""

    manifest = Manifest.load(manifest_path or default_manifest_path())
    dest = Path(dest_dir)
    return verify_directory(
        dest / "images",
        manifest,
        metadata_path=dest / METADATA_MEMBER,
    )


def build_sample_cases(
    metadata_path: Path | str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """从元数据里挑若干例，组装成评估表可用的**脱敏**病例样例。

    **脱敏口径**：JSRT 原始元数据带 ``age`` / ``gender`` / ``diagnosis`` ——
    这些是放射科医生对真实病例的判读，**不属本产品的评估口径**，
    而且把它们搬进仓库等于在无授权的情况下二次传播个人相关数据。
    因此这里只保留评估真正需要的四样：**检查号、结节位置、尺寸、难度**，
    并把检查号换成 ``JSRT-SAMPLE-xxx`` 形式的占位号
    （``CLAUDE.md``「反拟真数据」：不得让样例看起来像真实住院流水）。

    **抽样口径**：**结节例与阴性例都要有**。只取结节例的话，
    样例集里一例真阴性都没有，特异度就没法算 —— 而特异度正是本产品
    最容易被「只看漏诊」漏掉的那一半。因此按阳性 / 阴性交替取，
    阴性例偏多时（JSRT 是 154 阳性 / 93 阴性）仍各取至少一例。
    """

    rows = read_metadata_rows(metadata_path)

    positives: list[Mapping[str, str]] = []
    negatives: list[Mapping[str, str]] = []
    for row in rows:
        study_id = (row.get("study_id") or "").strip()
        if not study_id:
            continue
        if (row.get("state") or "").strip() == "non-nodule":
            negatives.append(row)
        else:
            positives.append(row)

    # 阴性例至少占三分之一：阳性例多时也留出阴性样本，否则特异度无从谈起。
    negative_quota = max(1, limit // 3) if negatives else 0
    picked: list[Mapping[str, str]] = []
    neg_index = 0
    pos_index = 0
    while len(picked) < limit and (neg_index < len(negatives) or pos_index < len(positives)):
        # 先补阴性配额，再按 2 正 1 负的节奏交替。
        if neg_index < negative_quota and neg_index < len(negatives):
            picked.append(negatives[neg_index])
            neg_index += 1
            continue
        if pos_index < len(positives):
            picked.append(positives[pos_index])
            pos_index += 1
            continue
        if neg_index < len(negatives):
            picked.append(negatives[neg_index])
            neg_index += 1
            continue
        break

    cases: list[dict[str, Any]] = []
    for row in picked:
        study_id = (row.get("study_id") or "").strip()
        position = (row.get("position") or "").strip()
        size = (row.get("size") or "").strip()
        subtlety = (row.get("subtlety") or "").strip()
        state = (row.get("state") or "").strip()
        if state == "non-nodule":
            gold_text = "双肺未见明显结节。"
        else:
            gold_text = f"{_position_zh(position)}见结节影，大小约 {size or '?'}mm。"
        cases.append(
            {
                "accession_no": f"JSRT-SAMPLE-{len(cases) + 1:03d}",
                "gold_standard_text": gold_text,
                "image_ref": f"jsrt://{study_id}",
                "study_uid": f"STUDY-{study_id.replace('.png', '')}",
                "source_study_id": study_id,
                "has_nodule": state != "non-nodule",
                "nodule_size_mm": size,
                "nodule_position": position,
                "subtlety": subtlety,
            }
        )
    return cases


#: JSRT 位置写法 → 中文写法（评估表里给科室看的中文）。
_POSITION_ZH: Mapping[str, str] = {
    "l.upper lobe": "左肺上叶",
    "l.mid lobe": "左肺中叶",
    "l.lower lobe": "左肺下叶",
    "r.upper lobe": "右肺上叶",
    "r.mid lobe": "右肺中叶",
    "r.lower lobe": "右肺下叶",
}


def _position_zh(position: str) -> str:
    """位置写法转中文；带肺段后缀（如 ``l.upper lobe(S5)``）取所属肺叶。"""

    raw = position.strip()
    if not raw:
        return "肺内"
    base = raw.split("(")[0].strip()
    return _POSITION_ZH.get(base, raw or "肺内")


def write_sample_cases(
    metadata_path: Path | str,
    out_path: Path | str,
    *,
    limit: int = 5,
) -> Path:
    """把脱敏样例写到 JSON 文件。"""

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cases = build_sample_cases(metadata_path, limit=limit)
    target.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def iter_image_names(manifest: Manifest) -> Iterable[str]:
    """清单里全部图像名（已排序）。"""

    return sorted(manifest.images)
