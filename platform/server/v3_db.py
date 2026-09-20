"""V3 平台台账的 **SQLite 持久化层**（唯一入口）。

替代此前散落在 ``home`` 下的 JSONL / JSON 台账（策略研究轮、OMS 台账、对账留痕、
headless 调用日志、SDK 回合记录）。设计约束（与任务书一致）：

  * **无损迁移**：``migrate_from_files`` 把既有文件逐条搬进表里，**不删除也不改写**原
    文件（保留为冷备）；重复调用不重复插入（kv 记录行数 + payload 内容去重双保险）。
  * **不破坏既有接口**：调用方（``v3_analytics`` / ``v3_ops``）的对外响应字段、顺序、
    类型一律不变——数据库纯属内部实现，读取失败就回退到原文件。
  * **写入安全**：WAL + ``busy_timeout`` + 短事务（``BEGIN IMMEDIATE``）；多进程/多线程
    各自开连接，写冲突靠 busy 重试而不是长事务。
  * **绝不因数据库异常 500**：本模块的公开函数只在真正失败时抛 :class:`V3DbError`；
    :func:`init_db` / :func:`stats` / :func:`migrate_from_files` **不抛异常**，失败写进返回
    字典的 ``error`` 字段，由调用方决定怎么如实上报。

路径口径::

    data_dir = 显式 data_dir 参数 → $QUANT_V3_DATA → home
    db_path  = $QUANT_V3_DB → <data_dir>/v3.db

（``QUANT_V3_DATA`` 就是既有平台数据目录的环境变量口径：迁移源文件与数据库都在同一
目录下解析，因此「文件在哪、库就在哪」。）

表（``TABLE_SPECS`` 是唯一事实来源，列名/类型/JSON 列都从这里生成 DDL）::

    strategy_runs(id, as_of, market, universe(json), stages(json), proposals(json),
                  payload(json), created_at)
    oms_orders(id TEXT PK, ticker, side, qty, price, value, stage, risk(json),
               plan_id, history(json), updated_at, payload(json))
    headless_log(id, started_at, success, exit_code, duration_ms, tokens_estimate, payload(json))
    sdk_turns(id, at, session_id, kind, code, answer, tool_calls(json), route(json), payload(json))
    oms_sync(id, at, plans, orders, nav, nav_source, drawdown_pct, industry_source,
             stages(json), payload(json))          # 相对建议 schema 的一处补充：对账留痕
    kv(namespace, key, value(json), expires_at, PRIMARY KEY(namespace, key))
    schema_version(version, applied_at)

每张事件表都同时存 **常用列提取**（便于 ``where``/索引查询）与整条 **payload JSON**
（无损重建原始记录——读出去的对象与迁移前逐字段一致）。

备份/快照（只读，不需要停机）::

    v3_db.backup_to(home, "/path/to/snapshot.db")   # 内部 VACUUM INTO，在线一致快照

用法（调用方只需这三步）::

    import v3_db
    v3_db.init_db(home)                      # 启动时：建表 + 迁移（幂等）
    v3_db.append_event(home, "strategy_runs", run)
    runs = v3_db.list_events(home, "strategy_runs", limit=1)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "DATA_DIR_ENV",
    "DB_ENV",
    "DB_FILENAME",
    "LEGACY_FILES",
    "SCHEMA_VERSION",
    "TABLE_SPECS",
    "TABLES",
    "V3DbError",
    "append_event",
    "backup_to",
    "connect",
    "data_dir_path",
    "db_path",
    "get_kv",
    "init_db",
    "list_events",
    "migrate_from_files",
    "replace_events",
    "reset_counters",
    "set_kv",
    "stats",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DB_FILENAME = "v3.db"
#: 数据库路径覆盖（绝对路径或相对 home 的路径）
DB_ENV = "QUANT_V3_DB"
#: 平台数据目录覆盖（**既有口径**：迁移源文件与数据库都按它解析）
DATA_DIR_ENV = "QUANT_V3_DATA"
#: 忙等超时（毫秒）：多进程/多线程并发写时的等待上限，超时才报 busy
TIMEOUT_ENV = "QUANT_V3_DB_TIMEOUT_MS"
DEFAULT_TIMEOUT_MS = 5000

SCHEMA_VERSION = 1

#: 迁移源文件（相对 data_dir；**只读不删**）。新表 oms_sync 是对建议 schema 的补充。
LEGACY_FILES = (
    "v3-strategy-runs.jsonl",
    "v3-oms-orders.json",
    "v3-oms-sync.jsonl",
    "v3-headless-log.jsonl",
    "v3-sdk-turns.jsonl",
)

STRATEGY_RUNS_FILE = "v3-strategy-runs.jsonl"
OMS_ORDERS_FILE = "v3-oms-orders.json"
OMS_SYNC_FILE = "v3-oms-sync.jsonl"
HEADLESS_LOG_FILE = "v3-headless-log.jsonl"
SDK_TURNS_FILE = "v3-sdk-turns.jsonl"

#: 事件表列规格：``columns`` 是「列名 → payload 里依次尝试的键」，``json`` 是 JSON 列，
#: ``order`` 是默认排序列，``key`` 存在即该表按它做 upsert（否则纯追加）。
TABLE_SPECS = {
    "strategy_runs": {
        "key": None,
        "order": "id",
        "time": "created_at",
        "columns": (
            ("id", "INTEGER", None),
            ("as_of", "TEXT", ("asOf", "as_of", "at")),
            ("market", "TEXT", ("market",)),
            ("universe", "JSON", ("universe",)),
            ("stages", "JSON", ("stages",)),
            ("proposals", "JSON", ("proposals",)),
            ("created_at", "TEXT", ("createdAt", "created_at")),
        ),
        "json": ("universe", "stages", "proposals"),
        "index": (("market",), ("as_of",)),
    },
    "oms_orders": {
        "key": "id",
        "order": "id",
        "time": "updated_at",
        "columns": (
            ("id", "TEXT", ("id", "order_id", "client_order_id")),
            ("ticker", "TEXT", ("ticker", "symbol")),
            ("side", "TEXT", ("side",)),
            ("qty", "REAL", ("qty",)),
            ("price", "REAL", ("price",)),
            ("value", "REAL", ("value",)),
            ("stage", "TEXT", ("stage",)),
            ("risk", "JSON", ("risk",)),
            ("plan_id", "TEXT", ("plan_id",)),
            ("history", "JSON", ("history",)),
            ("updated_at", "TEXT", ("updated_at",)),
        ),
        "json": ("risk", "history"),
        "index": (("stage",), ("ticker",), ("updated_at",)),
    },
    "headless_log": {
        "key": None,
        "order": "id",
        "time": "started_at",
        "columns": (
            ("id", "INTEGER", None),
            ("started_at", "TEXT", ("started_at", "startedAt", "at")),
            ("success", "INTEGER", ("success",)),
            ("exit_code", "INTEGER", ("exit_code", "exitCode")),
            ("duration_ms", "REAL", ("duration_ms", "durationMs")),
            ("tokens_estimate", "INTEGER", ("tokens_estimate", "tokensEstimate")),
        ),
        "json": (),
        "index": (("started_at",),),
    },
    "sdk_turns": {
        "key": None,
        "order": "id",
        "time": "at",
        "columns": (
            ("id", "INTEGER", None),
            ("at", "TEXT", ("at",)),
            ("session_id", "TEXT", ("session_id", "sessionId")),
            ("kind", "TEXT", ("kind",)),
            ("code", "TEXT", ("code",)),
            ("answer", "TEXT", ("answer",)),
            ("tool_calls", "JSON", ("tool_calls", "toolCalls")),
            ("route", "JSON", ("route",)),
        ),
        "json": ("tool_calls", "route"),
        "index": (("session_id",), ("at",)),
    },
    "oms_sync": {
        "key": None,
        "order": "id",
        "time": "at",
        "columns": (
            ("id", "INTEGER", None),
            ("at", "TEXT", ("at",)),
            ("plans", "INTEGER", ("plans",)),
            ("orders", "INTEGER", ("orders",)),
            ("nav", "REAL", ("nav",)),
            ("nav_source", "TEXT", ("nav_source",)),
            ("drawdown_pct", "REAL", ("drawdown_pct",)),
            ("industry_source", "TEXT", ("industry_source",)),
            ("stages", "JSON", ("stages",)),
        ),
        "json": ("stages",),
        "index": (("at",),),
    },
}

#: 事件表（含 payload 列）——kv / schema_version 是内部表，不在 append_event 的可用面里。
TABLES = tuple(TABLE_SPECS)
INTERNAL_TABLES = ("kv", "schema_version")
ALL_TABLES = TABLES + INTERNAL_TABLES

#: 迁移清单：文件 → 表 + 解析方式（``jsonl`` 逐行；``json-orders`` 取 ``orders`` 字典）
MIGRATIONS = (
    {"file": STRATEGY_RUNS_FILE, "table": "strategy_runs", "format": "jsonl"},
    {"file": OMS_ORDERS_FILE, "table": "oms_orders", "format": "json-orders"},
    {"file": OMS_SYNC_FILE, "table": "oms_sync", "format": "jsonl"},
    {"file": HEADLESS_LOG_FILE, "table": "headless_log", "format": "jsonl"},
    {"file": SDK_TURNS_FILE, "table": "sdk_turns", "format": "jsonl"},
)

MIGRATION_NAMESPACE = "migration"


class V3DbError(RuntimeError):
    """数据库层的明确错误（调用方捕获后按「回退只读文件 / 错误信封」处理）。"""


# ---------------------------------------------------------------------------
# 计数器（进程内；``/api/v3/metrics`` 的 db.writes / db.reads 读数口）
# ---------------------------------------------------------------------------
_COUNTERS = {"writes": 0, "reads": 0, "errors": 0}
_COUNTER_LOCK = threading.Lock()


def _count(key, amount=1):
    with _COUNTER_LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0) + amount


def reset_counters():
    """清空进程内计数（单测用；与 ``v3_ops.reset_counters`` 同风格）。"""
    with _COUNTER_LOCK:
        for key in _COUNTERS:
            _COUNTERS[key] = 0


def counters():
    with _COUNTER_LOCK:
        return dict(_COUNTERS)


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------
def _now_iso():
    """``new Date().toISOString()`` 等价（毫秒精度 + ``Z``）。"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _now_epoch():
    return time.time()


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def _home_path(home):
    if home in (None, ""):
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    return Path(str(home)).expanduser()


