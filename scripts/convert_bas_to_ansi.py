#!/usr/bin/env python3
"""把 ``.bas`` 宏源码转成 **VBA 编辑器读得懂的 ANSI 编码**。

**为什么需要这一步**：代码仓库里的 ``.bas`` 以 UTF-8 保存（便于看 diff、便于
CI 检查）。而 VBA 编辑器按**系统 ANSI 代码页**读 ``.bas`` —— 中文 Windows 是
GBK/GB18030。直接导入 UTF-8 的 ``.bas``，中文提示与中文表头别名会变成乱码
（ASCII 的字段名不受影响，宏仍然能跑，但提示是乱的）。

用法::

    python scripts/convert_bas_to_ansi.py src/nodule_eval/sheets/xlsm_vba
    python scripts/convert_bas_to_ansi.py src/nodule_eval/sheets/xlsm_vba --encoding gbk

默认输出到同目录下的 ``<同名>.ansi.bas``（**不覆盖原文件**）——
原文件保持 UTF-8，仓库里始终只有一份真源。

也可以用 ``--in-place`` 直接覆盖（会先备份成 ``*.bak``）。

依赖：仅标准库。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

#: Windows 中文代码页（GB18030 是 GBK 的超集，繁体与生僻字也能过）。
DEFAULT_ENCODING = "gb18030"

#: 中文 Windows 上 VBA 用的代码页别名。
ALIASES = {
    "gbk": "gb18030",
    "gb2312": "gb18030",
    "gb18030": "gb18030",
    "cp936": "gb18030",
    "big5": "cp950",
    "cp950": "cp950",
    "shift-jis": "cp932",
    "cp932": "cp932",
}


def resolve_encoding(name: str) -> str:
    """把编码别名解析成 Python 认识的名字。"""

    key = (name or "").strip().lower()
    if not key:
        return DEFAULT_ENCODING
    if key not in ALIASES:
        raise SystemExit(
            f"不认识的编码：{name}；可用：{'、'.join(sorted(ALIASES))}"
        )
    return ALIASES[key]


def convert_file(path: Path, encoding: str, *, in_place: bool) -> Path:
    """转一个 ``.bas`` 文件；返回实际写出的路径。"""

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # 已经是 ANSI 的（转过一遍），原样再转一次即可
        text = raw.decode(encoding, errors="replace")

    encoded = text.encode(encoding, errors="replace")
    if in_place:
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        path.write_bytes(encoded)
        return path

    target = path.with_suffix(".ansi.bas")
    target.write_bytes(encoded)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="convert_bas_to_ansi",
        description="把 .bas 宏源码转成 VBA 编辑器读得懂的 ANSI 编码",
    )
    parser.add_argument(
        "target",
        help="要转换的 .bas 文件或目录（目录则转其下全部 .bas）",
    )
    parser.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"目标代码页（默认 {DEFAULT_ENCODING}；可用：{'、'.join(sorted(ALIASES))}）",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="直接覆盖原文件（先备份成 *.bak）",
    )
    args = parser.parse_args(argv)

    encoding = resolve_encoding(args.encoding)
    target = Path(args.target)
    if not target.exists():
        print(f"[CONTRACT_INVALID] 路径不存在：{target}", file=sys.stderr)
        return 2

    files = sorted(target.glob("*.bas")) if target.is_dir() else [target]
    if not files:
        print(f"[CONTRACT_INVALID] 目录下没有 .bas 文件：{target}", file=sys.stderr)
        return 2

    for path in files:
        written = convert_file(path, encoding, in_place=args.in_place)
        print(f"已转换：{path} → {written}（{encoding}）")
    print("提示：在 VBA 编辑器里用「文件 → 导入文件」导入转换后的 .bas。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
