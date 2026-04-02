# ==========================================
# LYS 美妍SPA館 — LINE AI 客服機器人
# 產業：美容撥經 / 撥經教學 / 溫和式舒壓護理
# ==========================================

from flask import Flask, request, jsonify, render_template_string, make_response, redirect
import anthropic
import requests
import os
import json
from datetime import datetime
import sys
import threading


app = Flask(__name__)
app.logger.setLevel("INFO")
app.logger.addHandler(logging_handler := __import__('logging').StreamHandler(sys.stdout))
logging_handler.setLevel("INFO")

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
LINE_TOKEN = os.environ.get("LINE_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "lys2024")
BOSS_USER_ID = os.environ.get("BOSS_USER_ID", "")
ADMIN_URL = os.environ.get("ADMIN_URL", "")

paused_users = set()
user_profiles = {}
app_logs = []
user_state = {}           # userId -> {"flow": "collecting_booking", "step": "name"}
user_booking_data = {}    # userId -> {"name": ..., "phone": ..., "time": ...}
user_quiz_data = {}       # userId -> {"q1": ..., "q2": ..., "q3": ...}
user_message_count = {}   # userId -> int
testimonial_sent = set()
welcome_sent = set()

TRIGGER_WORDS = ["找真人", "找人工", "找客服", "找老師", "真人", "人工"]
BOOKING_KEYWORDS = ["我要預約", "我想預約", "預約撥經", "我要預約撥經", "我想體驗", "預約體驗"]
QUIZ_KEYWORDS = ["開始測驗", "開始我的專屬測驗", "專屬測驗"]

# 老師輪流分配清單
TEACHER_ROTATION = ["好羚老師", "微雅老師", "懿珊老師", "家媛老師", "33老師", "Charlotte老師"]

SYSTEM_PROMPT = """你是「小美」，LYS美妍SPA館的專業AI客服。語氣親切溫柔、像好姊妹聊天，讓客人感覺被重視、被照顧。

【店家資訊】
店名：LYS美妍｜美容撥經｜撥經教學｜溫和式舒壓護理
地址：桃園市中壢區志航街217號
電話：0916-660-072
營業時間：每天 10:00 - 20:00
預約方式：透過LINE官方帳號選單到Ezpretty進行預約

【師資團隊】
好羚老師 — 專業撥經師
微雅老師 — 專業撥經師
懿珊老師 — 專業撥經師（預約須加指定費）
家媛 — 專業撥經師
33 — 專業撥經師
Charlotte — 專業撥經師

【單項價格】
新客順氣鬆經課（僅限第一次）60分鐘 1,280元起
臉部撥經 90分鐘 3,200元起
背部撥經 90分鐘 2,300元起
腿部撥經 90分鐘 2,300元起
胸部撥經 45分鐘 1,500元起
腹部撥經 45分鐘 1,500元起
胯部八髎撥經 30分鐘 1,000元起
無煙艾灸 60分鐘 1,100元起
循環美容議 30分鐘 500元起

【療癒撥經套餐】
A套餐：背／腿撥經 120分鐘 3,200元起
B套餐：胸／腹撥經 90分鐘 2,900元起
C套餐：背／腹撥經 135分鐘 3,600元起
D套餐：背／胸撥經 135分鐘 3,600元起

【推薦首次體驗】
新客推薦「新客順氣鬆經課」，60分鐘只要1,280元起，是認識撥經最好的入門課程！

【禁用詞】絕對不可說：治療、療效、醫療、診斷、治癒、改善疾病、消除、根治
改說：舒緩、放鬆、調理、養護、舒壓、讓身體更有活力、促進循環

【回覆原則】
1. 語氣親切溫柔，像好姊妹聊天
2. 回覆簡潔，一次不超過3-4行
3. 每次回覆結尾自然引導預約體驗
4. 不確定的事情說「讓我幫您問一下老師，請稍等哦～」然後觸發轉人工
5. 新客推薦「新客順氣鬆經課」作為入門體驗
6. 介紹服務時可簡單說明撥經的好處（舒緩肌肉緊繃、促進循環、放鬆身心）"""

SETTINGS_FILE = "/data/settings.json"
WELCOME_SENT_FILE = "/data/welcome_sent.json"


def _load_welcome_sent():
    try:
        with open(WELCOME_SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_welcome_sent():
    try:
        os.makedirs(os.path.dirname(WELCOME_SENT_FILE), exist_ok=True)
        with open(WELCOME_SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(list(welcome_sent), f)
    except Exception as e:
        print(f"[WELCOME] Save error: {e}", flush=True)


def _load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[SETTINGS] Save error: {e}", flush=True)
        return False


def get_setting(key, default=None):
    data = _load_settings()
    return data.get(key, default)


def set_setting(key, value):
    data = _load_settings()
    data[key] = value
    return _save_settings(data)

# 啟動時載入已發送歡迎卡片的用戶清單
welcome_sent = _load_welcome_sent()


def _get_next_teacher():
    """輪流分配老師，計數器持久化"""
    idx = int(get_setting("teacher_rotation_index", 0))
    teacher = TEACHER_ROTATION[idx % len(TEACHER_ROTATION)]
    set_setting("teacher_rotation_index", (idx + 1) % len(TEACHER_ROTATION))
    return teacher


# ===== 後台 HTML =====
ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ brand_name }} 後台</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#f7f5f2;color:#2d1f14}
.topbar{background:#f0ebe3;border-bottom:0.5px solid #e0d8ce;padding:16px 22px;display:flex;align-items:center;justify-content:space-between}
.topbar-brand{display:flex;align-items:center;gap:12px}
.topbar-logo{width:32px;height:32px;background:#8b5e83;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#fff}
.topbar-name{font-size:15px;font-weight:600;color:#2d1f14}
.topbar-sub{font-size:11px;color:#b0a090;margin-top:2px}
.online{display:flex;align-items:center;gap:6px}
.pulse{width:7px;height:7px;border-radius:50%;background:#6abf69}
.online span{font-size:12px;color:#b0a090}
.tabs{display:flex;background:#f0ebe3;border-bottom:0.5px solid #e0d8ce;padding:0 20px}
.tab{padding:10px 18px;font-size:13px;font-weight:500;color:#b0a090;text-decoration:none;border-bottom:2px solid transparent}
.tab.active{color:#8b5e83;border-bottom:2px solid #8b5e83;font-weight:600}
.tab:hover{color:#2d1f14}
.stats{display:flex;gap:10px;padding:18px 20px 8px}
.stat{background:#fff;border-radius:10px;padding:14px 16px;flex:1;border:0.5px solid #e8e2d8}
.stat-n{font-size:26px;font-weight:600;color:#2d1f14}
.stat-n.orange{color:#8b5e83}
.stat-n.green{color:#3b6d11}
.stat-l{font-size:11px;color:#b0a090;margin-top:2px}
.notify{margin:8px 20px 4px;background:#faf5f9;border:0.5px solid #d8b8d0;border-radius:8px;padding:11px 14px;display:flex;align-items:center;gap:10px}
.notify-dot{width:7px;height:7px;border-radius:50%;background:#8b5e83;flex-shrink:0}
.notify-txt{font-size:12px;color:#6b3a63}
.main{padding:14px 20px 24px}
.sec-label{font-size:11px;font-weight:600;color:#c8b8a8;letter-spacing:2px;margin-bottom:10px;margin-top:4px}
.card{background:#fff;border-radius:10px;padding:13px 15px;margin-bottom:8px;border:0.5px solid #e8e2d8;display:flex;align-items:center;gap:12px}
.card.paused{border-left:3px solid #8b5e83;border-radius:0 10px 10px 0;background:#faf5f9}
.card.active{border-left:3px solid #6abf69;border-radius:0 10px 10px 0}
.ava{width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:600;flex-shrink:0;background:#f0ebe3;color:#6b3a63;overflow:hidden}
.ava img{width:100%;height:100%;object-fit:cover}
.uinfo{flex:1;min-width:0}
.uname{font-size:13px;font-weight:600;color:#2d1f14}
.umsg{font-size:12px;color:#b0a090;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px}
.utime{font-size:11px;color:#ccc;margin-top:2px}
.badge{font-size:11px;padding:3px 9px;border-radius:10px;font-weight:500;flex-shrink:0}
.badge-ai{background:#d4e8d0;color:#27500a}
.badge-human{background:#f0d5e8;color:#6b2858}
.btn{border:0.5px solid;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:500;cursor:pointer;flex-shrink:0;transition:0.15s}
.btn-stop{background:#faf0f5;color:#6b2858;border-color:#d8b0c8}
.btn-stop:hover{background:#f0d5e8}
.btn-go{background:#d4e8d0;color:#27500a;border-color:#b0d0a8}
.btn-go:hover{background:#c0ddb8}
.divider{height:0.5px;background:#e8e2d8;margin:14px 0}
.empty{text-align:center;padding:40px;color:#c8b8a8;font-size:14px}
.login-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh;background:#f7f5f2}
.login-box{background:#fff;border-radius:12px;padding:32px;width:300px;border:0.5px solid #e8e2d8;text-align:center}
.login-logo{width:48px;height:48px;background:#8b5e83;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700;color:#fff;margin:0 auto 16px}
.login-box h2{font-size:16px;font-weight:600;margin-bottom:20px;color:#2d1f14}
.login-box input{width:100%;padding:10px 14px;border:0.5px solid #e0d8ce;border-radius:6px;font-size:14px;margin-bottom:12px;text-align:center;background:#f7f5f2}
.login-box button{width:100%;padding:10px;background:#8b5e83;color:#fff;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer}
.err{color:#8b5e83;font-size:12px;margin-top:8px}
.toast{position:fixed;bottom:20px;right:20px;background:#2d1f14;color:#f5ede0;padding:10px 18px;border-radius:6px;font-size:13px;display:none;z-index:999}
</style>
</head>
<body>
{% if not authenticated %}
<div class="login-wrap">
  <div class="login-box">
    <div class="login-logo">LYS</div>
    <h2>後台管理登入</h2>
    <form method="POST" action="/admin/login">
      <input type="password" name="password" placeholder="請輸入密碼" required>
      <button type="submit">登入</button>
    </form>
    {% if error %}<p class="err">密碼錯誤，請再試一次</p>{% endif %}
  </div>
</div>
{% else %}
<div class="topbar">
  <div class="topbar-brand">
    <div class="topbar-logo">LYS</div>
    <div>
      <div class="topbar-name">{{ brand_name }} 後台</div>
      <div class="topbar-sub">LINE AI 客服管理系統</div>
    </div>
  </div>
  <div class="online">
    <div class="pulse"></div>
    <span>系統運作中</span>
  </div>
</div>
<div class="tabs">
  <a href="/admin" class="tab active">對話管理</a>
  <a href="/admin/settings" class="tab">設定</a>
</div>

<div class="stats">
  <div class="stat"><div class="stat-n">{{ total }}</div><div class="stat-l">今日對話</div></div>
  <div class="stat"><div class="stat-n green">{{ active }}</div><div class="stat-l">AI 回覆中</div></div>
  <div class="stat"><div class="stat-n orange">{{ paused_count }}</div><div class="stat-l">待人工處理</div></div>
  <div class="stat"><div class="stat-n">{{ ai_rate }}<span style="font-size:13px;color:#bbb;">%</span></div><div class="stat-l">AI 回覆率</div></div>
</div>

{% if pending_users %}
<div class="notify">
  <div class="notify-dot"></div>
  <div class="notify-txt">{{ pending_users[0].name }} 需要您回覆，共 {{ paused_count }} 位客人等待中</div>
</div>
{% endif %}

<div class="main">
  {% if paused_users_list %}
  <div class="sec-label">待處理</div>
  {% for u in paused_users_list %}
  <div class="card paused">
    <div class="ava">
      {% if u.picture %}<img src="{{ u.picture }}" onerror="this.style.display='none'">{% else %}{{ u.name[0] }}{% endif %}
    </div>
    <div class="uinfo">
      <div class="uname">{{ u.name }}</div>
      <div class="umsg">{{ u.lastMessage }}</div>
      <div class="utime">{{ u.lastTime }}</div>
    </div>
    <span class="badge badge-human">人工中</span>
    <button class="btn btn-go" onclick="toggle('{{ u.id }}','resume')">恢復 AI</button>
  </div>
  {% endfor %}
  <div class="divider"></div>
  {% endif %}

  {% if active_users %}
  <div class="sec-label">AI 回覆中</div>
  {% for u in active_users %}
  <div class="card active">
    <div class="ava">
      {% if u.picture %}<img src="{{ u.picture }}" onerror="this.style.display='none'">{% else %}{{ u.name[0] }}{% endif %}
    </div>
    <div class="uinfo">
      <div class="uname">{{ u.name }}</div>
      <div class="umsg">{{ u.lastMessage }}</div>
      <div class="utime">{{ u.lastTime }}</div>
    </div>
    <span class="badge badge-ai">AI 中</span>
    <button class="btn btn-stop" onclick="toggle('{{ u.id }}','pause')">暫停 AI</button>
  </div>
  {% endfor %}
  {% endif %}

  {% if not paused_users_list and not active_users %}
  <div class="empty">還沒有客人傳訊息進來</div>
  {% endif %}
</div>

<div class="toast" id="toast"></div>
<script>
function toggle(uid, action) {
  fetch('/admin/toggle', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({userId: uid, action: action})
  }).then(r => r.json()).then(() => {
    const t = document.getElementById('toast')
    t.textContent = action === 'pause' ? '已暫停 AI，換您回覆' : '已恢復 AI 自動回覆'
    t.style.display = 'block'
    setTimeout(() => { t.style.display = 'none'; location.reload() }, 1000)
  })
}
setTimeout(() => location.reload(), 30000)
</script>
{% endif %}
</body>
</html>"""

SETTINGS_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ brand_name }} 設定</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#f7f5f2;color:#2d1f14}
.topbar{background:#f0ebe3;border-bottom:0.5px solid #e0d8ce;padding:16px 22px;display:flex;align-items:center;justify-content:space-between}
.topbar-brand{display:flex;align-items:center;gap:12px}
.topbar-logo{width:32px;height:32px;background:#8b5e83;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#fff}
.topbar-name{font-size:15px;font-weight:600;color:#2d1f14}
.topbar-sub{font-size:11px;color:#b0a090;margin-top:2px}
.tabs{display:flex;background:#f0ebe3;border-bottom:0.5px solid #e0d8ce;padding:0 20px}
.tab{padding:10px 18px;font-size:13px;font-weight:500;color:#b0a090;text-decoration:none;border-bottom:2px solid transparent}
.tab.active{color:#8b5e83;border-bottom:2px solid #8b5e83;font-weight:600}
.tab:hover{color:#2d1f14}
.main{padding:18px 20px 24px;max-width:700px}
.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:14px;border:0.5px solid #e8e2d8}
.card-title{font-size:13px;font-weight:600;color:#2d1f14;margin-bottom:8px}
.card-desc{font-size:11px;color:#b0a090;margin-bottom:10px}
textarea{width:100%;border:0.5px solid #e0d8ce;border-radius:6px;padding:10px 12px;font-size:13px;font-family:-apple-system,sans-serif;background:#f7f5f2;color:#2d1f14;resize:vertical;line-height:1.6}
textarea:focus{outline:none;border-color:#8b5e83}
.btn-row{display:flex;gap:10px;margin-top:16px}
.btn-save{background:#8b5e83;color:#fff;border:none;border-radius:6px;padding:10px 24px;font-size:13px;font-weight:600;cursor:pointer}
.btn-save:hover{background:#6b3a63}
.btn-reset{background:#fff;color:#6b3a63;border:0.5px solid #d8b0c8;border-radius:6px;padding:10px 24px;font-size:13px;font-weight:500;cursor:pointer}
.btn-reset:hover{background:#faf5f9}
.toast{position:fixed;bottom:20px;right:20px;background:#2d1f14;color:#f5ede0;padding:10px 18px;border-radius:6px;font-size:13px;display:none;z-index:999}
</style>
</head>
<body>
<div class="topbar">
  <div class="topbar-brand">
    <div class="topbar-logo">LYS</div>
    <div>
      <div class="topbar-name">{{ brand_name }} 後台</div>
      <div class="topbar-sub">LINE AI 客服管理系統</div>
    </div>
  </div>
</div>
<div class="tabs">
  <a href="/admin" class="tab">對話管理</a>
  <a href="/admin/settings" class="tab active">設定</a>
</div>
<div class="main">
  <div class="card">
    <div class="card-title">機器人指令 (System Prompt)</div>
    <div class="card-desc">控制機器人的角色、口氣、回覆風格和所有資訊內容（電話、地址、價格等）</div>
    <textarea id="prompt" rows="16">{{ system_prompt }}</textarea>
  </div>
  <div class="card">
    <div class="card-title">轉人工關鍵字</div>
    <div class="card-desc">當客人訊息包含以下任一關鍵字時，自動暫停 AI 並通知您（一行一個）</div>
    <textarea id="triggers" rows="6">{{ trigger_words }}</textarea>
  </div>
  <div class="btn-row">
    <button class="btn-save" onclick="saveSettings()">儲存設定</button>
    <button class="btn-reset" onclick="resetSettings()">恢復預設</button>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2000);
}
function saveSettings() {
  const prompt = document.getElementById('prompt').value.trim();
  const triggers = document.getElementById('triggers').value.trim();
  if (!prompt) { showToast('Prompt 不能為空'); return; }
  fetch('/admin/settings/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({system_prompt: prompt, trigger_words: triggers})
  }).then(r => r.json()).then(d => {
    showToast(d.status === 'ok' ? '設定已儲存，立即生效！' : '儲存失敗：' + (d.error || '未知錯誤'));
  }).catch(() => showToast('儲存失敗，請重試'));
}
function resetSettings() {
  if (!confirm('確定要恢復為預設設定嗎？')) return;
  fetch('/admin/settings/reset', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'}
  }).then(r => r.json()).then(d => {
    if (d.status === 'ok') { showToast('已恢復預設'); setTimeout(() => location.reload(), 1000); }
    else showToast('操作失敗');
  }).catch(() => showToast('操作失敗，請重試'));
}
</script>
</body>
</html>"""


# ===== LINE API 通用函式 =====
def reply_messages(reply_token, messages):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"},
        json={"replyToken": reply_token, "messages": messages},
        timeout=10
    )


def push_messages(user_id, messages):
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"},
        json={"to": user_id, "messages": messages},
        timeout=10
    )
    log_msg = f"[PUSH] to={user_id[-6:]} status={r.status_code}"
    print(log_msg, flush=True)
    app_logs.append({"time": datetime.now().strftime("%m/%d %H:%M:%S"), "msg": log_msg})
    return r


def push_text(user_id, text):
    push_messages(user_id, [{"type": "text", "text": text}])


def push_flex(user_id, flex):
    push_messages(user_id, [flex])


# ===== Flex Message 建構 =====
def build_welcome_flex():
    """Follow 歡迎卡片 — 撥經SPA版"""
    return {
        "type": "flex",
        "altText": "歡迎來到 LYS 美妍SPA館！",
        "contents": {
            "type": "bubble",
            "size": "giga",
            "header": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "歡迎來到 LYS 美妍SPA館", "weight": "bold", "size": "lg", "color": "#6b3a63"},
                    {"type": "text", "text": "美容撥經｜撥經教學｜溫和式舒壓護理", "size": "md", "margin": "sm", "color": "#555555"}
                ],
                "paddingAll": "20px", "backgroundColor": "#faf5f9"
            },
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "嗨～我是小美，您的專屬客服\n有任何問題都可以問我哦！也可以直接點選下方按鈕快速了解", "wrap": True, "size": "sm", "color": "#666666"},
                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "您想了解什麼呢？", "weight": "bold", "size": "md", "margin": "lg", "color": "#2d1f14"},
                    {
                        "type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md",
                        "contents": [
                            {"type": "button", "action": {"type": "message", "label": "服務項目與價格", "text": "你們有什麼服務？價格怎麼算？"}, "style": "primary", "color": "#8b5e83", "height": "sm"},
                            {"type": "button", "action": {"type": "message", "label": "療癒套餐介紹", "text": "有什麼套餐可以選？"}, "style": "primary", "color": "#a87ca0", "height": "sm"}
                        ]
                    },
                    {
                        "type": "box", "layout": "horizontal", "spacing": "sm", "margin": "sm",
                        "contents": [
                            {"type": "button", "action": {"type": "message", "label": "新客體驗價", "text": "第一次去有什麼推薦的嗎？"}, "style": "primary", "color": "#c8a0c0", "height": "sm"},
                            {"type": "button", "action": {"type": "message", "label": "預約撥經", "text": "我想預約"}, "style": "primary", "color": "#d4766a", "height": "sm"}
                        ]
                    },
                    {"type": "separator", "margin": "lg"},
                    {"type": "button", "action": {"type": "message", "label": "🔮 開始我的專屬測驗", "text": "開始我的專屬測驗"}, "style": "primary", "color": "#d4766a", "margin": "lg", "height": "sm"},
                    {"type": "text", "text": "也可以直接打字問我任何問題哦！", "wrap": True, "size": "xs", "color": "#999999", "margin": "sm"}
                ],
                "paddingAll": "20px"
            }
        }
    }


def build_quiz_q1_flex():
    """測驗第 1 題"""
    return {
        "type": "flex", "altText": "專屬測驗 Q1",
        "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
            {"type": "text", "text": "專屬測驗 ①/③", "size": "xs", "color": "#8b5e83", "weight": "bold"},
            {"type": "text", "text": "最近身體上，最有感的是哪一種？", "weight": "bold", "size": "md", "color": "#2d1f14", "margin": "md", "wrap": True},
            {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": [
                {"type": "button", "action": {"type": "message", "label": "肩頸僵硬痠痛", "text": "肩頸僵硬痠痛"}, "style": "secondary", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "腰背緊繃不適", "text": "腰背緊繃不適"}, "style": "secondary", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "腿部沉重水腫", "text": "腿部沉重水腫"}, "style": "secondary", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "整個人疲憊無力", "text": "整個人疲憊無力"}, "style": "secondary", "height": "sm"}
            ]}
        ]}}
    }


def build_quiz_q2_flex():
    """測驗第 2 題"""
    return {
        "type": "flex", "altText": "專屬測驗 Q2",
        "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
            {"type": "text", "text": "專屬測驗 ②/③", "size": "xs", "color": "#8b5e83", "weight": "bold"},
            {"type": "text", "text": "最近的生活節奏呢？", "weight": "bold", "size": "md", "color": "#2d1f14", "margin": "md", "wrap": True},
            {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": [
                {"type": "button", "action": {"type": "message", "label": "忙碌高壓，幾乎沒休息", "text": "忙碌高壓，幾乎沒休息"}, "style": "secondary", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "還好，但睡眠品質不太好", "text": "還好，但睡眠品質不太好"}, "style": "secondary", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "作息正常，想保養放鬆", "text": "作息正常，想保養放鬆"}, "style": "secondary", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "長時間久坐／久站", "text": "長時間久坐／久站"}, "style": "secondary", "height": "sm"}
            ]}
        ]}}
    }


def build_quiz_q3_flex():
    """測驗第 3 題"""
    return {
        "type": "flex", "altText": "專屬測驗 Q3",
        "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
            {"type": "text", "text": "專屬測驗 ③/③", "size": "xs", "color": "#8b5e83", "weight": "bold"},
            {"type": "text", "text": "做完撥經後，最渴望感受到的是什麼？", "weight": "bold", "size": "md", "color": "#2d1f14", "margin": "md", "wrap": True},
            {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": [
                {"type": "button", "action": {"type": "message", "label": "全身輕鬆、不再緊繃", "text": "全身輕鬆、不再緊繃"}, "style": "secondary", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "睡一場好覺", "text": "睡一場好覺"}, "style": "secondary", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "氣色變好、更有精神", "text": "氣色變好、更有精神"}, "style": "secondary", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "好好寵愛自己一下", "text": "好好寵愛自己一下"}, "style": "secondary", "height": "sm"}
            ]}
        ]}}
    }


def build_quiz_result_flex(teacher, q1, q2, q3):
    """測驗結果卡片"""
    # 根據回答生成小結
    body_map = {"肩頸僵硬痠痛": "肩頸", "腰背緊繃不適": "腰背", "腿部沉重水腫": "腿部", "整個人疲憊無力": "全身"}
    body_part = body_map.get(q1, "身體")

    return {
        "type": "flex", "altText": "測驗結果出爐！",
        "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
            {"type": "text", "text": "✨ 分析完成！", "weight": "bold", "size": "lg", "color": "#6b3a63"},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": f"根據您的回答，您目前{body_part}比較需要照顧，加上生活節奏「{q2}」，很適合透過撥經來好好舒緩放鬆一下哦！", "wrap": True, "size": "sm", "color": "#666666", "margin": "md"},
            {"type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "15px", "backgroundColor": "#faf5f9", "cornerRadius": "10px", "contents": [
                {"type": "text", "text": f"為您推薦：{teacher}", "weight": "bold", "size": "md", "color": "#6b3a63"},
                {"type": "text", "text": f"新客首次體驗「背部鬆經」\n60 分鐘只要 1,280 元！", "wrap": True, "size": "sm", "color": "#555555", "margin": "sm"}
            ]},
            {"type": "button", "action": {"type": "message", "label": "立即預約體驗", "text": "我想預約"}, "style": "primary", "color": "#8b5e83", "margin": "lg", "height": "sm"},
            {"type": "button", "action": {"type": "message", "label": "想先了解更多", "text": "你們有什麼服務？價格怎麼算？"}, "style": "link", "color": "#8b5e83", "margin": "sm", "height": "sm"}
        ]}}
    }


def build_booking_start_flex():
    """預約撥經 — 第一步：詢問姓名"""
    return {
        "type": "flex",
        "altText": "太好了！幫您安排預約",
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "太好了！", "weight": "bold", "size": "lg", "color": "#6b3a63"},
                    {"type": "text", "text": "讓我幫您安排預約，只需要簡單 3 個資訊：", "wrap": True, "size": "sm", "color": "#666666", "margin": "md"},
                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "① 請問您怎麼稱呼？", "weight": "bold", "size": "md", "margin": "lg", "color": "#2d1f14"},
                    {"type": "text", "text": "直接打字回覆就好囉", "size": "xs", "color": "#999999", "margin": "sm"}
                ],
                "paddingAll": "20px"
            }
        }
    }


def build_booking_complete_flex(data):
    """預約撥經 — 完成確認卡片"""
    return {
        "type": "flex",
        "altText": "預約資料收到囉！",
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "預約資料收到囉！", "weight": "bold", "size": "lg", "color": "#6b3a63"},
                    {"type": "separator", "margin": "lg"},
                    {"type": "box", "layout": "horizontal", "margin": "lg", "contents": [
                        {"type": "text", "text": "姓名", "size": "sm", "color": "#999999", "flex": 2},
                        {"type": "text", "text": data.get("name", ""), "size": "sm", "weight": "bold", "flex": 4}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "電話", "size": "sm", "color": "#999999", "flex": 2},
                        {"type": "text", "text": data.get("phone", ""), "size": "sm", "weight": "bold", "flex": 4}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "想體驗", "size": "sm", "color": "#999999", "flex": 2},
                        {"type": "text", "text": data.get("service", ""), "size": "sm", "weight": "bold", "flex": 4}
                    ]},
                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "我們會盡快透過 LINE 與您確認預約時間，請留意訊息通知哦", "wrap": True, "size": "sm", "color": "#666666", "margin": "lg"}
                ],
                "paddingAll": "20px"
            }
        }
    }


