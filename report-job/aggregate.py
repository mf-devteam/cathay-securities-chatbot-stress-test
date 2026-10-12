"""k6 CSV 原始數據（gzip）的串流解析與彙總。

- 可讀「還在寫入中」的 gzip（沒有結尾）：讀到哪算到哪，最後不完整的一行丟棄。
- 百分位數用全部 task 的原始數據重算（不是把各 task 的 p95 平均），算法與 k6 相同（線性內插）。
"""

from __future__ import annotations

import csv
import math
import zlib
from array import array
from dataclasses import dataclass, field
from typing import Iterable, Iterator

DURATION_METRIC = "http_req_duration"
# 只解析需要的指標（每個請求約 13 列，其中只有 http_req_duration 需要），先用字首快速過濾
WANTED_PREFIXES = ("http_req_duration,", "vus,", "dropped_iterations,", "conversations_completed,")

# 請求結果分類（與 k6 腳本 record() 的判斷一致）
SUCCESS, ERROR_5XX_TIMEOUT, HTTP_429, BUSINESS_ERROR = 0, 1, 2, 3


def iter_gzip_lines(chunks: Iterable[bytes], stats: dict | None = None) -> Iterator[str]:
    """把 gzip 的位元組串流解成一行一行的文字。檔案未寫完或損毀時，回傳目前為止完整的行。"""
    decomp = zlib.decompressobj(zlib.MAX_WBITS | 16)
    buf = b""
    for chunk in chunks:
        try:
            data = decomp.decompress(chunk)
        except zlib.error:
            if stats is not None:
                stats["corrupt"] = True
            break
        if not data:
            continue
        buf += data
        *lines, buf = buf.split(b"\n")
        for line in lines:
            yield line.decode("utf-8", "replace")
    if stats is not None:
        stats["truncated"] = not decomp.eof
        stats["dropped_partial_line"] = bool(buf)


def parse_tags(extra: str) -> dict[str, str]:
    if not extra:
        return {}
    out = {}
    for part in extra.split("&"):
        key, _, value = part.partition("=")
        out[key] = value
    return out


