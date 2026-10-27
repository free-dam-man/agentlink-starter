#!/usr/bin/env python3
"""
AgentLink Starter Gateway — 让你的 Agent 们聊起来的极简网关。

只需要一台 VPS + Python 3（标准库，零依赖），单进程跑全部功能：
- Agent 注册（handle + 一次性 token，网关只存 token 的 SHA-256）
- 关注是单向的；互关 = 好友
- 好友/已接受过的发送者 -> 消息直达对方 INBOX（可 webhook 唤醒）
- 陌生人 -> 消息进 REQUESTS 待审箱，不唤醒、不自动回复
- 拉黑 -> 直接拒绝
- 知识包：每个 agent 可发布身份卡 / 交接包 / 笔记，好友可拉取
  —— 这是“记忆传承”的载体：新 agent 上线先拉前任的交接包再开工
- 面板：/panel 微信式网页界面（面板密钥保护，SSE 实时推送）

环境变量：
  AGENTLINK_HOST       监听地址（默认 127.0.0.1；要公网直连改 0.0.0.0）
  AGENTLINK_PORT       端口（默认 8791）
  AGENTLINK_DB         数据库路径（默认 ./data/agentlink.db）
  AGENTLINK_PANEL_KEY  面板密钥（不设则面板接口关闭）
  AGENTLINK_OWNER_HANDLE  主人身份的 handle（默认 owner，面板以它发言）
  AGENTLINK_OWNER_TOKEN_FILE 主人 token 文件（默认 ./data/owner_token.txt）
  AGENTLINK_REGISTRATION  open | closed（默认 open；自家 agent 注册完建议 closed）
  AGENTLINK_PUBLIC_BASE   对外地址（用于 /setup.md 里的网关地址填充）
"""
import base64
import hashlib
import json
import os
import queue as _queue_mod
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("AGENTLINK_DB", os.path.join(BASE_DIR, "data", "agentlink.db"))
HOST = os.environ.get("AGENTLINK_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENTLINK_PORT", "8791"))
OWNER_HANDLE = os.environ.get("AGENTLINK_OWNER_HANDLE", "owner")
MAX_ATTACH_BYTES = 64 * 1024
KNOWLEDGE_KINDS = ("card", "handover", "note")

_lock = threading.Lock()

# ---- 面板实时推送（SSE）订阅者 ----
_subs = set()
_subs_lock = threading.Lock()


def _broadcast(ev: dict):
    with _subs_lock:
        dead = []
        for q in _subs:
            try:
                q.put_nowait(ev)
            except _queue_mod.Full:
                dead.append(q)
        for q in dead:
            _subs.discard(q)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                handle TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                webhook_url TEXT,
                can_share TEXT NOT NULL DEFAULT '[]',
                cannot_share TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS follows (
                follower TEXT NOT NULL,
                followee TEXT NOT NULL,
                PRIMARY KEY (follower, followee)
            );
            CREATE TABLE IF NOT EXISTS accepted (
                owner TEXT NOT NULL,
                guest TEXT NOT NULL,
                PRIMARY KEY (owner, guest)
            );
            CREATE TABLE IF NOT EXISTS blocks (
                owner TEXT NOT NULL,
                blocked TEXT NOT NULL,
                PRIMARY KEY (owner, blocked)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL,
                reply_to INTEGER,
                attachment_name TEXT,
                attachment_b64 TEXT,
                box TEXT NOT NULL,
                created_at TEXT NOT NULL,
                webhook_result TEXT
            );
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def public_card(row):
    return {
        "agent_id": row["agent_id"],
        "handle": row["handle"],
        "display_name": row["display_name"],
        "profile": f"/@{row['handle']}",
        "disclosure_policy": {
            "can_share": json.loads(row["can_share"]),
            "cannot_share": json.loads(row["cannot_share"]),
            "note": "Private values are never stored on the gateway; they live only in the agent's local config.",
        },
        "created_at": row["created_at"],
    }


def is_friend_or_accepted(c, sender: str, recipient: str) -> bool:
    f1 = c.execute("SELECT 1 FROM follows WHERE follower=? AND followee=?", (sender, recipient)).fetchone()
    f2 = c.execute("SELECT 1 FROM follows WHERE follower=? AND followee=?", (recipient, sender)).fetchone()
    if f1 and f2:
        return True
    acc = c.execute("SELECT 1 FROM accepted WHERE owner=? AND guest=?", (recipient, sender)).fetchone()
    return acc is not None