def build_notify_boss_flex(customer_name, name, phone, service, time_str):
    """通知老闆 Flex 卡片"""
    return {
        "type": "flex",
        "altText": f"新的預約：{name}",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical",
                "contents": [{"type": "text", "text": "新的預約諮詢！", "weight": "bold", "size": "lg", "color": "#6b3a63"}],
                "paddingAll": "16px", "backgroundColor": "#faf5f9"
            },
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "LINE 名稱", "size": "sm", "color": "#999999", "flex": 3},
                        {"type": "text", "text": customer_name, "size": "sm", "weight": "bold", "flex": 5}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "姓名", "size": "sm", "color": "#999999", "flex": 3},
                        {"type": "text", "text": name or "未提供", "size": "sm", "weight": "bold", "flex": 5}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "電話", "size": "sm", "color": "#999999", "flex": 3},
                        {"type": "text", "text": phone or "未提供", "size": "sm", "weight": "bold", "flex": 5}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "想體驗", "size": "sm", "color": "#999999", "flex": 3},
                        {"type": "text", "text": service or "未提供", "size": "sm", "weight": "bold", "flex": 5}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "時間", "size": "sm", "color": "#999999", "flex": 3},
                        {"type": "text", "text": time_str, "size": "sm", "flex": 5}
                    ]},
                    {"type": "separator", "margin": "lg"},
                    {"type": "button", "action": {"type": "uri", "label": "查看後台", "uri": ADMIN_URL or "https://line-ai-lys-production.up.railway.app/admin"}, "style": "primary", "color": "#8b5e83", "margin": "lg", "height": "sm"}
                ],
                "paddingAll": "16px"
            }
        }
    }


