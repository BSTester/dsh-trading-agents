"""TCA（规格 §6.3）：到达价 vs 成交价，滑点 bps，按标的/日聚合。"""
import datetime as dt
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tca(
  client_order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, day TEXT NOT NULL,
  arrival REAL NOT NULL, filled REAL NOT NULL, side TEXT NOT NULL,
  bps REAL NOT NULL, created_at TEXT NOT NULL);
"""


def slippage_bps(arrival, filled, side):
    direction = 1 if side == "BUY" else -1
    return (filled - arrival) / arrival * 10000 * direction


def _ensure(conn):
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("需要 sqlite 连接")
    conn.executescript(SCHEMA)


def record(conn, client_order_id, symbol, arrival, filled, side):
    _ensure(conn)
    bps = slippage_bps(arrival, filled, side)
    day = dt.date.today().isoformat()
    conn.execute("INSERT OR REPLACE INTO tca VALUES(?,?,?,?,?,?,?,?)",
                 (client_order_id, symbol, day, arrival, filled, side, bps,
                  dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return bps


def aggregate(conn):
    _ensure(conn)
    row = conn.execute("SELECT COUNT(*) AS n, AVG(bps) AS avg FROM tca").fetchone()
    return {"count": row["n"], "avg_bps": round(row["avg"] or 0.0, 2)}
