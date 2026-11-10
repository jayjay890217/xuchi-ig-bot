"""旭馳 IG「追蹤才能領」機器人

流程：
1. 客人在貼文留言關鍵字（例如「保養」）
2. 自動私訊他一顆「領取」按鈕，並在留言底下回「已私訊你囉」
3. 客人按按鈕 → 檢查有沒有追蹤旭馳
   - 有追蹤：送價目表／資料
   - 沒追蹤：請他先追蹤，再按一次

關鍵字設定：Google 試算表（發佈成 CSV，網址放 RULES_CSV_URL），沒有設定就用 rules.json。
"""
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import threading
import time

import requests
from flask import Flask, abort, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ig")

GRAPH = "https://graph.instagram.com/v23.0"
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")      # 自己訂的一串字，Meta 後台填一樣的
APP_SECRET = os.environ.get("APP_SECRET", "")          # Meta 後台 Instagram App 密鑰
RULES_CSV_URL = os.environ.get("RULES_CSV_URL", "")    # 試算表發佈成 CSV 的網址
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")  # 有填才會把新 token 存回 Render
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")

_token = {"value": os.environ.get("IG_TOKEN", ""), "refreshed": 0}
_rules_cache = {"rules": [], "at": 0}
_last_rule = {}   # 客人 id → 上次留言對到的規則（客人打字「領取」時用）
_lock = threading.Lock()
_done_once = set()  # 已處理過的留言 id

# ---------- 文字（要改語氣改這裡） ----------
MSG_INVITE = "嗨 {name}！謝謝你留言「{kw}」🙌\n按下面的按鈕就能領取【{title}】"
MSG_INVITE_FALLBACK = "嗨 {name}！謝謝你留言「{kw}」🙌\n回覆「領取」兩個字，就能拿到【{title}】"
MSG_NEED_FOLLOW = "差一步！先追蹤 @{biz} 再按一次「我追蹤好了」，就會馬上送你【{title}】😊"
MSG_COMMENT_REPLY = "已經私訊你囉，記得去收訊息 📩"
BTN_CLAIM = "領取"
BTN_FOLLOWED = "我追蹤好了"
CLAIM_WORDS = {"領取", "领取", "我追蹤好了", "追蹤好了", "已追蹤"}


# ---------- 存取 token ----------
# IGAA 開頭＝Instagram 權杖（graph.instagram.com）；EAA 開頭＝Facebook 權杖（graph.facebook.com，
# 要先換成粉絲專頁權杖，私訊改用 /{粉專ID}/messages）
FB_MODE = _token["value"].startswith("EAA")
if FB_MODE:
    GRAPH = "https://graph.facebook.com/v23.0"
_fb = {}


def fb_setup():
    """用 Facebook 權杖找出連著旭馳 IG 的粉絲專頁，取得粉專權杖。"""
    if _fb.get("page_token"):
        return _fb
    raw = _token["value"]
    q = {"access_token": raw, "fields": "id,name,access_token,instagram_business_account{id,username}"}
    data = requests.get(f"{GRAPH}/me/accounts", params=q, timeout=15).json()
    for page in data.get("data", []):
        iba = page.get("instagram_business_account")
        if iba:
            _fb.update(page_id=page["id"], page_name=page.get("name"), page_token=page.get("access_token") or raw,
                       ig_id=iba["id"], ig_username=iba.get("username"))
            return _fb
    q["fields"] = "id,name,instagram_business_account{id,username}"
    me = requests.get(f"{GRAPH}/me", params=q, timeout=15).json()
    iba = me.get("instagram_business_account")
    if iba:  # 權杖本身就是粉專權杖
        _fb.update(page_id=me["id"], page_name=me.get("name"), page_token=raw,
                   ig_id=iba["id"], ig_username=iba.get("username"))
    else:
        _fb["error"] = {"accounts": data, "me": me}
        log.error("找不到連著 IG 的粉專：%s", _fb["error"])
    return _fb


