"""Persist claims before sending. An interrupted claim remains uncertain for review."""
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from http.client import HTTPException

SCHEMA = """CREATE TABLE IF NOT EXISTS telegram_outbox (
    batch_id TEXT PRIMARY KEY, summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    message_id INTEGER, detail TEXT)"""


def deliver(db, token, chat, checkpoint, send=urlopen):
    rows = db.execute("SELECT batch_id,summary FROM telegram_outbox WHERE status='pending' ORDER BY rowid").fetchall()
    if not rows:
        return
    failed = False
    for key, summary in rows:
        with db:
            claim = db.execute("""UPDATE telegram_outbox SET status='uncertain', detail='delivery claimed'
                WHERE batch_id=? AND status='pending'""", (key,))
        if not claim.rowcount:
            continue
        checkpoint()
        message_id = None
        try:
            with send(f"https://api.telegram.org/bot{token}/sendMessage",
                      data=urlencode(dict(chat_id=chat, text=summary)).encode(), timeout=15) as response:
                payload = json.load(response)
            if payload.get("ok") is False:
                status, detail = "pending", "Telegram rejected request"
            else:
                message_id = int(payload["result"]["message_id"])
                status, detail = "sent", "acknowledged"
        except HTTPError as exc:
            status = "pending" if 400 <= exc.code < 500 else "uncertain"
            detail = f"HTTP {exc.code}"
        except (OSError, HTTPException, ValueError, KeyError, TypeError) as exc:
            status, detail = "uncertain", type(exc).__name__
        with db:
            db.execute("UPDATE telegram_outbox SET status=?,detail=?,message_id=? WHERE batch_id=?",
                       (status, detail, message_id, key))
        failed |= status != "sent"
        checkpoint()
    if failed:
        raise RuntimeError("Telegram delivery incomplete; inspect telegram_outbox")
