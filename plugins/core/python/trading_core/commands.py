"""指令目录（规格 §8.2）：工作台 → daemon 的唯一通道。白名单外一律拒绝；
nonce + processed/ 目录保证幂等；原子写防半文件。"""
import json, uuid
from pathlib import Path

COMMANDS = {"execute_plan", "cancel_plan", "kill", "unkill", "run_job"}


def _dir(home):
    from . import daemon
    return daemon.commands_dir(home)


def write_command(home, type_, payload):
    if type_ not in COMMANDS:
        raise ValueError(f"指令不在白名单：{type_}")
    d = _dir(home)
    (d / "pending").mkdir(parents=True, exist_ok=True)
    nonce = payload.get("nonce") or uuid.uuid4().hex
    body = json.dumps({"type": type_, "nonce": nonce, **payload},
                      ensure_ascii=False, sort_keys=True)
    target = d / "pending" / f"{nonce}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return nonce


def poll(home, handler):
    """轮询 pending/，逐个 handler 处理；成功移入 processed/（以 nonce 去重）。"""
    pending = _dir(home) / "pending"
    processed = _dir(home) / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(pending.glob("*.json")):
        try:
            cmd = json.loads(f.read_text(encoding="utf-8"))
            if (processed / f.name).exists():
                f.unlink()
                continue
            result = handler(cmd)
            out.append({**cmd, "result": result})
            f.replace(processed / f.name)
        except Exception as error:  # noqa: BLE001 —— 单条指令失败不阻塞其余
            out.append({"file": f.name, "error": str(error)[:160]})
    return out
