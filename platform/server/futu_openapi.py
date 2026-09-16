"""OpenAPI 凭据的服务端私钥落盘与指纹（WP8 任务 7：Web 设置页的唯一写入口）。

职责刻意窄：只做「PEM 校验 → 原子写 0600」与「公钥指纹」两件事；凭据 JSON 的读写、
算法匹配校验、通道联动都在 ``server/settings_api.py``（组装层），签名与请求在
``trading_datasource.futu_openapi``（全仓库唯一客户端实现）。

安全约定（与 CredentialStore 同一口径）：
  * 私钥只落 ``<home>/futu-openapi-key.pem``（与 scripts/futu_openapi_check.py 的
    默认路径一致），0600 原子写（同目录临时文件 + fsync + chmod + os.replace）；
  * 落盘内容是**规范化后的 PKCS8 PEM**（从加载成功的私钥重新序列化），不回写用户
    原文的任意字节——坏尾随内容、CRLF 混排在这里被归一；
  * 本模块的任何返回值都不含私钥原文：指纹是公钥 DER 的 SHA256 前 16 hex，只能
    用于「两端配置的是不是同一把钥匙」的人工比对，不能反推私钥。
"""
import hashlib
import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization

PRIVATE_KEY_FILENAME = "futu-openapi-key.pem"


def private_key_path(home):
    """``<home>/futu-openapi-key.pem``（设置页写私钥的固定落点）。"""
    return Path(home) / PRIVATE_KEY_FILENAME


def write_private_key(home, pem_text):
    """校验 PEM 并原子写 0600 到 ``<home>/futu-openapi-key.pem``，返回路径字符串。

    PEM 不可解析（或不是私钥）→ ``ValueError``，调用方（settings_api）转业务失败
    信封；此时**不产生任何文件**。写成功后内容是从加载成功的私钥重新序列化的
    规范 PKCS8 PEM——与 ``AppKeySigner.from_path``（load_pem_private_key）完全兼容。
    """
    if not isinstance(pem_text, str) or "PRIVATE KEY" not in pem_text:
        raise ValueError(
            "私钥内容必须是 PEM 格式（-----BEGIN PRIVATE KEY----- 开头的完整 PEM 原文）")
    try:
        key = serialization.load_pem_private_key(pem_text.encode("utf-8"), password=None)
    except Exception as error:  # noqa: BLE001 —— cryptography 的各坏 PEM 异常统一转 ValueError
        raise ValueError(f"私钥 PEM 无法解析：{error}") from error
    payload = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    target = private_key_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 原子写（与 CredentialStore.save 同构）：同目录临时文件 + fsync + chmod 0600
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".futu-openapi-key.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return str(target)


def key_fingerprint(pem_path):
    """私钥文件 → 公钥指纹（SPKI DER 的 SHA256 前 16 hex）；文件缺失/坏 PEM → None。

    指纹是给**人**比对用的（「网页保存的这把」与「控制台登记的是不是同一把」），
    绝不回传私钥原文本身。
    """
    try:
        pem = Path(pem_path).expanduser().read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception:  # noqa: BLE001 —— 缺文件/权限/坏 PEM 都按「无指纹」如实呈现
        return None
    der = key.public_key().public_bytes(serialization.Encoding.DER,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]
