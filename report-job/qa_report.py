"""對話回應時間測試（k6 script-qa.js）的報告：以「一問一答」為單位看回應時間與失敗率。

- 對話＝使用者提問的一問一答（一次聊天 API /api/chat/ask）
- 開場問候不計入對話統計（客戶 2026-11-12 回覆），只單獨列出（qa_opening_failed），不設門檻
- 回應時間：k6 http_req_duration（送出到收完回應，含 AI 生成時間），依 flow 分 FAQ／業務查詢
- 失敗率＝對話失敗筆數 ÷ 對話總筆數：k6 腳本寫的 qa_failed（回應不是預期結果＝5xx、timeout、429、業務錯誤）
- 判定只看持續期間（不含爬升）；「整輪」含爬升

原本的報告（render.py）照常產生，這份是另外一份（reports/qa-latest.html）。
"""

from __future__ import annotations

import os
from array import array

import aggregate
from aggregate import percentile
from render import COLORS, STAGE_NAMES, _page, e, fmt_n, fmt_pct, fmt_rate, fmt_s, line_chart, mark, note, task_table

QA_METRIC = "qa_failed"
OPENING_METRIC = "qa_opening_failed"
EXTRA_METRICS = (QA_METRIC, OPENING_METRIC)
FLOWS = ("faq", "business", "init")
FLOW_NAMES = {"all": "全部對話", "faq": "FAQ 問答", "business": "業務查詢", "init": "開場問候（不計入）"}


def thresholds_from_env(default_fail_rate: float) -> dict:
    """與 k6 script-qa.js 相同的環境變數與預設值。"""
    def num(name, default):
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else default

    return {
        "faq": num("QA_FAQ_MAX_MS", 8000),
        "business": num("QA_BUSINESS_MAX_MS", 2000),
        "pct": num("QA_LATENCY_PCT", 95),
        "fail_rate": num("QA_FAIL_RATE", default_fail_rate),
    }


def has_data(c: aggregate.Collector) -> bool:
    return bool(c.extra.get(QA_METRIC) or c.extra.get(OPENING_METRIC))


def _latency(durs, limit, pct):
    durs = sorted(durs)
    n = len(durs)
    within = sum(1 for d in durs if d <= limit) if limit is not None else None
    return {
        "count": n,
        "avg": sum(durs) / n if n else None,
        "p50": percentile(durs, 0.50),
        "p90": percentile(durs, 0.90),
        "p95": percentile(durs, 0.95),
        "p99": percentile(durs, 0.99),
        "max": durs[-1] if n else None,
        "judged": percentile(durs, pct),  # 判定用的百分位（QA_LATENCY_PCT）
        "within": (within / n) if (n and within is not None) else None,
    }


