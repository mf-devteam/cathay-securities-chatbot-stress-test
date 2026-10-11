"""本機執行：cd report-job && python3 -m unittest discover -s tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import annotate  # noqa: E402

PAGE = "<html><body><div id='root'></div></body></html>"


class AnnotateTest(unittest.TestCase):
    def test_inserts_script_before_body_end(self):
        out = annotate.annotate_html(PAGE)
        self.assertTrue(annotate.is_annotated(out))
        self.assertLess(out.index(annotate.MARKER), out.rindex("</body>"))
        self.assertIn("<div id='root'></div>", out)  # 原本內容不變

    def test_idempotent(self):
        once = annotate.annotate_html(PAGE)
        self.assertEqual(annotate.annotate_html(once), once)

    def test_task_banner_included_and_escaped(self):
        out = annotate.annotate_html(PAGE, annotate.task_banner("task-<1>", 3))
        self.assertIn("1/3", out)
        self.assertIn("task-&lt;1&gt;", out)

    def test_rejects_non_html(self):
        with self.assertRaises(ValueError):
            annotate.annotate_html("not html")


if __name__ == "__main__":
    unittest.main()