def data_dir_path(home, data_dir=None):
    """平台数据目录：显式参数 → ``$QUANT_V3_DATA`` → ``home``（与既有口径一致）。"""
    if data_dir not in (None, ""):
        return Path(str(data_dir)).expanduser()
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return _home_path(home)


def db_path(home, data_dir=None):
    """数据库文件路径：``$QUANT_V3_DB`` → ``<data_dir>/v3.db``。"""
    override = os.environ.get(DB_ENV)
    if override:
        return Path(override).expanduser()
    return data_dir_path(home, data_dir) / DB_FILENAME


def _file_path(home, name, data_dir=None):
    return data_dir_path(home, data_dir) / name


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def _timeout_ms():
    raw = os.environ.get(TIMEOUT_ENV)
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS
    return max(50, value)


def connect(home, *, data_dir=None):
    """打开数据库连接：WAL + ``foreign_keys`` + ``busy_timeout``。

    缺省 auto-commit（``isolation_level=None``），事务由本模块显式用
    ``BEGIN IMMEDIATE`` 起——短事务是多进程并发写的安全前提。
    父目录不存在**不**创建（读路径不该有副作用）；写路径由 :func:`init_db` 负责建目录。
    """
    path = db_path(home, data_dir=data_dir)
    try:
        conn = sqlite3.connect(str(path), timeout=_timeout_ms() / 1000.0,
                               isolation_level=None)
    except sqlite3.Error as error:
        _count("errors")
        raise V3DbError(f"数据库打不开：{path}：{error}") from error
    conn.row_factory = sqlite3.Row
    try:
        # 顺序有意：先设 busy_timeout，再切 WAL——首次切 WAL 需要写锁，等锁比立刻报忙好。
        conn.execute("PRAGMA busy_timeout=%d" % _timeout_ms())
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as error:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        _count("errors")
        raise V3DbError(f"数据库 PRAGMA 失败：{error}") from error
    return conn