def token():
    if FB_MODE:
        return fb_setup().get("page_token") or _token["value"]
    return _token["value"]


def msg_path():
    return f"{fb_setup().get('page_id')}/messages" if FB_MODE else "me/messages"


def refresh_token_if_needed():
    """Instagram 長期 token 60 天會過期；每 7 天換新一次。"""
    if FB_MODE or not token() or time.time() - _token["refreshed"] < 7 * 86400:
        return
    try:
        r = requests.get(f"{GRAPH.rsplit('/', 1)[0]}/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": token()}, timeout=15)
        data = r.json()
        if "access_token" in data:
            _token["value"] = data["access_token"]
            _token["refreshed"] = time.time()
            log.info("token 已更新，%s 天後到期", int(data.get("expires_in", 0)) // 86400)
            save_token_to_render(data["access_token"])
        else:
            log.error("token 更新失敗：%s", data)
    except Exception as e:  # noqa: BLE001
        log.error("token 更新錯誤：%s", e)


def save_token_to_render(new_token):
    if not (RENDER_API_KEY and RENDER_SERVICE_ID):
        return
    try:
        r = requests.put(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars/IG_TOKEN",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}"}, json={"value": new_token}, timeout=15)
        log.info("新 token 存回 Render：%s", r.status_code)
    except Exception as e:  # noqa: BLE001
        log.error("存回 Render 失敗：%s", e)


def graph_get(path, **params):
    params["access_token"] = token()
    r = requests.get(f"{GRAPH}/{path}", params=params, timeout=15)
    return r.json()


def graph_post(path, payload):
    r = requests.post(f"{GRAPH}/{path}", json=payload,
                      headers={"Authorization": f"Bearer {token()}"}, timeout=15)
    data = r.json()
    if "error" in data:
        log.warning("Graph 錯誤 %s：%s", path, data["error"].get("message"))
    return data


# ---------- 關鍵字規則 ----------
def load_rules():
    """欄位：關鍵字、貼文網址或ID（空白=所有貼文）、名稱、送的文字、圖片網址、連結、啟用"""
    if time.time() - _rules_cache["at"] < 120 and _rules_cache["rules"]:
        return _rules_cache["rules"]
    rules = []
    try:
        if RULES_CSV_URL:
            r = requests.get(RULES_CSV_URL, timeout=15)
            r.encoding = "utf-8"
            for row in csv.DictReader(io.StringIO(r.text)):
                rules.append({k.strip(): (v or "").strip() for k, v in row.items() if k})
        else:
            with open(os.path.join(os.path.dirname(__file__), "rules.json"), encoding="utf-8") as f:
                rules = json.load(f)
    except Exception as e:  # noqa: BLE001
        log.error("讀取規則失敗：%s", e)
        return _rules_cache["rules"]
    rules = [x for x in rules if x.get("關鍵字") and x.get("啟用", "是") not in ("否", "N", "n", "0", "FALSE")]
    _rules_cache.update(rules=rules, at=time.time())
    return rules


def norm(s):
    return "".join((s or "").split()).lower()


def match_rule(text, media_id):
    t = norm(text)
    best = None
    for rule in load_rules():
        post = rule.get("貼文網址或ID", "")
        if post and not media_matches(post, media_id):
            continue
        if norm(rule["關鍵字"]) in t:
            # 有指定貼文的規則優先
            if best is None or (post and not best.get("貼文網址或ID")):
                best = rule
    return best


_shortcode_cache = {}


def media_matches(post, media_id):
    """試算表可以填貼文 ID，也可以直接貼貼文網址（用 shortcode 比對）。"""
    if not media_id:
        return False
    if media_id in post:
        return True
    if media_id not in _shortcode_cache:
        data = graph_get(media_id, fields="permalink,shortcode")
        _shortcode_cache[media_id] = (data.get("permalink", "") + " " + data.get("shortcode", "")).strip()
    info = _shortcode_cache[media_id]
    return bool(info) and any(part and part in post for part in info.split())


