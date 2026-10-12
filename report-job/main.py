"""壓測報表 Job：合併所有壓測機（task）目前已上傳的 k6 原始數據，產生單一 HTML 報告。

部署成 Cloud Run Job，需要看報告時在 Console 按「執行」；本機模擬時直接執行：
    python3 main.py --source .local-gcs/runs/<RUN_ID>

環境變數（或對應的命令列參數）：
    SOURCE_URI        必填：gs://<bucket>/<prefix>/<RUN_ID> 或本機資料夾
    RUN_ID            顯示用，預設取 SOURCE_URI 的最後一段
    EXPECTED_TASKS    預期 task 數（用來提示缺少的 task），選填
    REPORT_TITLE      報告標題，預設「新阿發 壓力測試報告」
    THRESHOLDS_JSON   參考門檻（毫秒／比例），預設為規劃 v1.4 的證券方文件參考值
    ANNOTATE_DASHBOARDS  預設 true：替每個已結束 task 的 k6 dashboard 產生教學版（tasks/<task>/dashboard-annotated.html）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aggregate
import annotate
import render
from sources import GcsStore, open_store, read_json

DEFAULT_THRESHOLDS = {
    "faq": {"p95": 10000, "p99": 20000},
    "business": {"p95": 5000, "p99": 10000},
    "error_rate": 0.005,
}
STAGE_ORDER = list(render.STAGE_NAMES.keys())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=os.environ.get("SOURCE_URI"))
    ap.add_argument("--run-id", default=os.environ.get("RUN_ID"))
    ap.add_argument("--expected-tasks", type=int, default=int(os.environ["EXPECTED_TASKS"]) if os.environ.get("EXPECTED_TASKS") else None)
    ap.add_argument("--title", default=os.environ.get("REPORT_TITLE", "新阿發 壓力測試報告"))
    args = ap.parse_args()
    if not args.source:
        ap.error("需要 --source 或環境變數 SOURCE_URI")

    thresholds = json.loads(os.environ["THRESHOLDS_JSON"]) if os.environ.get("THRESHOLDS_JSON") else DEFAULT_THRESHOLDS
    run_id = args.run_id or args.source.rstrip("/").rsplit("/", 1)[-1]
    store = open_store(args.source)
    started = time.time()

    raw_files = [f for f in store.list("raw") if f.endswith(".csv.gz")]
    print(f"[report] {args.source}：找到 {len(raw_files)} 個 raw 檔", flush=True)

    collector = aggregate.Collector()
    warnings: list[str] = []
    for rel in raw_files:
        task = rel.rsplit("/", 1)[-1].removesuffix(".csv.gz")
        status = read_json(store, f"status/{task}.json")
        info = collector.add_task(task, store.read_chunks(rel), status)
        print(f"[report]   {task}: {info.requests:,} 筆請求（{status.get('state', '未知狀態')}）", flush=True)
        if info.corrupt:
            warnings.append(f"{task} 的原始數據中段損毀，只使用到損毀前的資料。")

    if os.environ.get("ANNOTATE_DASHBOARDS", "true") == "true":
        annotate_dashboards(store, collector.tasks)

    expected = args.expected_tasks or max((t.status.get("task_count") or 0 for t in collector.tasks), default=0) or None
    running = [t.name for t in collector.tasks if t.status.get("state") != "finished"]
    if running:
        warnings.append(f"壓測仍在進行或未正常結束的 task：{', '.join(running)}。此報告為「截至目前」的資料。")

    result = aggregate.compute(collector, STAGE_ORDER)
    generated_ms = time.time() * 1000
    page = render.render(result, run_id=run_id, title=args.title, thresholds=thresholds,
                         generated_ms=generated_ms, expected_tasks=expected, warnings=warnings)

    stamp = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%d-%H%M%S")
    data = page.encode("utf-8")
    out = store.write(f"reports/report-{stamp}.html", data, "text/html; charset=utf-8")
    latest = store.write("reports/latest.html", data, "text/html; charset=utf-8")
    store.write(f"reports/summary-{stamp}.json", json.dumps(_summary_json(result), ensure_ascii=False, indent=2).encode(), "application/json")

    print(f"[report] 完成：{result.get('requests', 0):,} 筆請求，耗時 {time.time() - started:.1f} 秒", flush=True)
    print(f"[report] 報告：{out}", flush=True)
    print(f"[report] 最新：{latest}", flush=True)
    if isinstance(store, GcsStore):
        print(f"[report] 下載（需有 bucket 讀取權限）：{store.console_url(f'reports/report-{stamp}.html')}", flush=True)
    return 0


def annotate_dashboards(store, tasks) -> None:
    """每個 task 結束時會上傳自己的 k6 dashboard；在旁邊產生加上中文說明的教學版，合併報告會連結過去。"""
    for info in tasks:
        page = store.read_text(f"tasks/{info.name}/dashboard.html")
        if not page:
            continue  # 該 task 尚未結束，或執行時間太短 k6 沒有產出 dashboard
        try:
            annotated = annotate.annotate_html(page, annotate.task_banner(info.name, info.status.get("task_count")))
        except ValueError as exc:
            print(f"[report]   {info.name}: dashboard 無法加註（{exc}）", flush=True)
            continue
        store.write(f"tasks/{info.name}/dashboard-annotated.html", annotated.encode("utf-8"), "text/html; charset=utf-8")
        info.dashboard = True
        print(f"[report]   {info.name}: 已產生教學版 dashboard", flush=True)


def _summary_json(result: dict) -> dict:
    """給後續分析用的精簡 JSON（不含時間序列）。"""
    if not result.get("requests"):
        return {"requests": 0}
    return {
        "requests": result["requests"],
        "data_from_ms": result["t0_ms"],
        "data_until_ms": result["t1_ms"],
        "overall": result["overall"],
        "stages": result["stages"],
        "counters": result["counters"],
        "tasks": [{"name": t.name, "requests": t.requests, "last_ts": t.last_ts, "status": t.status} for t in result["tasks"]],
    }


if __name__ == "__main__":
    sys.exit(main())