def build_testimonial_flex():
    """見證卡片"""
    return {
        "type": "flex",
        "altText": "看看其他客人怎麼說",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical",
                "contents": [{"type": "text", "text": "客人真心分享", "weight": "bold", "size": "md", "color": "#2d1f14"}],
                "paddingAll": "16px", "backgroundColor": "#faf5f9"
            },
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "「第一次體驗撥經就愛上了！整個背部鬆超多，好羚老師手法很溫柔，做完覺得身體輕好多～大推！」", "wrap": True, "size": "sm", "color": "#555555", "style": "italic"},
                    {"type": "text", "text": "— 新客體驗 小雯", "size": "xs", "color": "#999999", "margin": "md", "align": "end"},
                    {"type": "separator", "margin": "lg"},
                    {
                        "type": "box", "layout": "horizontal", "margin": "lg",
                        "contents": [
                            {"type": "box", "layout": "vertical", "flex": 1, "contents": [
                                {"type": "text", "text": "新客體驗", "size": "xs", "color": "#999999", "align": "center"},
                                {"type": "text", "text": "1,280", "size": "xl", "weight": "bold", "color": "#8b5e83", "align": "center"}
                            ]},
                            {"type": "box", "layout": "vertical", "flex": 1, "contents": [
                                {"type": "text", "text": "師資", "size": "xs", "color": "#999999", "align": "center"},
                                {"type": "text", "text": "6位", "size": "xl", "weight": "bold", "color": "#3b6d11", "align": "center"}
                            ]},
                            {"type": "box", "layout": "vertical", "flex": 1, "contents": [
                                {"type": "text", "text": "服務時段", "size": "xs", "color": "#999999", "align": "center"},
                                {"type": "text", "text": "10-20", "size": "xl", "weight": "bold", "color": "#1a6bc8", "align": "center"}
                            ]}
                        ]
                    },
                    {"type": "separator", "margin": "lg"},
                    {"type": "button", "action": {"type": "message", "label": "我也想體驗", "text": "我想預約"}, "style": "primary", "color": "#8b5e83", "margin": "lg", "height": "sm"}
                ],
                "paddingAll": "16px"
            }
        }
    }


