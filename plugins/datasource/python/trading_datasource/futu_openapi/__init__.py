"""富途 OpenAPI（REST）客户端包 —— OAuth 2.1+PKCE 与 AppKey 双认证。

模块切分（WP13 任务 0 拆包，对调用方零改动；所有 `from trading_datasource.futu_openapi
import X` 原样可用）：

| 模块 | 职责 |
|---|---|
| `errors` | 异常族（信封错误 / 传输失败 / 非预期响应 / 券商二次确认） |
| `auth` | OAuth 2.1+PKCE、AppKey 签名、凭据落盘（0600） |
| `envelope` | 响应信封解析（s==ok/error、错误码映射、need_order_confirm） |
| `client` | HTTP 客户端（认证请求、token 刷新、传输异常收敛） |
| `validators` | 参数校验基类（枚举/区间/日期/正文白名单，坏参数零网络往返） |
| `paths` | **全部路径常量**（唯一事实源，锁定测试绑定对象） |
| `groups/{market,trade,dataplane,f10}` | REST 方法组（路径一律引用 `paths`，禁止内联字面量） |
| `surface` | 锁定面声明（`WP12_TRANSPORT_ENDPOINTS`/`WP12_F10_ENDPOINTS`） |

默认传输用标准库 urllib，把 URLError/超时/连接重置包装为 ``TransportError``；仅授权流程
（scripts/futu_auth.py --openapi）负责注册/浏览器授权/落盘，本包负责凭据读写与带认证请求。

本 `__init__` **原样再导出**拆包前的历史命名空间（含 ``_default_http``/``_safe_json_dict``
等被外部引用的私有名，以及历史上经 ``fo.base64``/``fo.hashes``/``fo.padding`` 访问的
stdlib/加密库模块名）——这是零行为变化的一部分，勿删。
"""
# ── 历史命名空间兼容：拆包前这些 stdlib/加密库名经模块属性可达 ──────────────
import base64  # noqa: F401
import hashlib  # noqa: F401
import http.client  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import secrets  # noqa: F401
import string  # noqa: F401
import tempfile  # noqa: F401
import time  # noqa: F401
import urllib.error  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401
from pathlib import Path  # noqa: F401

from cryptography.hazmat.primitives import hashes, serialization  # noqa: F401
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: F401
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: F401

from . import paths  # noqa: F401
from .paths import *  # noqa: F401,F403  （全部路径常量）
from .errors import (OpenApiError, OrderConfirmRequired, TransportError,  # noqa: F401
                     UnexpectedResponse)
from .auth import (NONCE_ALPHABET, NONCE_PATTERN, AppKeySigner,  # noqa: F401
                   CredentialStore, Pkce, default_credential_path)
from .envelope import (_safe_json_dict, json_body_bytes, parse_envelope,  # noqa: F401
                       parse_envelope_meta, query_string)
from .client import DEFAULT_HOST, TOKEN_PATH  # noqa: F401
from .client import OpenApiClient, _default_http  # noqa: F401
from .validators import _RestValidators  # noqa: F401
from .groups.market import OpenApiMarket  # noqa: F401
from .groups.trade import OpenApiTrade  # noqa: F401
from .groups.dataplane import (OpenApiBasicData, OpenApiDerivatives,  # noqa: F401
                               OpenApiIpo, OpenApiPlate, OpenApiScreen,
                               OpenApiShort, OpenApiWatchlist)
from .groups.f10 import OpenApiF10  # noqa: F401
from .groups.simtrade import OpenApiSimTrade  # noqa: F401
from .surface import (WP12_F10_ENDPOINTS, WP12_TRANSPORT_ENDPOINTS,  # noqa: F401
                      WP13_SIM_TRADE_ENDPOINTS)