def rule_by_key(key):
    for rule in load_rules():
        if norm(rule["關鍵字"]) == key:
            return rule
    return None


# ---------- 送訊息 ----------
def button_message(text, btn_title, payload):
    return {"attachment": {"type": "template", "payload": {
        "template_type": "button", "text": text,
        "buttons": [{"type": "postback", "title": btn_title, "payload": payload}]}}}


def biz_username():
    if FB_MODE:
        return fb_setup().get("ig_username") or "旭馳"
    if not hasattr(biz_username, "v"):
        biz_username.v = graph_get("me", fields="username").get("username", "旭馳")
    return biz_username.v


def send_gift(user_id, rule):
    text = rule.get("送的文字") or f"這是你的【{rule.get('名稱', '')}】"
    if rule.get("連結"):
        text += f"\n{rule['連結']}"
    graph_post(msg_path(), {"recipient": {"id": user_id}, "message": {"text": text}})
    for url in (rule.get("圖片網址") or "").split():
        graph_post(msg_path(), {"recipient": {"id": user_id},
                                   "message": {"attachment": {"type": "image", "payload": {"url": url}}}})
    log.info("已送出【%s】給 %s", rule.get("名稱"), user_id)


def handle_comment(biz_id, value):
    if value.get("id") in _done_once:
        return
    _done_once.add(value.get("id"))
    frm = value.get("from", {})
    if frm.get("id") == biz_id or value.get("parent_id"):
        return  # 自己的留言、或回覆別人的留言，不處理
    rule = match_rule(value.get("text", ""), value.get("media", {}).get("id", ""))
    if not rule:
        return
    kw = rule["關鍵字"]
    name = frm.get("username", "")
    payload = "CLAIM:" + norm(kw)
    title = rule.get("名稱") or kw
    res = graph_post(msg_path(), {"recipient": {"comment_id": value["id"]},
                                     "message": button_message(MSG_INVITE.format(name=name, kw=kw, title=title),
                                                               BTN_CLAIM, payload)})
    if "error" in res:
        res = graph_post(msg_path(), {"recipient": {"comment_id": value["id"]},
                                         "message": {"text": MSG_INVITE_FALLBACK.format(name=name, kw=kw, title=title)}})
    if "error" not in res:
        _last_rule[frm.get("id")] = norm(kw)
        _last_rule["@" + name] = norm(kw)
        graph_post(f"{value['id']}/replies", {"message": MSG_COMMENT_REPLY})
        log.info("留言「%s」→ 私訊 %s", kw, name)


def handle_claim(user_id, key):
    rule = rule_by_key(key) if key else None
    if not rule:
        return
    _last_rule[user_id] = key
    prof = graph_get(user_id, fields="username,is_user_follow_business")
    if prof.get("is_user_follow_business"):
        send_gift(user_id, rule)
    else:
        title = rule.get("名稱") or rule["關鍵字"]
        graph_post(msg_path(), {"recipient": {"id": user_id}, "message": button_message(
            MSG_NEED_FOLLOW.format(biz=biz_username(), title=title), BTN_FOLLOWED, "CLAIM:" + key)})
        log.info("%s 還沒追蹤", prof.get("username", user_id))


def handle_messaging(biz_id, ev):
    sender = ev.get("sender", {}).get("id")
    if not sender or sender == biz_id:
        return
    msg = ev.get("message", {})
    if msg.get("is_echo"):
        return
    if "postback" in ev:
        p = ev["postback"].get("payload", "")
        if p.startswith("CLAIM:"):
            handle_claim(sender, p[6:])
    elif norm(msg.get("text")) in CLAIM_WORDS:
        handle_claim(sender, _last_rule.get(sender))


