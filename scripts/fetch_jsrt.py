#!/usr/bin/env python3
"""取 JSRT 公开数据集（247 例正位胸片 + 手绘结节标注）。

用法::

    python scripts/fetch_jsrt.py                      # 取全量并校验
    python scripts/fetch_jsrt.py --verify-only        # 只校验已缓存的，不联网
    python scripts/fetch_jsrt.py --sample-out tests/fixtures/jsrt/sample_cases.json

**这个脚本做什么**：把公开数据集抓下来，逐文件验 SHA256、验数量恰好 247 例，
供本产品做**真实规模**的评估与压测。

**它不是什么**：JSRT **不是**本产品的对接面 —— 院内真实数据一律走 RIS 适配器
（``GET {RIS_BASE_URL}/api/v1/imaging-studies``）。公开数据只进
``data/``（缓存，已在 ``.gitignore`` 里）与 ``tests/fixtures/``（脱敏样例）。

**断网时怎么办**：脚本**以非零退出码结束**并打印明确的降级提示，
不会静默跳过、更不会留下半份数据集却报「成功」。
已经缓存过的可以直接 ``--verify-only``，不必重新下载。

数据来源与许可见 README「数据来源」一节。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能从项目根直接跑（`python scripts/fetch_jsrt.py`）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nodule_eval.datasets import jsrt  # noqa: E402
from nodule_eval.datasets.jsrt import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    DatasetFetchError,
    fetch_dataset,
    verify_cached,
    write_sample_cases,
)

#: 退出码：成功。
EXIT_OK = 0
#: 退出码：取数或校验失败。
EXIT_FAILED = 3


def _progress(done: int, total: int | None) -> None:
    """下载进度（同一行刷新）。

    **只在终端上刷**：重定向到日志文件时每次 MB 变更都写一行，
    400MB 的归档会刷出几百行，把日志淹掉。
    """

    if not sys.stderr.isatty():
        return
    if total:
        percent = done * 100 // total
        sys.stderr.write(f"\r下载中 {done / 1e6:.1f}/{total / 1e6:.1f} MB（{percent}%）")
    else:
        sys.stderr.write(f"\r下载中 {done / 1e6:.1f} MB")
    sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="取 JSRT 公开数据集（247 例）并做逐文件 SHA256 与数量校验",
    )
    parser.add_argument(
        "--dest",
        default=str(DEFAULT_CACHE_DIR),
        metavar="DIR",
        help=f"缓存目录（缺省 {DEFAULT_CACHE_DIR}/）",
    )
    parser.add_argument(
        "--manifest",
        default="",
        metavar="FILE",
        help="清单路径（缺省 scripts/jsrt_manifest.json）",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="只校验已缓存的数据集，不联网",
    )
    parser.add_argument(
        "--sample-out",
        default="",
        metavar="FILE",
        help="同时导出脱敏样例病例（≤5 例）到该 JSON 文件",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        help="导出的样例例数（缺省 5）",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest) if args.manifest else jsrt.default_manifest_path()
    dest = Path(args.dest)

    try:
        if args.verify_only:
            result = verify_cached(dest, manifest_path=manifest_path)
            print(f"缓存目录：{dest}")
            print(f"校验通过 {result.checked} 例")
            if not result.ok:
                print("校验未通过：", file=sys.stderr)
                if not result.count_ok:
                    print(f"  - 数量不符（应为 247 例，实得 {result.checked} 例）", file=sys.stderr)
                if result.missing:
                    print(f"  - 缺 {len(result.missing)} 例", file=sys.stderr)
                if result.unexpected:
                    print(f"  - 多出 {len(result.unexpected)} 例", file=sys.stderr)
                if result.hash_mismatches:
                    print(
                        f"  - {len(result.hash_mismatches)} 例哈希不符："
                        f"{[name for name, _ in result.hash_mismatches[:3]]}",
                        file=sys.stderr,
                    )
                if not result.metadata_ok:
                    print("  - 元数据文件缺失或哈希不符", file=sys.stderr)
                return EXIT_FAILED
            print("全部通过。")
            return EXIT_OK

        report = fetch_dataset(
            dest,
            manifest_path=manifest_path,
            on_progress=_progress,
        )
        sys.stderr.write("\n")

        print(f"缓存目录：{report.cache_dir}")
        print(report.summary())
        print(f"元数据行数：{report.metadata_rows}")

        if report.failures:
            print("取数未完成：", file=sys.stderr)
            for failure in report.failures:
                print(f"  - {failure}", file=sys.stderr)
            print(
                "提示：可重跑本脚本续传；若镜像站不可达，"
                "请把归档 jsrt-archive.zip 放到缓存目录后重跑。",
                file=sys.stderr,
            )
            return EXIT_FAILED

        if args.sample_out:
            metadata_path = dest / "jsrt_metadata.csv"
            out = write_sample_cases(
                metadata_path,
                args.sample_out,
                limit=args.sample_limit,
            )
            print(f"脱敏样例已导出：{out}")

        print("全部通过。")
        return EXIT_OK

    except DatasetFetchError as exc:
        print(f"取数失败：{exc}", file=sys.stderr)
        print(
            "提示：JSRT 只用于本地真实规模验证，不是产品对接面；"
            "断网不影响院内数据路径（RIS / 推理服务 / 科研平台）。",
            file=sys.stderr,
        )
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
