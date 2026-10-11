"""把彙總結果輸出成單一 HTML（圖表為內嵌 SVG，下載後離線可開）。"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

STAGE_NAMES = {
    "smoke": "Smoke 1%",
    "low5": "低量 5%",
    "low10": "低量 10%",
    "baseline": "Baseline 25%",
    "mid50": "中量 50%",
    "x1": "1 倍",
    "x2": "2 倍",
    "x3": "3 倍",
    "recovery": "Recovery 25%",
    "drain": "收尾",
    "unknown": "未標示",
}
FLOW_NAMES = {"all": "全部", "faq": "FAQ 問答", "business": "業務查詢", "init": "開場問候", "login": "登入", "unknown": "未分類"}
COLORS = {"sent": "#2563eb", "success": "#16a34a", "p50": "#0891b2", "p95": "#d97706", "p99": "#dc2626",
          "faq": "#7c3aed", "business": "#0d9488", "err": "#dc2626", "vus": "#475569", "ref": "#9ca3af", "stop": "#dc2626"}

e = html.escape


def fmt_s(ms):
    return "—" if ms is None else f"{ms / 1000:.2f}"


def fmt_pct(r, digits=2):
    return "—" if r is None else f"{r * 100:.{digits}f}%"


def fmt_n(x):
    return "—" if x is None else f"{x:,.0f}"


def fmt_rate(x):
    return "—" if x is None else f"{x:,.1f}"


def fmt_time(ms):
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------- SVG 圖表 ----------------

def line_chart(t, series, bands, *, title, y_label, y_fmt=lambda v: f"{v:g}", refs=(), height=240):
    """series: [(名稱, 顏色, 值陣列, 虛線?)]；refs: [(值, 標籤, 顏色)] 水平參考線。"""
    W, H = 960, height
    L, R, T, B = 64, 16, 28, 34
    pw, ph = W - L - R, H - T - B
    xmax = max(t[-1], 1) if t else 1
    vals = [v for _, _, ys, *_ in series for v in ys if v is not None] + [r[0] for r in refs]
    ymax = max(vals) if vals else 1
    ymax = ymax * 1.1 if ymax > 0 else 1

    def x(v):
        return L + pw * v / xmax

    def y(v):
        return T + ph - ph * v / ymax

    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{e(title)}">']
    # 階段色帶
    for i, b in enumerate(bands):
        x0, x1 = x(b["start"]), x(max(b["end"], b["start"] + 1))
        fill = "#eef2ff" if b["phase"] == "hold" else "#f8fafc"
        out.append(f'<rect x="{x0:.1f}" y="{T}" width="{max(1, x1 - x0):.1f}" height="{ph}" fill="{fill}"/>')
        if b["phase"] == "hold":
            out.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{T - 8}" class="band">{e(STAGE_NAMES.get(b["stage"], b["stage"]))}</text>')
    # 座標軸
    for k in range(5):
        v = ymax * k / 4
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>')
        out.append(f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="ytick">{e(y_fmt(v))}</text>')
    step = max(1, round(xmax / 60 / 8)) * 60 if xmax > 600 else max(10, round(xmax / 8 / 10) * 10)
    tick = 0
    while tick <= xmax:
        label = f"{tick / 60:.0f} 分" if step >= 60 else f"{tick:.0f} 秒"
        out.append(f'<text x="{x(tick):.1f}" y="{H - 12}" class="xtick">{label}</text>')
        tick += step
    out.append(f'<text x="14" y="{T + ph / 2:.1f}" class="ylabel" transform="rotate(-90 14 {T + ph / 2:.1f})">{e(y_label)}</text>')
    # 參考線
    for v, label, color in refs:
        if v <= ymax:
            out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{color}" stroke-dasharray="6 4" stroke-width="1.5"/>')
            out.append(f'<text x="{W - R - 4}" y="{y(v) - 4:.1f}" class="ref" fill="{color}">{e(label)}</text>')
    # 資料線
    for name, color, ys, *rest in series:
        dashed = rest[0] if rest else False
        pts, path = [], []
        for tv, yv in zip(t, ys):
            if yv is None:
                if pts:
                    path.append(pts)
                    pts = []
                continue
            pts.append(f"{x(tv):.1f},{y(yv):.1f}")
        if pts:
            path.append(pts)
        for p in path:
            dash = ' stroke-dasharray="4 3"' if dashed else ""
            out.append(f'<polyline points="{" ".join(p)}" fill="none" stroke="{color}" stroke-width="1.8"{dash}/>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="background:{c}"></i>{e(n)}</span>' for n, c, *_ in series)
    return f'<figure><figcaption>{e(title)}</figcaption>{"".join(out)}<div class="legend">{legend}</div></figure>'


# ---------------- 表格 ----------------

def judge(row_flows, thresholds):
    """回傳 [(項目, 實測, 門檻, 是否通過)]。"""
    out = []
    for fl in ("faq", "business"):
        s = row_flows.get(fl)
        th = thresholds.get(fl, {})
        if not s:
            continue
        for p in ("p95", "p99"):
            if p in th and s.get(p) is not None:
                out.append((f"{FLOW_NAMES[fl]} {p}", f"{fmt_s(s[p])} 秒", f"≤ {th[p] / 1000:g} 秒", s[p] <= th[p]))
    s = row_flows.get("all")
    if s and s.get("error_rate") is not None:
        lim = thresholds.get("error_rate", 0.005)
        out.append(("錯誤率", fmt_pct(s["error_rate"]), f"≤ {lim * 100:g}%", s["error_rate"] <= lim))
    return out


def mark(ok):
    return '<b class="ok">✓</b>' if ok else '<b class="ng">✗</b>'


def stage_summary_table(stages, thresholds):
    head = "<tr><th>階段</th><th>每秒請求<br>（全部）</th><th>FAQ p95</th><th>業務 p95</th><th>錯誤率<br>5xx+timeout</th><th>HTTP 429</th><th>參考門檻</th></tr>"
    rows = []
    for st in stages:
        f = st["flows"]
        a, faq, biz = f.get("all", {}), f.get("faq", {}), f.get("business", {})
        checks = judge(f, thresholds)
        verdict = "—" if not checks else (mark(all(c[3] for c in checks)) + (" 全部通過" if all(c[3] for c in checks) else " " + "、".join(c[0] for c in checks if not c[3])))
        rows.append(
            f"<tr><td>{e(STAGE_NAMES.get(st['stage'], st['stage']))}</td><td>{fmt_rate(a.get('rps'))}</td>"
            f"<td>{fmt_s(faq.get('p95'))} 秒</td><td>{fmt_s(biz.get('p95'))} 秒</td>"
            f"<td>{fmt_pct(a.get('error_rate'))}</td><td>{fmt_n(a.get('http_429'))}</td><td class='left'>{verdict}</td></tr>"
        )
    return f"<table>{head}{''.join(rows)}</table>"


def detail_table(rows_by_label):
    head = ("<tr><th>階段</th><th>類別</th><th>請求數</th><th>每秒</th><th>成功</th><th>avg</th><th>p50</th><th>p90</th>"
            "<th>p95</th><th>p99</th><th>max</th><th>錯誤率</th><th>429</th><th>業務錯誤</th></tr>")
    rows = []
    for label, flows in rows_by_label:
        first = True
        for fl, s in flows.items():
            rows.append(
                f"<tr><td>{e(label) if first else ''}</td><td>{e(FLOW_NAMES.get(fl, fl))}</td><td>{fmt_n(s['requests'])}</td>"
                f"<td>{fmt_rate(s.get('rps'))}</td><td>{fmt_n(s['success'])}</td><td>{fmt_s(s['avg'])}</td><td>{fmt_s(s['p50'])}</td>"
                f"<td>{fmt_s(s['p90'])}</td><td>{fmt_s(s['p95'])}</td><td>{fmt_s(s['p99'])}</td><td>{fmt_s(s['max'])}</td>"
                f"<td>{fmt_pct(s['error_rate'])}</td><td>{fmt_n(s['http_429'])}</td><td>{fmt_n(s['business_errors'])}</td></tr>"
            )
            first = False
    return f'<table class="detail">{head}{"".join(rows)}</table><p class="small">回應時間單位：秒。</p>'


def task_table(tasks, expected_count):
    rows = []
    for tk in tasks:
        st = tk.status or {}
        flags = []
        if tk.truncated:
            flags.append("寫入中（讀到目前為止）")
        if tk.corrupt:
            flags.append("檔案中段損毀")
        rows.append(
            f"<tr><td>{e(tk.name)}</td><td>{e(st.get('state', '—'))}</td><td>{e(str(st.get('updated_at', '—')))}</td>"
            f"<td>{fmt_n(tk.requests)}</td><td>{fmt_time(tk.last_ts)}</td><td>{e(str(st.get('k6_exit', '—')))}</td>"
            f"<td class='left'>{dashboard_links(tk)}</td><td class='left'>{e('、'.join(flags) or '—')}</td></tr>"
        )
    note = ""
    if expected_count and len(tasks) < expected_count:
        note = f'<p class="warn">預期 {expected_count} 個 task，目前只讀到 {len(tasks)} 個（尚未上傳或已失敗）。</p>'
    head = "<tr><th>Task</th><th>狀態</th><th>最後上傳（UTC）</th><th>請求數</th><th>資料截至</th><th>k6 exit</th><th>k6 dashboard</th><th>備註</th></tr>"
    return f"{note}<table>{head}{''.join(rows)}</table>"


def dashboard_links(tk):
    # 相對於 reports/ 的路徑：在 GCS（同一個 RUN_ID 資料夾）或本機資料夾開啟時可以直接點
    if not tk.dashboard:
        return '<span class="small">該 task 結束後產生</span>'
    base = f"../tasks/{e(tk.name)}"
    return f'<a href="{base}/dashboard-annotated.html">教學版</a>｜<a href="{base}/dashboard.html">原始</a>'


GUIDE = """
<details class="guide" open><summary>閱讀指南：第一次看這份報告，照這三步驟</summary>
<ol>
<li><b>先看「各階段判定」表</b>：找出最後一個全部 ✓ 的階段，那就是在參考門檻內觀察到的容量；第一個 ✗ 的階段後面會列出是哪個指標沒通過。</li>
<li><b>再看「趨勢圖」</b>：確認是從哪個時間點開始變差（送出量與成功量分開、p95 往上爬、錯誤率超過虛線），對照背景色塊就知道是哪一階。</li>
<li><b>最後看頁首警告與「資料來源」</b>：有紅色警告（壓測機不足、出現 429、task 尚未完成）時，結果要打折扣看；全部 task 都是 finished 才是完整結果。</li>
</ol>
<p class="small">黃色「怎麼看」框是教學說明，右上角按鈕可以隱藏；列印或貼進正式報告前可以先隱藏。</p>
</details>
"""

GLOSSARY = [
    ("VU（虛擬使用者）", "壓測工具模擬的一位使用者。本壓測中一個 VU 會跑完一整段對話（開場問候 ＋ 8～11 則訊息，中間有思考時間）。"),
    ("每秒請求（req/s）", "每秒送出的聊天 API 呼叫數。規劃的「每秒處理量」看送出量與成功量兩個數字。"),
    ("p50／p95／p99", "把所有回應時間由快到慢排序：p50 是中間那一筆（一半的請求比它快）；p95 是第 95% 那一筆（只有 5% 比它慢）；p99 同理。p95、p99 代表「慢的那一群使用者」的體驗。"),
    ("avg（平均）", "所有回應時間的平均。容易被少數極慢的請求拉高，所以判定以 p95 為主。"),
    ("錯誤率（5xx ＋ timeout）", "系統錯誤（HTTP 5xx）與逾時佔全部請求的比例，是規劃定義的錯誤率。"),
    ("業務錯誤", "不是系統錯誤，但回應不符合預期（例如 4xx）。另外列出，不算進錯誤率。"),
    ("HTTP 429", "超過每日聊天次數上限被拒絕。依規劃出現就立即停止並檢查設定。"),
    ("持續期間／爬升期間", "每個加壓階段先花幾分鐘把流量爬升到目標（爬升），再維持一段時間（持續）。各階段判定只看持續期間。"),
    ("參考門檻", "證券方文件的參考值（FAQ p95 ≤ 10 秒、業務查詢 p95 ≤ 5 秒、錯誤率 ≤ 0.5%）。正式門檻在 Baseline 實測後共同確認。"),
    ("task（壓測機）", "Cloud Run Jobs 平行執行的一台壓測機，各送 1/N 的流量。本報告把所有 task 的原始數據合併計算。"),
    ("dropped_iterations", "壓測機來不及開始新對話而跳過的次數。大於 0 代表實際送出的負載低於設定，是壓測機不足，不是受測系統的問題。"),
]


def glossary():
    rows = "".join(f"<tr><th>{e(k)}</th><td class='left'>{e(v)}</td></tr>" for k, v in GLOSSARY)
    return f'<details class="guide"><summary>名詞解釋</summary><table class="glossary">{rows}</table></details>'


def note(text):
    return f'<div class="note"><b>怎麼看</b>{text}</div>'


# ---------------- 主頁 ----------------

CSS = """
body{font-family:-apple-system,"Segoe UI","PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;margin:0;background:#f5f6f8;color:#1f2937}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px;border-left:4px solid #2563eb;padding-left:8px}
.meta{color:#4b5563;font-size:13px}.card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-top:12px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}th,td{border:1px solid #e5e7eb;padding:6px 8px;text-align:right;white-space:nowrap}
th{background:#f3f4f6;text-align:center}td.left{text-align:left;white-space:normal}td:first-child{text-align:left}
.detail td:nth-child(2){text-align:left}.ok{color:#16a34a}.ng{color:#dc2626}
.note{background:#fffbeb;border:1px solid #fcd34d;border-radius:6px;padding:10px 12px;font-size:13px;margin:8px 0;line-height:1.6}
.note b{display:inline-block;background:#f59e0b;color:#fff;border-radius:4px;padding:0 6px;margin-right:8px;font-size:12px}
.warn{background:#fef2f2;border:1px solid #fca5a5;border-radius:6px;padding:8px 12px;font-size:13px;color:#991b1b}
figure{margin:16px 0}figcaption{font-weight:600;font-size:14px;margin-bottom:4px}
.chart{width:100%;height:auto;background:#fff;border:1px solid #e5e7eb;border-radius:6px}
.chart text{font-size:11px;fill:#6b7280}.chart .band{text-anchor:middle;font-size:11px;fill:#4338ca}
.chart .ytick{text-anchor:end}.chart .xtick{text-anchor:middle}.chart .ref{text-anchor:end;font-size:11px}.chart .grid{stroke:#eef0f3}
.legend{font-size:12px;color:#4b5563;margin-top:4px}.legend span{margin-right:14px}.legend i{display:inline-block;width:12px;height:3px;margin-right:4px;vertical-align:middle}
.small{font-size:12px;color:#6b7280}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.kpi{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px}.kpi div{font-size:12px;color:#6b7280}.kpi b{font-size:20px}
a{color:#2563eb}
.guide{background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:10px 14px;margin:12px 0;font-size:14px;line-height:1.7}
.guide summary{font-weight:600;cursor:pointer}.guide ol{margin:8px 0 4px 20px;padding:0}
.glossary th{text-align:left;width:180px;background:#fef3c7;white-space:normal}.glossary{margin-top:8px}
#teach-toggle{position:fixed;top:12px;right:12px;z-index:10;background:#f59e0b;color:#fff;border:0;border-radius:6px;padding:8px 12px;font-size:14px;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,.2)}
body.teach-hidden .note,body.teach-hidden .guide{display:none}
@media print{body{background:#fff}.card,.chart{break-inside:avoid}#teach-toggle{display:none}}
"""


def render(result: dict, *, run_id: str, title: str, thresholds: dict, generated_ms: float, expected_tasks: int | None,
           warnings: list[str]) -> str:
    if not result.get("requests"):
        body = '<p class="warn">目前沒有任何請求資料（壓測尚未開始，或還沒有 task 上傳原始數據）。</p>'
        if result.get("tasks"):
            body += task_table(result["tasks"], expected_tasks)
        return _page(title, run_id, generated_ms, None, body)

    s = result["series"]
    bands = result["bands"]
    ov = result["overall"]
    a = ov.get("all", {})
    counters = result["counters"]
    th_faq, th_biz = thresholds.get("faq", {}), thresholds.get("business", {})
    err_lim = thresholds.get("error_rate", 0.005)

    warn_html = "".join(f'<p class="warn">{e(w)}</p>' for w in warnings)
    dropped = counters.get("dropped_iterations", 0)
    if dropped:
        warn_html += f'<p class="warn">壓測機來不及開新對話（dropped_iterations）共 {dropped:,.0f} 次：實際送出的負載低於設定，需增加 VU 上限或 task 數。</p>'
    if a.get("http_429"):
        warn_html += f'<p class="warn">出現 HTTP 429 共 {a["http_429"]:,} 次（依規劃應立即停止並檢查次數上限設定）。</p>'

    kpis = f"""<div class="kpis">
<div class="kpi"><div>請求總數（送出）</div><b>{fmt_n(a.get('requests'))}</b></div>
<div class="kpi"><div>成功</div><b>{fmt_n(a.get('success'))}</b></div>
<div class="kpi"><div>錯誤率（5xx + timeout）</div><b>{fmt_pct(a.get('error_rate'))}</b></div>
<div class="kpi"><div>FAQ 問答 p95</div><b>{fmt_s(ov.get('faq', {}).get('p95'))} 秒</b></div>
<div class="kpi"><div>業務查詢 p95</div><b>{fmt_s(ov.get('business', {}).get('p95'))} 秒</b></div>
<div class="kpi"><div>完成對話數</div><b>{fmt_n(counters.get('conversations_completed'))}</b></div>
<div class="kpi"><div>資料時間長度</div><b>{result['duration_s'] / 60:.1f} 分</b></div>
</div>"""

    charts = [
        line_chart(s["t"], [("送出（每秒請求）", COLORS["sent"], s["sent_rps"]), ("成功（每秒請求）", COLORS["success"], s["success_rps"], True)],
                   bands, title="① 每秒處理量：送出量 vs 成功量", y_label="次／秒", y_fmt=lambda v: f"{v:,.0f}" if v >= 10 else f"{v:.1f}"),
        note("藍線是壓測機送出的請求量，綠色虛線是成功的量。兩條線重疊代表系統跟得上；"
             "綠線開始低於藍線，代表有請求失敗。背景色塊是各加壓階段（深色＝持續期間，淺色＝爬升期間）。"),
        line_chart(s["t"], [("p50", COLORS["p50"], [None if v is None else v / 1000 for v in s["p50"]]),
                            ("p95", COLORS["p95"], [None if v is None else v / 1000 for v in s["p95"]]),
                            ("p99", COLORS["p99"], [None if v is None else v / 1000 for v in s["p99"]])],
                   bands, title="② 回應時間（全部請求）", y_label="秒", y_fmt=lambda v: f"{v:.0f}" if v >= 10 else f"{v:.1f}",
                   refs=[(30, "負載平衡器 timeout 30 秒", COLORS["stop"])]),
        note("p50 是一半的請求在這個時間內完成；p95 是 95% 的請求在這個時間內完成（最慢的 5% 不算）；p99 同理。"
             "看加壓時這三條線是否明顯往上爬：p50 穩定但 p99 上升，代表少數請求開始卡住；三條一起上升，代表整體變慢。"
             "超過 30 秒的請求會被負載平衡器直接回錯誤。"),
        line_chart(s["t"], [("FAQ 問答 p95", COLORS["faq"], [None if v is None else v / 1000 for v in s["flow_p95"].get("faq", [])]),
                            ("業務查詢 p95", COLORS["business"], [None if v is None else v / 1000 for v in s["flow_p95"].get("business", [])])],
                   bands, title="③ p95 回應時間：FAQ 問答 vs 業務查詢", y_label="秒", y_fmt=lambda v: f"{v:.0f}" if v >= 10 else f"{v:.1f}",
                   refs=[(th_faq.get("p95", 10000) / 1000, f"FAQ p95 參考 {th_faq.get('p95', 10000) / 1000:g} 秒", COLORS["faq"]),
                         (th_biz.get("p95", 5000) / 1000, f"業務 p95 參考 {th_biz.get('p95', 5000) / 1000:g} 秒", COLORS["business"])]),
        note("規劃要求 FAQ 問答與業務查詢分開判定，兩者的參考門檻不同（虛線）。線在虛線以下代表符合參考值。"
             "正式門檻會在 Baseline 實測後與證券方共同確認，確認前以「相對 Baseline 是否明顯惡化」為主。"),
        line_chart(s["t"], [("錯誤率", COLORS["err"], [None if v is None else v * 100 for v in s["error_rate"]])],
                   bands, title="④ 錯誤率（5xx + timeout）", y_label="%", y_fmt=lambda v: f"{v:.1f}",
                   refs=[(err_lim * 100, f"參考 {err_lim * 100:g}%", COLORS["ref"]), (5, "停止條件 5%", COLORS["stop"])]),
        note("只計算系統錯誤（5xx）與逾時；Model Armor 攔截等業務回應不算錯誤，另外統計。"
             "依規劃，錯誤率連續 3 分鐘超過 5% 就停止加壓，以前一階作為實測容量。"),
        line_chart(s["t"], [("VU（虛擬使用者，全部 task 合計）", COLORS["vus"], s["vus"])],
                   bands, title="⑤ 同時在線的虛擬使用者（VU）", y_label="VU", y_fmt=lambda v: f"{v:,.0f}"),
        note("每個 VU 代表一位正在對話中的使用者（含思考時間）。VU 隨各階段上升；如果 VU 碰到上限而出現 dropped_iterations 警告，代表壓測機不夠，不是系統瓶頸。"),
    ]

    detail_rows = [(STAGE_NAMES.get(st["stage"], st["stage"]), st["flows"]) for st in result["stages"]]
    detail_rows.append(("整輪（含爬升）", ov))

    body = f"""
{warn_html}
{GUIDE}
{glossary()}
<h2>總覽</h2>
{kpis}
{note("這份報告由所有壓測機（task）上傳的 k6 原始數據合併重算，百分位數是全體請求的正確值，不是各 task 平均。"
      "報告時間點是「資料截至」所示時間；壓測進行中產生的報告，最後幾分鐘的資料可能尚未上傳。")}

<h2>各階段判定（只計持續期間，不含爬升）</h2>
{stage_summary_table(result['stages'], thresholds)}
{note("每一列是一個加壓階段。依規劃的判定原則：在某個每秒請求量下，p95 與錯誤率同時符合門檻，才算該階段通過；"
      "任一指標超標就停止加壓，以前一階作為實測容量。✗ 後面列出未通過的項目。")}

<h2>趨勢圖</h2>
{''.join(charts)}

<h2>明細（各階段 × 類別）</h2>
{detail_table(detail_rows)}
{note("開場問候與登入只觀察、不設門檻。「成功」是回應符合預期（例如 Model Armor 攔截預期回 205）；"
      "「業務錯誤」是非系統錯誤但不符預期的回應。")}

<h2>資料來源（各 task）</h2>
{task_table(result['tasks'], expected_tasks)}
{note("每個 task 定時把原始數據上傳到 GCS。狀態 running 表示壓測仍在進行，報告只包含到「資料截至」的資料；"
      "全部 task 都是 finished 才是完整結果。「k6 dashboard」是各 task 自己的 k6 內建報表，只代表該 task 的流量，"
      "適合用來排查個別壓測機；「教學版」加上了每張圖怎麼看的說明。連結在 GCS 或本機資料夾中開啟本報告時可以直接點；"
      "單獨下載本報告時，請另外下載同一個 RUN_ID 底下 tasks/ 的檔案。")}
"""
    return _page(title, run_id, generated_ms, result["t1_ms"], body)


def _page(title, run_id, generated_ms, data_until_ms, body):
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{CSS}</style></head>
<body><main>
<button id="teach-toggle" type="button" onclick="this.textContent=document.body.classList.toggle('teach-hidden')?'顯示說明':'隱藏說明'">隱藏說明</button>
<h1>{e(title)}</h1>
<div class="meta">Run ID：{e(run_id)}　｜　資料截至：{fmt_time(data_until_ms)}　｜　報告產生：{fmt_time(generated_ms)}（台北時間）</div>
{body}
</main></body></html>"""
