"""本機執行：cd report-job && python3 -m unittest discover -s tests"""

import gzip
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aggregate  # noqa: E402

HEADER = "metric_name,timestamp,metric_value,check,error,error_code,expected_response,group,method,name,proto,scenario,service,status,subproto,tls_version,url,extra_tags,metadata\n"


def row(ts, value, status="200", expected="true", tags="flow=faq&case=B2&stage=x1&phase=hold", metric="http_req_duration"):
    return f"{metric},{ts},{value},,,,{expected},,POST,POST /api/chat/ask,HTTP/1.1,conversation,,{status},,,http://127.0.0.1/api/chat/ask,{tags},\n"


class GzipStreamTest(unittest.TestCase):
    def test_reads_unfinished_gzip_and_drops_partial_line(self):
        text = HEADER + row(1000000000000, 100) + row(1000000001000, 200)
        full = gzip.compress(text.encode())
        cut = full[: len(full) - 8]  # 去掉 gzip 結尾：模擬上傳時檔案還在寫入
        stats = {}
        lines = list(aggregate.iter_gzip_lines([cut], stats))
        self.assertEqual(lines[0] + "\n", HEADER)
        self.assertTrue(stats["truncated"])
        # 完整的行都要讀到，最後沒有換行的部分會被丟棄
        self.assertGreaterEqual(len(lines), 2)

    def test_collector_classifies_outcomes(self):
        text = (HEADER + row(1000000000000, 100) + row(1000000000500, 200, status="503", expected="false")
                + row(1000000001000, 300, status="429", expected="false") + row(1000000001500, 50, status="205", expected="true")
                + row(1000000002000, 400, status="206", expected="false") + row(1000000002500, 60000, status="0", expected="false")
                + "vus,1000000000000,3.000000,,,,,,,,,conversation,,,,,,,\n")
        c = aggregate.Collector()
        c.add_task("task-000", [gzip.compress(text.encode())])
        r = aggregate.compute(c, ["x1"])
        a = r["overall"]["all"]
        self.assertEqual(a["requests"], 6)
        self.assertEqual(a["success"], 2)
        self.assertEqual(a["errors_5xx_timeout"], 2)
        self.assertEqual(a["http_429"], 1)
        self.assertEqual(a["business_errors"], 1)
        self.assertEqual(r["stages"][0]["stage"], "x1")


class PercentileTest(unittest.TestCase):
    def test_linear_interpolation_like_k6(self):
        vals = [1, 2, 3, 4]
        self.assertEqual(aggregate.percentile(vals, 0.5), 2.5)
        self.assertAlmostEqual(aggregate.percentile(vals, 0.95), 3.85)
        self.assertEqual(aggregate.percentile([7], 0.99), 7)
        self.assertIsNone(aggregate.percentile([], 0.5))

    def test_merge_is_not_average_of_task_percentiles(self):
        # 兩個 task：一個全快、一個全慢。全體 p50 應由合併資料重算，不等於兩個 p50 的平均
        fast, slow = [100.0] * 90, [10000.0] * 10
        merged = sorted(fast + slow)
        self.assertEqual(aggregate.percentile(merged, 0.5), 100.0)
        self.assertNotEqual((aggregate.percentile(fast, 0.5) + aggregate.percentile(slow, 0.5)) / 2, 100.0)


if __name__ == "__main__":
    unittest.main()