@contextlib.contextmanager
def _transaction(conn, attempts=5):
    """短写事务：``BEGIN IMMEDIATE``（遇 busy/locked 退避重试）+ 提交/回滚。

    只重试**取锁**这一步：``yield`` 之后主体已经跑过，就不再重放（重放会重复副作用）；
    COMMIT 仍被占用则如实抛 :class:`V3DbError`，由调用方决定回退。
    """
    delay = 0.01
    last = None
    for _ in range(max(1, attempts)):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as error:
            last = error
            if "locked" not in str(error) and "busy" not in str(error):
                _count("errors")
                raise V3DbError(f"写事务无法开始：{error}") from error
            time.sleep(delay)
            delay = min(0.4, delay * 2)
    else:
        _count("errors")
        raise V3DbError(f"写事务在重试后仍被占用：{last}")
    try:
        yield conn
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    try:
        conn.execute("COMMIT")
    except sqlite3.OperationalError as error:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        _count("errors")
        raise V3DbError(f"写事务提交失败：{error}") from error


def _open(home, data_dir=None):
    """打开连接并确保 schema 就绪（读路径也自愈；父目录不存在 → ``V3DbError``）。"""
    conn = connect(home, data_dir=data_dir)
    try:
        _ensure_schema(conn)
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def _column_ddl(name, type_):
    if name == "id":
        return "id INTEGER PRIMARY KEY AUTOINCREMENT" if type_ == "INTEGER" \
            else "id TEXT PRIMARY KEY"
    return f"{name} {type_}"