def process(body):
    if True:  # 不等定時查看的鎖，留言一來就處理
        refresh_token_if_needed()
        for entry in body.get("entry", []):
            biz_id = entry.get("id")
            kinds = [c.get("field") for c in entry.get("changes", [])] +                 [",".join(k for k in ev if k not in ("sender", "recipient", "timestamp")) +
                 ("(echo)" if ev.get("message", {}).get("is_echo") else "") for ev in entry.get("messaging", [])]
            log.info("收到通知 object=%s 種類=%s", body.get("object"), kinds)
            for ch in entry.get("changes", []):
                if ch.get("field") == "comments":
                    try:
                        handle_comment(biz_id, ch.get("value", {}))
                    except Exception:  # noqa: BLE001
                        log.exception("處理留言失敗")
            if entry.get("standby"):
                log.info("收到 standby（訊息被其他工具接走）：%s", [list(e) for e in entry["standby"]])
            for ev in entry.get("messaging", []) + entry.get("standby", []):
                try:
                    handle_messaging(biz_id, ev)
                except Exception:  # noqa: BLE001
                    log.exception("處理私訊失敗")


# ---------- 定時查看私訊（Meta 沒送私訊通知時的備案） ----------
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))


def rule_from_thread(msgs, biz_id):
    """從旭馳先前送出的邀請訊息裡的【名稱】找回是哪一條規則。"""
    for m in msgs:
        if m.get("from", {}).get("id") == biz_id:
            for t in re.findall(r"【(.+?)】", m.get("message") or ""):
                for rule in load_rules():
                    if t in ((rule.get("名稱") or ""), rule["關鍵字"]):
                        return rule
    return None


def poll_once():
    if not FB_MODE:
        return
    info = fb_setup()
    page_id, biz_id = info.get("page_id"), info.get("ig_id")
    if not page_id:
        return
    data = graph_get(f"{page_id}/conversations", platform="instagram", limit=10,
                     fields="updated_time,participants,messages.limit(10){message,from,created_time}")
    for conv in data.get("data", []):
        msgs = conv.get("messages", {}).get("data", [])  # 新的在前
        if not msgs:
            continue
        last = msgs[0]
        frm = last.get("from", {})
        # 只處理「最後一則是客人打的領取」：旭馳一回覆就不會重複處理
        if frm.get("id") == biz_id or norm(last.get("message")) not in CLAIM_WORDS:
            continue
        rule = rule_from_thread(msgs, biz_id)
        if not rule:
            key = _last_rule.get(frm.get("id")) or _last_rule.get("@" + (frm.get("username") or ""))
            rule = rule_by_key(key) if key else None
        if not rule and len(load_rules()) == 1:
            rule = load_rules()[0]
        if not rule:
            log.info("查看私訊：%s 打了領取，但找不到是哪個活動", frm.get("username"))
        if rule:
            log.info("查看私訊：%s 要領取【%s】", frm.get("username"), rule.get("名稱"))
            handle_claim(frm["id"], norm(rule["關鍵字"]))


# 只處理這個時間之後的新留言（避免把舊留言補發私訊）
COMMENTS_AFTER = os.environ.get("COMMENTS_AFTER", "2026-11-05T06:00:00+0000")
_done_comments = set()


def poll_comments():
    if not FB_MODE:
        return
    info = fb_setup()
    biz_id = info.get("ig_id")
    if not biz_id:
        return
    media = graph_get(f"{biz_id}/media", fields="id,timestamp", limit=int(os.environ.get("POLL_MEDIA", "25")))
    for md in media.get("data", []):
        cs = graph_get(f"{md['id']}/comments", limit=30,
                       fields="id,text,timestamp,from,username,replies{username}")
        if "error" in cs:
            log.warning("讀留言失敗：%s", cs["error"].get("message"))
            return
        if _poll_n["n"] <= 1:
            fresh = [(x.get("username"), (x.get("text") or "")[:10], x.get("timestamp"))
                     for x in cs.get("data", []) if x.get("timestamp", "") >= COMMENTS_AFTER]
            if fresh:
                log.info("讀留言：貼文 %s 有新留言 %s", md["id"], fresh)
        for c in cs.get("data", []):
            if c["id"] in _done_comments or c.get("timestamp", "") < COMMENTS_AFTER:
                continue
            _done_comments.add(c["id"])
            replied = any(r.get("username") == info.get("ig_username")
                          for r in c.get("replies", {}).get("data", []))
            if replied:
                continue
            frm = c.get("from") or {"username": c.get("username")}
            handle_comment(biz_id, {"id": c["id"], "text": c.get("text", ""), "from": frm,
                                    "media": {"id": md["id"]}})


