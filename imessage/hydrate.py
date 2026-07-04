"""Hydrate offloaded attachments by driving Messages.app scrollback.

Messages-in-iCloud offloads old attachments to Apple's private CloudKit store —
no API or CLI can fetch them; only Messages.app downloads them, lazily, as the
conversation scrolls into view. This helper automates exactly that: you open the
conversation and this script holds Page Up while watching the local attachment
count grow, stopping when history stops yielding.

    imsg hydrate 512 519 638

Needs: Messages.app open with the conversation selected, the Mac unlocked, and
Terminal granted Accessibility permission (System Settings → Privacy & Security
→ Accessibility). Only what iCloud still holds can come back. Stop with Ctrl-C;
progress is inherently resumable — downloaded files stay downloaded.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from pathlib import Path

_PAGE_UP = ('tell application "Messages" to activate\n'
            'tell application "System Events" to key code 116')


def _local_count(db_path, chat_ids) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    placeholders = ",".join("?" * len(chat_ids))
    rows = con.execute(
        f"SELECT a.filename FROM attachment a "
        f"JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID "
        f"JOIN chat_message_join cmj ON cmj.message_id = maj.message_id "
        f"WHERE cmj.chat_id IN ({placeholders})", chat_ids).fetchall()
    con.close()
    return sum(1 for (f,) in rows
               if f and os.path.exists(os.path.expanduser(f)))


def hydrate(chat_ids, db_path=None, stall_minutes=5) -> None:
    db_path = db_path or (Path.home() / "Library/Messages/chat.db")
    start = last_gain = _local_count(db_path, chat_ids)
    last_gain_at = time.time()
    print(f"[hydrate] {start} attachments currently local for chats {chat_ids}.")
    print("[hydrate] click the conversation in Messages.app — scrolling starts in 5s "
          "(Ctrl-C to stop)…", flush=True)
    time.sleep(5)
    pages = 0
    try:
        while True:
            subprocess.run(["osascript", "-e", _PAGE_UP], capture_output=True)
            pages += 1
            time.sleep(0.5)
            if pages % 40 == 0:                       # ~every 20s, check the disk
                now = _local_count(db_path, chat_ids)
                if now > last_gain:
                    last_gain, last_gain_at = now, time.time()
                print(f"\r[hydrate] {pages} page-ups · {now} local (+{now - start})",
                      end="", flush=True)
                if time.time() - last_gain_at > stall_minutes * 60:
                    print(f"\n[hydrate] no new attachments for {stall_minutes} min — "
                          "top of history (or iCloud has no more). Done.")
                    break
    except KeyboardInterrupt:
        pass
    final = _local_count(db_path, chat_ids)
    print(f"\n[hydrate] finished: {final} local (+{final - start} recovered). "
          "Re-run `atlas caption` / `atlas faces` / `atlas extract` to fold them in.")