def _table_ddl(table):
    spec = TABLE_SPECS[table]
    columns = [_column_ddl(name, type_) for name, type_, _keys in spec["columns"]]
    columns.append("payload TEXT NOT NULL")
    return f"CREATE TABLE IF NOT EXISTS {table} (\n  " + ",\n  ".join(columns) + "\n)"


_DDL = [*(_table_ddl(table) for table in TABLES),
        "CREATE TABLE IF NOT EXISTS kv (\n"
        "  namespace TEXT NOT NULL,\n  key TEXT NOT NULL,\n  value TEXT,\n"
        "  expires_at REAL,\n  PRIMARY KEY (namespace, key)\n)",
        "CREATE TABLE IF NOT EXISTS schema_version (\n"
        "  version INTEGER NOT NULL,\n  applied_at TEXT NOT NULL\n)"]

for _table, _spec in TABLE_SPECS.items():
    for _index in _spec.get("index", ()):
        _DDL.append(f"CREATE INDEX IF NOT EXISTS idx_{_table}_{'_'.join(_index)} "
                    f"ON {_table}({', '.join(_index)})")


def _schema_ready(conn):
    """快路径探测：schema_version 到位且哨兵表在 → 不必再跑一遍 DDL。

    每次读都跑 DDL 会让并发读也去抢写锁；稳态下（99% 的调用）这里只花两条只读语句。
    """
    try:
        row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
        version = row["version"] if row is not None else None
        if version != SCHEMA_VERSION:
            return False
        found = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                             "AND name = 'oms_orders'").fetchone()
        return found is not None
    except sqlite3.Error:
        return False


def _ensure_schema(conn):
    if _schema_ready(conn):
        return
    for statement in _DDL:
        conn.execute(statement)
    row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    current = row["version"] if row is not None and row["version"] is not None else 0
    if current < SCHEMA_VERSION:
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                     (SCHEMA_VERSION, _now_iso()))


# ---------------------------------------------------------------------------
# 值编解码
# ---------------------------------------------------------------------------
def _canonical(payload):
    """规范化 JSON 文本：键排序、紧凑分隔——同名 payload 在任何路径下都逐字节可比。"""
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return json.dumps(str(payload), ensure_ascii=False)


def _coerce(value, type_):
    if value is None:
        return None
    try:
        if type_ == "INTEGER":
            if isinstance(value, bool):
                return int(value)
            return int(float(value))
        if type_ == "REAL":
            return float(value)
        if type_ == "TEXT":
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return None
    return value


def _encode(value, type_):
    if type_ == "JSON":
        return None if value is None else _canonical(value)
    return _coerce(value, type_)


def _decode(text, type_):
    if type_ != "JSON" or text is None:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return None


def _spec(table):
    spec = TABLE_SPECS.get(str(table))
    if spec is None:
        raise V3DbError(f"未知表 {table!r}（可用：{'/'.join(TABLES)}）")
    return spec


def _row_values(payload, spec):
    """按规格从 payload 提取常用列（首个存在的键胜出）+ 整条 payload。"""
    if not isinstance(payload, dict):
        raise V3DbError("payload 需为 JSON 对象")
    values = {}
    for name, type_, keys in spec["columns"]:
        if keys is None:  # 自增主键
            continue
        for key in keys:
            if key in payload and payload[key] is not None:
                values[name] = _encode(payload[key], type_)
                break
        else:
            values[name] = None
    time_column = spec.get("time")
    if time_column and not values.get(time_column):
        values[time_column] = _now_iso()
    return values


