"""院内 RIS 检查清单端点的**协议替身**（只在测试层）。

模拟的是 ``spec.md`` 里那条**权威数据源**的读接口::

    GET {RIS_BASE_URL}/api/v1/imaging-studies?modality=DX&from=..&to=..&cursor=..
    Authorization: Bearer $RIS_API_TOKEN
    -> {"items": [{"accession_no", "study_uid", "modality", "study_date",
                   "report_status", "image_ref"}], "next_cursor": ...}

**支持注入的场景**：

- **一对多绑定**：``items`` 里放两条同 ``accession_no`` 的记录（不同时间点），
  产品侧必须报 ``AMBIGUOUS_BINDING`` 并挂起人工判定，**绝不自动取第一条**；
- **游标翻页**：``next_cursor`` 非空时下一页带上 ``cursor`` 参数，直到取空为止；
- **非 DX 记录**：模态不是 DX 的记录照样返回（过滤是产品侧的责任，替身不替产品做决定）；
- **连续 5xx**：注入 ``status_sequence=[500, 500, 500]``。

**纪律**：本模块只允许出现在 ``tests/`` 下，产品代码不 import 它。
"""

from __future__ import annotations

import argparse
import time
from typing import Any, Mapping, Sequence

from stub_base import BaseStub

#: 一条正常的 DX 检查记录。
DEFAULT_ITEM: dict[str, Any] = {
    "accession_no": "DEMO-0001",
    "study_uid": "STUDY-DEMO-001",
    "modality": "DX",
    "study_date": "2026-03-01",
    "report_status": "final",
    "image_ref": "dx://demo/study-001",
}

#: 一页放几条（0 = 不分页，一次全给）。
DEFAULT_PAGE_SIZE = 0


class RISStub(BaseStub):
    """RIS 检查清单端点替身。

    参数
    ----
    items
        全部检查记录（**不分页时**一次全给）。需要模拟一对多就放两条同检查号的记录。
    page_size
        每页条数；``0`` 表示不分页。分页时用 ``next_cursor`` 串起来。
    status_sequence
        依次返回的 HTTP 状态码。
    """

    endpoint_path = "/api/v1/imaging-studies"

    def __init__(
        self,
        *,
        items: Sequence[Mapping[str, Any]] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        status_sequence: Sequence[int] | None = None,
    ) -> None:
        super().__init__(status_sequence=status_sequence)
        self.items = [dict(item) for item in (items if items is not None else [DEFAULT_ITEM])]
        self.page_size = int(page_size)

    # ---- 响应编排 ----
    def next_response(self, request: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        index = min(len(self.requests) - 1, len(self.status_sequence) - 1)
        status = self.status_sequence[index]
        if status >= 400:
            return status, {"error": "stubbed failure", "status": status}
        return status, self.build_payload(request)

    def build_payload(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """按字段规范组装检查清单（含游标翻页）。"""

        current = request if request is not None else (self.requests[-1] if self.requests else None)
        if not current:
            return {"items": [], "next_cursor": ""}

        cursor = str(current.get("query", {}).get("cursor") or "")
        offset = int(cursor) if cursor.isdigit() else 0

        if self.page_size > 0:
            page = self.items[offset : offset + self.page_size]
            next_offset = offset + self.page_size
            next_cursor = str(next_offset) if next_offset < len(self.items) else ""
        else:
            page = self.items[offset:]
            next_cursor = ""

        return {"items": [dict(item) for item in page], "next_cursor": next_cursor}


def main() -> None:
    """命令行启动替身：``python tests/stub_ris.py --port 8789``。"""

    parser = argparse.ArgumentParser(description="RIS 检查清单协议替身（仅测试层）")
    parser.add_argument("--port", type=int, default=8789, help="监听端口（0 = 随机可用端口）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    stub = RISStub()
    stub.start(args.port)
    print(f"RIS 检查清单协议替身已启动：{stub.base_url}{RISStub.endpoint_path}")
    print("按 Ctrl-C 结束。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stub.stop()


if __name__ == "__main__":
    main()