# ===== 按鈕引導卡片 =====
GUIDED_BUTTONS = {
    "你們有什麼服務？價格怎麼算？": {
        "title": "服務項目與價格",
        "info": [
            "【單項服務】",
            "新客順氣鬆經課 60min 1,280元起（首次限定）",
            "臉部撥經 90min 3,200元起",
            "背部撥經 90min 2,300元起",
            "腿部撥經 90min 2,300元起",
            "胸部撥經 45min 1,500元起",
            "腹部撥經 45min 1,500元起",
            "胯部八髎撥經 30min 1,000元起",
            "無煙艾灸 60min 1,100元起",
            "循環美容議 30min 500元起",
        ]
    },
    "有什麼套餐可以選？": {
        "title": "療癒撥經套餐",
        "info": [
            "【療癒撥經套餐】",
            "",
            "A套餐：背＋腿撥經",
            "　120分鐘 3,200元起",
            "",
            "B套餐：胸＋腹撥經",
            "　90分鐘 2,900元起",
            "",
            "C套餐：背＋腹撥經",
            "　135分鐘 3,600元起",
            "",
            "D套餐：背＋胸撥經",
            "　135分鐘 3,600元起",
            "",
            "套餐更划算，一次照顧多個部位～",
        ]
    },
    "第一次去有什麼推薦的嗎？": {
        "title": "新客推薦體驗",
        "info": [
            "第一次來的話，最推薦：",
            "",
            "【新客順氣鬆經課】",
            "60分鐘｜1,280元起",
            "",
            "這是專為新客設計的入門體驗課程，",
            "讓您認識撥經、感受身體的變化～",
            "",
            "我們有6位專業撥經師：",
            "好羚老師、微雅老師、懿珊老師",
            "家媛、33、Charlotte",
            "",
            "（懿珊老師預約須加指定費）",
            "",
            "地址：桃園市中壢區志航街217號",
            "營業時間：每天 10:00 - 20:00",
        ]
    },
}