# ---------------------------------------------------------------------------
# 写：append_event
# ---------------------------------------------------------------------------
def append_event(home, table, payload):
    """追加一条事件（有 ``key`` 的表按主键 upsert）；返回 rowid。

    ``payload`` 整条进 ``payload`` 列（无损重建），同时按 :data:`TABLE_SPECS` 提取常用列。
    失败抛 :class:`V3DbError`（调用方决定回退文件还是如实报错）。
    """
    spec = _spec(table)
    values = _row_values(payload, spec)
    values["payload"] = _canonical(payload)
    names = list(values)
    placeholders = ", ".join("?" for _ in names)
    key = spec.get("key")
    if key:
        updates = ", ".join(f"{name}=excluded.{name}" for name in names if name != key)
        statement = (f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders}) "
                     f"ON CONFLICT({key}) DO UPDATE SET {updates}")
    else:
        statement = f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})"
    params = [values[name] for name in names]
    conn = _open(home)
    try:
        with _transaction(conn):
            cursor = conn.execute(statement, params)
            rowid = cursor.lastrowid
        _count("writes")
    except sqlite3.Error as error:
        _count("errors")
        raise V3DbError(f"写入 {table} 失败：{error}") from error
    finally:
        conn.close()
    return int(rowid or 0)


# ---------------------------------------------------------------------------
# 读：list_events
# ---------------------------------------------------------------------------
def list_events(home, table, *, limit=100, order="desc", where=None):
    """列出事件（``where`` 是常用列的等值过滤）；返回 **payload 对象**列表。

    ``limit=None`` 表示不限条数（OMS 台账全量读取走这条）。读失败抛 :class:`V3DbError`。
    """
    spec = _spec(table)
    key = spec.get("key")
    order_column = key or spec.get("order") or "id"
    direction = "ASC" if str(order or "desc").lower() in ("asc", "oldest", "old") else "DESC"
    clauses = []
    params = []
    types = {name: type_ for name, type_, _keys in spec["columns"]}
    for column, value in (where or {}).items():
        column = str(column)
        if column not in types:
            raise V3DbError(f"表 {table} 没有列 {column!r}")
        clauses.append(f"{column} = ?")
        params.append(_encode(value, types[column]))
    sql = f"SELECT payload FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY {order_column} {direction}"
    if limit is not None:
        try:
            sql += " LIMIT %d" % max(0, int(limit))
        except (TypeError, ValueError):
            raise V3DbError(f"limit 需为整数或 None，收到 {limit!r}") from None
    conn = _open(home)
    try:
        rows = conn.execute(sql, params).fetchall()
        _count("reads")
    except sqlite3.Error as error:
        _count("errors")
        raise V3DbError(f"读取 {table} 失败：{error}") from error
    finally:
        conn.close()
    out = []
    for row in rows:
        value = _decode(row["payload"], "JSON")
        if isinstance(value, dict):
            out.append(value)
    return out


def count_events(home, table):
    """表内行数（stats 用；失败抛 V3DbError）。"""
    _spec(table)
    conn = _open(home)
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"]) if row is not None else 0
    finally:
        conn.close()


def replace_events(home, table, payloads):
    """整体替换一张**带主键**的表内容（upsert + 删除入参里没有的行）。

    ``OmsLedger.write`` 的台账是全量字典（写出去的就是当前全集），用它保证库与那份
    「当前全集」严格一致（不会因为删单留下幽灵行）。返回 ``{inserted, updated, deleted}``。
    """
    spec = _spec(table)
    key = spec.get("key")
    if not key:
        raise V3DbError(f"表 {table} 没有主键，不支持整体替换（用 append_event）")
    rows = [row for row in payloads if isinstance(row, dict)]
    wanted = {}
    for row in rows:
        identifier = row.get(key)
        if identifier is None:
            continue
        wanted[str(identifier)] = _canonical(row)
    result = {"inserted": 0, "updated": 0, "deleted": 0}
    conn = _open(home)
    try:
        existing = {str(row[0]): row[1] for row in
                    conn.execute(f"SELECT {key}, payload FROM {table}")}
        with _transaction(conn):
            for row in rows:
                identifier = row.get(key)
                if identifier is None:
                    continue
                identifier = str(identifier)
                encoded = _canonical(row)
                if existing.get(identifier) == encoded:
                    continue
                _upsert_payload(conn, table, spec, row)
                result["inserted" if identifier not in existing else "updated"] += 1
                existing[identifier] = encoded
            stale = [identifier for identifier in existing if identifier not in wanted]
            for identifier in stale:
                conn.execute(f"DELETE FROM {table} WHERE {key} = ?", (identifier,))
                result["deleted"] += 1
        _count("writes", result["inserted"] + result["updated"] + result["deleted"])
    except sqlite3.Error as error:
        _count("errors")
        raise V3DbError(f"替换 {table} 失败：{error}") from error
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# KV
# ---------------------------------------------------------------------------
def _kv_encode(value):
    return _canonical(value)