def compute(c: aggregate.Collector, result: dict, th: dict, stage_order: list[str]) -> dict:
    flows, stages, phases = c.flows.names, c.stages.names, c.phases.names
    t0, bucket_s = result["t0_ms"], result["series"]["bucket_s"]
    nb = len(result["series"]["t"])
    pct = th["pct"] / 100.0
    limits = {"faq": th["faq"], "business": th["business"]}

    # 回應時間：聊天 API 的 http_req_duration（key＝(階段 or "all", flow)）
    durs: dict[tuple[str, str], array] = {}
    b_durs = {fl: [array("d") for _ in range(nb)] for fl in FLOWS}
    for i in range(len(c.ts)):
        fl = flows[c.flow[i]]
        if fl not in FLOWS:
            continue
        d = c.dur[i]
        counted = fl != "init"  # 開場問候不計入「全部對話」
        keys = [("all", fl)] + ([("all", "all")] if counted else [])
        if phases[c.phase[i]] == "hold":
            st = stages[c.stage[i]]
            keys += [(st, fl)] + ([(st, "all")] if counted else [])
        for k in keys:
            durs.setdefault(k, array("d")).append(d)
        b = int((c.ts[i] - t0) / 1000.0 // bucket_s)
        if 0 <= b < nb:
            b_durs[fl][b].append(d)

    # 失敗：qa_failed（使用者提問）、qa_opening_failed（開場問候，只單獨列出）；1＝失敗
    fails: dict[tuple[str, str], list[int]] = {}
    b_total, b_fail, b_opening = [0] * nb, [0] * nb, [0] * nb
    seen_stages = []
    samples = [(r, False) for r in c.extra.get(QA_METRIC, [])] + [(r, True) for r in c.extra.get(OPENING_METRIC, [])]
    for (ts, value, fl, st, ph), opening in samples:
        failed = 1 if value >= 0.5 else 0
        fl = "init" if opening else fl
        keys = [("all", fl)] + ([] if opening else [("all", "all")])
        if ph == "hold":
            keys += [(st, fl)] + ([] if opening else [(st, "all")])
            if st not in seen_stages:
                seen_stages.append(st)
        for k in keys:
            f = fails.setdefault(k, [0, 0])
            f[0] += 1
            f[1] += failed
        b = int((ts - t0) / 1000.0 // bucket_s)
        if 0 <= b < nb:
            if opening:
                b_opening[b] += 1
            else:
                b_total[b] += 1
                b_fail[b] += failed

    def summary(scope, fl, secs):
        lat = _latency(durs.get((scope, fl), ()), limits.get(fl), pct)
        total, failed = fails.get((scope, fl), [0, 0])
        lat.update({"total": total, "failed": failed, "fail_rate": (failed / total) if total else None,
                    "tps": (total / secs) if secs else None})
        return lat

    # 各階段持續期間的秒數（沿用原本報告的計算）
    secs_by_stage = {row["stage"]: row["seconds"] for row in result["stages"]}
    ordered = [s for s in stage_order if s in seen_stages] + [s for s in seen_stages if s not in stage_order]
    stage_rows = []
    for st in ordered:
        secs = secs_by_stage.get(st)
        row = {"stage": st, "seconds": secs, "flows": {fl: summary(st, fl, secs) for fl in ("all",) + FLOWS if fails.get((st, fl))}}
        row["checks"] = judge(row["flows"], th)
        stage_rows.append(row)
    total_secs = max(1.0, result["duration_s"])
    overall = {fl: summary("all", fl, total_secs) for fl in ("all",) + FLOWS if fails.get(("all", fl))}

    series = {
        "t": result["series"]["t"],
        "tps": [x / bucket_s for x in b_total],
        "chat_tps": [(b_total[b] + b_opening[b]) / bucket_s for b in range(nb)],
        "fail_rate": [(b_fail[b] / b_total[b]) if b_total[b] else None for b in range(nb)],
        "lat": {fl: [percentile(sorted(v), pct) for v in b_durs[fl]] for fl in FLOWS},
    }
    return {"overall": overall, "stages": stage_rows, "series": series, "checks": judge(overall, th)}


def judge(row_flows: dict, th: dict) -> list[tuple[str, str, str, bool]]:
    """回傳 [(項目, 實測, 門檻, 是否通過)]。"""
    key = f"p{th['pct']:g}"
    out = []
    for fl in ("faq", "business"):
        s = row_flows.get(fl)
        if not s or not s["count"]:
            continue
        v = s["judged"]
        out.append((f"{FLOW_NAMES[fl]} {key}", f"{fmt_s(v)} 秒", f"≤ {th[fl] / 1000:g} 秒", v is not None and v <= th[fl]))
    s = row_flows.get("all")
    if s and s.get("fail_rate") is not None:
        out.append(("對話失敗率", fmt_pct(s["fail_rate"]), f"≤ {th['fail_rate'] * 100:g}%", s["fail_rate"] <= th["fail_rate"]))
    return out


def _verdict(checks):
    if not checks:
        return "—"
    if all(c[3] for c in checks):
        return mark(True) + " 全部通過"
    return mark(False) + " " + "、".join(c[0] for c in checks if not c[3])


# ---------------- HTML ----------------

def _stage_table(stage_rows, th):
    key = f"p{th['pct']:g}"
    head = (f"<tr><th>階段</th><th>每秒對話<br>（不含開場問候）</th><th>對話總筆數</th><th>失敗筆數</th><th>失敗率</th>"
            f"<th>FAQ {key}</th><th>業務 {key}</th><th>判定</th></tr>")
    rows = []
    for st in stage_rows:
        f = st["flows"]
        a, faq, biz = f.get("all", {}), f.get("faq", {}), f.get("business", {})
        rows.append(
            f"<tr><td>{e(STAGE_NAMES.get(st['stage'], st['stage']))}</td><td>{fmt_rate(a.get('tps'))}</td>"
            f"<td>{fmt_n(a.get('total'))}</td><td>{fmt_n(a.get('failed'))}</td><td>{fmt_pct(a.get('fail_rate'))}</td>"
            f"<td>{fmt_s(faq.get('judged'))} 秒</td><td>{fmt_s(biz.get('judged'))} 秒</td>"
            f"<td class='left'>{_verdict(st['checks'])}</td></tr>"
        )
    return f"<table>{head}{''.join(rows)}</table>"


def _detail_table(rows_by_label, th):
    head = ("<tr><th>階段</th><th>類別</th><th>對話筆數</th><th>失敗</th><th>失敗率</th><th>avg</th><th>p50</th><th>p90</th>"
            "<th>p95</th><th>p99</th><th>max</th><th>門檻內比例</th></tr>")
    rows = []
    for label, flows in rows_by_label:
        first = True
        for fl in ("all",) + FLOWS:
            s = flows.get(fl)
            if not s:
                continue
            within = "—" if s["within"] is None else f"{fmt_pct(s['within'])}（≤ {th[fl] / 1000:g} 秒）"
            rows.append(
                f"<tr><td>{e(label) if first else ''}</td><td>{e(FLOW_NAMES[fl])}</td><td>{fmt_n(s['total'])}</td>"
                f"<td>{fmt_n(s['failed'])}</td><td>{fmt_pct(s['fail_rate'])}</td><td>{fmt_s(s['avg'])}</td><td>{fmt_s(s['p50'])}</td>"
                f"<td>{fmt_s(s['p90'])}</td><td>{fmt_s(s['p95'])}</td><td>{fmt_s(s['p99'])}</td><td>{fmt_s(s['max'])}</td>"
                f"<td>{within}</td></tr>"
            )
            first = False
    return f'<table class="detail">{head}{"".join(rows)}</table><p class="small">回應時間單位：秒。</p>'


def _check_table(checks):
    if not checks:
        return ""
    rows = "".join(f"<tr><td>{e(n)}</td><td>{e(v)}</td><td>{e(lim)}</td><td>{mark(ok)}</td></tr>" for n, v, lim, ok in checks)
    return f"<table><tr><th>項目</th><th>實測（整輪）</th><th>門檻</th><th>判定</th></tr>{rows}</table>"


def render(qa: dict, result: dict, *, run_id: str, title: str, th: dict, generated_ms: float,
           expected_tasks: int | None, warnings: list[str]) -> str:
    ov, s, bands = qa["overall"], qa["series"], result["bands"]
    a = ov.get("all", {})
    key = f"p{th['pct']:g}"
    sec = lambda v: None if v is None else v / 1000  # noqa: E731
    y_s = lambda v: f"{v:.0f}" if v >= 10 else f"{v:.1f}"  # noqa: E731

    warn_html = "".join(f'<p class="warn">{e(w)}</p>' for w in warnings)
    dropped = result["counters"].get("dropped_iterations", 0)
    if dropped:
        warn_html += f'<p class="warn">壓測機來不及送出新對話（dropped_iterations）共 {dropped:,.0f} 次：實際送出的負載低於設定，需增加 QA_MAX_VUS 或 runner。</p>'

    kpis = f"""<div class="kpis">
<div class="kpi"><div>對話總筆數（不含開場問候）</div><b>{fmt_n(a.get('total'))}</b></div>
<div class="kpi"><div>對話失敗筆數</div><b>{fmt_n(a.get('failed'))}</b></div>
<div class="kpi"><div>失敗率</div><b>{fmt_pct(a.get('fail_rate'))}</b></div>
<div class="kpi"><div>FAQ 問答 {key}</div><b>{fmt_s(ov.get('faq', {}).get('judged'))} 秒</b></div>
<div class="kpi"><div>業務查詢 {key}</div><b>{fmt_s(ov.get('business', {}).get('judged'))} 秒</b></div>
<div class="kpi"><div>平均每秒對話（不含開場問候）</div><b>{fmt_rate(a.get('tps'))}</b></div>
<div class="kpi"><div>資料時間長度</div><b>{result['duration_s'] / 60:.1f} 分</b></div>
</div>"""

    charts = [
        line_chart(s["t"], [("聊天 API 合計（TPS，含開場問候）", COLORS["sent"], s["chat_tps"]),
                            ("使用者提問（計入對話統計）", COLORS["success"], s["tps"], True)],
                   bands, title="① 每秒對話數（一問一答）", y_label="筆／秒",
                   y_fmt=lambda v: f"{v:,.0f}" if v >= 10 else f"{v:.1f}"),
        note("藍線是壓測機每秒送出的聊天 API 請求數，也就是設定的 TPS（與地端規劃的 TPS 定義相同，含開場問候）；"
             "綠色虛線是其中使用者提問的部分，也就是計入對話統計的對話（約佔 9 成）。"
             "先在爬升期間（淺色）拉到目標，之後在持續期間（深色）維持固定負載。"),
        line_chart(s["t"], [(f"FAQ 問答 {key}", COLORS["faq"], [sec(v) for v in s["lat"]["faq"]]),
                            (f"業務查詢 {key}", COLORS["business"], [sec(v) for v in s["lat"]["business"]]),
                            (f"開場問候 {key}", COLORS["vus"], [sec(v) for v in s["lat"]["init"]], True)],
                   bands, title=f"② 對話回應時間（{key}）", y_label="秒", y_fmt=y_s,
                   refs=[(th["faq"] / 1000, f"FAQ 門檻 {th['faq'] / 1000:g} 秒", COLORS["faq"]),
                         (th["business"] / 1000, f"業務門檻 {th['business'] / 1000:g} 秒", COLORS["business"])]),
        note(f"回應時間＝使用者送出問題到收完回覆（含 AI 生成時間）。{key} 是 {th['pct']:g}% 的對話在這個時間內完成。"
             "線在同色虛線以下代表符合門檻；開場問候只觀察，不計入統計。"),
        line_chart(s["t"], [("失敗率", COLORS["err"], [None if v is None else v * 100 for v in s["fail_rate"]])],
                   bands, title="③ 對話失敗率", y_label="%", y_fmt=lambda v: f"{v:.1f}",
                   refs=[(th["fail_rate"] * 100, f"門檻 {th['fail_rate'] * 100:g}%", COLORS["ref"])]),
        note("失敗率＝對話失敗筆數 ÷ 對話總筆數。只要回應不是預期結果就算失敗：系統錯誤（5xx）、逾時、HTTP 429、"
             "業務錯誤（例如 AI 無法生成、Model Armor 該攔截卻沒有攔截）。對話失敗後訪客照樣繼續問下去。"),
    ]

    detail_rows = [(STAGE_NAMES.get(st["stage"], st["stage"]), st["flows"]) for st in qa["stages"]]
    detail_rows.append(("整輪（含爬升）", ov))
    opening = ov.get("init")
    opening_html = ("" if not opening else note(
        f"開場問候（訪客進入聊天時系統自動回覆的招呼語）照真實訪客流程送出，但不計入對話總筆數、失敗率與判定，只在明細單獨列出供參考："
        f"共 {fmt_n(opening['total'])} 則，失敗 {fmt_n(opening['failed'])} 則，{key} {fmt_s(opening['judged'])} 秒。"))

    body = f"""
{warn_html}
<h2>總覽</h2>
{kpis}
{note(f"這份報告以「對話」（使用者提問的一問一答＝一次聊天 API）為單位，不含開場問候。回應時間門檻：FAQ 問答 {key} ≤ {th['faq'] / 1000:g} 秒（含 AI 思考時間）、"
      f"業務查詢 {key} ≤ {th['business'] / 1000:g} 秒；失敗率門檻 ≤ {th['fail_rate'] * 100:g}%。"
      "原本以請求為單位、5xx＋timeout 為錯誤率的報告仍另外產生（latest.html）。")}
{_check_table(qa['checks'])}
{opening_html}

<h2>各階段判定（只計持續期間，不含爬升）</h2>
{_stage_table(qa['stages'], th)}

<h2>趨勢圖</h2>
{''.join(charts)}

<h2>明細（各階段 × 類別）</h2>
{_detail_table(detail_rows, th)}
{note("「門檻內比例」是回應時間在門檻內的對話佔該類別的比例，例如 FAQ 98% 代表每 100 則 FAQ 有 98 則在門檻內回覆。")}

<h2>資料來源（各 task）</h2>
{task_table(result['tasks'], expected_tasks)}
"""
    return _page(title, run_id, generated_ms, result["t1_ms"], body)
