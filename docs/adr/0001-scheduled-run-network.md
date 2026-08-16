# ADR 0001：本機排程的網路與發布恢復策略

- 狀態：Accepted
- 日期：2026-08-04

## 背景

信用卡活動更新由本機 ChatGPT／Codex desktop 的 cron automation 執行，不是由 GitHub Actions 抓取銀行資料。GitHub Actions 只在資料提交到 `main` 後驗證並部署 GitHub Pages。

本機 scheduled task 以 `workspace-write` sandbox 啟動；依 Codex scheduled-task 權限模型，這個模式預設禁止網路。2026-08-01、2026-08-03 與 2026-08-04 的一般更新因此在排程 sandbox 內出現系統性 DNS 失敗。互動式受控外網測試可解析相同官方網域，證明問題位於排程執行環境，而非銀行封鎖或抓取器。

此外，月排程與週排程原本先以 `generated_at` 做 30 分鐘重疊判斷，並在本輪 publish guard 被攔時禁止任何提交。兩條規則交互作用，使前一輪已通過 guard、但尚未提交的公開資料產物被當成不可處理的工作區變更，可能無限期擱置。

## 決策

1. 排程維持在本機 Codex cron 與 `workspace-write` sandbox，不改成 GitHub Actions，也不啟用 full access。
2. 網路更新只使用 `~/.codex/rules/default.rules` 中 `env PYTHONPATH=src python3 scripts/update_data.py` 的最小 allowlist，在 sandbox 外執行；不再先於無網路 sandbox 內執行一次必然失敗的更新。
3. 若受控外網更新仍回報系統性 DNS 失敗，只允許一次完全相同的受控外網重試；仍失敗則停止，不提交候選資料。
4. 在 30 分鐘重疊閘門之前，先檢查未提交的分層資料產物。快照是不可拆分的相容集合，包含 `docs/data/promotions.json`、`docs/data/banks/*.json`、`docs/data/activities/*.json`、`docs/calendars/registration.ics` 與 `data/activity_cache.json`；不得只恢復或提交其中一個檔案。只有同時符合下列條件，才視為「待補提交的已通過快照」：
   - `publish_guard.status == "passed"`
   - `blocked == false`
   - `reason_codes` 為空
   - `dns_failures == 0`
   - failed source 不超過 2
   - `generated_at` 新於 `origin/main` 的公開資料
5. 待補快照先完成 Python、前端與 `scripts/validate_public_artifacts.py` 驗證，再將整組相容產物建立獨立本機 commit。若後續新更新被 guard 攔截，只禁止提交該次被攔候選；先前已驗證的恢復 commit 仍可推送，避免再次擱置。
6. 本 ADR 的排程網路修復不藉由改動 `assess_publish_guard`、`DNS_FAILURE_MARKERS`、`PUBLISH_GUARD_EXIT_CODE`、抓取器、解析器、官方網域白名單或前端功能達成。後續若另案修正單筆活動容錯，仍須維持相同安全邊界與發布門檻。

## 已知失效模式

- 無網路 sandbox：多數官方 hostname 回報 `nodename nor servname provided`，publish guard 正確產生 `systemic_dns_failure`。
- 受控外網仍暫時無 DNS：publish guard 會攔截，排程只允許一次相同外網重試，之後停止並告警。
- 單一 HTTPS timeout：完整更新可能以非 guard 退出碼中止；只允許一次相同的受控外網重試，不放寬 guard。
- 本機分支落後 `origin/main` 或不是 `main`：不得在舊 feature branch 上重寫歷史；先保全已通過快照，再以最新 `origin/main` 的隔離 worktree 修復並發布。

## 驗證與發布條件

資料只能在以下條件全部成立後提交：更新退出碼為 0 或 2、`reports/latest.json` 的 guard passed、failed source 不超過 2、DNS 失敗為 0、Python tests 通過、前端 JavaScript 語法與測試通過、`scripts/validate_public_artifacts.py` 與 diff 檢查通過。排程只可提交上述分層資料產物，不得夾帶程式碼或文件變更。推送後還要確認 `Refresh and deploy GitHub Pages` 成功，並分開回報 Git 合併、Pages 部署、公開 JSON 與固定 ICS feed 的線上驗證狀態。

## 影響

此決策消除排程先做一次必然失敗的無網路抓取，並讓「本輪被 guard 攔截」只影響本輪候選，不再連帶凍結先前已通過的快照。代價是 Codex 新載入 rules 後才能使用新增 allowlist；若本機 resolver 本身故障，排程仍會由既有 guard 安全停止並告警，而不會繞過發布防護。