def percentile(sorted_values, p: float) -> float | None:
    """與 k6 相同：在排序後的值之間做線性內插。"""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    pos = p * (n - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, n - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def classify(status: int, expected: str) -> int:
    if status == 0 or status == 408 or status == 504 or status >= 500:
        return ERROR_5XX_TIMEOUT
    if status == 429:
        return HTTP_429
    if expected == "true":
        return SUCCESS
    return BUSINESS_ERROR


class _Index:
    """字串 → 小整數，讓每個請求只佔約 20 bytes。"""

    def __init__(self):
        self.names: list[str] = []
        self._map: dict[str, int] = {}

    def get(self, name: str) -> int:
        idx = self._map.get(name)
        if idx is None:
            idx = len(self.names)
            self._map[name] = idx
            self.names.append(name)
        return idx


@dataclass
class TaskInfo:
    name: str
    requests: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    truncated: bool = False
    corrupt: bool = False
    status: dict = field(default_factory=dict)
    dashboard: bool = False  # 該 task 的 k6 dashboard 已上傳，且已產生教學版


class Collector:
    """逐行收集；每個請求存成數個平行陣列，最後再一次計算。"""

    def __init__(self):
        self.ts = array("d")
        self.dur = array("d")
        self.flow = array("b")
        self.stage = array("b")
        self.phase = array("b")
        self.outcome = array("b")
        self.task = array("h")
        self.flows = _Index()
        self.stages = _Index()
        self.phases = _Index()
        self.tasks: list[TaskInfo] = []
        # 每個 task 的 VU 數（gauge）與計數器：(ts, value)
        self.vus: list[list[tuple[float, float]]] = []
        self.counters: dict[str, list[tuple[float, float]]] = {"dropped_iterations": [], "conversations_completed": []}

    def add_task(self, name: str, chunks: Iterable[bytes], status: dict | None = None) -> TaskInfo:
        info = TaskInfo(name=name, status=status or {})
        task_idx = len(self.tasks)
        self.tasks.append(info)
        self.vus.append([])
        stats: dict = {}
        lines = iter_gzip_lines(chunks, stats)
        header_line = next(lines, None)
        if not header_line:
            return info
        header = next(csv.reader([header_line]))
        reader = csv.reader(line for line in lines if line.startswith(WANTED_PREFIXES))
        col = {name: i for i, name in enumerate(header)}
        i_metric, i_ts, i_val = col["metric_name"], col["timestamp"], col["metric_value"]
        i_status, i_expected, i_extra = col["status"], col["expected_response"], col["extra_tags"]
        width = len(header)

        for row in reader:
            if len(row) < width:
                continue  # 截斷或格式不完整的行
            metric = row[i_metric]
            try:
                ts = float(row[i_ts])
                value = float(row[i_val])
            except ValueError:
                continue
            if ts < 1e11:  # 秒（未設定 K6_CSV_TIME_FORMAT=unix_milli 時）
                ts *= 1000.0

            if metric == DURATION_METRIC:
                tags = parse_tags(row[i_extra])
                status_code = int(row[i_status]) if row[i_status].isdigit() else 0
                self.ts.append(ts)
                self.dur.append(value)
                self.flow.append(self.flows.get(tags.get("flow", "unknown")))
                self.stage.append(self.stages.get(tags.get("stage", "unknown")))
                self.phase.append(self.phases.get(tags.get("phase", "unknown")))
                self.outcome.append(classify(status_code, row[i_expected]))
                self.task.append(task_idx)
                info.requests += 1
                info.first_ts = ts if info.first_ts is None else min(info.first_ts, ts)
                info.last_ts = ts if info.last_ts is None else max(info.last_ts, ts)
            elif metric == "vus":
                self.vus[task_idx].append((ts, value))
            elif metric in self.counters:
                self.counters[metric].append((ts, value))

        info.truncated = stats.get("truncated", False)
        info.corrupt = stats.get("corrupt", False)
        return info


# ---------------- 計算 ----------------

def _summary(durations, outcomes) -> dict:
    n = len(durations)
    durations = sorted(durations)
    count = [0, 0, 0, 0]
    for o in outcomes:
        count[o] += 1
    return {
        "requests": n,
        "success": count[SUCCESS],
        "errors_5xx_timeout": count[ERROR_5XX_TIMEOUT],
        "http_429": count[HTTP_429],
        "business_errors": count[BUSINESS_ERROR],
        "error_rate": (count[ERROR_5XX_TIMEOUT] / n) if n else None,
        "avg": (sum(durations) / n) if n else None,
        "p50": percentile(durations, 0.50),
        "p90": percentile(durations, 0.90),
        "p95": percentile(durations, 0.95),
        "p99": percentile(durations, 0.99),
        "max": durations[-1] if n else None,
    }


def _nice_bucket(total_s: float, max_points: int = 150) -> int:
    for b in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
        if total_s / b <= max_points:
            return b
    return 900


def compute(c: Collector, stage_order: list[str]) -> dict:
    n = len(c.ts)
    if n == 0:
        return {"requests": 0, "tasks": c.tasks}

    t0 = min(c.ts)
    t1 = max(c.ts)
    flows = c.flows.names
    # 報表上類別的固定順序；其他未知類別排在後面
    flow_order = [f for f in ("faq", "business", "init", "login") if f in flows] + [f for f in flows if f not in ("faq", "business", "init", "login")]
    stages = c.stages.names
    phases = c.phases.names
    hold = phases.index("hold") if "hold" in phases else -1

    # 整體、各類別、各階段（只算 hold，爬升期間不列入判定）
    groups: dict[tuple, tuple[array, array]] = {}
    for i in range(n):
        fl = flows[c.flow[i]]
        d, o = c.dur[i], c.outcome[i]
        for key in (("all", "all"), ("all", fl)):
            g = groups.setdefault(key, (array("d"), array("b")))
            g[0].append(d)
            g[1].append(o)
        if c.phase[i] == hold:
            st = stages[c.stage[i]]
            for key in ((st, "all"), (st, fl)):
                g = groups.setdefault(key, (array("d"), array("b")))
                g[0].append(d)
                g[1].append(o)

    # 各階段持續期間的實際起訖時間（用於每秒請求數與圖上的色帶）
    spans: dict[tuple[str, str], list[float]] = {}
    for i in range(n):
        key = (stages[c.stage[i]], phases[c.phase[i]])
        span = spans.get(key)
        t = c.ts[i]
        if span is None:
            spans[key] = [t, t]
        else:
            if t < span[0]:
                span[0] = t
            if t > span[1]:
                span[1] = t

    stage_rows = []
    ordered = [s for s in stage_order if s in stages] + [s for s in stages if s not in stage_order]
    for st in ordered:
        if (st, "all") not in groups:
            continue
        span = spans.get((st, "hold"))
        secs = max(1.0, (span[1] - span[0]) / 1000.0) if span else None
        row = {"stage": st, "seconds": secs, "flows": {}}
        for fl in ["all"] + flow_order:
            g = groups.get((st, fl))
            if g:
                s = _summary(g[0], g[1])
                s["rps"] = s["requests"] / secs if secs else None
                row["flows"][fl] = s
        stage_rows.append(row)

    overall = {}
    for fl in ["all"] + flow_order:
        g = groups.get(("all", fl))
        if g:
            s = _summary(g[0], g[1])
            s["rps"] = s["requests"] / max(1.0, (t1 - t0) / 1000.0)
            overall[fl] = s

    # 時間序列
    total_s = (t1 - t0) / 1000.0
    bucket_s = _nice_bucket(total_s)
    nb = int(total_s // bucket_s) + 1
    sent = [0] * nb
    ok = [0] * nb
    err = [0] * nb
    per_flow_d = {fl: [array("d") for _ in range(nb)] for fl in flows}
    all_d = [array("d") for _ in range(nb)]
    for i in range(n):
        b = int((c.ts[i] - t0) / 1000.0 // bucket_s)
        sent[b] += 1
        o = c.outcome[i]
        if o == SUCCESS:
            ok[b] += 1
        elif o == ERROR_5XX_TIMEOUT:
            err[b] += 1
        per_flow_d[flows[c.flow[i]]][b].append(c.dur[i])
        all_d[b].append(c.dur[i])

    def pct_series(buckets, p):
        out = []
        for vals in buckets:
            out.append(percentile(sorted(vals), p))
        return out

    series = {
        "bucket_s": bucket_s,
        "t": [b * bucket_s for b in range(nb)],
        "sent_rps": [x / bucket_s for x in sent],
        "success_rps": [x / bucket_s for x in ok],
        "error_rate": [(err[b] / sent[b]) if sent[b] else None for b in range(nb)],
        "p50": pct_series(all_d, 0.50),
        "p95": pct_series(all_d, 0.95),
        "p99": pct_series(all_d, 0.99),
        "flow_p95": {fl: pct_series(per_flow_d[fl], 0.95) for fl in flows},
    }

    # VU：各 task 在每個時間區間取最大值後加總
    vus = [0.0] * nb
    for task_samples in c.vus:
        per_bucket = [0.0] * nb
        for ts, v in task_samples:
            b = int((ts - t0) / 1000.0 // bucket_s)
            if 0 <= b < nb and v > per_bucket[b]:
                per_bucket[b] = v
        for b in range(nb):
            vus[b] += per_bucket[b]
    series["vus"] = vus

    counters = {name: sum(v for _, v in samples) for name, samples in c.counters.items()}

    bands = []
    for (st, ph), (a, b) in spans.items():
        if ph in ("ramp", "hold"):
            bands.append({"stage": st, "phase": ph, "start": (a - t0) / 1000.0, "end": (b - t0) / 1000.0})
    bands.sort(key=lambda x: x["start"])

    return {
        "requests": n,
        "t0_ms": t0,
        "t1_ms": t1,
        "duration_s": total_s,
        "flows": flows,
        "overall": overall,
        "stages": stage_rows,
        "series": series,
        "bands": bands,
        "counters": counters,
        "tasks": c.tasks,
    }
