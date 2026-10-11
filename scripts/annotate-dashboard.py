#!/usr/bin/env python3
"""在 k6 Web Dashboard 匯出的 HTML 報告上加入中文教學說明，輸出新檔案，不改原檔。

用法：
    python3 scripts/annotate-dashboard.py <k6 匯出的 report.html> [輸出檔，預設 <原檔名>-annotated.html]

說明內容與邏輯在 report-job/annotate.py（報表 Job 也用同一份，對每個 task 的 dashboard 自動產生教學版）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "report-job"))

from annotate import annotate_html, is_annotated  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(src.stem + "-annotated.html")
    page = src.read_text(encoding="utf-8")
    if is_annotated(page):
        raise SystemExit(f"{src} 已經加過說明")
    dst.write_text(annotate_html(page), encoding="utf-8")
    print(f"已輸出：{dst}")


if __name__ == "__main__":
    main()
