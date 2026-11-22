"""Telegram setup check for gss-wire. Run by the 'Telegram setup' workflow.

Never prints the token. Reports whether Telegram accepts it, lists chat ids that
have messaged the bot, and sends a test message if TELEGRAM_CHAT_ID is set.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request


def clean_token(raw):
    t = raw.strip().strip("<>").strip()
    if t.lower().startswith("bot") and ":" in t and t[3:4].isdigit():
        t = t[3:]  # pasted with the 'bot' prefix
    return t


def describe(raw, t):
    notes = [f"length {len(t)} (a real token is usually 44-46)"]
    if raw != raw.strip():
        notes.append("had spaces or line breaks at the ends (removed)")
    if t.count(":") != 1:
        notes.append(f"has {t.count(':')} colons (should be exactly 1)")
    else:
        left, right = t.split(":")
        notes.append(f"part before the colon is {'all digits' if left.isdigit() else 'NOT all digits'} ({len(left)} chars)")
        notes.append(f"part after the colon is {len(right)} chars (should be about 35)")
        if re.search(r"[^A-Za-z0-9_-]", right):
            notes.append("part after the colon has unexpected characters (spaces, quotes or punctuation)")
    return notes


def call(token, method, payload=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error_code": e.code, "description": str(e)}


def main():
    raw = os.environ.get("TELEGRAM_TOKEN", "")
    if not raw.strip():
        print("RESULT: TELEGRAM_TOKEN secret is missing or empty.")
        return 1
    token = clean_token(raw)
    print("Token shape:", "; ".join(describe(raw, token)))

    me = call(token, "getMe")
    if not me.get("ok"):
        print(f"RESULT: Telegram rejected the token ({me.get('error_code')} {me.get('description')}).")
        return 1
    print(f"RESULT: token OK, bot is @{me['result'].get('username')}")

    upd = call(token, "getUpdates")
    chats = {}
    for u in upd.get("result", []) if upd.get("ok") else []:
        msg = u.get("message") or u.get("edited_message") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if chats:
        for cid, name in chats.items():
            print(f"CHAT_ID: {cid}  ({name})")
    else:
        print("CHAT_ID: none found yet. Open the bot in Telegram, press Start, send 'hi', then run this again.")

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if chat_id:
        sent = call(token, "sendMessage", {"chat_id": chat_id, "text": "gss-wire test: if this is on your lock screen, alerts are working."})
        if sent.get("ok"):
            print("TEST MESSAGE: sent")
        else:
            print(f"TEST MESSAGE: failed ({sent.get('error_code')} {sent.get('description')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
