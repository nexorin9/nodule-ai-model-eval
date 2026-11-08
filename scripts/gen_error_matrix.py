#!/usr/bin/env python3
"""生成错误码落地矩阵（``docs/error_matrix.md``）。

    python scripts/gen_error_matrix.py [--check]

``--check`` 只核对文件与当前代码是否一致（不一致时退出码非零），
CI / 收尾自检用得上 —— 错误码表改了但矩阵没重生成，当场就能看出来。

**这个脚本不改任何业务状态**，只是把 ``errors.py`` 与源码扫描的结果
渲染成一份可读的矩阵：每个错误码在哪儿触发、说什么话、能不能重试、
进不进指标分母、用户在哪儿看到。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from nodule_eval.error_matrix import (  # noqa: E402  (路径设好再导入)
    render_matrix_markdown,
    scan_hardcoded_error_text,
)

TARGET = ROOT / "docs" / "error_matrix.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成错误码落地矩阵")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只核对文件是否与当前代码一致（不一致时退出码非零）",
    )
    args = parser.parse_args(argv)

    violations = scan_hardcoded_error_text()
    if violations:
        print("错误文案被硬编码在业务代码里（应改走 errors.py）：")
        for item in violations:
            print(f"  {item}")
        return 1

    content = render_matrix_markdown()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current != content:
            print(f"{TARGET} 与当前代码不一致 —— 请重新运行本脚本生成。")
            return 2
        print(f"{TARGET} 与当前代码一致。")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(content, encoding="utf-8")
    print(f"已生成 {TARGET}（{len(content)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