def build_guided_flex(user_message):
    """按鈕引導卡片"""
    config = GUIDED_BUTTONS[user_message]
    info_text = "\n".join(config["info"])

    other_buttons = []
    button_map = {
        "你們有什麼服務？價格怎麼算？": ("單項服務價格", "#8b5e83"),
        "有什麼套餐可以選？": ("療癒套餐", "#a87ca0"),
        "第一次去有什麼推薦的嗎？": ("新客推薦", "#c8a0c0"),
    }
    for text, (label, color) in button_map.items():
        if text != user_message:
            other_buttons.append(
                {"type": "button", "action": {"type": "message", "label": label, "text": text}, "style": "secondary", "height": "sm"}
            )

    return {
        "type": "flex",
        "altText": config["title"],
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical",
                "contents": [{"type": "text", "text": config["title"], "weight": "bold", "size": "lg", "color": "#6b3a63"}],
                "paddingAll": "16px", "backgroundColor": "#faf5f9"
            },
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": info_text, "wrap": True, "size": "sm", "color": "#555555"},
                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "繼續了解", "weight": "bold", "size": "sm", "margin": "lg", "color": "#2d1f14"},
                    {"type": "box", "layout": "vertical", "margin": "md", "spacing": "sm", "contents": other_buttons + [
                        {"type": "button", "action": {"type": "message", "label": "我想預約體驗", "text": "我想預約"}, "style": "primary", "color": "#8b5e83", "height": "sm"}
                    ]}
                ],
                "paddingAll": "16px"
            }
        }
    }


# ===== 延遲推播跟進 =====
def schedule_followups(user_id):
    followup_configs = [
        (86400, "24hr"),
        (172800, "48hr"),
        (604800, "7day"),
    ]
    for delay, msg_type in followup_configs:
        timer = threading.Timer(delay, send_followup, args=[user_id, msg_type])
        timer.daemon = True
        timer.start()


