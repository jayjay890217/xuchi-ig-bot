"""本機測試：假裝 Meta 打 webhook，檢查程式會送出什麼（不會真的連 IG）。"""
import hashlib, hmac, json, os
os.environ.update(APP_SECRET="s", VERIFY_TOKEN="v", IG_TOKEN="t")
import app as m

sent = []
m.graph_post = lambda path, payload: sent.append((path, payload)) or {}
follow = {"v": False}
def fake_get(path, **p):
    if path == "me": return {"username": "xuchi_test"}
    if "is_user_follow_business" in p.get("fields", ""): return {"username": "amy", "is_user_follow_business": follow["v"]}
    return {"permalink": "https://www.instagram.com/p/ABC/", "shortcode": "ABC"}
m.graph_get = fake_get
m._token["refreshed"] = 9e18

c = m.app.test_client()
assert c.get("/webhook?hub.mode=subscribe&hub.verify_token=v&hub.challenge=123").data == b"123"
assert c.get("/webhook?hub.mode=subscribe&hub.verify_token=x&hub.challenge=1").status_code == 403
assert c.post("/webhook", data=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 403

# 1. 留言關鍵字
m.process({"entry": [{"id": "BIZ", "changes": [{"field": "comments", "value": {
    "id": "C1", "text": "想要 保養 價目", "from": {"id": "U1", "username": "amy"}, "media": {"id": "M1"}}}]}]})
print("留言後：", json.dumps(sent, ensure_ascii=False)); assert sent[0][1]["recipient"] == {"comment_id": "C1"}
assert sent[1][0] == "C1/replies"
sent.clear()
# 不相關留言
m.process({"entry": [{"id": "BIZ", "changes": [{"field": "comments", "value": {
    "id": "C2", "text": "好帥", "from": {"id": "U1"}, "media": {"id": "M1"}}}]}]})
assert not sent
# 2. 沒追蹤按按鈕
m.process({"entry": [{"id": "BIZ", "messaging": [{"sender": {"id": "U1"}, "postback": {"payload": "CLAIM:保養"}}]}]})
print("沒追蹤：", json.dumps(sent, ensure_ascii=False)); assert "差一步" in json.dumps(sent, ensure_ascii=False)
sent.clear()
# 3. 追蹤後打字「我追蹤好了」
follow["v"] = True
m.process({"entry": [{"id": "BIZ", "messaging": [{"sender": {"id": "U1"}, "message": {"text": "我追蹤好了"}}]}]})
print("有追蹤：", json.dumps(sent, ensure_ascii=False)); assert "價目表" in json.dumps(sent, ensure_ascii=False)
# 簽章正確的 POST
body = json.dumps({"entry": []}).encode()
sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
assert c.post("/webhook", data=body, headers={"X-Hub-Signature-256": sig}).status_code == 200
# 指定貼文網址的規則
m._rules_cache.update(rules=[{"關鍵字": "改裝", "貼文網址或ID": "https://www.instagram.com/reel/ABC/?igsh=x", "名稱": "改裝"}], at=9e18)
assert m.match_rule("改裝", "M1") and not m.match_rule("保養", "M1")
m._shortcode_cache["M2"] = "https://www.instagram.com/p/ZZZ/ ZZZ"
assert not m.match_rule("改裝", "M2")
print("全部通過")
