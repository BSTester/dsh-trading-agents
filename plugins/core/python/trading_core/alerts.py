"""告警（规格 §8.4）：info/warn/critical 落表；critical 置心跳标志位；
桌面通知为可选能力（platform_notify.detect() 为 None 时不发）。"""
import datetime as dt
import json
import subprocess
from pathlib import Path

from . import platform_notify


def emit(conn, home, level, title, detail=""):
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO alerts(level,title,detail,created_at) VALUES(?,?,?,?)",
                 (level, title, detail, now))
    conn.commit()
    hb_path = Path(home) / "trading-daemon.json"
    hb = {}
    if hb_path.exists():
        hb = json.loads(hb_path.read_text(encoding="utf-8"))
    if level == "critical":
        hb["critical"] = True
        hb["critical_title"] = title
    hb.setdefault("heartbeat", now)
    tmp = hb_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
    tmp.replace(hb_path)
    _maybe_desktop_notify(level, title, detail)


def list_recent(conn, limit=50):
    rows = conn.execute("SELECT level,title,detail,created_at FROM alerts"
                        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"level": r["level"], "title": r["title"], "detail": r["detail"],
             "created_at": r["created_at"]} for r in rows]


def _maybe_desktop_notify(level, title, detail):
    if level != "critical":
        return
    cmd = platform_notify.detect()
    if not cmd:
        return
    text = f"[量化] {title}"
    try:
        if cmd == "osascript":
            subprocess.run(["osascript", "-e", f'display notification "{text}"'], timeout=5)
        else:
            subprocess.run([cmd, text], timeout=5)
    except Exception:
        pass  # 通知失败不影响告警落库