def send_followup(user_id, msg_type):
    if user_id in user_booking_data:
        return

    messages = {
        "24hr": {
            "type": "flex", "altText": "還沒來得及了解嗎？",
            "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
                {"type": "text", "text": "嗨～還沒來得及了解嗎？", "weight": "bold", "size": "md", "color": "#2d1f14"},
                {"type": "text", "text": "新客體驗「順氣鬆經課」只要 1,280 元起，60 分鐘讓身體好好放鬆一下～\n\n有任何問題都可以問我哦！", "wrap": True, "size": "sm", "color": "#666666", "margin": "md"},
                {"type": "button", "action": {"type": "message", "label": "了解服務內容", "text": "你們有什麼服務？價格怎麼算？"}, "style": "primary", "color": "#8b5e83", "margin": "lg", "height": "sm"}
            ]}}
        },
        "48hr": {
            "type": "flex", "altText": "身體緊繃嗎？",
            "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
                {"type": "text", "text": "最近身體有點緊繃嗎？", "weight": "bold", "size": "md", "color": "#2d1f14"},
                {"type": "text", "text": "很多客人第一次體驗撥經都說：\n「早知道就早點來了！」\n\n溫和式的撥經手法，不用擔心會痛，做完整個人輕鬆好多～", "wrap": True, "size": "sm", "color": "#666666", "margin": "md"},
                {"type": "button", "action": {"type": "message", "label": "第一次去推薦什麼？", "text": "第一次去有什麼推薦的嗎？"}, "style": "primary", "color": "#8b5e83", "margin": "lg", "height": "sm"}
            ]}}
        },
        "7day": {
            "type": "flex", "altText": "給自己放鬆一下吧",
            "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
                {"type": "text", "text": "忙碌了一週，該犒賞自己了！", "weight": "bold", "size": "md", "color": "#6b3a63"},
                {"type": "text", "text": "LYS 美妍SPA館在中壢志航街，交通方便～\n新客體驗 1,280 元起，給自己一個放鬆的機會吧！", "wrap": True, "size": "sm", "color": "#666666", "margin": "md"},
                {"type": "button", "action": {"type": "message", "label": "立即預約體驗", "text": "我想預約"}, "style": "primary", "color": "#8b5e83", "margin": "lg", "height": "sm"}
            ]}}
        }
    }

    msg = messages.get(msg_type)
    if msg:
        push_flex(user_id, msg)
        log_msg = f"[FOLLOWUP] {msg_type} sent to {user_id[-6:]}"
        print(log_msg, flush=True)
        app_logs.append({"time": datetime.now().strftime("%m/%d %H:%M:%S"), "msg": log_msg})


# ===== 通知老闆 =====
def notify_boss_booking(customer_name, name, phone, service):
    time_str = datetime.now().strftime("%m/%d %H:%M")
    flex = build_notify_boss_flex(customer_name, name, phone, service, time_str)
    if BOSS_USER_ID:
        push_flex(BOSS_USER_ID, flex)


def get_line_profile(user_id):
    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/profile/{user_id}",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
            timeout=5
        )
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return {"displayName": "用戶" + user_id[-4:], "pictureUrl": ""}


def reply_to_user(reply_token, message):
    reply_messages(reply_token, [{"type": "text", "text": message}])


def notify_boss(customer_name, message, time_str):
    if not BOSS_USER_ID:
        return
    text = (
        f"\U0001f514 有客人需要您回覆！\n"
        f"客人：{customer_name}\n"
        f"訊息：{message}\n"
        f"時間：{time_str}\n"
        f"\U0001f449 後台：{ADMIN_URL}"
    )
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"},
        json={"to": BOSS_USER_ID, "messages": [{"type": "text", "text": text}]},
        timeout=10
    )
    log_msg = f"[NOTIFY_BOSS] status={r.status_code}"
    print(log_msg, flush=True)
    app_logs.append({"time": datetime.now().strftime("%m/%d %H:%M:%S"), "msg": log_msg})


def ask_claude(user_message):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=get_setting('system_prompt', SYSTEM_PROMPT),
        messages=[{"role": "user", "content": user_message}]
    )
    return msg.content[0].text


