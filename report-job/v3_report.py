"""v3 分開測試的合併報表：腳本 A 業務查詢、腳本 B FAQ 問答各自的原始數據 → 一份 HTML（A、B 分區，不混算）。

由 control 的 onprem/bin/k6suite 在兩支腳本跑完後呼叫；也可以手動重跑：
    python3 v3_report.py --suite-dir /var/lib/k6-stress/suites/<SUITE_ID> --runs-dir /var/lib/k6-stress/runs

讀取：
    <suite-dir>/suite.env、state.env   k6suite 寫的本組參數（負載、門檻、自動停止）與各腳本的結束原因
    <runs-dir>/<RUN_A>、<RUN_B>        各腳本的 raw/task-*.csv.gz（k6 CSV）與 status/task-*.json
輸出（<suite-dir>/reports/）：
    report-<SUITE_ID>.html、latest.html   合併報表（給長官看的版本：結論在最前面，列印時 A、B 各自分頁）
    summary.json                          數字摘要
    verdict.env                           A_VERDICT／B_VERDICT 等，k6menu 顯示結論用

計算口徑（與 script-qa.js 的對話回應時間報告相同）：
- 對話＝使用者提問的一問一答（一次 /api/chat/ask）；開場問候照送，但不計入統計，只另外列出
- 回應時間＝k6 http_req_duration（送出到收完回應，含 AI 生成時間）
- 失敗＝k6 腳本寫的 qa_failed（5xx、逾時、HTTP 429、回應內容不符預期）
- 判定只看「固定負載期間」（不含爬升與收尾）；沒有固定負載期間的數據時（例如爬升中就停止）改用整輪
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aggregate import iter_gzip_lines, parse_tags, percentile
from render import line_chart

TZ = ZoneInfo("Asia/Taipei")

PARTS = {
    "A": {"name": "業務查詢", "flow": "business", "max_key": "A_MAX_MS", "color": "#0d9488"},
    "B": {"name": "FAQ 問答", "flow": "faq", "max_key": "B_MAX_MS", "color": "#7c3aed"},
}
# 與 runner/k6/lib/scenarios.js 的 CATALOG 相同
CASE_NAMES = {
    "B1": "開啟聊天（開場問候）",
    "B2": "FAQ 問答",
    "B2-2": "FAQ intent",
    "B3": "多輪對話",
    "B4": "知識庫以外問題",
    "B5": "Model Armor 攔截",
    "C1": "查看 ETF 清單",
}
CHAT_FLOWS = ("faq", "business", "init")
KIND_NAMES = {"smoke": "試跑", "x1": "正式測試 1 倍", "x3": "正式測試 3 倍"}
REASON_NAMES = {
    "completed": "已跑完",
    "aborted": "自動停止（失敗率超過設定）",
    "stopped": "手動停止",
    "skipped": "未執行",
    "start_failed": "無法啟動",
    "error": "異常結束",
}
WANTED = ("http_req_duration,", "qa_failed,", "qa_opening_failed,", "dropped_iterations,")


# ---------------- 讀取 ----------------

def read_env(path: str) -> dict[str, str]:
    """讀 k6suite 以 printf %q 寫出的 KEY=VALUE（只有英數與 ;:/. 等字元）。"""
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            try:
                parts = shlex.split(value)
                out[key] = parts[0] if parts else ""
            except ValueError:
                out[key] = value
    return out


class RunData:
    """一支腳本（一個 RUN_ID）的聊天 API 數據。"""

    def __init__(self):
        # (ts_ms, 回應時間 ms, flow, case, phase, HTTP 狀態)
        self.reqs: list[tuple[float, float, str, str, str, int]] = []
        # (ts_ms, 失敗?, flow, case, phase)；開場問候另存
        self.qa: list[tuple[float, bool, str, str, str]] = []
        self.opening: list[tuple[float, bool, str, str, str]] = []
        self.dropped = 0.0
        self.tasks: list[dict] = []  # 各 runner 的 status json 與請求數

    @property
    def empty(self) -> bool:
        return not self.reqs and not self.qa


def load_run(run_dir: str) -> RunData:
    data = RunData()
    raw_dir = os.path.join(run_dir, "raw")
    if not os.path.isdir(raw_dir):
        return data
    for fname in sorted(os.listdir(raw_dir)):
        if not fname.endswith(".csv.gz"):
            continue
        task = fname.removesuffix(".csv.gz")
        status = {}
        try:
            with open(os.path.join(run_dir, "status", f"{task}.json"), encoding="utf-8") as f:
                status = json.load(f)
        except (OSError, ValueError):
            pass
        n = _add_task(data, os.path.join(raw_dir, fname))
        data.tasks.append({"name": task, "requests": n, "state": status.get("state"), "k6_exit": status.get("k6_exit")})
    return data


def _read_chunks(path: str, size: int = 1 << 20):
    with open(path, "rb") as f:
        while True:
            chunk = f.read(size)
            if not chunk:
                return
            yield chunk


def _add_task(data: RunData, path: str) -> int:
    lines = iter_gzip_lines(_read_chunks(path))
    header_line = next(lines, None)
    if not header_line:
        return 0
    header = next(csv.reader([header_line]))
    col = {name: i for i, name in enumerate(header)}
    i_metric, i_ts, i_val = col["metric_name"], col["timestamp"], col["metric_value"]
    i_status, i_extra = col["status"], col["extra_tags"]
    width = len(header)
    n = 0
    for row in csv.reader(line for line in lines if line.startswith(WANTED)):
        if len(row) < width:
            continue
        try:
            ts = float(row[i_ts])
            value = float(row[i_val])
        except ValueError:
            continue
        if ts < 1e11:
            ts *= 1000.0
        metric = row[i_metric]
        if metric == "dropped_iterations":
            data.dropped += value
            continue
        tags = parse_tags(row[i_extra])
        flow = tags.get("flow", "")
        if flow not in CHAT_FLOWS:
            continue  # 前端的進站、頁面、泡泡請求不計入
        case, phase = tags.get("case", ""), tags.get("phase", "")
        if metric == "http_req_duration":
            status = int(row[i_status]) if row[i_status].isdigit() else 0
            data.reqs.append((ts, value, flow, case, phase, status))
            n += 1
        elif metric == "qa_failed":
            data.qa.append((ts, value >= 0.5, flow, case, phase))
        elif metric == "qa_opening_failed":
            data.opening.append((ts, value >= 0.5, flow, case, phase))
    return n


# ---------------- 計算 ----------------

def _lat(durs: list[float], limit: float | None, pct: float) -> dict:
    durs = sorted(durs)
    n = len(durs)
    return {
        "count": n,
        "avg": sum(durs) / n if n else None,
        "p50": percentile(durs, 0.50),
        "p90": percentile(durs, 0.90),
        "p95": percentile(durs, 0.95),
        "p99": percentile(durs, 0.99),
        "max": durs[-1] if n else None,
        "judged": percentile(durs, pct / 100.0),
        "within": (sum(1 for d in durs if d <= limit) / n) if (n and limit) else None,
    }


def _span_s(ts: list[float]) -> float:
    return max(1.0, (max(ts) - min(ts)) / 1000.0) if ts else 0.0


def _nice_bucket(total_s: float, max_points: int = 150) -> int:
    for b in (1, 2, 5, 10, 15, 30, 60, 120, 300):
        if total_s / b <= max_points:
            return b
    return 600


def compute_part(data: RunData, *, flow: str, limit_ms: float, pct: float, fail_rate_limit: float) -> dict:
    if data.empty:
        return {"empty": True}
    hold_reqs = [r for r in data.reqs if r[4] == "hold"]
    hold_qa = [q for q in data.qa if q[4] == "hold"]
    basis = "hold"
    if not hold_qa:  # 爬升中就停止：沒有固定負載期間，改用整輪
        hold_reqs, hold_qa, basis = data.reqs, data.qa, "all"

    def summary(reqs, qa, secs):
        durs = [r[1] for r in reqs if r[2] == flow]
        s = _lat(durs, limit_ms, pct)
        total = len(qa)
        failed = sum(1 for q in qa if q[1])
        # 完成對話數＝有正常回覆的一問一答（對話筆數 − 失敗筆數）；每秒完成數＝實際處理量
        s.update({"total": total, "failed": failed, "completed": total - failed,
                  "fail_rate": (failed / total) if total else None,
                  "tps": (total / secs) if secs else None,
                  "completed_tps": ((total - failed) / secs) if secs else None})
        return s

    hold_secs = _span_s([r[0] for r in hold_reqs])
    all_secs = _span_s([r[0] for r in data.reqs])
    judged = summary(hold_reqs, hold_qa, hold_secs)
    judged["chat_tps"] = (len(hold_reqs) / hold_secs) if hold_secs else None  # 含開場問候＝設定的 TPS
    whole = summary(data.reqs, data.qa, all_secs)

    # 各情境（只計判定期間）
    cases = []
    case_ids = sorted({q[3] for q in hold_qa}, key=lambda c: list(CASE_NAMES).index(c) if c in CASE_NAMES else 99)
    for cid in case_ids:
        reqs = [r for r in hold_reqs if r[3] == cid]
        qa = [q for q in hold_qa if q[3] == cid]
        s = summary(reqs, qa, hold_secs)
        s.update({"case": cid, "name": CASE_NAMES.get(cid, cid), "share": (len(qa) / len(hold_qa)) if hold_qa else None})
        cases.append(s)

    # 失敗原因（判定期間）：HTTP 層看得到的分類；其餘是 HTTP 正常但回應內容不符預期
    causes = {"timeout": 0, "5xx": 0, "429": 0, "4xx": 0}
    for r in hold_reqs:
        if r[2] != flow:
            continue
        st = r[5]
        if st == 0 or st in (408, 504):
            causes["timeout"] += 1
        elif st >= 500:
            causes["5xx"] += 1
        elif st == 429:
            causes["429"] += 1
        elif 400 <= st < 500:
            causes["4xx"] += 1
    causes["content"] = max(0, judged["failed"] - sum(causes.values()))

    # 開場問候（不計入，只列出）
    op_durs = [r[1] for r in (hold_reqs if basis == "hold" else data.reqs) if r[2] == "init"]
    op = [o for o in data.opening if (o[4] == "hold" or basis == "all")]
    opening = {"total": len(op), "failed": sum(1 for o in op if o[1]), "p95": percentile(sorted(op_durs), 0.95)}

    # 時間序列
    t0 = min(r[0] for r in data.reqs) if data.reqs else min(q[0] for q in data.qa)
    t1 = max(r[0] for r in data.reqs) if data.reqs else max(q[0] for q in data.qa)
    total_s = (t1 - t0) / 1000.0
    bucket = _nice_bucket(total_s)
    nb = int(total_s // bucket) + 1
    chat = [0] * nb
    durs_b = [[] for _ in range(nb)]
    qa_n, qa_f = [0] * nb, [0] * nb
    for r in data.reqs:
        b = int((r[0] - t0) / 1000.0 // bucket)
        chat[b] += 1
        if r[2] == flow:
            durs_b[b].append(r[1])
    for q in data.qa:
        b = int((q[0] - t0) / 1000.0 // bucket)
        if 0 <= b < nb:
            qa_n[b] += 1
            qa_f[b] += 1 if q[1] else 0
    series = {
        "t": [b * bucket for b in range(nb)],
        "chat_tps": [x / bucket for x in chat],
        "qa_tps": [x / bucket for x in qa_n],
        "done_tps": [(qa_n[b] - qa_f[b]) / bucket for b in range(nb)],
        "lat": [percentile(sorted(v), pct / 100.0) for v in durs_b],
        "fail_rate": [(qa_f[b] / qa_n[b]) if qa_n[b] else None for b in range(nb)],
    }
    spans: dict[str, list[float]] = {}
    for r in data.reqs:
        if r[4] in ("ramp", "hold"):
            s = spans.setdefault(r[4], [r[0], r[0]])
            s[0], s[1] = min(s[0], r[0]), max(s[1], r[0])
    bands = [{"stage": "", "phase": ph, "start": (a - t0) / 1000.0, "end": (b - t0) / 1000.0} for ph, (a, b) in spans.items()]
    bands.sort(key=lambda x: x["start"])

    checks = []
    if judged["count"]:
        v = judged["judged"]
        checks.append(("latency", v is not None and v <= limit_ms))
    if judged["fail_rate"] is not None:
        checks.append(("fail_rate", judged["fail_rate"] <= fail_rate_limit))
    return {
        "empty": False, "basis": basis, "judged": judged, "whole": whole, "cases": cases, "causes": causes,
        "opening": opening, "series": series, "bands": bands, "checks": checks,
        "t0_ms": t0, "t1_ms": t1, "hold_s": hold_secs, "duration_s": total_s,
        "dropped": data.dropped, "tasks": data.tasks,
    }


def verdict(part: dict, reason: str) -> str:
    """pass／fail／incomplete（手動停止、異常）／nodata。"""
    if part.get("empty"):
        return "nodata"
    if reason == "aborted":
        return "fail"
    if reason in ("stopped", "error", "start_failed"):
        return "incomplete"
    if not part["checks"]:
        return "nodata"
    return "pass" if all(ok for _, ok in part["checks"]) else "fail"


# ---------------- 格式 ----------------

def e(x) -> str:
    import html
    return html.escape(str(x))


def sec(ms, digits=2):
    return "—" if ms is None else f"{ms / 1000:.{digits}f}"


def pct_txt(r, digits=2):
    return "—" if r is None else f"{r * 100:.{digits}f}%"


def num(x):
    return "—" if x is None else f"{x:,.0f}"


def rate1(x):
    return "—" if x is None else f"{x:,.1f}"


def hm(ms, fmt="%H:%M"):
    return "—" if not ms else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TZ).strftime(fmt)


def g(x):
    return f"{x:g}"


def plan_text(plan: str) -> tuple[str, float]:
    """x3:354tps:10:1 → （爬升 1 分鐘到 354 TPS，固定負載 10 分鐘, 354）"""
    parts, peak = [], 0.0
    for entry in [p for p in plan.split(";") if p]:
        f = entry.split(":")
        load = f[1].removesuffix("tps") if len(f) > 1 else "?"
        hold = f[2] if len(f) > 2 else "?"
        ramp = f[3] if len(f) > 3 else "?"
        try:
            peak = max(peak, float(load))
        except ValueError:
            pass
        parts.append(f"爬升 {ramp} 分鐘到 {load} TPS，固定負載 {hold} 分鐘")
    return "；".join(parts), peak


def weights_text(w: str) -> str:
    items = []
    for p in [x for x in w.split(";") if x]:
        cid, _, wt = p.partition(":")
        items.append((cid, float(wt or 0)))
    total = sum(wt for _, wt in items) or 1
    return "、".join(f"{cid} {CASE_NAMES.get(cid, '')} {wt / total * 100:.0f}%" for cid, wt in items)


# ---------------- HTML ----------------

CSS = """
:root{--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f4f5f7;--card:#fff;--ok:#15803d;--ok-bg:#ecfdf3;--ng:#b91c1c;--ng-bg:#fef2f2;
--warn:#a16207;--warn-bg:#fefce8;--a:#0d9488;--b:#7c3aed;--accent:#1d4ed8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"Segoe UI","PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 16px 72px}
header.top{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px 28px}
header.top h1{margin:0;font-size:26px;letter-spacing:.5px}
header.top .sub{color:var(--muted);margin-top:2px;font-size:15px}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:4px 24px;margin-top:16px;font-size:13px}
.meta div span{display:block;color:var(--muted);font-size:12px}
h2{font-size:20px;margin:40px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--ink)}
h2 small{font-size:13px;color:var(--muted);font-weight:normal;margin-left:8px}
h3{font-size:16px;margin:28px 0 8px}
.verdicts{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.vcard{background:var(--card);border:1px solid var(--line);border-top:6px solid var(--c);border-radius:12px;padding:20px 22px}
.vcard .head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.vcard .who{font-size:13px;color:var(--muted)}.vcard .name{font-size:20px;font-weight:700}
.badge{font-size:18px;font-weight:700;padding:6px 14px;border-radius:999px;white-space:nowrap}
.badge.pass{color:var(--ok);background:var(--ok-bg)}.badge.fail{color:var(--ng);background:var(--ng-bg)}
.badge.incomplete,.badge.nodata{color:var(--warn);background:var(--warn-bg)}
.metric{margin-top:16px}.metric .label{font-size:13px;color:var(--muted)}
.metric .val{font-size:30px;font-weight:700;line-height:1.2}.metric .val small{font-size:14px;font-weight:normal;color:var(--muted);margin-left:6px}
.gauge{position:relative;height:8px;background:#eef0f3;border-radius:4px;margin-top:6px}
.gauge i{position:absolute;left:0;top:0;bottom:0;border-radius:4px}
.gauge b{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--ink)}
.facts{display:flex;flex-wrap:wrap;gap:4px 20px;margin-top:14px;font-size:13px;color:var(--muted)}.facts b{color:var(--ink)}
.callout{margin-top:12px;padding:8px 12px;border-radius:8px;font-size:13px;background:var(--warn-bg);color:var(--warn)}
.summary{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--accent);border-radius:8px;padding:14px 18px;margin-top:16px;font-size:16px}
.timeline{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 22px}
.tl{display:flex;height:34px;border-radius:6px;overflow:hidden;font-size:13px;color:#fff;font-weight:600}
.tl div{display:flex;align-items:center;justify-content:center;white-space:nowrap;overflow:hidden;padding:0 6px}
.tl .gap{background:#d1d5db;color:#374151}.tl .rep{background:#9ca3af}
.tl-axis{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:4px}
ul.how{margin:14px 0 0;padding-left:20px;font-size:14px}ul.how li{margin:2px 0}
.part{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 24px 22px;margin-top:16px}
.part-title{display:flex;align-items:center;gap:12px;margin:18px 0 4px}
.part-title .tag{background:var(--c);color:#fff;font-weight:700;border-radius:6px;padding:2px 10px}
.part-title .t{font-size:20px;font-weight:700}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-top:12px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:10px 12px}.kpi span{display:block;font-size:12px;color:var(--muted)}.kpi b{font-size:20px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}
th,td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:right;white-space:nowrap}
th{background:#f9fafb;font-weight:600;color:#374151;border-top:1px solid var(--line)}
td.l,th.l{text-align:left;white-space:normal}
tr.total td{font-weight:700;background:#fafafa}
.ok{color:var(--ok);font-weight:700}.ng{color:var(--ng);font-weight:700}.muted{color:var(--muted)}
.hint{font-size:13px;color:var(--muted);margin:6px 0 0}
figure{margin:18px 0 0}figcaption{font-weight:600;font-size:14px;margin-bottom:4px}
.chart{width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:#fff}
.chart text{font-size:11px;fill:#6b7280}.chart .band{text-anchor:middle;font-size:11px;fill:#4338ca}
.chart .ytick{text-anchor:end}.chart .xtick{text-anchor:middle}.chart .ref{text-anchor:end;font-size:11px}.chart .grid{stroke:#eef0f3}
.legend{font-size:12px;color:#4b5563;margin-top:4px}.legend span{margin-right:14px}.legend i{display:inline-block;width:12px;height:3px;margin-right:4px;vertical-align:middle}
.warn{background:var(--ng-bg);color:var(--ng);border-radius:8px;padding:8px 12px;font-size:13px;margin-top:10px}
dl.gloss{display:grid;grid-template-columns:180px 1fr;gap:6px 16px;font-size:14px;margin:8px 0}dl.gloss dt{font-weight:600}dl.gloss dd{margin:0}
.rules td.grp{color:var(--muted);text-align:left;width:72px}.rules td.name{text-align:left;font-weight:600;width:150px;white-space:normal}
.rules td.l{line-height:1.6}
footer{margin-top:40px;font-size:12px;color:var(--muted);text-align:center}
@media (max-width:720px){.verdicts{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(2,1fr)}dl.gloss{grid-template-columns:1fr}}
@media print{
 @page{size:A4;margin:14mm}
 body{background:#fff;font-size:12px}main{max-width:none;padding:0}
 .part,.vcard,.timeline,header.top,figure,table{break-inside:avoid}
 .page{break-before:page}
 h2{margin-top:20px}
}
"""

BADGE = {"pass": "✓ 通過", "fail": "✗ 未通過", "incomplete": "未完成", "nodata": "未執行"}


def _gauge(value, limit, color):
    if value is None or not limit:
        return ""
    scale = max(value, limit) * 1.15
    w = min(100.0, value / scale * 100)
    mark = limit / scale * 100
    return f'<div class="gauge"><i style="width:{w:.1f}%;background:{color}"></i><b style="left:{mark:.1f}%" title="門檻"></b></div>'


def verdict_card(pid, part, v, reason, *, limit_ms, fail_limit, pct, target_tps, abort_rate):
    info = PARTS[pid]
    color = info["color"]
    head = (f'<div class="head"><div><div class="who">腳本 {pid}</div><div class="name">{e(info["name"])}</div></div>'
            f'<div class="badge {v}">{BADGE[v]}</div></div>')
    if part.get("empty"):
        why = REASON_NAMES.get(reason, "沒有數據")
        return f'<div class="vcard" style="--c:{color}">{head}<div class="callout">這支腳本{e(why)}，沒有可判定的數據。</div></div>'
    j = part["judged"]
    lat_ok = j["judged"] is not None and j["judged"] <= limit_ms
    fr_ok = j["fail_rate"] is not None and j["fail_rate"] <= fail_limit
    gc = lambda ok: "#16a34a" if ok else "#dc2626"  # noqa: E731
    body = f"""
<div class="metric"><div class="label">回應時間 p{g(pct)}（{pct:g}% 的對話在這個時間內回覆）</div>
<div class="val">{sec(j['judged'])} 秒<small>門檻 ≤ {g(limit_ms / 1000)} 秒　{'<span class="ok">✓</span>' if lat_ok else '<span class="ng">✗</span>'}</small></div>
{_gauge(j['judged'], limit_ms, gc(lat_ok))}</div>
<div class="metric"><div class="label">對話失敗率</div>
<div class="val">{pct_txt(j['fail_rate'])}<small>門檻 ≤ {g(fail_limit * 100)}%　{'<span class="ok">✓</span>' if fr_ok else '<span class="ng">✗</span>'}</small></div>
{_gauge(j['fail_rate'], fail_limit, gc(fr_ok))}</div>
<div class="facts"><span>完成對話 <b>{num(j['completed'])}</b>／共 {num(j['total'])} 筆</span><span>失敗 <b>{num(j['failed'])}</b> 筆</span>
<span>實際負載 <b>{rate1(j.get('chat_tps'))}</b> TPS（目標 {g(target_tps)}）</span></div>"""
    notes = []
    if reason == "aborted":
        notes.append(f"開始後約 {part['duration_s'] / 60:.0f} 分鐘，失敗率超過 {g(abort_rate * 100)}%，依設定自動停止。")
    elif reason == "stopped":
        notes.append(f"執行人員於開始後約 {part['duration_s'] / 60:.0f} 分鐘手動停止，以下為停止前的數據。")
    elif reason in ("error", "start_failed"):
        notes.append("壓測機異常結束，數據可能不完整。")
    if part["basis"] == "all":
        notes.append("沒有固定負載期間的數據（爬升中就結束），改用整輪數據判定。")
    callout = "".join(f'<div class="callout">{e(n)}</div>' for n in notes)
    return f'<div class="vcard" style="--c:{color}">{head}{body}{callout}</div>'


def one_line_summary(parts, verdicts, limits, target, reasons):
    bits = []
    for pid in ("A", "B"):
        p = parts[pid]
        if p.get("empty"):
            bits.append(f"{PARTS[pid]['name']}未執行")
            continue
        j = p["judged"]
        bits.append(f"{PARTS[pid]['name']} p95 {sec(j['judged'])} 秒、失敗率 {pct_txt(j['fail_rate'])}")
    vs = [verdicts["A"], verdicts["B"]]
    if all(v == "pass" for v in vs):
        tail = "兩者皆符合門檻。"
    elif all(v in ("fail",) for v in vs):
        tail = "兩者皆未符合門檻。"
    else:
        tail = "；".join(f"{PARTS[pid]['name']}{BADGE[verdicts[pid]].lstrip('✓✗ ')}" for pid in ("A", "B")) + "。"
    # 兩支都完整跑完，才能說「在 N TPS 固定負載下」
    complete = all(reasons.get(pid) == "completed" for pid in ("A", "B"))
    lead = f"在 {g(target)} TPS 固定負載下，" if (target and complete) else ""
    return lead + "，".join(bits) + "，" + tail


def timeline_html(env, parts, state):
    segs = []
    def ms(key):
        v = state.get(key)
        return float(v) * 1000 if v else None
    a0, a1, b0, b1 = ms("A_START"), ms("A_END"), ms("B_START"), ms("B_END")
    r0, r1 = ms("REPORT_START"), ms("DONE_AT") or time.time() * 1000
    points = [x for x in (a0, a1, b0, b1, r0) if x]
    if not points:
        return ""
    t_start, t_end = min(points), max(points + [r1 or 0])
    total = max(1.0, t_end - t_start)
    def seg(cls, style, x0, x1, label):
        if x0 is None or x1 is None or x1 <= x0:
            return ""
        share = (x1 - x0) / total
        text = e(label) if share >= 0.05 else ""  # 太窄的區段不放文字（滑鼠移上去看）
        return f'<div class="{cls}" style="flex:{share:.4f};{style}" title="{e(label)}">{text}</div>'
    segs.append(seg("", f"background:{PARTS['A']['color']}", a0, a1, f"A 業務查詢 {hm(a0)}–{hm(a1)}"))
    segs.append(seg("gap", "", a1, b0, "休息"))
    segs.append(seg("", f"background:{PARTS['B']['color']}", b0, b1, f"B FAQ 問答 {hm(b0)}–{hm(b1)}"))
    segs.append(seg("rep", "", r0, r1, "報表"))
    return (f'<div class="tl">{"".join(segs)}</div>'
            f'<div class="tl-axis"><span>{hm(t_start)}</span><span>{hm(t_end)}</span></div>')


def part_section(pid, part, reason, *, limit_ms, fail_limit, pct, plan, weights, target_tps):
    info = PARTS[pid]
    color = info["color"]
    title = (f'<div class="part-title" style="--c:{color}"><span class="tag" style="background:{color}">腳本 {pid}</span>'
             f'<span class="t">{e(info["name"])}</span><span class="muted">{e(plan_text(plan)[0])}</span></div>')
    if part.get("empty"):
        return f'<section class="part page">{title}<p class="callout">這支腳本{e(REASON_NAMES.get(reason, "沒有數據"))}，沒有數據。</p></section>'
    j, w = part["judged"], part["whole"]
    key = f"p{g(pct)}"
    basis = "固定負載期間" if part["basis"] == "hold" else "整輪"
    warn = ""
    if part["dropped"]:
        warn = (f'<div class="warn">壓測機來不及送出的對話（dropped_iterations）共 {num(part["dropped"])} 次：實際負載低於設定，'
                "是壓測機資源不足，不是受測系統的問題。</div>")
    kpis = f"""<div class="kpis">
<div class="kpi"><span>對話筆數</span><b>{num(j['total'])}</b></div>
<div class="kpi"><span>完成對話數（每秒）</span><b>{num(j['completed'])}</b>（{rate1(j['completed_tps'])}／秒）</div>
<div class="kpi"><span>失敗筆數／失敗率</span><b>{num(j['failed'])}</b>　{pct_txt(j['fail_rate'])}</div>
<div class="kpi"><span>回應時間 平均／{key}</span><b>{sec(j['avg'])}</b>／{sec(j['judged'])} 秒</div>
<div class="kpi"><span>實際負載（含開場問候）</span><b>{rate1(j.get('chat_tps'))}</b> TPS</div>
</div>"""

    def ck(ok):
        return '<span class="ok">✓ 通過</span>' if ok else '<span class="ng">✗ 未通過</span>'
    lat_ok = j["judged"] is not None and j["judged"] <= limit_ms
    fr_ok = j["fail_rate"] is not None and j["fail_rate"] <= fail_limit
    judge_tbl = f"""<table>
<tr><th class="l">判定項目（{basis}）</th><th>實測</th><th>門檻</th><th>結果</th></tr>
<tr><td class="l">回應時間 {key}</td><td>{sec(j['judged'])} 秒</td><td>≤ {g(limit_ms / 1000)} 秒</td><td>{ck(lat_ok) if j['count'] else '—'}</td></tr>
<tr><td class="l">對話失敗率</td><td>{pct_txt(j['fail_rate'])}</td><td>≤ {g(fail_limit * 100)}%</td><td>{ck(fr_ok) if j['fail_rate'] is not None else '—'}</td></tr>
</table>"""

    def stat_row(label, s, share=None, cls=""):
        within = "—" if s["within"] is None else pct_txt(s["within"], 1)
        return (f'<tr class="{cls}"><td class="l">{label}</td><td>{"" if share is None else pct_txt(share, 1)}</td><td>{num(s["total"])}</td><td>{num(s["completed"])}</td>'
                f'<td>{num(s["failed"])}</td><td>{pct_txt(s["fail_rate"])}</td><td>{sec(s["avg"])}</td><td>{sec(s["p50"])}</td>'
                f'<td>{sec(s["p90"])}</td><td>{sec(s["p95"])}</td><td>{sec(s["p99"])}</td><td>{sec(s["max"])}</td><td>{within}</td></tr>')
    rows = [stat_row(f"{e(c['case'])} {e(c['name'])}", c, c["share"]) for c in part["cases"]]
    rows.append(stat_row(f"合計（{basis}）", j, 1.0 if j["total"] else None, "total"))
    if part["basis"] == "hold":
        rows.append(stat_row("參考：整輪（含爬升、收尾）", w))
    case_tbl = (f'<table><tr><th class="l">情境</th><th>占比</th><th>對話筆數</th><th>完成</th><th>失敗</th><th>失敗率</th><th>平均</th><th>p50</th>'
                f'<th>p90</th><th>p95</th><th>p99</th><th>最慢</th><th>門檻內比例</th></tr>{"".join(rows)}</table>'
                f'<p class="hint">回應時間單位：秒。「門檻內比例」＝回應時間 ≤ {g(limit_ms / 1000)} 秒的對話比例。'
                f'占比設定：{e(weights_text(weights))}（每則提問依比例隨機抽選）。</p>')

    c = part["causes"]
    if j["failed"]:
        cause_rows = [("逾時（60 秒未回應，或 408／504）", c["timeout"]), ("系統錯誤（HTTP 5xx）", c["5xx"]),
                      ("超過次數上限（HTTP 429）", c["429"]), ("其他 HTTP 4xx", c["4xx"]),
                      ("回應內容不符預期（例如 AI 無法生成、該攔截卻沒攔截）", c["content"])]
        body = "".join(f'<tr><td class="l">{e(n)}</td><td>{num(v)}</td><td>{pct_txt(v / j["failed"], 1)}</td></tr>'
                       for n, v in cause_rows if v)
        cause_tbl = f'<table><tr><th class="l">失敗原因（{basis}）</th><th>筆數</th><th>占失敗</th></tr>{body}</table>'
    else:
        cause_tbl = f'<p class="hint">{basis}沒有失敗的對話。</p>'

    s = part["series"]
    tsec = lambda v: None if v is None else v / 1000  # noqa: E731
    charts = "".join([
        line_chart(s["t"], [("聊天 API 合計（含開場問候）", "#2563eb", s["chat_tps"]),
                            ("使用者提問（計入統計）", "#94a3b8", s["qa_tps"], True),
                            ("完成對話（正常回覆）", "#16a34a", s["done_tps"])],
                   part["bands"], title="① 負載：每秒對話數（TPS）", y_label="筆／秒",
                   y_fmt=lambda v: f"{v:,.0f}" if v >= 10 else f"{v:.1f}",
                   refs=[(target_tps, f"目標 {g(target_tps)} TPS", "#9ca3af")] if target_tps else ()),
        line_chart(s["t"], [(f"回應時間 {key}", color, [tsec(v) for v in s["lat"]])],
                   part["bands"], title=f"② 回應時間（{key}，秒）", y_label="秒",
                   y_fmt=lambda v: f"{v:.0f}" if v >= 10 else f"{v:.1f}",
                   refs=[(limit_ms / 1000, f"門檻 {g(limit_ms / 1000)} 秒", "#dc2626")]),
        line_chart(s["t"], [("失敗率", "#dc2626", [None if v is None else v * 100 for v in s["fail_rate"]])],
                   part["bands"], title="③ 對話失敗率（%）", y_label="%", y_fmt=lambda v: f"{v:.1f}",
                   refs=[(fail_limit * 100, f"門檻 {g(fail_limit * 100)}%", "#9ca3af")]),
    ])
    op = part["opening"]
    op_note = (f'<p class="hint">開場問候（訪客進入聊天時的招呼語）照真實流程送出，但不計入上面的統計：'
               f'共 {num(op["total"])} 則，失敗 {num(op["failed"])} 則，p95 {sec(op["p95"])} 秒。</p>') if op["total"] else ""
    return f"""<section class="part page">{title}
<p class="hint">執行時間 {hm(part['t0_ms'], '%m/%d %H:%M')}–{hm(part['t1_ms'])}，固定負載 {part['hold_s'] / 60:.1f} 分鐘。
淺色背景為爬升期間、深色為固定負載期間；判定只看固定負載期間。</p>
{warn}{kpis}
<h3>判定</h3>{judge_tbl}
<h3>各情境明細</h3>{case_tbl}
<h3>失敗原因</h3>{cause_tbl}
<h3>趨勢圖</h3>{charts}
{op_note}
</section>"""


def rules_table(pct, limits, fail_limit, abort_rate):
    """判定規則與計算方式（報表會被轉寄，判讀規則要寫在報表裡，不能只放在文件）。"""
    key = f"p{g(pct)}"
    lim = f"A {g(limits['A'] / 1000)} 秒、B {g(limits['B'] / 1000)} 秒"
    rows = [
        ("判定", "通過", f"回應時間 {key} ≤ 門檻（{lim}）<b>且</b> 對話失敗率 ≤ {g(fail_limit * 100)}%。"),
        ("判定", "未通過", f"上面兩項任一項超過門檻；或依設定自動停止（失敗率超過 {g(abort_rate * 100)}%）。"),
        ("判定", "未完成", "執行人員手動停止，或壓測機異常結束。只列出停止前的數據供參考，不判定通過與否。"),
        ("判定", "未執行", "這支腳本沒有開始，例如前一支執行時已手動停止整組測試。"),
        ("數字", "判定範圍", "每支腳本爬升到目標 TPS 之後的固定負載期間（不含爬升與收尾）。爬升中就停止時，改用整輪數據。"),
        ("數字", f"回應時間 {key}", f"判定範圍內所有對話的回應時間由快到慢排序，取第 {g(pct)}% 那一筆（兩筆之間線性內插，與 k6 相同）。"
                               "A、B 各自計算，不合併。"),
        ("數字", "對話失敗率", "失敗筆數 ÷ 對話筆數。"),
        ("數字", "完成對話數", "對話筆數 − 失敗筆數（有正常回覆的對話）。每秒完成＝完成對話數 ÷ 判定範圍的秒數，代表系統實際處理量。"),
        ("數字", "實際負載（TPS）", "判定範圍內的聊天 API 請求數 ÷ 秒數，含開場問候，因此會比「每秒對話數」略高。"),
        ("數字", "門檻內比例", "回應時間 ≤ 門檻的對話筆數 ÷ 對話筆數。"),
        ("失敗原因", "逾時／5xx／429／4xx", "依聊天 API 回應的 HTTP 狀態碼分類；逾時＝60 秒未回應，或 408／504。"),
        ("失敗原因", "回應內容不符預期", "失敗筆數 − 上述 HTTP 錯誤筆數：HTTP 正常，但回應內容不是預期結果，"
                               "例如 AI 無法生成、B5 該被 Model Armor 攔截卻沒有攔截。"),
    ]
    body, last = [], None
    for grp, name, how in rows:
        g_cell = f'<td class="grp">{e(grp)}</td>' if grp != last else '<td class="grp"></td>'
        last = grp
        body.append(f"<tr>{g_cell}<td class='name'>{e(name)}</td><td class='l'>{how}</td></tr>")
    return (f'<h3>判定規則與計算方式</h3><table class="rules"><tr><th class="l">類別</th><th class="l">項目</th>'
            f'<th class="l">規則／公式</th></tr>{"".join(body)}</table>')


def render_page(env, state, parts, verdicts, *, title, generated_ms):
    pct = float(env.get("LATENCY_PCT", 95))
    fail_limit = float(env.get("FAIL_RATE", 0.005))
    abort_rate = float(env.get("ABORT_FAIL_RATE", 0.05))
    limits = {pid: float(env.get(PARTS[pid]["max_key"], 0)) for pid in PARTS}
    plans = {"A": env.get("PLAN_A", ""), "B": env.get("PLAN_B", "")}
    targets = {pid: plan_text(plans[pid])[1] for pid in PARTS}
    weights = {"A": env.get("WEIGHTS_A", ""), "B": env.get("WEIGHTS_B", "")}
    reasons = {"A": state.get("A_REASON", ""), "B": state.get("B_REASON", "")}
    kind = KIND_NAMES.get(env.get("KIND", ""), env.get("KIND", ""))
    t_first = min((p["t0_ms"] for p in parts.values() if not p.get("empty")), default=None)
    t_last = max((p["t1_ms"] for p in parts.values() if not p.get("empty")), default=None)
    same_target = targets["A"] if targets["A"] == targets["B"] else None

    cards = "".join(verdict_card(pid, parts[pid], verdicts[pid], reasons[pid], limit_ms=limits[pid], fail_limit=fail_limit,
                                 pct=pct, target_tps=targets[pid], abort_rate=abort_rate) for pid in ("A", "B"))
    stopped_note = ""
    if state.get("FINAL") == "stopped" or os.path.exists(os.path.join(env["_dir"], "stop-requested")):
        stopped_note = '<div class="callout">本組測試由執行人員手動停止，報表為停止前的數據。</div>'

    abort_txt = (f"開啟（開始 {e(env.get('ABORT_DELAY', ''))} 後，累計失敗率超過 {g(abort_rate * 100)}% 就停止該支腳本）"
                 if env.get("ABORT") == "true" else "關閉（一律跑完）")
    params = "".join(
        f"<tr><td class='l'>腳本 {pid} {e(PARTS[pid]['name'])}</td><td class='l'>{e(plan_text(plans[pid])[0])}</td>"
        f"<td class='l'>{e(weights_text(weights[pid]))}</td><td>p{g(pct)} ≤ {g(limits[pid] / 1000)} 秒</td>"
        f"<td>{g(fail_limit * 100)}%</td></tr>" for pid in ("A", "B"))
    tasks = "".join(
        f"<tr><td class='l'>腳本 {pid}</td><td class='l'>{e(t['name'])}</td><td>{e(t.get('state') or '—')}</td>"
        f"<td>{e(t.get('k6_exit') if t.get('k6_exit') is not None else '—')}</td><td>{num(t['requests'])}</td></tr>"
        for pid in ("A", "B") for t in (parts[pid].get("tasks") or []))

    body = f"""
<header class="top">
<h1>{e(title)}</h1>
<div class="sub">業務查詢（腳本 A）與 FAQ 問答（腳本 B）分開測試｜{e(kind)}</div>
<div class="meta">
<div><span>測試編號</span>{e(env.get('SUITE_ID', ''))}</div>
<div><span>測試時間</span>{hm(t_first, '%Y-%m-%d %H:%M')}–{hm(t_last)}</div>
<div><span>受測端</span>{e(env.get('TARGET', '—'))}</div>
<div><span>壓測機</span>{e(env.get('RUNNER_COUNT', '—'))} 台</div>
<div><span>報表產生</span>{hm(generated_ms, '%Y-%m-%d %H:%M')}</div>
</div>
</header>

<h2>測試結論</h2>
<div class="verdicts">{cards}</div>
<div class="summary">{e(one_line_summary(parts, verdicts, limits, same_target, reasons))}</div>
<p class="hint">判定方式：回應時間 p{g(pct)} 與對話失敗率<b>兩項都在門檻內</b>才算通過，任一項超過即未通過。各項數字的計算方式見附錄「判定規則與計算方式」。</p>
{stopped_note}

<h2>測試方式</h2>
<div class="timeline">
{timeline_html(env, parts, state)}
<ul class="how">
<li>先跑腳本 A（業務查詢），休息 {e(env.get('GAP_MIN', ''))} 分鐘讓系統回穩，再跑腳本 B（FAQ 問答）。兩支腳本的數據分開計算，不混在一起。</li>
<li>每支腳本：{e(plan_text(plans['A'])[0]) if plans['A'] == plans['B'] else '負載見附錄'}。<b>判定只看固定負載期間</b>，不含爬升。</li>
<li>以「對話」為單位：使用者提問的一問一答＝一則對話。回應時間＝送出問題到收完回覆（含 AI 生成時間）。</li>
<li>失敗＝回應不是預期結果：系統錯誤（5xx）、逾時、HTTP 429，或回應內容不符預期。</li>
<li>每位模擬訪客進入時會先收到開場問候，照真實流程送出，但不計入統計。</li>
</ul>
</div>

{part_section('A', parts['A'], reasons['A'], limit_ms=limits['A'], fail_limit=fail_limit, pct=pct, plan=plans['A'], weights=weights['A'], target_tps=targets['A'])}
{part_section('B', parts['B'], reasons['B'], limit_ms=limits['B'], fail_limit=fail_limit, pct=pct, plan=plans['B'], weights=weights['B'], target_tps=targets['B'])}

<h2 class="page">附錄</h2>
<h3>測試參數</h3>
<table><tr><th class="l">腳本</th><th class="l">負載</th><th class="l">情境占比</th><th>回應時間門檻</th><th>失敗率門檻</th></tr>{params}</table>
<p class="hint">自動停止：{abort_txt}。TPS＝聊天 API 每秒請求數（含開場問候）。</p>
{rules_table(pct, limits, fail_limit, abort_rate)}
<h3>名詞說明</h3>
<dl class="gloss">
<dt>對話</dt><dd>使用者提問的一問一答，也就是一次聊天 API 呼叫。開場問候不算。</dd>
<dt>TPS</dt><dd>每秒送到聊天 API 的請求數，也就是負載大小。</dd>
<dt>p95</dt><dd>95% 的對話都在這個時間內回覆。比平均值更能反映「慢的那群使用者」的體驗。</dd>
<dt>固定負載期間</dt><dd>流量爬升到目標 TPS 之後、維持不變的那段時間。</dd>
<dt>dropped_iterations</dt><dd>壓測機來不及送出的對話次數。大於 0 代表壓測機資源不足、實際負載低於設定，不是受測系統的問題。</dd>
</dl>
<h3>資料來源</h3>
<table><tr><th class="l">腳本</th><th class="l">壓測機</th><th>狀態</th><th>k6 結束碼</th><th>聊天 API 請求數</th></tr>{tasks}</table>
<p class="hint">原始數據：{e(env.get('RUN_A', ''))}、{e(env.get('RUN_B', ''))}（/var/lib/k6-stress/runs/）。</p>
<footer>新阿發 壓力測試｜{e(env.get('SUITE_ID', ''))}｜報表產生 {hm(generated_ms, '%Y-%m-%d %H:%M:%S')}（台北時間）</footer>
"""
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}｜{e(env.get('SUITE_ID', ''))}</title><style>{CSS}</style></head>
<body><main>{body}</main></body></html>"""


# ---------------- 主程式 ----------------

def build(suite_dir: str, runs_dir: str, title: str) -> dict:
    env = read_env(os.path.join(suite_dir, "suite.env"))
    env["_dir"] = suite_dir
    state = read_env(os.path.join(suite_dir, "state.env"))
    pct = float(env.get("LATENCY_PCT", 95))
    fail_limit = float(env.get("FAIL_RATE", 0.005))
    parts, verdicts = {}, {}
    for pid, info in PARTS.items():
        run = env.get(f"RUN_{pid}", "")
        data = load_run(os.path.join(runs_dir, run)) if run else RunData()
        print(f"[v3-report] 腳本 {pid} {run}：{len(data.reqs):,} 筆聊天 API 請求", flush=True)
        parts[pid] = compute_part(data, flow=info["flow"], limit_ms=float(env.get(info["max_key"], 0)), pct=pct,
                                  fail_rate_limit=fail_limit)
        verdicts[pid] = verdict(parts[pid], state.get(f"{pid}_REASON", ""))
    generated_ms = time.time() * 1000
    page = render_page(env, state, parts, verdicts, title=title, generated_ms=generated_ms)

    out_dir = os.path.join(suite_dir, "reports")
    os.makedirs(out_dir, exist_ok=True)
    suite_id = env.get("SUITE_ID", "v3")
    for name in (f"report-{suite_id}.html", "latest.html"):
        _write(os.path.join(out_dir, name), page)

    summary = {"suite_id": suite_id, "generated_ms": generated_ms, "verdicts": verdicts}
    lines = []
    for pid in PARTS:
        p = parts[pid]
        j = None if p.get("empty") else p["judged"]
        summary[pid] = None if j is None else {
            k: j[k] for k in ("total", "completed", "completed_tps", "failed", "fail_rate", "avg", "p50", "p90", "p95", "p99", "max", "judged", "chat_tps")}
        lines.append(f"{pid}_VERDICT={verdicts[pid]}")
        if j is not None:
            lines.append(f"{pid}_TEXT=p{g(pct)} {sec(j['judged'])} 秒（門檻 {g(float(env.get(PARTS[pid]['max_key'], 0)) / 1000)} 秒）"
                         f"、失敗率 {pct_txt(j['fail_rate'])}（門檻 {g(fail_limit * 100)}%）"
                         f"、完成對話 {num(j['completed'])}／{num(j['total'])} 筆")
    _write(os.path.join(out_dir, "summary.json"), json.dumps(summary, ensure_ascii=False, indent=2))
    _write(os.path.join(out_dir, "verdict.env"), "\n".join(lines) + "\n")
    print(f"[v3-report] 報表：{os.path.join(out_dir, f'report-{suite_id}.html')}", flush=True)
    return {"parts": parts, "verdicts": verdicts, "page": page}


def _write(path: str, text: str) -> None:
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite-dir", required=True)
    ap.add_argument("--runs-dir", required=True)
    ap.add_argument("--title", default="新阿發 對話回應時間測試報告")
    args = ap.parse_args()
    build(args.suite_dir, args.runs_dir, args.title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
