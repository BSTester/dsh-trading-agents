"""PIT SQLite 存储层（规格 §四）。三条硬规则在本层强制：

  1. PIT 纪律 —— read_bars/read_fundamentals/read_universe/read_adjustments 必须显式
     as_of，缺省直接拒绝（ValueError），不存在"全量读"口子；
  2. 复权统一 —— bars 一律存原始价，复权因子在 adjustments 表，口径查询时派生；
  3. 宁缺毋假 —— 本层不生成占位行；announced_at 未合并的基本面行对 PIT 读取不可见。

连接约定：连接由调用方负责关闭；默认不可跨线程共享（sqlite3 默认 check_same_thread）；
写函数即写即提交，外部无法编排多语句事务（单进程 CLI 场景，规格 §四既定）。
"""
import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2  # v2：+valuations（WP2 估值因子按日落库）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars(
  symbol TEXT NOT NULL, period TEXT NOT NULL, ts TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL NOT NULL, source TEXT NOT NULL, adj_factor REAL NOT NULL DEFAULT 1.0, -- 预留列，勿写入：复权唯一事实来源是 adjustments 表
  PRIMARY KEY(symbol, period, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS adjustments(
  symbol TEXT NOT NULL, ex_date TEXT NOT NULL,
  cum_forward REAL, cum_backward REAL, actions TEXT, source TEXT NOT NULL,
  PRIMARY KEY(symbol, ex_date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fundamentals(
  symbol TEXT NOT NULL, field TEXT NOT NULL, period_end TEXT NOT NULL,
  announced_at TEXT, announced_source TEXT, value REAL NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY(symbol, field, period_end)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS universe(
  snapshot_date TEXT NOT NULL, symbol TEXT NOT NULL, index_name TEXT NOT NULL,
  bias_note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
  PRIMARY KEY(snapshot_date, symbol, index_name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS calendar(
  market TEXT NOT NULL, day TEXT NOT NULL, trade_date_type TEXT, trade_second INTEGER,
  PRIMARY KEY(market, day)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS valuations(
  symbol TEXT NOT NULL, day TEXT NOT NULL, field TEXT NOT NULL,
  value REAL NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY(symbol, day, field)) WITHOUT ROWID;
"""


def db_path(dsh_home=None):
    import os
    home = Path(dsh_home or os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    return home / "trading-data" / "trading.sqlite"


def connect(path=None):
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn):
    conn.executescript(_SCHEMA)
    row = conn.execute("PRAGMA user_version").fetchone()[0]
    if row < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def _require_as_of(as_of):
    if not as_of:
        raise ValueError("PIT 纪律：读取必须显式提供 as_of（规格 §4.2 规则 1）")
    return as_of


def upsert_bars(conn, symbol, period, bars, source):
    rows = [(symbol, period, b["t"], b["o"], b["h"], b["l"], b["c"], b.get("v") or 0.0, source)
            for b in bars]
    conn.executemany(
        "INSERT OR REPLACE INTO bars(symbol,period,ts,open,high,low,close,volume,source)"
        " VALUES(?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def read_bars(conn, symbol, period, as_of, limit=None):
    _require_as_of(as_of)
    sql = "SELECT ts,open,high,low,close,volume,source FROM bars" \
          " WHERE symbol=? AND period=? AND ts<=? ORDER BY ts DESC"
    params = [symbol, period, as_of]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    rows.reverse()  # 升序返回，与 load_bars 口径一致
    return [{"t": r["ts"], "o": r["open"], "h": r["high"], "l": r["low"],
             "c": r["close"], "v": r["volume"], "source": r["source"]} for r in rows]


def last_bar_date(conn, symbol, period):
    row = conn.execute("SELECT MAX(ts) FROM bars WHERE symbol=? AND period=?",
                       (symbol, period)).fetchone()
    return row[0]


def upsert_adjustments(conn, symbol, rows, source):
    params = [(symbol, r["ex_date"], r.get("cum_forward"), r.get("cum_backward"),
               json.dumps(r.get("actions") or [], ensure_ascii=False), source) for r in rows]
    conn.executemany("INSERT OR REPLACE INTO adjustments"
                     "(symbol,ex_date,cum_forward,cum_backward,actions,source)"
                     " VALUES(?,?,?,?,?,?)", params)
    conn.commit()
    return len(params)


def read_adjustments(conn, symbol, as_of):
    _require_as_of(as_of)
    rows = conn.execute(
        "SELECT ex_date,cum_forward,cum_backward,actions FROM adjustments"
        " WHERE symbol=? AND ex_date<=? ORDER BY ex_date", (symbol, as_of)).fetchall()
    return [{"ex_date": r["ex_date"], "cum_forward": r["cum_forward"],
             "cum_backward": r["cum_backward"], "actions": json.loads(r["actions"] or "[]")}
            for r in rows]


def upsert_fundamentals(conn, symbol, rows, source):
    params = [(symbol, r["field"], r["period_end"], r.get("announced_at"),
               r.get("announced_source"), r["value"], source) for r in rows]
    # 冲突只刷新 value/source：announced_at/announced_source 一经合并，
    # 重跑 sync_fundamentals 不得打回 NULL（新行仍为 NULL，宁缺毋假不受影响）
    conn.executemany(
        "INSERT INTO fundamentals(symbol,field,period_end,announced_at,announced_source,value,source)"
        " VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(symbol,field,period_end) DO UPDATE SET value=excluded.value,"
        " source=excluded.source", params)
    conn.commit()
    return len(params)


def set_announced_at(conn, symbol, period_end, announced_at, source):
    cur = conn.execute(
        "UPDATE fundamentals SET announced_at=?, announced_source=?"
        " WHERE symbol=? AND period_end=? AND announced_at IS NULL",
        (announced_at, source, symbol, period_end))
    conn.commit()
    return cur.rowcount


def read_fundamentals(conn, symbol, as_of, field=None):
    _require_as_of(as_of)
    sql = ("SELECT field,period_end,announced_at,announced_source,value,source FROM fundamentals"
           " WHERE symbol=? AND announced_at IS NOT NULL AND announced_at<=?")
    params = [symbol, as_of]
    if field:
        sql += " AND field=?"
        params.append(field)
    rows = conn.execute(sql + " ORDER BY period_end", params).fetchall()
    return [{"field": r["field"], "period_end": r["period_end"],
             "announced_at": r["announced_at"], "announced_source": r["announced_source"],
             "value": r["value"], "source": r["source"]} for r in rows]


def announced_coverage(conn):
    """按市场前缀统计 announced_at 覆盖率（规格 §13.5 验收口径）。"""
    out = {}
    rows = conn.execute(
        "SELECT substr(symbol,1,instr(symbol,'.')-1) AS mkt, COUNT(*) AS total,"
        " SUM(announced_at IS NOT NULL) AS with_date FROM fundamentals GROUP BY mkt").fetchall()
    for r in rows:
        out[r["mkt"] or "?"] = {"total": r["total"], "with_date": r["with_date"] or 0}
    return out


def store_universe(conn, as_of, index_name, symbols, source, bias_note=""):
    params = [(as_of, sym, index_name, bias_note, source) for sym in symbols]
    conn.executemany("INSERT OR REPLACE INTO universe"
                     "(snapshot_date,symbol,index_name,bias_note,source)"
                     " VALUES(?,?,?,?,?)", params)
    conn.commit()
    return len(params)


def read_universe(conn, as_of, index_name):
    _require_as_of(as_of)
    row = conn.execute("SELECT MAX(snapshot_date) FROM universe WHERE index_name=? AND snapshot_date<=?",
                       (index_name, as_of)).fetchone()
    if not row or not row[0]:
        return None
    snap_date = row[0]
    rows = conn.execute("SELECT symbol,bias_note FROM universe WHERE index_name=? AND snapshot_date=?",
                        (index_name, snap_date)).fetchall()
    return {"snapshot_date": snap_date,
            "symbols": [r["symbol"] for r in rows],
            "bias_note": rows[0]["bias_note"] if rows else ""}


def upsert_calendar(conn, market, days):
    params = [(market.upper(), d["day"], d.get("trade_date_type"), d.get("trade_second"))
              for d in days]
    conn.executemany("INSERT OR REPLACE INTO calendar(market,day,trade_date_type,trade_second)"
                     " VALUES(?,?,?,?)", params)
    conn.commit()
    return len(params)


def _require_calendar(conn, market):
    n = conn.execute("SELECT COUNT(*) FROM calendar WHERE market=?", (market.upper(),)).fetchone()[0]
    if not n:
        raise RuntimeError(f"日历未同步：{market}（先跑 python -m trading_core calendar）")


def is_trading_day(conn, market, day):
    _require_calendar(conn, market)
    row = conn.execute("SELECT 1 FROM calendar WHERE market=? AND day=?"
                       " AND trade_date_type IS NOT NULL AND trade_date_type!='CLOSE'",
                       (market.upper(), day)).fetchone()
    return row is not None


def trading_days(conn, market, start, end):
    _require_calendar(conn, market)
    rows = conn.execute("SELECT day FROM calendar WHERE market=? AND day>=? AND day<=?"
                        " AND trade_date_type IS NOT NULL AND trade_date_type!='CLOSE'"
                        " ORDER BY day", (market.upper(), start, end)).fetchall()
    return [r["day"] for r in rows]


def kv_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def upsert_valuations(conn, symbol, day, fields, source):
    """估值因子按日落库（schema v2）：{field: value}，None 字段不入库（宁缺毋假）。"""
    params = [(symbol, day, k, float(v), source) for k, v in fields.items() if v is not None]
    conn.executemany("INSERT OR REPLACE INTO valuations(symbol,day,field,value,source)"
                     " VALUES(?,?,?,?,?)", params)
    conn.commit()
    return len(params)


def read_valuations(conn, symbol, as_of):
    """PIT 读取：返回 as_of 当日（含）最近一个落库日的 {field: value}；无任何记录返回 {}。"""
    _require_as_of(as_of)
    rows = conn.execute(
        "SELECT field, value FROM valuations WHERE symbol=? AND day="
        " (SELECT MAX(day) FROM valuations WHERE symbol=? AND day<=?)",
        (symbol, symbol, as_of)).fetchall()
    return {r["field"]: r["value"] for r in rows}