# ===== Webhook 主邏輯 =====
@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json()
    if not body or "events" not in body:
        return jsonify({"status": "ok"})

    for event in body["events"]:
        event_type = event.get("type")

        # ===== Follow Event：加好友歡迎訊息 =====
        if event_type == "follow":
            user_id = event["source"]["userId"]
            reply_token = event["replyToken"]
            log_msg = f"[FOLLOW] new follower: {user_id[-6:]}"
            print(log_msg, flush=True)
            app_logs.append({"time": datetime.now().strftime("%m/%d %H:%M:%S"), "msg": log_msg})

            profile = get_line_profile(user_id)
            user_profiles[user_id] = {
                "name": profile.get("displayName", "用戶"),
                "picture": profile.get("pictureUrl", ""),
                "lastMessage": "（剛加好友）",
                "lastTime": datetime.now().strftime("%m/%d %H:%M")
            }

            welcome_sent.add(user_id)
            _save_welcome_sent()
            reply_messages(reply_token, [build_welcome_flex()])
            schedule_followups(user_id)
            continue

        # ===== 只處理文字訊息 =====
        if event_type != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_id = event["source"]["userId"]
        reply_token = event["replyToken"]
        user_message = event["message"]["text"].strip()
        log_msg = f"[MSG] {user_id[-6:]}: {user_message[:50]}"
        print(log_msg, flush=True)
        app_logs.append({"time": datetime.now().strftime("%m/%d %H:%M:%S"), "msg": log_msg})

        # 更新用戶資料
        if user_id not in user_profiles:
            profile = get_line_profile(user_id)
            user_profiles[user_id] = {
                "name": profile.get("displayName", "用戶"),
                "picture": profile.get("pictureUrl", ""),
                "lastMessage": user_message,
                "lastTime": datetime.now().strftime("%m/%d %H:%M")
            }
        else:
            user_profiles[user_id]["lastMessage"] = user_message
            user_profiles[user_id]["lastTime"] = datetime.now().strftime("%m/%d %H:%M")

        # ----- 0. 舊用戶第一次互動，補發歡迎卡片 -----
        if user_id not in welcome_sent:
            welcome_sent.add(user_id)
            _save_welcome_sent()
            reply_messages(reply_token, [build_welcome_flex()])
            continue

        # ----- 0b. 關鍵字叫出歡迎卡片 -----
        if user_message in ("選單", "主選單", "菜單", "menu"):
            reply_messages(reply_token, [build_welcome_flex()])
            continue

        # ----- 0c. 測驗關鍵字：啟動專屬測驗 -----
        if any(kw in user_message for kw in QUIZ_KEYWORDS):
            user_state[user_id] = {"flow": "quiz", "step": "q1"}
            user_quiz_data[user_id] = {}
            reply_messages(reply_token, [build_quiz_q1_flex()])
            continue

        # ----- 1a. 檢查：是否在測驗流程中 -----
        if user_id in user_state and user_state[user_id].get("flow") == "quiz":
            step = user_state[user_id].get("step")

            if step == "q1":
                user_quiz_data.setdefault(user_id, {})
                user_quiz_data[user_id]["q1"] = user_message
                user_state[user_id]["step"] = "q2"
                reply_messages(reply_token, [build_quiz_q2_flex()])
                continue

            elif step == "q2":
                user_quiz_data[user_id]["q2"] = user_message
                user_state[user_id]["step"] = "q3"
                reply_messages(reply_token, [build_quiz_q3_flex()])
                continue

            elif step == "q3":
                user_quiz_data[user_id]["q3"] = user_message
                del user_state[user_id]
                data = user_quiz_data[user_id]
                teacher = _get_next_teacher()
                log_msg = f"[QUIZ] {user_id[-6:]} 完成測驗，分配 {teacher}"
                print(log_msg, flush=True)
                app_logs.append({"time": datetime.now().strftime("%m/%d %H:%M:%S"), "msg": log_msg})
                reply_messages(reply_token, [build_quiz_result_flex(teacher, data["q1"], data["q2"], data["q3"])])
                continue

        # ----- 1b. 檢查：是否在預約資料收集流程中 -----
        if user_id in user_state and user_state[user_id].get("flow") == "collecting_booking":
            step = user_state[user_id].get("step")

            if step == "name":
                user_booking_data.setdefault(user_id, {})
                user_booking_data[user_id]["name"] = user_message
                user_state[user_id]["step"] = "phone"
                reply_messages(reply_token, [
                    {"type": "flex", "altText": "請留下電話",
                     "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
                         {"type": "text", "text": f"收到！{user_message} 您好", "weight": "bold", "size": "md", "color": "#2d1f14"},
                         {"type": "text", "text": "② 請留下您的聯絡電話", "weight": "bold", "size": "md", "margin": "lg", "color": "#2d1f14"},
                         {"type": "text", "text": "方便我們跟您確認預約時間", "size": "xs", "color": "#999999", "margin": "sm"}
                     ]}}}
                ])
                continue

            elif step == "phone":
                user_booking_data.setdefault(user_id, {})
                user_booking_data[user_id]["phone"] = user_message
                user_state[user_id]["step"] = "service"
                reply_messages(reply_token, [
                    {"type": "flex", "altText": "請選擇想體驗的服務",
                     "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
                         {"type": "text", "text": "③ 最後一題！您想體驗哪個服務呢？", "weight": "bold", "size": "md", "color": "#2d1f14"},
                         {"type": "box", "layout": "vertical", "margin": "md", "spacing": "sm", "contents": [
                             {"type": "button", "action": {"type": "message", "label": "新客順氣鬆經課 1,280起", "text": "新客順氣鬆經課"}, "style": "secondary", "height": "sm"},
                             {"type": "button", "action": {"type": "message", "label": "臉部撥經 3,200起", "text": "臉部撥經"}, "style": "secondary", "height": "sm"},
                             {"type": "button", "action": {"type": "message", "label": "背部撥經 2,300起", "text": "背部撥經"}, "style": "secondary", "height": "sm"},
                             {"type": "button", "action": {"type": "message", "label": "套餐（到店再選）", "text": "套餐，到店再選"}, "style": "secondary", "height": "sm"},
                             {"type": "button", "action": {"type": "message", "label": "還不確定，想先諮詢", "text": "還不確定，想先諮詢"}, "style": "secondary", "height": "sm"}
                         ]}
                     ]}}}
                ])
                continue

            elif step == "service":
                user_booking_data.setdefault(user_id, {})
                user_booking_data[user_id]["service"] = user_message
                del user_state[user_id]
                customer_name = user_profiles.get(user_id, {}).get("name", "用戶")
                data = user_booking_data[user_id]

                # 通知老闆
                notify_boss_booking(
                    customer_name,
                    data.get("name", ""),
                    data.get("phone", ""),
                    data.get("service", "")
                )

                reply_messages(reply_token, [build_booking_complete_flex(data)])
                continue

        # ----- 2. 檢查：按鈕引導（服務/套餐/新客） -----
        if user_message in GUIDED_BUTTONS:
            reply_messages(reply_token, [build_guided_flex(user_message)])
            continue

        # ----- 3. 檢查：預約關鍵字 -----
        if any(kw in user_message for kw in BOOKING_KEYWORDS):
            user_state[user_id] = {"flow": "collecting_booking", "step": "name"}
            user_booking_data[user_id] = {}
            reply_messages(reply_token, [build_booking_start_flex()])
            continue

        # ----- 4. 檢查：找真人（暫停 AI + 通知老闆） -----
        current_triggers = json.loads(get_setting('trigger_words', json.dumps(TRIGGER_WORDS)))
        if any(word in user_message for word in current_triggers):
            if BOSS_USER_ID and user_id == BOSS_USER_ID:
                reply_to_user(reply_token, "老闆您好！這是轉人工功能，客人觸發時您會收到通知")
                continue
            paused_users.add(user_id)
            reply_to_user(reply_token, "好的！我馬上幫您通知老師，請稍候片刻，我們會盡快與您聯繫")
            customer_name = user_profiles[user_id]["name"]
            time_str = user_profiles[user_id]["lastTime"]
            notify_boss(customer_name, user_message, time_str)
            continue

        # ----- 5. 檢查：暫停中的用戶 -----
        if user_id in paused_users:
            continue

        # ----- 6. AI 回覆 + 見證卡片觸發 -----
        try:
            ai_response = ask_claude(user_message)
            reply_to_user(reply_token, ai_response)

            user_message_count[user_id] = user_message_count.get(user_id, 0) + 1
            if user_message_count[user_id] == 3 and user_id not in testimonial_sent:
                testimonial_sent.add(user_id)
                timer = threading.Timer(3.0, push_flex, args=[user_id, build_testimonial_flex()])
                timer.daemon = True
                timer.start()

        except Exception as e:
            log_msg = f"[ERROR] Claude API: {str(e)}"
            print(log_msg, flush=True)
            app_logs.append({"time": datetime.now().strftime("%m/%d %H:%M:%S"), "msg": log_msg})
            reply_to_user(reply_token, "抱歉，系統暫時忙碌中，請稍後再試或直接撥打 0916-660-072")

    return jsonify({"status": "ok"})


# ===== 後台路由 =====
@app.route("/admin")
def admin():
    authenticated = request.cookies.get("admin_auth") == ADMIN_PASSWORD
    brand_name = "LYS 美妍SPA館"

    all_users = []
    for uid, p in user_profiles.items():
        all_users.append({
            "id": uid,
            "name": p["name"],
            "picture": p.get("picture", ""),
            "lastMessage": p.get("lastMessage", ""),
            "lastTime": p.get("lastTime", ""),
            "paused": uid in paused_users
        })
    all_users.sort(key=lambda x: x["lastTime"], reverse=True)

    paused_list = [u for u in all_users if u["paused"]]
    active_list = [u for u in all_users if not u["paused"]]
    total = len(all_users)
    paused_count = len(paused_list)
    active_count = len(active_list)
    ai_rate = round((active_count / total * 100) if total > 0 else 100)

    html = render_template_string(
        ADMIN_HTML,
        authenticated=authenticated,
        brand_name=brand_name,
        paused_users_list=paused_list,
        active_users=active_list,
        pending_users=paused_list,
        total=total,
        active=active_count,
        paused_count=paused_count,
        ai_rate=ai_rate,
        error=False
    )
    resp = make_response(html)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp


