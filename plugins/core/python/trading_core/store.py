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
from datetime import date, timedelta
from pathlib import Path

SCHEMA_VERSION = 4  # WP2 预留 2；WP3 落 3（plans/orders/fills/risk_checks 四表）；
# WP9 落 4：plans 幂等追加 origin/market（ALTER 补列，_SCHEMA 的 CREATE 不改）

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
CREATE TABLE IF NOT EXISTS plans(
  plan_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, mode TEXT NOT NULL,
  strategy_id TEXT NOT NULL, target TEXT NOT NULL, content_hash TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, approved_at TEXT, approved_by TEXT);
CREATE TABLE IF NOT EXISTS orders(
  client_order_id TEXT PRIMARY KEY, plan_id TEXT, symbol TEXT NOT NULL,
  market TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
  price REAL, status TEXT NOT NULL, broker_order_id TEXT, mode TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, err TEXT);
CREATE TABLE IF NOT EXISTS fills(
  fill_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, price REAL NOT NULL,
  qty INTEGER NOT NULL, traded_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, symbol TEXT, rule INTEGER NOT NULL,
  allowed INTEGER NOT NULL, reason TEXT, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, title TEXT NOT NULL,
  detail TEXT, created_at TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS factor_snapshots(
  date TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sentiment_snapshots(
  date TEXT NOT NULL, symbol TEXT NOT NULL, source TEXT NOT NULL,
  payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(date, symbol, source)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS f10_snapshots(
  symbol TEXT NOT NULL, section TEXT NOT NULL, period_end TEXT NOT NULL,
  announced_at TEXT, payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(symbol, section, period_end, fetched_at)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS short_snapshots(
  symbol TEXT NOT NULL, date TEXT NOT NULL, payload TEXT NOT NULL,
  fetched_at TEXT NOT NULL, PRIMARY KEY(symbol, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS plate_snapshots(
  date TEXT NOT NULL, market TEXT NOT NULL, plate_class TEXT NOT NULL,
  payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(date, market, plate_class)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rules(
  rule_id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
  validation TEXT, created_at TEXT NOT NULL, approved_at TEXT, approved_by TEXT);
CREATE TABLE IF NOT EXISTS research_tasks(
  task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, as_of TEXT NOT NULL,
  market TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, result_ref TEXT, err TEXT,
  UNIQUE(kind, as_of, market));
"""

# ---------------------------------------------------------------------------
# v4（WP9）：plans 幂等追加 origin/market 两列——ALTER 补列而非改上面的 CREATE。
# 新建库先 CREATE（无此两列）再由 migrate 的 _add_columns 补齐，与旧库升级路径
# 终态一致；_add_columns 是全局唯一的幂等补列实现，后续 WP 追加列一律复用。
#   origin TEXT NOT NULL DEFAULT 'manual' —— auto 计划与手工计划来源隔离
#     （build_plan 作业写 'auto'；既有 planner/手工路径不传 → 默认 manual，行为不变）；
#   market TEXT —— auto 计划一计划一市场（匹配 per-market 作业链与 exec_at）；
#     手工计划 NULL，兼容现状。
# ---------------------------------------------------------------------------


def _add_columns(conn, table, cols):
    """幂等补列：PRAGMA 检查缺失才 ALTER，已有列零触碰。cols=[(name, decl), ...]。"""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


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
    _add_columns(conn, "plans", [("origin", "TEXT NOT NULL DEFAULT 'manual'"),
                                 ("market", "TEXT")])
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


# ---------------------------------------------------------------------------
# v3：执行闭环（plans/orders/fills/risk_checks，规格 §6）。时间一律 UTC+8 字符串。
# ---------------------------------------------------------------------------

_OPEN_STATES = ("draft", "frozen", "submitting", "submitted", "partial", "unknown")


def _now():
    import datetime as _dt
    tz8 = _dt.timezone(_dt.timedelta(hours=8))
    return _dt.datetime.now(tz8).strftime("%Y-%m-%d %H:%M:%S")


def insert_plan(conn, plan_id, as_of, mode, strategy_id, target, content_hash,
                status="frozen", approved_at=None, approved_by=None):
    conn.execute(
        "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
        "status,created_at,approved_at,approved_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (plan_id, as_of, mode, strategy_id,
         json.dumps(target, ensure_ascii=False), content_hash, status, _now(),
         approved_at, approved_by))
    conn.commit()


def upsert_plan_status(conn, plan_id, status):
    conn.execute("UPDATE plans SET status=? WHERE plan_id=?", (status, plan_id))
    conn.commit()


def get_plan(conn, plan_id):
    row = conn.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise ValueError(f"计划不存在 {plan_id}")
    plan = dict(row)
    plan["target"] = json.loads(plan["target"])
    return plan


def list_plans(conn):
    rows = conn.execute("SELECT * FROM plans ORDER BY created_at, plan_id").fetchall()
    out = []
    for row in rows:
        plan = dict(row)
        plan["target"] = json.loads(plan["target"])
        out.append(plan)
    return out


def list_plans_newest_first(conn):
    """计划按最新在前（端点取数口径的唯一实现）。

    与 ``list_plans`` 的差异只在排序方向：端点（plan/pipeline/reconcile 快照）里的
    「当前计划」都取 ``[0]``，多计划并存时按升序取会拿到最旧的一条。"""
    return list(reversed(list_plans(conn)))


# ---------------------------------------------------------------------------
# WP14：规则（rules）持久化——spec 与状态机的存储侧。
# 状态流转**不**在本层校验：rule_engine.set_rule_status / decide_rule 是唯一入口
# （非法流转在那里报错），本层只做读写与 JSON 序列化。
# ---------------------------------------------------------------------------


def upsert_rule(conn, rule_id, spec, status="candidate", validation=None,
                created_at=None):
    """规则提案落库（幂等覆盖 spec/status/validation）。

    created_at 仅首次写入；approved_at/approved_by 由 rule_engine.decide_rule 维护，
    覆盖 spec 不重置批准痕迹（ON CONFLICT 只更新 spec/status/validation）。
    """
    conn.execute(
        "INSERT INTO rules(rule_id,spec,status,validation,created_at,approved_at,approved_by)"
        " VALUES(?,?,?,?,?,NULL,NULL)"
        " ON CONFLICT(rule_id) DO UPDATE SET spec=excluded.spec, status=excluded.status,"
        " validation=excluded.validation",
        (rule_id, json.dumps(spec, ensure_ascii=False), status,
         None if validation is None else json.dumps(validation, ensure_ascii=False),
         created_at or _now()))
    conn.commit()


def update_rule(conn, rule_id, status=None, validation=None,
                approved_at=None, approved_by=None):
    """字段级更新（状态机的落地写入，由 rule_engine 校验流转后调用）。

    None = 不改该字段；缺失行报错（不静默插入）。validation 为 dict 或 None。
    """
    if conn.execute("SELECT 1 FROM rules WHERE rule_id=?", (rule_id,)).fetchone() is None:
        raise ValueError(f"规则不存在 {rule_id}")
    sets, params = [], []
    for col, val in (("status", status), ("approved_at", approved_at),
                     ("approved_by", approved_by)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if validation is not None:
        sets.append("validation=?")
        params.append(json.dumps(validation, ensure_ascii=False))
    if not sets:
        return
    params.append(rule_id)
    conn.execute(f"UPDATE rules SET {','.join(sets)} WHERE rule_id=?", params)
    conn.commit()


def get_rule(conn, rule_id):
    row = conn.execute("SELECT * FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
    if row is None:
        raise ValueError(f"规则不存在 {rule_id}")
    return _rule_row(row)


def find_rule(conn, rule_id):
    """按 id 查规则，**不存在返回 None**（非抛错口径）。

    与 ``get_rule`` 的分工：``get_rule`` 用于「按 id 操作」（缺失即错误，抛 ValueError），
    ``find_rule`` 用于**分流判定**——「规则不存在 / 存在但未启用 / 已通过」三种情形
    调用方要区别对待，不该用异常做流程控制（WP14 任务 4 的 plan_auto 加载器与
    rules-validate 的提案入口都属这一口径）。
    """
    row = conn.execute("SELECT * FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
    return None if row is None else _rule_row(row)


def get_rules(conn, status=None):
    """规则列表（可按状态过滤），按创建时间与 rule_id 稳定排序。"""
    sql = "SELECT * FROM rules"
    params = ()
    if status is not None:
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY created_at, rule_id"
    return [_rule_row(row) for row in conn.execute(sql, params).fetchall()]


def _rule_row(row):
    rule = dict(row)
    rule["spec"] = json.loads(rule["spec"])
    rule["validation"] = (json.loads(rule["validation"])
                          if rule["validation"] is not None else None)
    return rule


# ---------------------------------------------------------------------------
# WP9：auto 计划（origin='auto'）的查询与过期语义（规格 §4.2/§4.3）。
# ---------------------------------------------------------------------------


def get_latest_auto_plan(conn, market, as_of, status="frozen"):
    """auto_execute 守卫查询：origin=auto 且 market/as_of/status 匹配的最新一条。

    返回 dict（target 已反序列化，口径同 get_plan）；无匹配返回 None。
    排序 created_at DESC + rowid DESC：同秒多条时取最后插入的一条（确定性）。
    """
    row = conn.execute(
        "SELECT * FROM plans WHERE origin='auto' AND market=? AND as_of=? AND status=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (market, as_of, status)).fetchone()
    if row is None:
        return None
    plan = dict(row)
    plan["target"] = json.loads(plan["target"])
    return plan


def cancel_stale_auto_plans(conn, today):
    """过期语义（规格 §4.2）：as_of < today 的 frozen auto 计划置 cancelled。

    只动 origin='auto' 且 status='frozen'——手工计划与其他状态一律不碰
    （跨日计划不可执行，但执行中/已完结的历史保持原状）。沿用写函数即写即提交。

    **订单同步作废（2026-09-16 修订 I2）**：计划冻结时订单已登记为 ``draft``，只改
    计划状态会留下孤儿单（计划 cancelled、订单仍在途），OMS 台账自相矛盾。故经
    ``oms.cancel_pending`` 把未提交订单一并置 cancelled（在途订单不动，留给对账）。
    该口径与工作台 ``cancel_plan`` 指令共用同一实现——两处各写一遍正是缺口的成因。

    返回被置 cancelled 的 plan_id 列表（created_at 升序）；幂等（再跑返回 []）。
    """
    from . import oms  # 惰性：store 是底层模块，跨层调用只在需要时解析
    rows = conn.execute(
        "SELECT plan_id FROM plans WHERE origin='auto' AND status='frozen' AND as_of<?"
        " ORDER BY created_at, rowid", (today,)).fetchall()
    ids = [r["plan_id"] for r in rows]
    if ids:
        conn.executemany("UPDATE plans SET status='cancelled' WHERE plan_id=?",
                         [(pid,) for pid in ids])
        conn.commit()
        for plan_id in ids:
            oms.cancel_pending(conn, plan_id, err="plan_expired")
    return ids


def insert_order(conn, client_order_id, plan_id, symbol, market, side, qty, price,
                 mode, status="draft", broker_order_id=None):
    now = _now()
    conn.execute(
        "INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,price,"
        "status,broker_order_id,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (client_order_id, plan_id, symbol, market, side, int(qty), price, status,
         broker_order_id, mode, now, now))
    conn.commit()


def update_order_status(conn, client_order_id, status, broker_order_id=None, err=None):
    conn.execute(
        "UPDATE orders SET status=?, broker_order_id=COALESCE(?,broker_order_id),"
        " err=COALESCE(?,err), updated_at=? WHERE client_order_id=?",
        (status, broker_order_id, err, _now(), client_order_id))
    conn.commit()


def get_orders_by_plan(conn, plan_id):
    return conn.execute(
        "SELECT * FROM orders WHERE plan_id=? ORDER BY rowid", (plan_id,)).fetchall()


def get_open_orders(conn, plan_id=None):
    sql = ("SELECT * FROM orders WHERE status IN "
           f"({','.join('?' * len(_OPEN_STATES))})")
    params = list(_OPEN_STATES)
    if plan_id is not None:
        sql += " AND plan_id=?"
        params.append(plan_id)
    return conn.execute(sql + " ORDER BY rowid", params).fetchall()


def insert_fill(conn, fill_id, client_order_id, price, qty, traded_at=None):
    conn.execute(
        "INSERT INTO fills(fill_id,client_order_id,price,qty,traded_at,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (fill_id, client_order_id, price, int(qty), traded_at, _now()))
    conn.commit()


def fills_by_order(conn, client_order_id):
    return conn.execute(
        "SELECT * FROM fills WHERE client_order_id=? ORDER BY rowid",
        (client_order_id,)).fetchall()


def insert_risk_check(conn, plan_id, symbol, rule, allowed, reason=""):
    conn.execute(
        "INSERT INTO risk_checks(plan_id,symbol,rule,allowed,reason,checked_at)"
        " VALUES(?,?,?,?,?,?)",
        (plan_id, symbol, int(rule), 1 if allowed else 0, reason, _now()))
    conn.commit()


def risk_checks_by_plan(conn, plan_id):
    return conn.execute(
        "SELECT * FROM risk_checks WHERE plan_id=? ORDER BY id",
        (plan_id,)).fetchall()


def set_halt(conn, active, reason=None):
    kv_set(conn, "halt:active", {"active": bool(active), "reason": reason,
                                 "set_at": _now()})


def halt_state(conn):
    """熔断原始记录 ``{active, reason, set_at}``；未设置返回 None。

    单一读取入口（2026-09-16 修订 I3）：自动执行的守卫与每日 digest 都要显示**原因**
    ——只暴露 bool（``is_halted``）会让自动链路静默停摆（info 级跳过、页面无原因）。
    """
    value = kv_get(conn, "halt:active")
    return value if isinstance(value, dict) else None


def is_halted(conn):
    state = halt_state(conn)
    return bool(state and state.get("active"))


def halt_summary(conn):
    """digest/UI 用的熔断摘要：``{"halted": bool, "halt_reason": str|None}``。"""
    state = halt_state(conn) or {}
    return {"halted": bool(state.get("active")), "halt_reason": state.get("reason")}


def clear_halt(conn):
    set_halt(conn, False, reason=None)


# ---------------------------------------------------------------------------
# WP7：因子快照（服务调度每日收盘收集）。表与 WP4 alerts 同一先例——
# CREATE TABLE IF NOT EXISTS 幂等追加，SCHEMA_VERSION 不动（v3 语义不变）。
# ---------------------------------------------------------------------------


def save_factor_snapshot(conn, date, payload):
    """按日 upsert 因子快照（同日期覆盖）：payload 存 JSON，created_at 恒为本次写入时刻。"""
    conn.execute(
        "INSERT INTO factor_snapshots(date,payload,created_at) VALUES(?,?,?)"
        " ON CONFLICT(date) DO UPDATE SET payload=excluded.payload,"
        " created_at=excluded.created_at",
        (date, json.dumps(payload, ensure_ascii=False), _now()))
    conn.commit()


def list_factor_snapshots(conn, limit=30):
    """倒序（最新在前）返回 [{date, payload(反序列化对象), created_at}]。"""
    rows = conn.execute(
        "SELECT date,payload,created_at FROM factor_snapshots ORDER BY date DESC LIMIT ?",
        (int(limit),)).fetchall()
    return [{"date": r["date"], "payload": json.loads(r["payload"]),
             "created_at": r["created_at"]} for r in rows]


# ---------------------------------------------------------------------------
# WP11：情绪/资讯快照（服务调度每日收集，规格 §6.1）。表与 WP7 factor_snapshots
# 同一先例——CREATE TABLE IF NOT EXISTS 幂等追加，SCHEMA_VERSION 不动。
#
# 与 bars/fundamentals 的 PIT 语义差异（重要）：本表是**观测记录**，不是 bar 序列。
# 一行 = 某日对某标的某渠道的一次抓取，payload 是渠道原文 JSON，**不打分**——
# 打分算法会随模型/口径漂移，原始数据不会（PIT 一致性优先，规格 §6.1）。
# PIT 上界因此是「观测时间」（date <= 查询上界），而不是 bar 的 as_of 对齐；
# 采集作业是当日写入，故不套 _require_as_of（那会挡住正常写路径）。
# ---------------------------------------------------------------------------


def insert_sentiment(conn, date, symbol, source, payload, fetched_at=None):
    """按 (date,symbol,source) upsert：当日重跑幂等覆盖，不产生重复行。

    payload 为渠道原文：str 必须能解析为 JSON（非法直接 ValueError——宁缺毋假），
    dict/list 显式序列化。fetched_at 缺省为写入时刻（观测时点）。
    source 取值由采集侧决定（如 fin_sentiment/fin_news/last30days），本层不设白名单。
    """
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False)
    elif not isinstance(payload, str):
        raise ValueError(
            f"sentiment payload 必须是 JSON 字符串或对象，收到 {type(payload).__name__}")
    try:
        json.loads(payload)
    except ValueError as error:
        raise ValueError(f"sentiment payload 不是合法 JSON：{error}") from None
    conn.execute(
        "INSERT INTO sentiment_snapshots(date,symbol,source,payload,fetched_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(date,symbol,source) DO UPDATE SET"
        " payload=excluded.payload, fetched_at=excluded.fetched_at",
        (date, symbol, source, payload, fetched_at or _now()))
    conn.commit()


def read_sentiments(conn, symbol, limit=30, before=None):
    """倒序（最新在前）返回 [{date,symbol,source,payload(反序列化),fetched_at}]。

    PIT 上界（**含当日**）：``before`` 给定时只返 ``date <= before`` 的观测——闭区间，
    传当日即包含当日已落库的观测；这是研究查询的上界，不得看到未来观测。
    """
    sql = ("SELECT date,symbol,source,payload,fetched_at FROM sentiment_snapshots"
           " WHERE symbol=?")
    params = [symbol]
    if before:
        sql += " AND date<=?"
        params.append(before)
    sql += " ORDER BY date DESC, source LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [{"date": r["date"], "symbol": r["symbol"], "source": r["source"],
             "payload": json.loads(r["payload"]), "fetched_at": r["fetched_at"]}
            for r in rows]


def _market_like(market):
    """市场过滤的符号前缀模式：快照记录的 symbol 一律是带市场前缀的富途符号。"""
    return f"{market}.%"


def sentiment_days(conn, market=None):
    """有记录的不同日期数——「≥250 交易日可提检验申请」演进条款的口径。

    注意这是**累计**口径（允许断档），不是「连续」；连续口径见 sentiment_streak。

    ``market`` 给定时只数**该市场标的**的记录（符号前缀 ``SH.`` 过滤）；缺省（None）是
    全市场累计。两个范围都允许断档，展示时必须标明是哪一种——流程页按市场取数，
    ``sentiment_summary`` 的 ``days`` 保持全市场累计（规格 §五·流程页）。
    """
    if market:
        row = conn.execute(
            "SELECT COUNT(DISTINCT date) AS n FROM sentiment_snapshots"
            " WHERE symbol LIKE ?", (_market_like(market),)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(DISTINCT date) AS n FROM sentiment_snapshots").fetchone()
    return int(row["n"])


def sentiment_latest(conn, market=None):
    """最近有记录的日期；无任何记录返回 None。``market`` 给定时只算该市场标的。"""
    if market:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM sentiment_snapshots WHERE symbol LIKE ?",
            (_market_like(market),)).fetchone()
    else:
        row = conn.execute("SELECT MAX(date) AS d FROM sentiment_snapshots").fetchone()
    return row["d"]


def sentiment_summary(conn, date=None):
    """该日（缺省最近有记录日）的采集摘要：标的数/记录数/各源条数 + 累计积累天数。

    WP11 任务 3：``sentiment-history`` 不带 symbol 时的载荷来源。聚合在 SQL 侧完成
    （不把全表拉进 Python 再数）；``days`` 是**全市场累计**口径（``sentiment_days`` 不带
    market，允许断档）——流程页按市场展示的是**逐市场**口径（``sentiment_days(conn,
    market=…)`` 与 ``sentiment_streak``），两者范围不同，各自标明。
    无任何记录 → 空结构（空是事实，不是错误）。
    """
    day = date or sentiment_latest(conn)
    if day is None:
        return {"date": None, "symbols": 0, "records": 0, "sources": {}, "days": 0}
    row = conn.execute(
        "SELECT COUNT(*) AS records, COUNT(DISTINCT symbol) AS symbols"
        " FROM sentiment_snapshots WHERE date=?", (day,)).fetchone()
    sources = {r["source"]: int(r["n"]) for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM sentiment_snapshots WHERE date=?"
        " GROUP BY source ORDER BY source", (day,))}
    return {"date": day, "symbols": int(row["symbols"]), "records": int(row["records"]),
            "sources": sources, "days": sentiment_days(conn)}


def sentiment_streak(conn, market=None, today=None):
    """连续积累**交易日**数：从最近有记录的交易日往前数，遇无记录交易日即停。

    与 sentiment_days 的口径差异（必须分辨）：days 是「一共多少天有记录」，
    本函数是「最近一口气连了多少个交易日」——休市日不参与计数，因此不因周末断档。

    **记录集与日历都按市场**：给了 ``market`` 时，只有该市场标的的记录参与计数
    （别市场的记录不续长本市场的连续数）；未给时是全市场口径（与 ``sentiment_days``
    的缺省一致）。日历未同步（trading_days 抛 RuntimeError）或未给 market 时**退化**
    为自然日连续计数（该口径会在休市日断档，调用方展示时应知悉，不做静默补齐）。

    起算边界：``latest`` 落在**非交易日**时（日历未同步的北京日退化路径可能把观测记到
    休市日），按交易日历取「≤ latest 的最近交易日」起算——该交易日无记录即返回 0，
    休市日当天的记录本身不计入连续数（它不在交易日序列里）。如实计数，不做补齐。

    窗口：today 往前 550 自然日（约 380 个交易日，覆盖 250 日条款的展示余量）；
    窗口用尽即视为序列起点（返回值是窗口内可证实的下界）。
    """
    latest = sentiment_latest(conn, market=market)
    if latest is None:
        return 0
    if market:
        have = {r["d"] for r in conn.execute(
            "SELECT DISTINCT date AS d FROM sentiment_snapshots WHERE symbol LIKE ?",
            (_market_like(market),))}
    else:
        have = {r["d"] for r in conn.execute(
            "SELECT DISTINCT date AS d FROM sentiment_snapshots")}
    today = today or _now()[:10]
    days = None
    if market:
        try:
            start = (date.fromisoformat(today) - timedelta(days=550)).isoformat()
            days = [d for d in trading_days(conn, market, start, today) if d <= latest]
        except RuntimeError:
            days = None  # 日历未同步：退化，不抛错也不假装连续
    if days is not None:
        count = 0
        for day in reversed(days):
            if day not in have:
                break
            count += 1
        return count
    cursor = date.fromisoformat(latest)
    count = 0
    while cursor.isoformat() in have:
        count += 1
        cursor -= timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# WP12 任务 5：富途数据面研究快照三表（规格 §7.3）——F10 深度数据 / 做空 / 板块目录。
# 表与 WP7 factor_snapshots、WP11 sentiment_snapshots 同一先例（CREATE TABLE IF NOT
# EXISTS 幂等追加，SCHEMA_VERSION 不动）；同样是**观测记录**而非 bar 序列：一行 = 一次
# 抓取，payload 是上游原文 JSON，不打分、不重算（原始数据不会漂移，口径会）。
#
# PIT 语义（规格 §7.3「钥匙按 section 语义取值，不得统一硬套」）：
#   * ``announced_at`` 非空（财报/公告类）→ 可查询性由**披露时点**决定（antichain：
#     只有 announced_at <= as_of 的行可读，防「报告期早但尚未公告」的前视）；
#   * ``announced_at`` 为空（持仓/评级/共识等快照类，或上游未给披露时点）→ 退化为
#     **观测时点**口径：只有观测日 <= as_of 的行可读；采集侧在此情形写入
#     ``_observed_note`` 标注，读侧不做二次判定（标注只解释口径，不参与过滤）。
#
# 主键取 ``(symbol, section, period_end, fetched_at)`` 的理由（规格 §7.3 二选一）：
#   * 只按 period_end 做键会让**同一报告期内的后续观测覆盖先前观测**——早期 as_of 的
#     研究将读不到当时的观测，或读到更晚的值（前视），违反 PIT 的第一性要求；
#   * 纳入 fetched_at 后，每次观测各自留痕（观测不可被后来的观测抹掉），而同一观测时点
#     重跑仍覆盖自身（幂等）——作业侧以一次运行的观测时刻入参，重跑即同键。
#   * ``period_end`` 非空（无自然期的 section 回落观测日）：保证键可比较且同日幂等，
#     否则 NULL 在键里彼此互不相等，重跑会不断堆积。
# ---------------------------------------------------------------------------

#: 观测口径标注键（规格 §7.3）：可查询性依据是观测时点而非数据披露时点。
OBSERVED_NOTE_KEY = "_observed_note"


def _json_payload(payload, table):
    """渠道原文 → 存储字符串（dict/list 序列化；str 必须已是 JSON）。非法即 ValueError。

    宁缺毋假：写不进去的坏数据在这里就拒绝，**不写占位行**（与 insert_sentiment 同口径）。
    """
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False)
    elif not isinstance(payload, str):
        raise ValueError(f"{table} payload 必须是 JSON 字符串或对象，"
                         f"收到 {type(payload).__name__}")
    try:
        json.loads(payload)
    except ValueError as error:
        raise ValueError(f"{table} payload 不是合法 JSON：{error}") from None
    return payload


def insert_f10(conn, symbol, section, payload, period_end, announced_at=None,
               fetched_at=None):
    """按 (symbol,section,period_end,fetched_at) upsert F10 观测（同一观测时点幂等）。

    ``period_end`` 必须给（调用方约定：数据自身期，取不到则给观测日——见模块注释）；
    ``announced_at`` 只在**拿到披露时点**时给，否则留 None 走观测口径。
    """
    conn.execute(
        "INSERT INTO f10_snapshots(symbol,section,period_end,announced_at,payload,"
        "fetched_at) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(symbol,section,period_end,fetched_at) DO UPDATE SET"
        " announced_at=excluded.announced_at, payload=excluded.payload",
        (symbol, section, period_end, announced_at,
         _json_payload(payload, "f10_snapshots"), fetched_at or _now()))
    conn.commit()


def read_f10(conn, symbol, section, as_of):
    """该标的该 section 在 ``as_of`` 时点**可见**的 F10 观测（倒序，最新在前）。

    PIT 上界（规格 §7.3，两条互斥路径）：
      * ``announced_at`` 非空 → 只返 ``announced_at <= as_of``（含当日）；
      * ``announced_at`` 为空 → 只返 ``substr(fetched_at,1,10) <= as_of``（观测日上界）。
    """
    rows = conn.execute(
        "SELECT symbol,section,period_end,announced_at,payload,fetched_at"
        " FROM f10_snapshots WHERE symbol=? AND section=? AND ("
        "  (announced_at IS NOT NULL AND announced_at <= ?)"
        "  OR (announced_at IS NULL AND substr(fetched_at,1,10) <= ?))"
        " ORDER BY fetched_at DESC, period_end DESC",
        (symbol, section, as_of, as_of)).fetchall()
    return [{"symbol": r["symbol"], "section": r["section"], "period_end": r["period_end"],
             "announced_at": r["announced_at"], "payload": json.loads(r["payload"]),
             "fetched_at": r["fetched_at"]} for r in rows]


def insert_short(conn, symbol, date, payload, fetched_at=None):
    """按 (symbol,date) upsert 做空观测（同日重跑幂等覆盖）。"""
    conn.execute(
        "INSERT INTO short_snapshots(symbol,date,payload,fetched_at) VALUES(?,?,?,?)"
        " ON CONFLICT(symbol,date) DO UPDATE SET payload=excluded.payload,"
        " fetched_at=excluded.fetched_at",
        (symbol, date, _json_payload(payload, "short_snapshots"), fetched_at or _now()))
    conn.commit()


def read_shorts(conn, symbol, before=None):
    """该标的做空观测（日期倒序）；``before`` 给定时只返 ``date <= before``。"""
    sql = "SELECT symbol,date,payload,fetched_at FROM short_snapshots WHERE symbol=?"
    params = [symbol]
    if before:
        sql += " AND date<=?"
        params.append(before)
    sql += " ORDER BY date DESC"
    rows = conn.execute(sql, params).fetchall()
    return [{"symbol": r["symbol"], "date": r["date"], "fetched_at": r["fetched_at"],
             "payload": json.loads(r["payload"])} for r in rows]


def insert_plate(conn, date, market, plate_class, payload, fetched_at=None):
    """按 (date,market,plate_class) upsert 板块目录观测（同日重跑幂等覆盖）。"""
    conn.execute(
        "INSERT INTO plate_snapshots(date,market,plate_class,payload,fetched_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(date,market,plate_class) DO UPDATE SET"
        " payload=excluded.payload, fetched_at=excluded.fetched_at",
        (date, market, plate_class, _json_payload(payload, "plate_snapshots"),
         fetched_at or _now()))
    conn.commit()


def read_plates(conn, date=None, market=None, plate_class=None):
    """板块目录观测；``date`` 缺省取最近有观测的日期（无任何观测返回 []）。"""
    if date is None:
        row = conn.execute("SELECT MAX(date) AS d FROM plate_snapshots").fetchone()
        date = row["d"]
        if date is None:
            return []
    sql = ("SELECT date,market,plate_class,payload,fetched_at FROM plate_snapshots"
           " WHERE date=?")
    params = [date]
    for column, value in (("market", market), ("plate_class", plate_class)):
        if value:
            sql += f" AND {column}=?"
            params.append(value)
    sql += " ORDER BY market, plate_class"
    rows = conn.execute(sql, params).fetchall()
    return [{"date": r["date"], "market": r["market"], "plate_class": r["plate_class"],
             "fetched_at": r["fetched_at"], "payload": json.loads(r["payload"])}
            for r in rows]


def research_stats(conn):
    """研究数据覆盖统计（CLI ``research-snapshot --stats`` 的取数口径）。

    ``announced_ratio`` 是 F10 行里**拿到披露时点**的比例——它就是「有多少行是按公告日
    对齐的」的可观测下界；其余行走观测口径（规格 §7.3），值低不是错误，是如实状态。
    """
    f10 = conn.execute(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,"
        " COUNT(DISTINCT section) AS sections,"
        " SUM(CASE WHEN announced_at IS NOT NULL THEN 1 ELSE 0 END) AS announced,"
        " MAX(substr(fetched_at,1,10)) AS latest FROM f10_snapshots").fetchone()
    total = int(f10["rows"] or 0)
    announced = int(f10["announced"] or 0)
    short = conn.execute(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols, MAX(date) AS latest"
        " FROM short_snapshots").fetchone()
    plate = conn.execute(
        "SELECT COUNT(*) AS rows, MAX(date) AS latest FROM plate_snapshots").fetchone()
    return {"f10": {"rows": total, "symbols": int(f10["symbols"] or 0),
                    "sections": int(f10["sections"] or 0), "announced": announced,
                    "announced_ratio": (announced / total) if total else None,
                    "latest": f10["latest"]},
            "short": {"rows": int(short["rows"] or 0),
                      "symbols": int(short["symbols"] or 0), "latest": short["latest"]},
            "plate": {"rows": int(plate["rows"] or 0), "latest": plate["latest"]}}


# ---------------------------------------------------------------------------
# WP15：值班研究员任务队列（规格 §10.2–§10.3）。
#
# 本层是**纯状态机**：不 import 告警/网络/子进程，也不发任何 LLM 调用；升级为 failed
# 的告警由带 home 的业务层（``research_queue.reclaim``）发出——与 sentiment/
# research_sync（业务层发告警）和 store（纯数据层）同一分工。
#
# 队列即攻击面（规格 §十一.13）：kind 与 payload 键都是**白名单**，自由文本一律在入队
# 侧当场 ValueError（而不是等到执行侧才发现队列里躺着一句 prompt）。
# ---------------------------------------------------------------------------

#: 任务种类白名单（规格 §10.3）。新增种类必须同时更新此处与研究院技能的执行手册。
TASK_KINDS = ("daily_brief", "factor_patrol", "mining_round")
#: 任务载荷允许键——**只有结构化引用**，没有自由文本位置。
TASK_PAYLOAD_KEYS = ("as_of", "market", "refs", "digest_ref", "symbols",
                     "factor_list", "window")
#: 失败重试上限：attempts 达到即转 failed（不再重试，避免坏任务无限占用队列）。
TASK_MAX_ATTEMPTS = 3
#: running 超过该分钟数视为执行体已死（headless 超限退出/会话崩溃），回收重试。
TASK_TIMEOUT_MINUTES = 30


def _task_row(row):
    task = dict(row)
    task["payload"] = json.loads(task["payload"])
    return task


def _validate_task(kind, as_of, market, payload):
    """入队侧校验（fail-closed）——非法即 ValueError，**零落库**。"""
    if kind not in TASK_KINDS:
        raise ValueError(f"未知任务种类：{kind!r}（白名单 {'/'.join(TASK_KINDS)}）")
    if not payload or not isinstance(payload, dict):
        raise ValueError(f"任务载荷必须是对象，收到 {type(payload).__name__}")
    unknown = sorted(set(payload) - set(TASK_PAYLOAD_KEYS))
    if unknown:
        raise ValueError(f"任务载荷含白名单外的键：{unknown}（只接受结构化引用 "
                         f"{'/'.join(TASK_PAYLOAD_KEYS)}）")
    if not as_of or not str(as_of).strip():
        raise ValueError("任务缺少 as_of（观测日）")
    if not market or not str(market).strip():
        raise ValueError("任务缺少 market")


def validate_task(kind, as_of, market, payload):
    """公开校验入口（fail-closed）——与入队侧**同一实现**，供领取侧二次校验调用。

    规格 §10.3「队列即攻击面」：入队校验挡的是正常写入路径，而直改库（或将来任何绕过
    ``enqueue_task`` 的写入）能把自由文本塞进 payload。领取这一刻再校验一次，队列里
    躺着的可疑载荷才不会被执行（拒绝比猜测安全）。
    """
    _validate_task(kind, as_of, market, payload)


def enqueue_task(conn, kind, as_of, market, payload, created_at=None):
    """入队（当日幂等）→ ``task_id``。

    幂等键 ``(kind, as_of, market)``：同一交易日同一种类同一市场**只入队一次**——
    定时器重复唤醒、headless 与会话两条路径并发都不会重复研究。重复入队返回**已有**
    task_id 且不改动其状态/载荷（已领取/已完成的任务不会因重跑入队被打回 pending）。
    """
    _validate_task(kind, as_of, market, payload)
    as_of = str(as_of).strip()
    market = str(market).strip().upper()
    existing = conn.execute(
        "SELECT task_id FROM research_tasks WHERE kind=? AND as_of=? AND market=?",
        (kind, as_of, market)).fetchone()
    if existing is not None:
        return existing["task_id"]
    task_id = f"RT-{as_of.replace('-', '')}-{market}-{kind}-{_uuid_token()}"
    conn.execute(
        "INSERT INTO research_tasks(task_id,kind,as_of,market,payload,status,attempts,"
        "created_at) VALUES(?,?,?,?,?,'pending',0,?)",
        (task_id, kind, as_of, market, json.dumps(payload, ensure_ascii=False),
         created_at or _now()))
    conn.commit()
    return task_id


def _uuid_token():
    import uuid
    return uuid.uuid4().hex[:6].upper()


def peek_task(conn):
    """查看队首 pending（**不改状态**）；无 pending 返回 None。

    与 ``claim_task`` 同一排序口径（``created_at, task_id``）。存在的理由是领取侧二次
    校验：先看、验、再领——若载荷可疑就当场拒绝，任务状态保持 pending（而不是先领成
    running 再回滚，那会留下一个「刚刚被判不可信却动过状态」的中间态）。
    """
    row = conn.execute(
        "SELECT * FROM research_tasks WHERE status='pending'"
        " ORDER BY created_at, task_id LIMIT 1").fetchone()
    return None if row is None else _task_row(row)


def claim_task(conn, now):
    """领取最早 pending → running（``started_at=now``）；无 pending 返回 None。

    「最早」按 ``created_at, task_id`` 稳定排序——同秒入队的多条任务顺序确定，不靠
    数据库物理行序（换库/重建后仍可复现）。
    """
    row = conn.execute(
        "SELECT * FROM research_tasks WHERE status='pending'"
        " ORDER BY created_at, task_id LIMIT 1").fetchone()
    if row is None:
        return None
    conn.execute("UPDATE research_tasks SET status='running', started_at=?"
                 " WHERE task_id=?", (now, row["task_id"]))
    conn.commit()
    return _task_row(conn.execute("SELECT * FROM research_tasks WHERE task_id=?",
                                  (row["task_id"],)).fetchone())


def finish_task(conn, task_id, ok, result_ref=None, err=None):
    """回报结果：``ok=True`` → done；``ok=False`` → attempts+1，<上限回 pending，达上限 failed。

    回 pending 时清 ``started_at``（任务确实不在执行中），但**保留 err**——下一次领取者
    需要看到上一次为什么失败，否则重试等于盲试。
    """
    row = conn.execute("SELECT * FROM research_tasks WHERE task_id=?",
                       (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"未知任务：{task_id}")
    attempts = int(row["attempts"])
    if ok:
        conn.execute("UPDATE research_tasks SET status='done', finished_at=?,"
                     " result_ref=?, err=NULL WHERE task_id=?",
                     (_now(), result_ref, task_id))
    else:
        attempts += 1
        status = "failed" if attempts >= TASK_MAX_ATTEMPTS else "pending"
        conn.execute("UPDATE research_tasks SET status=?, attempts=?, started_at=NULL,"
                     " finished_at=?, err=? WHERE task_id=?",
                     (status, attempts, _now() if status == "failed" else None,
                      (str(err)[:300] if err else None), task_id))
    conn.commit()
    return _task_row(conn.execute("SELECT * FROM research_tasks WHERE task_id=?",
                                  (task_id,)).fetchone())


def reclaim_tasks(conn, now, timeout_minutes=TASK_TIMEOUT_MINUTES):
    """回收超时 running 任务 → ``{"requeued": [id], "failed": [task], "kept": [id]}``。

    执行体死掉时任务会永远停在 running（headless 被杀、会话中断）——回收是队列不卡死的
    唯一保障。``attempts`` 达上限的**不回收而是判 failed**：坏任务不能无限循环烧额度。
    """
    import datetime as _dt
    cutoff = (_dt.datetime.strptime(str(now), "%Y-%m-%d %H:%M:%S")
              - _dt.timedelta(minutes=int(timeout_minutes))).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT * FROM research_tasks WHERE status='running' AND started_at IS NOT NULL"
        " AND started_at < ? ORDER BY started_at, task_id", (cutoff,)).fetchall()
    stale = {r["task_id"] for r in rows}
    requeued, failed = [], []
    for row in rows:
        attempts = int(row["attempts"]) + 1
        if attempts >= TASK_MAX_ATTEMPTS:
            conn.execute("UPDATE research_tasks SET status='failed', attempts=?,"
                         " finished_at=?, err=? WHERE task_id=?",
                         (attempts, now, f"执行超时（>{timeout_minutes} 分钟）且已达重试上限",
                          row["task_id"]))
            failed.append(_task_row(conn.execute(
                "SELECT * FROM research_tasks WHERE task_id=?", (row["task_id"],)).fetchone()))
        else:
            conn.execute("UPDATE research_tasks SET status='pending', attempts=?,"
                         " started_at=NULL, err=? WHERE task_id=?",
                         (attempts, f"执行超时（>{timeout_minutes} 分钟），已回收重试",
                          row["task_id"]))
            requeued.append(row["task_id"])
    conn.commit()
    kept = [r["task_id"] for r in conn.execute(
        "SELECT task_id FROM research_tasks WHERE status='running'"
        " ORDER BY created_at, task_id").fetchall() if r["task_id"] not in stale]
    return {"requeued": requeued, "failed": failed, "kept": kept}


def get_tasks(conn, status=None, limit=50):
    """任务列表（可按状态过滤），按 ``created_at, task_id`` 稳定排序。"""
    sql = "SELECT * FROM research_tasks"
    params = ()
    if status is not None:
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY created_at, task_id LIMIT ?"
    rows = conn.execute(sql, (*params, int(limit))).fetchall()
    return [_task_row(row) for row in rows]


def task_exists_since(conn, kind, market, since):
    """该 ``(kind, market)`` 是否存在 ``as_of >= since`` 的任务（周度任务的周内判定）。"""
    row = conn.execute(
        "SELECT 1 FROM research_tasks WHERE kind=? AND market=? AND as_of>=? LIMIT 1",
        (kind, str(market).upper(), since)).fetchone()
    return row is not None


def snapshot_counts(conn, date):
    """该日的 PIT 快照行数（研究任务入队 refs 的取数口径，只读）。

    日期口径逐表如实：情绪/做空/板块按各自的 ``date`` 列；F10 的日期是**观测日**
    （``fetched_at`` 的日期部分）——F10 行的语义时点是财报期（``period_end``）与披露时点
    （``announced_at``），观测日只用来回答「今天采到了几行」，不冒充数据时点。
    """
    day = str(date)[:10]
    sentiment = conn.execute("SELECT COUNT(*) AS n FROM sentiment_snapshots WHERE date=?",
                             (day,)).fetchone()["n"]
    f10 = conn.execute(
        "SELECT COUNT(*) AS n FROM f10_snapshots WHERE substr(fetched_at,1,10)=?",
        (day,)).fetchone()["n"]
    short = conn.execute("SELECT COUNT(*) AS n FROM short_snapshots WHERE date=?",
                         (day,)).fetchone()["n"]
    plate = conn.execute("SELECT COUNT(*) AS n FROM plate_snapshots WHERE date=?",
                         (day,)).fetchone()["n"]
    return {"sentiment": int(sentiment), "f10": int(f10), "short": int(short),
            "plate": int(plate)}