_poll_n = {"n": 0}


def poll_loop():
    log.info("開始定時查看私訊（每 %s 秒）", POLL_SECONDS)
    while True:
        try:
            with _lock:
                poll_once()
            _poll_n["n"] += 1
            if _poll_n["n"] % 4 == 1:  # 約每分鐘看一次留言（不佔鎖）
                poll_comments()
        except Exception:  # noqa: BLE001
            log.exception("查看私訊失敗")
        time.sleep(POLL_SECONDS)


if os.environ.get("IG_TOKEN") and not os.environ.get("NO_POLL"):
    threading.Thread(target=poll_loop, daemon=True).start()


# ---------- 網址 ----------
@app.get("/")
def home():
    return f"旭馳 IG 機器人運作中（規則 {len(load_rules())} 條）"


@app.get("/status")
def status():
    """檢查連線用：不會顯示任何金鑰。"""
    if FB_MODE:
        me = {k: v for k, v in fb_setup().items() if k != "page_token"}
    else:
        me = graph_get("me", fields="id,username") if token() else {"error": "沒有 IG_TOKEN"}
    return {"graph": GRAPH, "token_type": token()[:3] if token() else "", "app_secret": bool(APP_SECRET),
            "me": me, "rules": len(load_rules())}


@app.get("/webhook")
def verify():
    if request.args.get("hub.mode") == "subscribe" and VERIFY_TOKEN and \
            request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", "")
    abort(403)


@app.post("/webhook")
def webhook():
    raw = request.get_data()
    sig = request.headers.get("X-Hub-Signature-256", "")
    expect = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not APP_SECRET or not hmac.compare_digest(sig, expect):
        log.warning("簽章不符，拒絕")
        abort(403)
    body = json.loads(raw or b"{}")
    log.info("webhook 內容：%s", raw[:1500].decode("utf-8", "replace"))
    threading.Thread(target=process, args=(body,), daemon=True).start()
    return "ok"


@app.get("/privacy")
def privacy():
    return PRIVACY_HTML


PRIVACY_HTML = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>旭馳車業 隱私權政策</title>
<body style="font-family:sans-serif;max-width:720px;margin:24px auto;padding:0 16px;line-height:1.7">
<h1>旭馳車業 Instagram 自動回覆 隱私權政策</h1>
<p>旭馳車業（新北市新莊區豐年街55-10號）使用 Instagram 自動回覆服務，在您於旭馳貼文留言指定關鍵字後，以私訊傳送您索取的資料。</p>
<h2>我們會用到的資料</h2>
<ul><li>您的 Instagram 使用者名稱與帳號編號</li><li>您的留言內容（只用來比對關鍵字）</li><li>您是否追蹤旭馳的 Instagram 帳號</li></ul>
<h2>用途</h2><p>只用於回覆您的留言與私訊，不會出售、不會提供給第三方，也不會另外儲存成名單。</p>
<h2>刪除資料</h2><p>若要刪除與您有關的資料，請私訊旭馳 Instagram 或來電告知，我們會在 7 天內處理。</p>
<p>更新日期：2026-11-05</p></body>"""


@app.get("/data-deletion")
def data_deletion():
    return PRIVACY_HTML


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)), debug=True)
