"""Who may use the bot and who gets alerts.

The owner (TELEGRAM_CHAT_ID) is the admin and always has access. Anyone else
who writes to the bot becomes a 'pending' request the owner approves or denies
with a button; approved users get the viewer menu and the alerts (each can mute
their own). Stored in history.db next to everything else (gitignored, survives
deploys).
"""
import os, time

import history

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_users (
    tg_id TEXT PRIMARY KEY, name TEXT, status TEXT NOT NULL,   -- pending | active | denied
    alerts INTEGER NOT NULL DEFAULT 1, since INTEGER);
"""


def owner():
    return str(os.environ.get("TELEGRAM_CHAT_ID", ""))


def connect(path=None):
    conn = history.connect(path)
    conn.executescript(SCHEMA)
    return conn


def status(conn, tg_id):
    if str(tg_id) == owner():
        return "admin"
    r = conn.execute("SELECT status FROM bot_users WHERE tg_id=?", (str(tg_id),)).fetchone()
    return r[0] if r else None


def request(conn, tg_id, name):
    """New request → True (notify the owner); repeat/denied/active → False."""
    if status(conn, tg_id):
        return False
    conn.execute("INSERT INTO bot_users (tg_id, name, status, since) VALUES (?,?,?,?)",
                 (str(tg_id), name, "pending", int(time.time())))
    conn.commit()
    return True


def set_status(conn, tg_id, st):
    conn.execute("UPDATE bot_users SET status=?, since=? WHERE tg_id=?", (st, int(time.time()), str(tg_id)))
    conn.commit()


def remove(conn, tg_id):
    """Removed users may ask again later (unlike denied, who are ignored)."""
    conn.execute("DELETE FROM bot_users WHERE tg_id=?", (str(tg_id),))
    conn.commit()


def toggle_alerts(conn, tg_id):
    conn.execute("UPDATE bot_users SET alerts = 1 - alerts WHERE tg_id=?", (str(tg_id),))
    conn.commit()
    return bool(conn.execute("SELECT alerts FROM bot_users WHERE tg_id=?", (str(tg_id),)).fetchone()[0])


def alerts_on(conn, tg_id):
    r = conn.execute("SELECT alerts FROM bot_users WHERE tg_id=?", (str(tg_id),)).fetchone()
    return bool(r[0]) if r else True


def listing(conn):
    return conn.execute("SELECT tg_id, name, status, alerts, since FROM bot_users "
                        "WHERE status IN ('active','pending') ORDER BY status, since").fetchall()


def recipients(conn=None):
    """Owner + active users with alerts on."""
    own = conn is None
    conn = conn or connect()
    ids = [r[0] for r in conn.execute("SELECT tg_id FROM bot_users WHERE status='active' AND alerts=1")]
    if own:
        conn.close()
    return [o for o in [owner()] if o] + [i for i in ids if i != owner()]


def selftest():
    os.environ["TELEGRAM_CHAT_ID"] = "1"
    c = connect(":memory:")
    assert status(c, 1) == "admin" and status(c, 2) is None
    assert request(c, 2, "Вася") and not request(c, 2, "Вася")      # second /start doesn't re-notify
    assert status(c, 2) == "pending" and recipients(c) == ["1"]      # pending gets nothing
    set_status(c, 2, "active")
    assert recipients(c) == ["1", "2"]
    assert toggle_alerts(c, 2) is False and recipients(c) == ["1"]   # muted
    toggle_alerts(c, 2)
    request(c, 3, "Спамер"); set_status(c, 3, "denied")
    assert not request(c, 3, "Спамер")                               # denied stays denied
    assert [r[0] for r in listing(c)] == ["2"]
    remove(c, 2)
    assert status(c, 2) is None and request(c, 2, "Вася")            # removed may ask again
    print("users selftest ok")


if __name__ == "__main__":
    selftest()