def _kv_decode(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _kv_get(conn, namespace, key):
    """读 KV（用**当前连接**，可在事务内调用，避免自锁）。"""
    row = conn.execute("SELECT value, expires_at FROM kv WHERE namespace = ? AND key = ?",
                       (str(namespace), str(key))).fetchone()
    if row is None:
        return None, False
    expires = row["expires_at"]
    if expires is not None and float(expires) <= _now_epoch():
        return None, True  # 已过期：调用方负责清理
    return _kv_decode(row["value"]), False


def _kv_set(conn, namespace, key, value, ttl_ms=None):
    """写 KV（用当前连接，事务由调用方给）。"""
    expires = None
    if ttl_ms is not None:
        try:
            expires = _now_epoch() + max(0.0, float(ttl_ms)) / 1000.0
        except (TypeError, ValueError):
            raise V3DbError(f"ttl_ms 需为数值，收到 {ttl_ms!r}") from None
    conn.execute(
        "INSERT INTO kv (namespace, key, value, expires_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value, "
        "expires_at=excluded.expires_at",
        (str(namespace), str(key), _kv_encode(value), expires))


def set_kv(home, namespace, key, value, ttl_ms=None):
    """写 KV（UPSERT）；``ttl_ms`` 给了就写 ``expires_at``（epoch 秒）。"""
    conn = _open(home)
    try:
        with _transaction(conn):
            _kv_set(conn, namespace, key, value, ttl_ms)
        _count("writes")
    except sqlite3.Error as error:
        _count("errors")
        raise V3DbError(f"写 KV 失败：{error}") from error
    finally:
        conn.close()
    return True


def get_kv(home, namespace, key):
    """读 KV；不存在/已过期 → ``None``（过期行顺手清掉，best-effort）。"""
    conn = _open(home)
    try:
        value, expired = _kv_get(conn, namespace, key)
        _count("reads")
        if expired:
            try:
                with _transaction(conn):
                    conn.execute("DELETE FROM kv WHERE namespace = ? AND key = ?",
                                 (str(namespace), str(key)))
                _count("writes")
            except (V3DbError, sqlite3.Error):
                pass  # 清理失败不影响「已过期即 None」的语义
    except sqlite3.Error as error:
        _count("errors")
        raise V3DbError(f"读 KV 失败：{error}") from error
    finally:
        conn.close()
    return value


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------
def _parse_jsonl(text):
    """JSONL 文本 → 记录列表（跳过空行/坏行/非对象行）。"""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _parse_oms_json(text):
    """OMS 台账 JSON → ``{id: record}``；解析不了 → ``({}, 原因)``。"""
    try:
        raw = json.loads(text)
    except ValueError:
        return {}, "JSON 解析失败"
    if not isinstance(raw, dict):
        return {}, "顶层不是 JSON 对象"
    orders = raw.get("orders")
    if not isinstance(orders, dict):
        return {}, "缺少 orders 对象"
    return {str(key): value for key, value in orders.items() if isinstance(value, dict)}, None


def _fingerprint(text):
    """文件内容指纹（sha1）：判「这份冷备变过没有」，比行数比对更可靠。"""
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _existing_payloads(conn, table):
    """表内已有 payload 的规范化集合——迁移去重的唯一依据。"""
    return {row[0] for row in conn.execute(f"SELECT payload FROM {table}")}


def _insert_payload(conn, table, spec, payload):
    values = _row_values(payload, spec)
    values["payload"] = _canonical(payload)
    names = list(values)
    statement = (f"INSERT INTO {table} ({', '.join(names)}) VALUES "
                 f"({', '.join('?' for _ in names)})")
    conn.execute(statement, [values[name] for name in names])


def _upsert_payload(conn, table, spec, payload):
    values = _row_values(payload, spec)
    values["payload"] = _canonical(payload)
    names = list(values)
    key = spec["key"]
    updates = ", ".join(f"{name}=excluded.{name}" for name in names if name != key)
    statement = (f"INSERT INTO {table} ({', '.join(names)}) VALUES "
                 f"({', '.join('?' for _ in names)}) "
                 f"ON CONFLICT({key}) DO UPDATE SET {updates}")
    conn.execute(statement, [values[name] for name in names])


def _migrate_file(conn, home, data_dir, entry):
    """迁移单个文件；返回真实统计（不抛异常，失败进 ``error``）。"""
    table = entry["table"]
    spec = TABLE_SPECS[table]
    path = _file_path(home, entry["file"], data_dir)
    result = {"file": entry["file"], "table": table, "path": str(path),
              "rows": 0, "inserted": 0, "updated": 0, "skipped": False, "at": _now_iso()}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        result["note"] = "文件不存在或不可读"
        result["skipped"] = True
        return result
    fingerprint = _fingerprint(text)
    result["fingerprint"] = fingerprint
    if entry["format"] == "jsonl":
        records = _parse_jsonl(text)
    else:
        orders, problem = _parse_oms_json(text)
        records = list(orders.values())
        if problem is not None:
            result["note"] = problem
            result["skipped"] = True
            return result
    result["rows"] = len(records)
    if not records:
        result["note"] = "文件为空（没有可迁移记录）"
        result["skipped"] = True
        return result

    previous = None
    with contextlib.suppress(V3DbError, sqlite3.Error):
        previous, _expired = _kv_get(conn, MIGRATION_NAMESPACE, entry["file"])
    previous = previous if isinstance(previous, dict) else {}
    db_rows = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    # 快路径：文件内容指纹没变、且库里已有不少于文件条数的行 → 什么都不用做。
    # 指纹（而不是行数）是必需的：冷备内容被改写而条数不变时，仍要被发现（无损）。
    if previous.get("fingerprint") == fingerprint and db_rows >= len(records):
        result["skipped"] = True
        result["note"] = f"已迁移过（记录 {len(records)} 条，文件指纹未变）"
        return result

    if spec.get("key"):
        by_key = {str(row[0]): row[1] for row in
                  conn.execute(f"SELECT {spec['key']}, payload FROM {table}")}
        for record in records:
            identifier = record.get(spec["key"]) or record.get("id")
            if identifier is None:
                continue
            identifier = str(identifier)
            key_value = _canonical(record)
            current = by_key.get(identifier)
            if current == key_value:
                continue
            _upsert_payload(conn, table, spec, record)
            if current is None:
                result["inserted"] += 1
            else:
                result["updated"] += 1
            by_key[identifier] = key_value
    else:
        existing = _existing_payloads(conn, table)
        for record in records:
            key_value = _canonical(record)
            if key_value in existing:
                continue
            _insert_payload(conn, table, spec, record)
            existing.add(key_value)
            result["inserted"] += 1
    result["at"] = _now_iso()
    return result


def migrate_from_files(home, *, data_dir=None):
    """JSONL/JSON → 表（**幂等**；不删原文件）。

    幂等两层：① ``kv`` 的 ``migration`` 命名空间记录每个文件已迁移的行数；② 逐条 payload
    规范化后与库内已有记录比对，重复的不再插入。因此重复调用、甚至「迁移后平台又经
    ``append_event`` 双写追加过新记录」都不会产生重复行。

    返回 ``{file: {rows, inserted, updated, skipped, at, path, table[, note][, error]}}``。
    **不抛异常**（迁移是本进程启动动作，失败只如实上报，绝不阻断服务）。
    """
    result = {}
    try:
        path = db_path(home, data_dir=data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _count("errors")
        return {"ok": False, "error": f"数据目录不可写：{error}"}
    try:
        conn = _open(home, data_dir=data_dir)
    except (V3DbError, sqlite3.Error) as error:
        _count("errors")
        return {"ok": False, "error": str(error)}
    try:
        for entry in MIGRATIONS:
            try:
                with _transaction(conn):
                    row = _migrate_file(conn, home, data_dir, entry)
                    # 迁移记录与数据同一事务提交：要么都生效，要么都不生效（不会「记了没插」）。
                    _kv_set(conn, MIGRATION_NAMESPACE, entry["file"], {
                        "rows": row["rows"],
                        "inserted": row.get("inserted", 0) + row.get("updated", 0),
                        "lastInserted": row.get("inserted", 0),
                        "fingerprint": row.get("fingerprint"),
                        "at": row.get("at"),
                        "path": row["path"],
                    })
                result[entry["file"]] = row
                if row.get("inserted") or row.get("updated"):
                    _count("writes", row.get("inserted", 0) + row.get("updated", 0))
            except (V3DbError, sqlite3.Error, OSError) as error:
                _count("errors")
                result[entry["file"]] = {"file": entry["file"], "table": entry["table"],
                                         "rows": 0, "inserted": 0, "skipped": True,
                                         "error": str(error)[:300]}
    finally:
        conn.close()
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# 统计 / 备份
# ---------------------------------------------------------------------------
def _migration_records(conn, home, data_dir=None):
    records = {}
    for entry in MIGRATIONS:
        with contextlib.suppress(V3DbError, sqlite3.Error, TypeError, ValueError):
            value, _expired = _kv_get(conn, MIGRATION_NAMESPACE, entry["file"])
            if isinstance(value, dict):
                records[entry["file"]] = value
    return records


def stats(home, *, data_dir=None):
    """``{path, sizeBytes, tables:{table: rows}, writes, reads, migrated, ...}``——**不抛异常**。

    ``tables`` 含 kv/schema_version（行数），调用方按需展示。数据库不存在时
    ``exists=False`` 且计数为零，绝不编造。
    """
    path = db_path(home, data_dir=data_dir)
    payload = {
        "path": str(path),
        "dataDir": str(data_dir_path(home, data_dir)),
        "sizeBytes": 0,
        "walBytes": 0,
        "exists": False,
        "tables": {table: 0 for table in ALL_TABLES},
        "writes": counters()["writes"],
        "reads": counters()["reads"],
        "errors": counters()["errors"],
        "migrated": {},
        "schemaVersion": 0,
        "ok": True,
    }
    try:
        if not path.exists():
            # 库还不存在：绝不为了「看一眼」而创建文件（读路径零副作用）。
            return payload
        payload["exists"] = True
        payload["sizeBytes"] = path.stat().st_size
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                payload["walBytes"] += side.stat().st_size
        conn = connect(home, data_dir=data_dir)
    except (V3DbError, sqlite3.Error, OSError) as error:
        payload["ok"] = False
        payload["error"] = str(error)[:300]
        return payload
    try:
        try:
            for table in ALL_TABLES:
                try:
                    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                    payload["tables"][table] = int(row["n"]) if row is not None else 0
                except sqlite3.Error:
                    payload["tables"][table] = None  # 表还不存在（未 init）
            try:
                row = conn.execute(
                    "SELECT MAX(version) AS version FROM schema_version").fetchone()
                payload["schemaVersion"] = int(row["version"]) if row and row["version"] else 0
            except sqlite3.Error:
                payload["schemaVersion"] = 0
            payload["migrated"] = _migration_records(conn, home, data_dir)
        except Exception as error:  # noqa: BLE001 —— 统计口径永不抛（坏库/坏文件都不许 500）
            payload["ok"] = False
            payload["error"] = str(error)[:300]
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    payload["writes"] = counters()["writes"]
    payload["reads"] = counters()["reads"]
    payload["errors"] = counters()["errors"]
    return payload


def backup_to(home, dest, *, data_dir=None):
    """只读在线快照（``VACUUM INTO``）；``dest`` 必须是**不存在**的路径。

    返回 ``{ok, path, sizeBytes}``；失败返回 ``{ok: False, error}``（不抛）。
    """
    target = Path(str(dest)).expanduser()
    if target.exists():
        return {"ok": False, "error": f"目标已存在，拒绝覆盖：{target}"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return {"ok": False, "error": f"目标目录不可写：{error}"}
    try:
        conn = _open(home, data_dir=data_dir)
    except (V3DbError, sqlite3.Error) as error:
        return {"ok": False, "error": str(error)}
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as error:
        _count("errors")
        return {"ok": False, "error": f"VACUUM INTO 失败：{error}"}
    finally:
        conn.close()
    return {"ok": True, "path": str(target),
            "sizeBytes": target.stat().st_size if target.exists() else 0}


def init_db(home, *, data_dir=None):
    """建表 + 迁移 + 返回统计（启动时调用一次；**不抛异常**）。

    幂等：已建的表/索引不动，已迁移的文件不再重复插入。失败时返回
    ``{..., ok: false, error}``，调用方据此在 ``/api/v3/metrics`` 里如实上报，
    但**不阻断启动**（库不可用时各接口回退到原文件只读）。
    """
    path = db_path(home, data_dir=data_dir)
    migration = {}
    error = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open(home, data_dir=data_dir)
        try:
            with _transaction(conn):
                row = conn.execute(
                    "SELECT MAX(version) AS version FROM schema_version").fetchone()
                current = int(row["version"]) if row and row["version"] else None
                if current is None or current < SCHEMA_VERSION:
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (SCHEMA_VERSION, _now_iso()))
        finally:
            conn.close()
        migration = migrate_from_files(home, data_dir=data_dir)
    except (V3DbError, sqlite3.Error, OSError) as exc:
        _count("errors")
        error = str(exc)[:300]
    payload = stats(home, data_dir=data_dir)
    payload["migration"] = migration
    if error is not None:
        payload["ok"] = False
        payload["error"] = error
    return payload