def fire_webhook(url: str, payload: dict) -> str:
    """尽力唤醒；失败只记录，绝不阻塞消息投递。"""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return f"ok:{resp.status}"
    except Exception as e:  # noqa: BLE001
        return f"failed:{type(e).__name__}"


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentLinkStarter/1.0"

    def log_message(self, fmt, *args):
        sys.stdout.write(f"[{now_iso()}] {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()

    # ---------- helpers ----------
    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode())
        except Exception:
            return {}

    def _auth_agent(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        th = hash_token(auth[7:].strip())
        with db() as c:
            return c.execute("SELECT * FROM agents WHERE token_hash=?", (th,)).fetchone()

    def _require_auth(self):
        row = self._auth_agent()
        if row is None:
            self._json(401, {"error": "missing or invalid bearer token"})
        return row

    def _panel_key_ok(self, supplied):
        panel_key = os.environ.get("AGENTLINK_PANEL_KEY")
        return bool(panel_key) and supplied == panel_key

    def _serve_file(self, filename, content_type, template_gateway=False):
        fpath = os.path.join(BASE_DIR, filename)
        if not os.path.exists(fpath):
            return self._json(404, {"error": f"{filename} not available on this gateway"})
        with open(fpath, "rb") as f:
            data = f.read()
        if template_gateway:
            base = os.environ.get("AGENTLINK_PUBLIC_BASE") or f"http://{self.headers.get('Host', HOST + ':' + str(PORT))}"
            data = data.decode("utf-8").replace("{{GATEWAY}}", base).encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/panel")
            self.end_headers()
            return
        if path == "/setup.md":
            return self._serve_file("SETUP.md", "text/markdown; charset=utf-8", template_gateway=True)
        if path == "/client.py":
            return self._serve_file("client.py", "text/x-python; charset=utf-8")
        if path == "/panel":
            return self._serve_file("panel.html", "text/html; charset=utf-8")
        if path == "/health":
            return self._json(200, {"ok": True, "service": "agentlink-gateway", "time": now_iso()})
        if path == "/api/messages":
            q = parse_qs(urlparse(self.path).query)
            if not self._panel_key_ok((q.get("key") or [""])[0]):
                return self._json(403, {"error": "panel key required"})
            try:
                after = int((q.get("after_id") or ["0"])[0] or 0)
            except ValueError:
                after = 0
            with db() as c:
                rows = c.execute(
                    "SELECT id,sender,recipient,body,reply_to,box,created_at,"
                    "(attachment_name IS NOT NULL) AS has_att FROM messages "
                    "WHERE id>? ORDER BY id", (after,)).fetchall()
                agents = c.execute("SELECT handle,display_name FROM agents ORDER BY created_at").fetchall()
                know = c.execute("SELECT id,author,kind,title,updated_at FROM knowledge ORDER BY id").fetchall()
            return self._json(200, {"messages": [dict(r) for r in rows],
                                    "agents": [dict(a) for a in agents],
                                    "knowledge": [dict(k) for k in know],
                                    "time": now_iso()})
        if path == "/api/stream":
            q = parse_qs(urlparse(self.path).query)
            if not self._panel_key_ok((q.get("key") or [""])[0]):
                return self._json(403, {"error": "panel key required"})
            sub = _queue_mod.Queue(maxsize=100)
            with _subs_lock:
                _subs.add(sub)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = sub.get(timeout=20)
                    except _queue_mod.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with _subs_lock:
                    _subs.discard(sub)
            return
        if path.startswith("/@") and len(path) > 2:
            handle = path[2:]
            with db() as c:
                row = c.execute("SELECT * FROM agents WHERE handle=?", (handle,)).fetchone()
            if not row:
                return self._json(404, {"error": "no such agent"})
            return self._json(200, public_card(row))
        if path == "/agents":
            with db() as c:
                rows = c.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
            return self._json(200, {"agents": [public_card(r) for r in rows]})
        if path.startswith("/wake/") and len(path) > 6:
            # 公开唤醒计数源：只给数量和最新消息 id，永不给内容。
            # Agent 侧哨兵轮询这个小接口，id 变了才唤醒大脑拉全量收件箱。
            handle = path[6:].strip().lower().lstrip("@")
            with db() as c:
                row = c.execute("SELECT 1 FROM agents WHERE handle=?", (handle,)).fetchone()
                if not row:
                    return self._json(404, {"error": "no such agent"})
                agg = c.execute(
                    "SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS latest FROM messages "
                    "WHERE recipient=? AND box='inbox'", (handle,)).fetchone()
                req = c.execute(
                    "SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS latest FROM messages "
                    "WHERE recipient=? AND box='requests'", (handle,)).fetchone()
            return self._json(200, {"handle": handle,
                                    "inbox_count": agg["n"], "inbox_latest_id": agg["latest"],
                                    "requests_count": req["n"], "requests_latest_id": req["latest"],
                                    "time": now_iso()})
        if path.startswith("/knowledge/"):
            # 知识包拉取：标题人人可见；正文只给本人、好友、已接受过的对象。
            target = path[len("/knowledge/"):].strip().lower().lstrip("@")
            viewer = self._auth_agent()
            with db() as c:
                t = c.execute("SELECT 1 FROM agents WHERE handle=?", (target,)).fetchone()
                if not t:
                    return self._json(404, {"error": "no such agent"})
                rows = c.execute(
                    "SELECT id,author,kind,title,body,updated_at FROM knowledge "
                    "WHERE author=? ORDER BY id", (target,)).fetchall()
                can_read = False
                if viewer is not None:
                    can_read = viewer["handle"] == target or is_friend_or_accepted(c, target, viewer["handle"])
            items = []
            for r in rows:
                d = dict(r)
                if not can_read:
                    d["body"] = None
                    d["locked"] = True
                items.append(d)
            return self._json(200, {"author": target, "can_read_bodies": can_read, "items": items})
        if path in ("/inbox", "/requests", "/friends", "/policy"):
            me = self._require_auth()
            if me is None:
                return
            handle = me["handle"]
            with db() as c:
                if path in ("/inbox", "/requests"):
                    box = "inbox" if path == "/inbox" else "requests"
                    rows = c.execute(
                        "SELECT * FROM messages WHERE recipient=? AND box=? ORDER BY id",
                        (handle, box),
                    ).fetchall()
                    msgs = [dict(r) for r in rows]
                    if path == "/inbox":
                        for m in msgs:
                            if m.get("attachment_b64"):
                                m["attachment_bytes"] = len(base64.b64decode(m["attachment_b64"]))
                    return self._json(200, {"messages": msgs})
                if path == "/friends":
                    rows = c.execute(
                        """SELECT f.followee AS h FROM follows f
                           JOIN follows r ON r.follower=f.followee AND r.followee=f.follower
                           WHERE f.follower=?""",
                        (handle,),
                    ).fetchall()
                    friends = [r["h"] for r in rows]
                    acc = c.execute("SELECT guest FROM accepted WHERE owner=?", (handle,)).fetchall()
                    return self._json(200, {"handle": handle, "friends_mutual": friends,
                                            "accepted_senders": [r["guest"] for r in acc]})
                if path == "/policy":
                    return self._json(200, {"handle": handle,
                                            "can_share": json.loads(me["can_share"]),
                                            "cannot_share": json.loads(me["cannot_share"]),
                                            "webhook_url": me["webhook_url"]})
        return self._json(404, {"error": "not found"})

    # ---------- POST ----------
    def _owner_row(self):
        """按 token 文件找到主人身份行（面板专用，token 不出服务器）。"""
        tok_path = os.environ.get(
            "AGENTLINK_OWNER_TOKEN_FILE",
            os.path.join(BASE_DIR, "data", "owner_token.txt"))
        try:
            with open(tok_path) as f:
                owner_token = f.read().strip()
        except OSError:
            return None
        if not owner_token:
            return None
        with db() as c:
            return c.execute("SELECT * FROM agents WHERE handle=? AND token_hash=?",
                             (OWNER_HANDLE, hash_token(owner_token))).fetchone()

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/register":
            if os.environ.get("AGENTLINK_REGISTRATION", "open") == "closed":
                return self._json(403, {"error": "registration is closed on this gateway"})
            handle = (body.get("handle") or "").strip().lower()
            display = (body.get("display_name") or handle).strip()
            if not re.fullmatch(r"[a-z0-9_]{2,32}", handle or ""):
                return self._json(400, {"error": "handle must be 2-32 chars of [a-z0-9_]"})
            token = secrets.token_urlsafe(32)
            agent_id = "ag_" + secrets.token_hex(6)
            with _lock, db() as c:
                exists = c.execute("SELECT 1 FROM agents WHERE handle=?", (handle,)).fetchone()
                if exists:
                    return self._json(409, {"error": f"handle @{handle} already taken"})
                c.execute(
                    "INSERT INTO agents(agent_id,handle,display_name,token_hash,webhook_url,can_share,cannot_share,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (agent_id, handle, display, hash_token(token), body.get("webhook_url"),
                     json.dumps(body.get("can_share") or [], ensure_ascii=False),
                     json.dumps(body.get("cannot_share") or [], ensure_ascii=False),
                     now_iso()),
                )
            return self._json(201, {"agent_id": agent_id, "handle": handle, "display_name": display,
                                    "token": token, "profile": f"/@{handle}",
                                    "note": "Token is shown once. Gateway stores only its SHA-256 hash."})

        if path in ("/api/send", "/api/accept"):
            # 面板操作：面板密钥把关，动作以主人身份执行。
            if not self._panel_key_ok(body.get("key") or ""):
                return self._json(403, {"error": "panel key required"})
            me = self._owner_row()
            if me is None:
                return self._json(503, {"error": "owner identity not configured"})
            handle = me["handle"]
            if path == "/api/accept":
                try:
                    msg_id = int(body.get("message_id"))
                except (TypeError, ValueError):
                    return self._json(400, {"error": "need message_id"})
                with _lock, db() as c:
                    row = c.execute("SELECT * FROM messages WHERE id=? AND recipient=?",
                                    (msg_id, handle)).fetchone()
                    if not row:
                        return self._json(404, {"error": "no such request message for owner"})
                    c.execute("UPDATE messages SET box='inbox' WHERE id=?", (msg_id,))
                    c.execute("INSERT OR IGNORE INTO accepted(owner,guest) VALUES(?,?)",
                              (handle, row["sender"]))
                _broadcast({"event": "accepted", "message_id": msg_id})
                return self._json(200, {"accepted_message": msg_id,
                                        "sender_now_direct": row["sender"]})
            path = "/send"  # 落入下面的普通发送流程
        else:
            me = self._require_auth()
            if me is None:
                return
            handle = me["handle"]

        if path == "/follow":
            target = (body.get("handle") or "").strip().lower().lstrip("@")
            with _lock, db() as c:
                t = c.execute("SELECT 1 FROM agents WHERE handle=?", (target,)).fetchone()
                if not t:
                    return self._json(404, {"error": "no such agent"})
                c.execute("INSERT OR IGNORE INTO follows(follower,followee) VALUES(?,?)", (handle, target))
                back = c.execute("SELECT 1 FROM follows WHERE follower=? AND followee=?", (target, handle)).fetchone()
            return self._json(200, {"following": target, "friends_now": bool(back)})

        if path == "/webhook":
            with _lock, db() as c:
                c.execute("UPDATE agents SET webhook_url=? WHERE handle=?", (body.get("webhook_url"), handle))
            return self._json(200, {"webhook_url": body.get("webhook_url")})

        if path == "/policy":
            with _lock, db() as c:
                c.execute("UPDATE agents SET can_share=?, cannot_share=? WHERE handle=?",
                          (json.dumps(body.get("can_share") or [], ensure_ascii=False),
                           json.dumps(body.get("cannot_share") or [], ensure_ascii=False), handle))
            return self._json(200, {"updated": True})

        if path == "/block":
            target = (body.get("handle") or "").strip().lower().lstrip("@")
            with _lock, db() as c:
                c.execute("INSERT OR IGNORE INTO blocks(owner,blocked) VALUES(?,?)", (handle, target))
                c.execute("UPDATE messages SET box='blocked' WHERE recipient=? AND sender=? AND box='requests'",
                          (handle, target))
            return self._json(200, {"blocked": target})

        m = re.fullmatch(r"/requests/(\d+)/accept", path)
        if m:
            msg_id = int(m.group(1))
            with _lock, db() as c:
                row = c.execute("SELECT * FROM messages WHERE id=? AND recipient=?", (msg_id, handle)).fetchone()
                if not row:
                    return self._json(404, {"error": "no such request message for you"})
                c.execute("UPDATE messages SET box='inbox' WHERE id=?", (msg_id,))
                c.execute("INSERT OR IGNORE INTO accepted(owner,guest) VALUES(?,?)", (handle, row["sender"]))
            _broadcast({"event": "accepted", "message_id": msg_id})
            return self._json(200, {"accepted_message": msg_id, "sender_now_direct": row["sender"]})

        if path == "/knowledge":
            kind = (body.get("kind") or "note").strip().lower()
            title = (body.get("title") or "").strip()
            text = body.get("body") or ""
            if kind not in KNOWLEDGE_KINDS:
                return self._json(400, {"error": f"kind must be one of {KNOWLEDGE_KINDS}"})
            if not title or not text:
                return self._json(400, {"error": "need 'title' and 'body'"})
            with _lock, db() as c:
                old = c.execute("SELECT id FROM knowledge WHERE author=? AND kind=? AND title=?",
                                (handle, kind, title)).fetchone()
                if old:
                    c.execute("UPDATE knowledge SET body=?, updated_at=? WHERE id=?",
                              (text, now_iso(), old["id"]))
                    kid = old["id"]
                else:
                    cur = c.execute(
                        "INSERT INTO knowledge(author,kind,title,body,updated_at) VALUES(?,?,?,?,?)",
                        (handle, kind, title, text, now_iso()))
                    kid = cur.lastrowid
            _broadcast({"event": "knowledge", "id": kid, "author": handle, "kind": kind})
            return self._json(201, {"knowledge_id": kid, "author": handle, "kind": kind, "title": title})

        if path in ("/send", "/reply"):
            to = (body.get("to") or "").strip().lower().lstrip("@")
            text = body.get("body") or ""
            if not to or not text:
                return self._json(400, {"error": "need 'to' and 'body'"})
            att_name, att_b64 = body.get("attachment_name"), body.get("attachment_b64")
            if att_b64:
                try:
                    if len(base64.b64decode(att_b64)) > MAX_ATTACH_BYTES:
                        return self._json(413, {"error": "attachment too large (>64KB)"})
                except Exception:
                    return self._json(400, {"error": "attachment_b64 is not valid base64"})
            reply_to = body.get("reply_to")
            with _lock, db() as c:
                recip = c.execute("SELECT * FROM agents WHERE handle=?", (to,)).fetchone()
                if not recip:
                    return self._json(404, {"error": "no such recipient"})
                blocked = c.execute("SELECT 1 FROM blocks WHERE owner=? AND blocked=?", (to, handle)).fetchone()
                if blocked:
                    return self._json(403, {"error": "recipient has blocked you"})
                direct = is_friend_or_accepted(c, handle, to)
                box = "inbox" if direct else "requests"
                cur = c.execute(
                    "INSERT INTO messages(sender,recipient,body,reply_to,attachment_name,attachment_b64,box,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (handle, to, text, reply_to, att_name, att_b64, box, now_iso()),
                )
                msg_id = cur.lastrowid
                webhook_url = recip["webhook_url"]
            webhook_result = None
            if box == "inbox" and webhook_url:
                webhook_result = fire_webhook(webhook_url, {
                    "event": "new_message", "message_id": msg_id, "from": handle, "to": to,
                    "preview": text[:120], "time": now_iso(),
                })
                with _lock, db() as c:
                    c.execute("UPDATE messages SET webhook_result=? WHERE id=?", (webhook_result, msg_id))
            _broadcast({"event": "new_message", "message_id": msg_id,
                        "from": handle, "to": to, "box": box})
            return self._json(201, {"message_id": msg_id, "from": handle, "to": to,
                                    "delivered_to": box, "webhook": webhook_result})

        return self._json(404, {"error": "not found"})


def main():
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[{now_iso()}] AgentLink gateway listening on http://{HOST}:{PORT} db={DB_PATH}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
