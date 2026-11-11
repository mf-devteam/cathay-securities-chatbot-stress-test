"""本機執行：cd report-job && python3 -m unittest discover -s tests"""

import gzip
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aggregate  # noqa: E402
import qa_report  # noqa: E402
from test_aggregate import HEADER, row  # noqa: E402

TH = {"faq": 8000, "business": 2000, "pct": 95, "fail_rate": 0.005}


def qa(ts, failed, flow, stage="qa", phase="hold"):
    return row(ts, 1 if failed else 0, tags=f"flow={flow}&case=X&stage={stage}&phase={phase}", metric="qa_failed")


class QaReportTest(unittest.TestCase):
    def build(self, text):
        c = aggregate.Collector(extra_metrics=(qa_report.QA_METRIC,))
        c.add_task("task-000", [gzip.compress(text.encode())])
        result = aggregate.compute(c, ["qa"])
        return c, result, qa_report.compute(c, result, TH, ["qa"])

    def test_fail_rate_counts_every_unexpected_answer(self):
        t = 1000000000000
        tags_faq = "flow=faq&case=B2&stage=qa&phase=hold"
        tags_biz = "flow=business&case=C1&stage=qa&phase=hold"
        # HTTP 200 但 body.status 206（業務錯誤）在 CSV 看起來是成功，只有 qa_failed 知道它失敗
        text = (HEADER
                + row(t, 3000, tags=tags_faq) + qa(t, False, "faq")
                + row(t + 100, 9000, tags=tags_faq) + qa(t + 100, True, "faq")
                + row(t + 200, 1000, tags=tags_biz) + qa(t + 200, False, "business")
                + row(t + 300, 500, status="429", expected="false", tags=tags_biz) + qa(t + 300, True, "business")
                + row(t + 400, 800, tags="flow=faq&case=B2&stage=qa&phase=ramp") + qa(t + 400, False, "faq", phase="ramp"))
        c, result, r = self.build(text)
        self.assertTrue(qa_report.has_data(c))
        a = r["overall"]["all"]
        self.assertEqual((a["total"], a["failed"]), (5, 2))
        self.assertAlmostEqual(a["fail_rate"], 0.4)
        # 階段判定只算持續期間
        st = r["stages"][0]
        self.assertEqual(st["flows"]["all"]["total"], 4)
        self.assertEqual(st["flows"]["faq"]["count"], 2)
        self.assertAlmostEqual(st["flows"]["faq"]["within"], 0.5)
        names = {name: ok for name, _, _, ok in st["checks"]}
        self.assertFalse(names["對話失敗率"])
        page = qa_report.render(r, result, run_id="t", title="t", th=TH, generated_ms=t, expected_tasks=1, warnings=[])
        self.assertIn("對話失敗率", page)

    def test_no_qa_metric_means_no_qa_report(self):
        c = aggregate.Collector(extra_metrics=(qa_report.QA_METRIC,))
        c.add_task("task-000", [gzip.compress((HEADER + row(1000000000000, 100)).encode())])
        self.assertFalse(qa_report.has_data(c))


if __name__ == "__main__":
    unittest.main()
