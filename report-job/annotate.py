"""在 k6 Web Dashboard 匯出的 HTML 報告上加入中文教學說明（黃色說明框）。

報表 Job 會對每個 task 的 dashboard 自動產生教學版；本機也可以用 scripts/annotate-dashboard.py 單獨執行。
做法：在 </body> 前插入一段 CSS 與 JavaScript，頁面載入後依各圖表／表格的標題文字插入說明。
k6 的資料與圖表本身不做任何修改；右上角按鈕可以隱藏或顯示說明。
"""

from __future__ import annotations

import html as _html
import json

MARKER = "k6-teach-script"

# 依 k6 報告上的標題文字（h2／h3／h4）對應說明
HEADING_NOTES = {
    "Overview": "整輪壓測最重要指標的時間變化圖。橫軸是時間，所以可以看出每個加壓階段的變化。"
                "<br><b>注意：</b>這份 dashboard 的所有圖表都是「全部請求合在一起」的數字，"
                "<b>沒有分 FAQ 問答／業務查詢</b>；分開的數字與各階段判定請看壓測報表（report-job 產出的報告）。",
    "HTTP Performance overview": "三條線放在同一張圖：<br>"
                                 "• <b>Request Rate</b>：每秒送出的請求數（規劃的「每秒處理量（送出量）」）<br>"
                                 "• <b>Request Duration p(95)</b>：95% 的請求在這個時間內完成<br>"
                                 "• <b>Request Failed</b>：非預期回應的比例<br>"
                                 "看法：請求量往上加時，p95 也跟著明顯上升，代表系統開始吃力；Failed 離開 0，代表開始出錯。",
    "VUs": "<b>VU（虛擬使用者）</b>＝同時在對話中的使用者數（包含思考時間）。本腳本一個 VU 會跑完一整段多輪對話。"
           "<br>VU 應該隨各加壓階段階梯式上升。如果 VU 停在上限不再增加，且 Summary 的 <code>dropped_iterations</code> 大於 0，"
           "代表<b>壓測機不夠力</b>（送不出設定的負載），不是受測系統的問題。",
    "Transfer Rate": "每秒收／送的網路流量。壓測報告通常不需要；可以用來確認壓測機的網路不是瓶頸。",
    "HTTP Request Duration": "回應時間的 <b>avg（平均）／p90／p95／p99</b> 隨時間的變化。"
                             "<br>• p95 = 95% 的請求在這個時間內完成（最慢的 5% 不算），p99 同理"
                             "<br>• 規劃要看的 p50 不在圖上，在下方 Summary 的 Trends 表（<code>med</code> 欄）"
                             "<br>• 參考門檻：FAQ 問答 p95 ≤ 10 秒、業務查詢 p95 ≤ 5 秒，但這張圖把兩類混在一起，不能直接拿來判定"
                             "<br>看法：p50 穩定但 p99 上升 → 少數請求開始卡住；全部一起上升 → 整體變慢。",
    "Iteration Duration": "一次 iteration ＝ 一位使用者的<b>一整段對話</b>（開場＋8～11 則訊息＋每則約 20 秒思考時間），"
                          "所以數值是「一段對話多久」（數分鐘），<b>不是回應時間</b>，不要拿來判定。",
    "Timings": "HTTP 請求各階段花費的時間。主要用來排查「時間花在哪裡」，例如是伺服器處理慢，還是連線、網路慢。",
    "Request Duration": "一個請求從送出到收完回應的總時間（＝ Sending ＋ Waiting ＋ Receiving），與 Overview 的 HTTP Request Duration 相同。",
    "Request Failed Rate": "非預期回應的比例（本腳本只把 200、205 視為預期）。"
                           "<br><b>注意：</b>這個比例<b>包含業務錯誤</b>（例如 4xx），"
                           "跟規劃定義的「錯誤率（5xx＋timeout）」不同；規劃的錯誤率請看 Summary 的 <code>errors_5xx_timeout</code>。",
    "Request Rate": "每秒送出的請求數，就是規劃的「每秒處理量（送出量）」。成功量請看 Summary 的 <code>chat_success</code>。",
    "Request Waiting": "送出後等待伺服器開始回應的時間（TTFB），也就是<b>伺服器處理時間</b>。"
                       "AI 回答慢主要反映在這裡；如果 Duration 幾乎等於 Waiting，代表時間都花在伺服器端，不是網路。",
    "TLS handshaking": "建立 HTTPS 加密連線的時間。mock 環境走 http，所以是 0；正式環境如果偏高，可能是連線沒有重複使用或負載平衡器負擔大。",
    "Request Sending": "把請求內容送出去的時間，通常很小；變大代表上傳頻寬有問題。",
    "Request Connecting": "建立 TCP 連線的時間。偏高可能代表連線建立有瓶頸，例如 Cloud NAT 的 port 不夠。",
    "Request Receiving": "下載回應內容的時間，通常很小；變大代表回應很大或下載頻寬有問題。",
    "Request Blocked": "k6 等待可用連線的時間（在壓測機端排隊）。偏高可能是壓測機端的瓶頸。",
    "Summary": "<b>整輪</b>的彙總數字（從頭到尾全部加在一起）。整輪混合了 Smoke 到 3 倍的所有階段，"
               "<b>不能代表某一個階段的容量</b>；各階段的數字請看壓測報表。黃色標示的列是對應規劃的重點指標。",
}

