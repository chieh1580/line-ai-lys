# LYS 美妍SPA館 — LINE AI 客服機器人

## 客戶資訊
- 品牌：LYS美妍｜美容撥經｜撥經教學｜溫和式舒壓護理
- 產業：美容撥經 SPA
- 地址：桃園市中壢區志航街217號
- 電話：0916-660-072
- 營業時間：10:00 - 20:00
- LINE Channel Secret: 0a65126c402447e81f76be27c1435818

## 架構
- 單檔 Flask 應用（app.py），部署在 Railway
- AI 模型：claude-sonnet-4-20250514
- 設定儲存：JSON 檔案掛載在 Railway Volume（/data/settings.json）
- SYSTEM_PROMPT 和 TRIGGER_WORDS 可在後台「設定」頁面即時修改

## 環境變數（在 Railway 設定）
- CLAUDE_API_KEY — Anthropic API Key
- LINE_TOKEN — LINE Channel Access Token（尚未取得）
- ADMIN_PASSWORD — 後台登入密碼
- BOSS_USER_ID — 老闆的 LINE User ID（用於推播通知）
- ADMIN_URL — 後台網址

## 注意事項
- 所有回覆使用繁體中文
- 禁用醫療相關用詞（治療、療效、醫療、診斷等）
- 修改程式碼後 push 到 GitHub 即自動部署
