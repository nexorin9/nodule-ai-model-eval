"""``scripts/fetch_jsrt.py`` 的测试。

覆盖三件事：

1. **数量校验**：必须恰好 247 例 —— 少一例是下载被截断，多一例是镜像站塞了别的东西。
2. **哈希校验**：逐文件 SHA256 —— 内容被换掉（同名不同内容）必须被抓出来。
3. **断网降级**：取数失败时以**非零退出码**结束并给出明确提示，**不静默跳过**。

这些测试**不联网**：用本地构造的清单与小文件走同一条校验路径，
断网场景用「指向不可达地址 + 替换下载实现」来模拟。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
SCRIPTS = PROJECT_ROOT / "scripts"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from nodule_eval.datasets import jsrt  # noqa: E402
from nodule_eval.datasets.jsrt import DatasetFetchError, Manifest  # noqa: E402
import fetch_jsrt  # noqa: E402  —— scripts/ 下的命令行入口

#: 真实清单里钉死的例数。
EXPECTED_CASE_COUNT = 247


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_manifest() -> Manifest:
    """项目自带的真实清单（247 例的逐文件哈希）。"""

    return Manifest.load(jsrt.default_manifest_path())


@pytest.fixture
def tiny_manifest(tmp_path: Path) -> Manifest:
    """一个只有 3 例的小清单 —— 供构造「少一例」「多一例」等场景。"""

    payload = {
        "dataset": "TINY",
        "version": "test",
        "source": "local",
        "mirror_urls": ["https://example.invalid/tiny.zip"],
        "case_count": 3,
        "archive": {"sha256": "0" * 64, "bytes": 1},
        "metadata_file": {"name": "tiny_metadata.csv", "sha256": "0" * 64},
        "images": {
            "A001.png": hashlib.sha256(b"a1").hexdigest(),
            "A002.png": hashlib.sha256(b"a2").hexdigest(),
            "A003.png": hashlib.sha256(b"a3").hexdigest(),
        },
    }
    path = tmp_path / "tiny_manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return Manifest.load(path)


def _write_images(directory: Path, manifest: Manifest, names: list[str]) -> None:
    """按清单内容写出图像文件（内容与哈希一致）。"""

    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        expected = manifest.images[name]
        # 内容必须与哈希对得上：写原始字节（sha256(b"a1") 对应 A001.png）。
        payload = {"A001.png": b"a1", "A002.png": b"a2", "A003.png": b"a3"}[name]
        assert hashlib.sha256(payload).hexdigest() == expected
        (directory / name).write_bytes(payload)


# --------------------------------------------------------------------------
# 清单本身
# --------------------------------------------------------------------------


def test_manifest_pins_247_cases(real_manifest: Manifest) -> None:
    """清单必须钉死 247 例 —— 这是本产品的规模验收基准。"""

    assert real_manifest.case_count == EXPECTED_CASE_COUNT
    assert len(real_manifest.images) == EXPECTED_CASE_COUNT
    assert real_manifest.archive_sha256
    assert real_manifest.metadata_sha256
    assert real_manifest.mirror_urls


def test_manifest_covers_both_positive_and_negative_cases(real_manifest: Manifest) -> None:
    """清单里必须同时有结节例与阴性例（否则特异度无从谈起）。"""

    names = list(real_manifest.images)
    nodule = [n for n in names if n.startswith("JPCLN")]
    non_nodule = [n for n in names if n.startswith("JPCNN")]
    assert len(nodule) == 154
    assert len(non_nodule) == 93
    assert nodule and non_nodule


# --------------------------------------------------------------------------
# 数量校验
# --------------------------------------------------------------------------


def test_verify_detects_missing_case(tiny_manifest: Manifest, tmp_path: Path) -> None:
    """少一例 → 数量校验不通过，且指名缺哪一例。"""

    images = tmp_path / "images"
    _write_images(images, tiny_manifest, ["A001.png", "A002.png"])

    result = jsrt.verify_directory(images, tiny_manifest)

    assert result.count_ok is False
    assert result.missing == ["A003.png"]
    assert result.ok is False


def test_verify_detects_extra_case(tiny_manifest: Manifest, tmp_path: Path) -> None:
    """多一例 → 数量校验不通过（镜像站塞了别的东西也算）。"""

    images = tmp_path / "images"
    _write_images(images, tiny_manifest, ["A001.png", "A002.png", "A003.png"])
    (images / "A999.png").write_bytes(b"unexpected")

    result = jsrt.verify_directory(images, tiny_manifest)

    assert result.count_ok is False
    assert result.unexpected == ["A999.png"]


def test_verify_passes_on_exact_count(tiny_manifest: Manifest, tmp_path: Path) -> None:
    """数量恰好 + 哈希全对 → 校验通过。"""

    images = tmp_path / "images"
    _write_images(images, tiny_manifest, ["A001.png", "A002.png", "A003.png"])

    result = jsrt.verify_directory(images, tiny_manifest)

    assert result.count_ok is True
    assert result.checked == 3
    assert result.ok is True


# --------------------------------------------------------------------------
# 哈希校验
# --------------------------------------------------------------------------


def test_verify_detects_content_swap(tiny_manifest: Manifest, tmp_path: Path) -> None:
    """同名不同内容 → 哈希校验必须抓出来（这正是逐文件 SHA256 的用武之地）。"""

    images = tmp_path / "images"
    _write_images(images, tiny_manifest, ["A001.png", "A002.png", "A003.png"])
    # 把 A002 的内容换成别的 —— 数量不变、文件名不变，只有哈希能发现。
    (images / "A002.png").write_bytes(b"tampered")

    result = jsrt.verify_directory(images, tiny_manifest)

    assert result.count_ok is True
    assert len(result.hash_mismatches) == 1
    assert result.hash_mismatches[0][0] == "A002.png"
    assert result.ok is False


def test_verify_detects_missing_metadata(tiny_manifest: Manifest, tmp_path: Path) -> None:
    """元数据缺失 → 校验不通过（元数据是金标准字段的来源，缺了没法归并）。"""

    images = tmp_path / "images"
    _write_images(images, tiny_manifest, ["A001.png", "A002.png", "A003.png"])

    result = jsrt.verify_directory(images, tiny_manifest, metadata_path=tmp_path / "none.csv")

    assert result.metadata_ok is False
    assert result.ok is False


def test_extract_rejects_corrupt_archive(tiny_manifest: Manifest, tmp_path: Path) -> None:
    """归档哈希不符 → 明确报错（不是解出一堆错字节再逐个报不匹配）。"""

    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"not a real zip at all")

    with pytest.raises(DatasetFetchError) as excinfo:
        jsrt.extract_archive(archive, tmp_path / "out", tiny_manifest)

    assert "SHA256" in str(excinfo.value)


# --------------------------------------------------------------------------
# 断网降级：非零退出码 + 不静默跳过
# --------------------------------------------------------------------------


def test_fetch_raises_on_network_failure(tiny_manifest: Manifest, tmp_path: Path, monkeypatch) -> None:
    """下载失败必须抛错 —— 绝不返回「看起来成功」的半份数据集。"""

    manifest_path = tmp_path / "tiny_manifest.json"
    manifest_path.write_text(json.dumps(tiny_manifest.raw), encoding="utf-8")

    def _boom(*args, **kwargs):
        raise DatasetFetchError("镜像站不可达：ConnectionError: connection refused")

    monkeypatch.setattr(jsrt, "download_archive", _boom)

    with pytest.raises(DatasetFetchError) as excinfo:
        jsrt.fetch_dataset(tmp_path / "cache", manifest_path=manifest_path)

    assert "不可达" in str(excinfo.value)


def test_cli_exits_nonzero_when_offline(monkeypatch, capsys, tmp_path: Path) -> None:
    """断网时命令行以**非零退出码**结束，并打印降级提示。"""

    def _boom(*args, **kwargs):
        raise DatasetFetchError("镜像站不可达：ConnectionError: connection refused")

    monkeypatch.setattr(jsrt, "download_archive", _boom)
    monkeypatch.setattr(
        jsrt,
        "default_manifest_path",
        lambda: Path(__file__).resolve().parents[1] / "scripts" / "jsrt_manifest.json",
    )

    code = fetch_jsrt.main(["--dest", str(tmp_path / "cache")])

    captured = capsys.readouterr()
    assert code != 0
    assert code == fetch_jsrt.EXIT_FAILED
    assert "取数失败" in captured.err
    assert "不影响院内数据路径" in captured.err


def test_cli_verify_only_reports_failure_without_network(monkeypatch, capsys, tmp_path: Path) -> None:
    """``--verify-only`` 不联网；缓存不全时同样非零退出。"""

    def _no_network(*args, **kwargs):  # pragma: no cover - 触发即测试失败
        raise AssertionError("--verify-only 不应发起网络请求")

    monkeypatch.setattr(jsrt, "download_archive", _no_network)
    monkeypatch.setattr(
        jsrt,
        "default_manifest_path",
        lambda: Path(__file__).resolve().parents[1] / "scripts" / "jsrt_manifest.json",
    )

    code = fetch_jsrt.main(["--verify-only", "--dest", str(tmp_path / "empty-cache")])

    captured = capsys.readouterr()
    assert code == fetch_jsrt.EXIT_FAILED
    assert "校验未通过" in captured.err


def test_cli_verify_only_passes_on_complete_cache(monkeypatch, capsys, tmp_path: Path) -> None:
    """缓存完整时 ``--verify-only`` 以 0 退出。"""

    def _no_network(*args, **kwargs):  # pragma: no cover - 触发即测试失败
        raise AssertionError("--verify-only 不应发起网络请求")

    monkeypatch.setattr(jsrt, "download_archive", _no_network)

    cache = tmp_path / "cache"
    images = cache / "images"
    images.mkdir(parents=True)
    patched = json.loads(jsrt.default_manifest_path().read_text(encoding="utf-8"))
    # 用空文件占位：把清单里的哈希换成空文件的哈希 —— 校验路径与真实一致。
    for name in patched["images"]:
        (images / name).write_bytes(b"")
    patched["images"] = {
        name: hashlib.sha256(b"").hexdigest() for name in patched["images"]
    }
    metadata = cache / "jsrt_metadata.csv"
    metadata.write_text("study_id\n", encoding="utf-8")
    patched["metadata_file"]["sha256"] = hashlib.sha256(b"study_id\n").hexdigest()

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(patched), encoding="utf-8")
    monkeypatch.setattr(jsrt, "default_manifest_path", lambda: manifest_path)

    code = fetch_jsrt.main(["--verify-only", "--dest", str(cache)])

    captured = capsys.readouterr()
    assert code == fetch_jsrt.EXIT_OK
    assert "全部通过" in captured.out


# --------------------------------------------------------------------------
# 脱敏样例
# --------------------------------------------------------------------------


def test_sample_cases_are_desensitized(real_manifest: Manifest, tmp_path: Path) -> None:
    """导出的样例不得带 age / gender / diagnosis（个人相关数据 + 超出口径）。"""

    metadata = tmp_path / "jsrt_metadata.csv"
    metadata.write_text(
        "study_id,subtlety,size,age,gender,x,y,state,position,diagnosis\n"
        "JPCLN001.png,5,15,53,Male,1634,692,malignant,l.upper lobe,lung cancer\n"
        "JPCNN001.png,,,70,Female,,,non-nodule,,\n",
        encoding="utf-8",
    )

    cases = jsrt.build_sample_cases(metadata, limit=2)

    assert len(cases) == 2
    for case in cases:
        assert "age" not in case
        assert "gender" not in case
        assert "diagnosis" not in case
        # 检查号是占位号，不是看起来像真实住院流水的东西。
        assert case["accession_no"].startswith("JSRT-SAMPLE-")


def test_sample_cases_cover_both_positive_and_negative(real_manifest: Manifest) -> None:
    """样例必须同时含阳性例与阴性例 —— 否则特异度算不出来。"""

    samples_path = PROJECT_ROOT / "tests" / "fixtures" / "jsrt" / "sample_cases.json"
    if not samples_path.is_file():  # pragma: no cover - 样例由脚本生成
        pytest.skip("样例尚未生成（先跑 scripts/fetch_jsrt.py --sample-out）")

    cases = json.loads(samples_path.read_text(encoding="utf-8"))
    assert cases
    assert any(case["has_nodule"] for case in cases)
    assert any(not case["has_nodule"] for case in cases)
    assert len(cases) <= 5