# Summary 表格中要標示的指標列
ROW_NOTES = {
    "http_req_duration": "回應時間（med＝p50）",
    "iteration_duration": "一段對話的時長，不是回應時間",
    "http_reqs": "送出量",
    "chat_success": "成功量",
    "conversations_completed": "完成的對話數",
    "dropped_iterations": "> 0 代表壓測機不足",
    "business_errors": "業務錯誤（另列）",
    "http_429": "> 0 依規劃立即停止",
    "errors_5xx_timeout": "規劃定義的錯誤率",
    "http_req_failed": "含業務錯誤，不等於規劃的錯誤率",
    "checks": "回應檢查通過率",
    "vus_max": "最多建立的 VU 數",
}

TABLE_NOTES = {
    "Trends": "每個指標的分布：avg 平均、med 中位數（＝p50）、p90／p95／p99、min／max。時間單位會自動換算（µs、ms、s）。",
    "Counters": "累計次數（count）與整輪平均每秒（rate）。",
    "Rates": "比例型指標，例如錯誤率、檢查通過率。",
    "Gauges": "結束當下的值（vus）與最大值（vus_max）。",
}

BANNER = """
<b>教學版說明</b>：黃色框是加上去的說明，k6 的圖表與數字沒有修改。右上角按鈕可隱藏說明。
<br>對照「壓力測試規劃 v1.4」：這份 k6 dashboard 適合看<b>整體趨勢</b>（請求量、回應時間、錯誤、VU）；
<b>FAQ／業務查詢分開統計、各階段判定、多台壓測機合併</b>不在這份 dashboard 裡，請看壓測報表（report-job）。
<br>在 Cloud Run 上分多個 task 執行時，每個 task 的 dashboard 只代表 1/N 的負載。
"""

STYLE = """
<style id="k6-teach-style">
.k6-teach{background:#fffbeb;border:1px solid #f59e0b;border-left:6px solid #f59e0b;border-radius:6px;
  padding:10px 12px;margin:8px 0 12px;font:14px/1.7 -apple-system,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;color:#422006}
.k6-teach code{background:#fde68a;padding:0 4px;border-radius:3px}
.k6-teach-banner{position:relative;margin:12px 0 16px;font-size:15px}
.k6-teach-row td,.k6-teach-row th{background:#fef3c7 !important}
.k6-teach-badge{display:inline-block;margin-left:8px;padding:0 6px;border-radius:4px;background:#f59e0b;color:#fff;font-size:12px;white-space:nowrap}
#k6-teach-toggle{position:fixed;top:12px;right:12px;z-index:9999;background:#f59e0b;color:#fff;border:0;border-radius:6px;
  padding:8px 12px;font-size:14px;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,.2)}
body.k6-teach-hidden .k6-teach,body.k6-teach-hidden .k6-teach-badge{display:none}
body.k6-teach-hidden .k6-teach-row td,body.k6-teach-hidden .k6-teach-row th{background:inherit !important}
@media print{#k6-teach-toggle{display:none}}
</style>
"""

