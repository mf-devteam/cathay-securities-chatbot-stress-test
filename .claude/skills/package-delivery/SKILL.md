---
name: package-delivery
description: 打包地端離線安裝包並準備交付給證券方。用於「打包」「出新版安裝包」「交付給客戶」「build bundle」。
---

# 打包地端離線安裝包

目標：從**乾淨的 commit** 產出 `dist/k6-stress-onprem-<commit>-el9.6-offline.tar.gz`＋`.sha256`，並整理交付資料夾。

## 步驟

1. **確認要打包的 commit**
   - `git status` 必須是乾淨的；有未 commit 的修改就先停下來問使用者。
   - 單元測試：`cd report-job && python3 -m unittest discover -s tests`，全過才繼續。
   - 記下 `git rev-parse --short HEAD`。

2. **從乾淨的 worktree 打包**（工作區的修改會被包進去，所以一定要另開 worktree；colima 只掛載 home，worktree 要開在家目錄下）
   ```bash
   SHA=$(git rev-parse --short HEAD)
   git worktree add ~/k6-bundle-$SHA $SHA
   cd ~/k6-bundle-$SHA && onprem/bundle/build-in-docker.sh el9.6
   cp dist/k6-stress-onprem-$SHA-el9.6-offline.tar.gz* <repo>/dist/
   cd - && git worktree remove ~/k6-bundle-$SHA
   ```
   需要 Docker（colima），約 2 分鐘。

3. **驗證**（至少做一項，結果要告訴使用者）
   - 有改 install.sh 或套件清單時：在 GCE 真 RHEL 9.6 映像（`rhel-9-v20250611`）**斷網**離線安裝。Rocky 容器測不出套件衝突。
   - 只改 k6 腳本或報表時：lab control 照手冊的「更新步驟」安裝後，跑一次 k6menu 2（試跑），流程與報表都正常就可以。
   - 打 lab 會用到 VM，先跟使用者確認 lab VM 有開。

4. **交付資料夾** `~/Downloads/新阿發壓測工具_交付_<YYYYMMDD>/`
   - 安裝包、`.sha256`、操作手冊（docx）、需要時附報表範例。
   - 手冊要更新版號、日期、安裝包檔名；有新功能就補說明和常見問題。
   - 寫給客戶的指令要能**直接複製貼上**，不要有佔位符。

5. **回報給使用者**：commit、安裝包檔名、sha256、驗證做了什麼，以及哪些沒驗證。

## 不要做

- 不要從有修改的工作區直接打包。
- 不要自己 push 或寄給客戶，交給使用者處理。
