"""本機執行：cd report-job && python3 -m unittest discover -s tests"""

import gzip
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import v3_report  # noqa: E402
from test_aggregate import HEADER, row  # noqa: E402

T = 1_000_000_000_000


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def req(ts, ms, flow, case, phase="hold", status="200"):
    return row(ts, ms, status=status, tags=f"flow={flow}&case={case}&stage=x3&phase={phase}")


def qa(ts, failed, flow, case, phase="hold"):
    metric = "qa_opening_failed" if flow == "init" else "qa_failed"
    return row(ts, 1 if failed else 0, tags=f"flow={flow}&case={case}&stage=x3&phase={phase}", metric=metric)


SUITE_ENV = """# test
SUITE_ID=20261113-v3x3-01
KIND=x3
CREATED=1000000000
PLAN_A=x3:354tps:10:1
PLAN_B=x3:354tps:10:1
GAP_MIN=2
ABORT=false
RUN_A=S-A
RUN_B=S-B
WEIGHTS_A=B2-2:36\\;C1:12
WEIGHTS_B=B2:28\\;B3:15\\;B4:5\\;B5:2
A_MAX_MS=6000
B_MAX_MS=20000
LATENCY_PCT=95
FAIL_RATE=0.005
ABORT_FAIL_RATE=0.05
ABORT_DELAY=3m
TARGET=https://uat.example
RUNNER_COUNT=3
"""


class V3ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.suite = os.path.join(self.root, "suites", "S")
        os.makedirs(self.suite)
        with open(os.path.join(self.suite, "suite.env"), "w") as f:
            f.write(SUITE_ENV)

    def tearDown(self):
        self.tmp.cleanup()

    def write_run(self, run, text):
        d = os.path.join(self.root, "runs", run)
        os.makedirs(os.path.join(d, "raw"))
        os.makedirs(os.path.join(d, "status"))
        with open(os.path.join(d, "raw", "task-000.csv.gz"), "wb") as f:
            f.write(gzip.compress(text.encode()))
        with open(os.path.join(d, "status", "task-000.json"), "w") as f:
            f.write('{"state":"finished","k6_exit":0}')

    def write_state(self, text):
        with open(os.path.join(self.suite, "state.env"), "w") as f:
            f.write(text)

    def build(self):
        return v3_report.build(self.suite, os.path.join(self.root, "runs"), "t")

    def test_parts_are_judged_separately_with_own_thresholds(self):
        # A：業務查詢 5 秒（≤ 6 秒通過）；爬升期間很慢、開場問候很慢都不影響判定；前端請求不計入
        a = (HEADER
             + req(T, 5000, "business", "B2-2") + qa(T, False, "business", "B2-2")
             + req(T + 100, 5000, "business", "C1") + qa(T + 100, False, "business", "C1")
             + req(T + 200, 50000, "business", "C1", phase="ramp") + qa(T + 200, True, "business", "C1", phase="ramp")
             + req(T + 300, 30000, "init", "B1") + qa(T + 300, True, "init", "B1")
             + req(T + 400, 90000, "bubble", "bubble"))
        # B：FAQ 15 秒（≤ 20 秒），但 1/2 失敗（HTTP 200 卻回應內容不符）→ 未通過
        b = (HEADER
             + req(T, 15000, "faq", "B2") + qa(T, False, "faq", "B2")
             + req(T + 100, 15000, "faq", "B5") + qa(T + 100, True, "faq", "B5"))
        self.write_run("S-A", a)
        self.write_run("S-B", b)
        self.write_state("A_REASON=completed\nB_REASON=completed\nFINAL=completed\n")
        r = self.build()
        self.assertEqual(r["verdicts"], {"A": "pass", "B": "fail"})
        ja = r["parts"]["A"]["judged"]
        self.assertEqual((ja["total"], ja["failed"], ja["count"]), (2, 0, 2))
        self.assertEqual(ja["completed"], 2)
        jb = r["parts"]["B"]["judged"]
        self.assertEqual((jb["total"], jb["completed"], jb["failed"]), (2, 1, 1))
        self.assertEqual(r["parts"]["B"]["cases"][1]["completed"], 0)  # B5 那一則失敗
        self.assertEqual(r["parts"]["A"]["whole"]["total"], 3)  # 整輪含爬升
        self.assertEqual(r["parts"]["A"]["opening"]["total"], 1)
        self.assertEqual([c["case"] for c in r["parts"]["A"]["cases"]], ["B2-2", "C1"])
        self.assertEqual(r["parts"]["B"]["causes"]["content"], 1)
        verdict_env = read(os.path.join(self.suite, "reports", "verdict.env"))
        self.assertIn("A_VERDICT=pass", verdict_env)
        self.assertIn("B_VERDICT=fail", verdict_env)
        page = read(os.path.join(self.suite, "reports", "report-20261113-v3x3-01.html"))
        self.assertIn("B2-2 FAQ intent", page)
        self.assertIn("完成對話數", page)
        self.assertIn("完成對話 1／2 筆", verdict_env)
        self.assertIn("≤ 6 秒", page)
        self.assertIn("≤ 20 秒", page)

    def test_aborted_part_fails_and_skipped_part_has_no_data(self):
        a = HEADER + req(T, 1000, "business", "C1") + qa(T, True, "business", "C1")
        self.write_run("S-A", a)
        self.write_state("A_REASON=aborted\nB_REASON=skipped\nFINAL=completed\n")
        r = self.build()
        self.assertEqual(r["verdicts"], {"A": "fail", "B": "nodata"})
        page = r["page"]
        self.assertIn("自動停止", page)
        self.assertIn("未執行", page)

    def test_stopped_during_ramp_uses_whole_run(self):
        a = HEADER + req(T, 1000, "business", "C1", phase="ramp") + qa(T, False, "business", "C1", phase="ramp")
        self.write_run("S-A", a)
        self.write_state("A_REASON=stopped\nB_REASON=skipped\nFINAL=stopped\n")
        open(os.path.join(self.suite, "stop-requested"), "w").close()
        r = self.build()
        self.assertEqual(r["parts"]["A"]["basis"], "all")
        self.assertEqual(r["verdicts"]["A"], "incomplete")
        self.assertIn("手動停止", r["page"])


if __name__ == "__main__":
    unittest.main()