SCRIPT = """
<script id="k6-teach-script">
(function () {
  var HEADINGS = %HEADINGS%, ROWS = %ROWS%, TABLES = %TABLES%, BANNER = %BANNER%;
  function note(html, cls) { var d = document.createElement('div'); d.className = 'k6-teach' + (cls ? ' ' + cls : ''); d.innerHTML = html; return d; }
  function apply() {
    var root = document.getElementById('root');
    if (!root || !root.querySelector('h2')) return false;
    if (!document.querySelector('.k6-teach-banner')) {
      var h1 = root.querySelector('h1');
      if (h1) h1.parentElement.insertAdjacentElement('afterend', note(BANNER, 'k6-teach-banner'));
    }
    root.querySelectorAll('h2,h3,h4').forEach(function (h) {
      var key = h.textContent.trim();
      if (HEADINGS[key] && !h.dataset.teach) { h.dataset.teach = '1'; h.insertAdjacentElement('afterend', note(HEADINGS[key])); }
    });
    root.querySelectorAll('table').forEach(function (t) {
      if (t.classList.contains('u-legend') || t.dataset.teach) return;
      var first = t.querySelector('th');
      var key = first ? first.textContent.trim() : '';
      if (TABLES[key]) { t.dataset.teach = '1'; t.insertAdjacentElement('beforebegin', note(TABLES[key])); }
      t.querySelectorAll('tr').forEach(function (tr) {
        var cell = tr.querySelector('td,th');
        var name = cell ? cell.textContent.trim() : '';
        if (ROWS[name] && !tr.dataset.teach) {
          tr.dataset.teach = '1'; tr.classList.add('k6-teach-row');
          var b = document.createElement('span'); b.className = 'k6-teach-badge'; b.textContent = ROWS[name]; cell.appendChild(b);
        }
      });
    });
    return true;
  }
  var btn = document.createElement('button'); btn.id = 'k6-teach-toggle'; btn.textContent = '隱藏說明';
  btn.onclick = function () { var hidden = document.body.classList.toggle('k6-teach-hidden'); btn.textContent = hidden ? '顯示說明' : '隱藏說明'; };
  document.addEventListener('DOMContentLoaded', function () { document.body.appendChild(btn); });
  // k6 報告是前端框架動態產生畫面：等畫面出現後套用，之後畫面重繪時再補上
  var tries = 0, timer = setInterval(function () { if (apply() || ++tries > 100) clearInterval(timer); }, 100);
  new MutationObserver(function () { apply(); }).observe(document.documentElement, { childList: true, subtree: true });
})();
</script>
"""



def is_annotated(page: str) -> bool:
    return MARKER in page


def annotate_html(page: str, extra_banner: str | None = None) -> str:
    """回傳加上說明的 HTML。extra_banner 會接在頁首說明後面（例如標示這是哪一個 task）。"""
    if is_annotated(page):
        return page
    idx = page.rfind("</body>")
    if idx < 0:
        raise ValueError("看起來不是 k6 匯出的 HTML 報告（找不到 </body>）")
    banner = BANNER.strip()
    if extra_banner:
        banner += "<br>" + extra_banner
    script = (SCRIPT.replace("%HEADINGS%", json.dumps(HEADING_NOTES, ensure_ascii=False))
              .replace("%ROWS%", json.dumps(ROW_NOTES, ensure_ascii=False))
              .replace("%TABLES%", json.dumps(TABLE_NOTES, ensure_ascii=False))
              .replace("%BANNER%", json.dumps(banner, ensure_ascii=False)))
    return page[:idx] + STYLE + script + page[idx:]


def task_banner(task: str, task_count: int | None) -> str:
    share = f"1/{task_count}" if task_count else "部分"
    return (f"<b>這份 dashboard 只代表壓測機 {_html.escape(task)}（約 {share} 的流量）</b>，"
            "數字不能直接當全體結果；全體數字請看合併報告。")