@app.route("/admin/login", methods=["POST"])
def admin_login():
    password = request.form.get("password")
    if password == ADMIN_PASSWORD:
        resp = make_response(redirect("/admin"))
        resp.set_cookie("admin_auth", ADMIN_PASSWORD, max_age=86400 * 7)
        return resp
    brand_name = "LYS 美妍SPA館"
    html = render_template_string(
        ADMIN_HTML, authenticated=False, brand_name=brand_name,
        paused_users_list=[], active_users=[], pending_users=[],
        total=0, active=0, paused_count=0, ai_rate=100, error=True
    )
    resp = make_response(html)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp


@app.route("/admin/settings")
def admin_settings():
    if request.cookies.get("admin_auth") != ADMIN_PASSWORD:
        return redirect("/admin")
    brand_name = "LYS 美妍SPA館"
    current_prompt = get_setting('system_prompt', SYSTEM_PROMPT)
    current_triggers = json.loads(get_setting('trigger_words', json.dumps(TRIGGER_WORDS)))
    trigger_text = "\n".join(current_triggers)
    html = render_template_string(
        SETTINGS_HTML,
        brand_name=brand_name,
        system_prompt=current_prompt,
        trigger_words=trigger_text
    )
    resp = make_response(html)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp


@app.route("/admin/settings/save", methods=["POST"])
def admin_settings_save():
    if request.cookies.get("admin_auth") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json()
    prompt = data.get("system_prompt", "").strip()
    triggers_text = data.get("trigger_words", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt 不能為空"}), 400
    triggers = [w.strip() for w in triggers_text.split("\n") if w.strip()]
    set_setting('system_prompt', prompt)
    set_setting('trigger_words', json.dumps(triggers, ensure_ascii=False))
    return jsonify({"status": "ok"})


@app.route("/admin/settings/reset", methods=["POST"])
def admin_settings_reset():
    if request.cookies.get("admin_auth") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    set_setting('system_prompt', SYSTEM_PROMPT)
    set_setting('trigger_words', json.dumps(TRIGGER_WORDS, ensure_ascii=False))
    return jsonify({"status": "ok"})


@app.route("/admin/toggle", methods=["POST"])
def admin_toggle():
    if request.cookies.get("admin_auth") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json()
    uid = data.get("userId")
    action = data.get("action")
    if action == "pause":
        paused_users.add(uid)
    elif action == "resume":
        paused_users.discard(uid)
    return jsonify({"status": "ok"})


@app.route("/debug/logs")
def debug_logs():
    if request.cookies.get("admin_auth") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(app_logs[-50:])


@app.route("/")
def index():
    return jsonify({"status": "ok", "service": "LYS 美妍SPA館 LINE AI 客服"})


# ===== Rich Menu 設定 =====
@app.route("/setup-richmenu")
def setup_richmenu():
    """一次性設定：建立 Rich Menu + 用自訂底圖 + 上傳 + 設為預設"""
    if request.cookies.get("admin_auth") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401

    try:
        from PIL import Image
    except ImportError:
        return jsonify({"error": "Pillow not installed"}), 500

    headers = {"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"}

    # Step 1: 刪除舊的預設 Rich Menu
    try:
        old = requests.get("https://api.line.me/v2/bot/user/all/richmenu", headers=headers, timeout=10)
        if old.status_code == 200:
            old_id = old.json().get("richMenuId")
            if old_id:
                requests.delete(f"https://api.line.me/v2/bot/richmenu/{old_id}", headers=headers, timeout=10)
    except Exception:
        pass

    # Step 2: 建立 Rich Menu 物件（3x2 六格）
    richmenu_data = {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "LYS 美妍SPA館選單",
        "chatBarText": "點我展開選單",
        "areas": [
            {"bounds": {"x": 0, "y": 0, "width": 833, "height": 843},
             "action": {"type": "message", "text": "我想預約"}},
            {"bounds": {"x": 833, "y": 0, "width": 834, "height": 843},
             "action": {"type": "message", "text": "你們有什麼服務？價格怎麼算？"}},
            {"bounds": {"x": 1667, "y": 0, "width": 833, "height": 843},
             "action": {"type": "message", "text": "第一次去有什麼推薦的嗎？"}},
            {"bounds": {"x": 0, "y": 843, "width": 833, "height": 843},
             "action": {"type": "message", "text": "請問你們的地址在哪裡？怎麼去？"}},
            {"bounds": {"x": 833, "y": 843, "width": 834, "height": 843},
             "action": {"type": "message", "text": "撥經是什麼？會痛嗎？"}},
            {"bounds": {"x": 1667, "y": 843, "width": 833, "height": 843},
             "action": {"type": "message", "text": "有什麼套餐可以選？"}}
        ]
    }

    r = requests.post("https://api.line.me/v2/bot/richmenu", headers=headers, json=richmenu_data, timeout=10)
    if r.status_code != 200:
        return jsonify({"error": "create richmenu failed", "detail": r.text}), 500
    richmenu_id = r.json()["richMenuId"]

    # Step 3: 載入自訂底圖並調整為 2500x1686
    import io
    W, H = 2500, 1686

    img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "richmenu.png")
    if not os.path.exists(img_path):
        return jsonify({"error": "richmenu.png not found"}), 500

    img = Image.open(img_path).convert("RGB")
    if img.size != (W, H):
        img = img.resize((W, H), Image.LANCZOS)

    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)

    # Step 4: 上傳圖片
    r2 = requests.post(
        f"https://api-data.line.me/v2/bot/richmenu/{richmenu_id}/content",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "image/png"},
        data=img_bytes.read(), timeout=30
    )
    if r2.status_code != 200:
        return jsonify({"error": "upload image failed", "detail": r2.text}), 500

    # Step 5: 設為預設
    r3 = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{richmenu_id}",
        headers={"Authorization": f"Bearer {LINE_TOKEN}"},
        timeout=10
    )
    if r3.status_code != 200:
        return jsonify({"error": "set default failed", "detail": r3.text}), 500

    return jsonify({"status": "ok", "richMenuId": richmenu_id, "message": "Rich Menu 建立完成！"})


@app.route("/delete-richmenu")
def delete_richmenu():
    """刪除 API 設定的預設 Rich Menu，讓 LINE 後台設定的圖文選單生效"""
    if request.cookies.get("admin_auth") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401

    headers = {"Authorization": f"Bearer {LINE_TOKEN}"}

    # 取得目前 API 預設的 Rich Menu
    old = requests.get("https://api.line.me/v2/bot/user/all/richmenu", headers=headers, timeout=10)
    if old.status_code != 200:
        return jsonify({"status": "ok", "message": "目前沒有透過 API 設定的預設圖文選單"})

    old_id = old.json().get("richMenuId")
    if not old_id:
        return jsonify({"status": "ok", "message": "目前沒有透過 API 設定的預設圖文選單"})

    # 取消預設
    requests.delete(f"https://api.line.me/v2/bot/user/all/richmenu", headers=headers, timeout=10)
    # 刪除 Rich Menu 物件
    requests.delete(f"https://api.line.me/v2/bot/richmenu/{old_id}", headers=headers, timeout=10)

    return jsonify({"status": "ok", "message": f"已刪除 API 圖文選單 ({old_id})，LINE 後台設定的選單將會生效"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
