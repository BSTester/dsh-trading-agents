"""V3 策略族（FR-STRAT-002）：特征工程 + Lasso / GBDT / MLP 三个模型的**纯计算内核**。

设计约束（与 ``server/v3_math.py`` 同一套纪律）：
  * **只做算术**：不取数、不读盘、不碰 FastAPI/网络——取数一律由 ``v3_analytics.ml_models``
    经既有限流器 ``v3_run("series", …)`` 完成，本模块只吃 ``bars_by_ticker``；
  * **PIT 严格**：``t`` 日特征只用 ``≤ t`` 的收盘价，标签用 ``t+horizon`` 收益；任何特征都
    不得触达 ``t`` 之后的数据（``tests/test_v3_ml.py::PITTests`` 用「打乱未来数据」钉住）；
  * **诚实标注实现**：本部署（``~/.dsh/trading-venv``，Python 3.13）**没有** ``sklearn``／
    ``lightgbm``，因此三个模型都走 numpy 自实现，``impl`` 如实写 ``numpy-lasso`` /
    ``numpy-gbdt-lite`` / ``numpy-mlp``。可选库只做「探测 + 存在时优先」，
    **不新增任何硬依赖**；导入失败一律回落 numpy 并保留真实实现名。
  * **取不到就是取不到**：样本不足抛 :class:`InsufficientSample`（调用方转
    ``ml/insufficient-sample``），绝不用小样本硬跑出好看的数。

方法学限制（如实写进 :func:`run_model_suite` 的 ``notes``）：
  * ``numpy-gbdt-lite`` 是**紧凑实现**，不是 LightGBM 官方库：直方图分位数分箱 +
    深度 1..3 的 CART（XGBoost 风格的 ``G²/(H+λ)`` 增益），无 GOSS/EFB/直方图减法优化；
  * ``numpy-lasso`` 用坐标下降（闭式软阈值更新）解 ``min 1/(2n)‖y−Xb‖² + α‖b‖₁``，
    与 sklearn ``Lasso`` 的目标函数同形（同一 ``alpha`` 口径），但不含 sklearn 的
    对偶间隙早停与解析 warm-start；
  * 所有指标都是**样本外**（按日期升序 70/30 留出），且**不做 purging/embargo**——
    相邻样本的 ``horizon`` 标签区间重叠，指标会因此偏乐观（属已知偏差，不掩盖）。
"""

import importlib.util
import math
from datetime import datetime, timezone

import numpy as np

from server import v3_math

__all__ = [
    "FEATURE_NAMES",
    "MOMENTUM_FEATURE",
    "MIN_SAMPLES",
    "InsufficientSample",
    "available_backends",
    "build_features",
    "evaluate",
    "momentum_baseline",
    "predict",
    "run_model_suite",
    "train_gbdt",
    "train_lasso",
    "train_mlp",
]

#: 特征名（**顺序即列序**；全部由收盘价推出，故所有标的可用同一套口径）。
FEATURE_NAMES = (
    "ret_1",        # 1 日收益
    "ret_5",        # 5 日收益
    "ret_window",   # window 日收益（**与现有动量基线同一定义**，故可同台对比）
    "vol_window",   # window 日收益标准差
    "ma_gap",       # 收盘 / window 日均线 − 1
    "ret_z",        # (1 日收益 − window 日均值) / window 日标准差
    "rsi_14",       # RSI(14) / 100
    "range_pos",    # (收盘 − window 日最低收盘) / (最高 − 最低)
    "skew_window",  # window 日收益偏度
    "mom_gap",      # ret_5 − ret_window（短长动量差）
)

#: 与现有动量策略同定义的列（动量基线只用这一列）。
MOMENTUM_FEATURE = "ret_window"

#: 样本不足阈值：低于此值一律 ``ml/insufficient-sample``，**不硬跑**。
MIN_SAMPLES = 120

#: RSI 回看期（固定 14，与工作台因子口径一致）。
RSI_PERIOD = 14

#: GBDT-lite 默认直方图分箱数 / 叶子 L2 正则 / 最小叶子样本数。
GBDT_BINS = 32
GBDT_L2 = 1.0
GBDT_MIN_CHILD = 1.0

#: 年化交易日数（与 ``v3_math.TRADING_DAYS`` 同值；这里独立命名以免耦合常量名）。
TRADING_DAYS = 252.0


class InsufficientSample(ValueError):
    """样本量低于 :data:`MIN_SAMPLES`——调用方转 ``ml/insufficient-sample``。

    ``detail`` 是可直接进错误信封的只读明细（真实样本量与要求值，不做四舍五入美化）。
    """

    def __init__(self, n_samples, required, *, window=None, horizon=None, tickers=None):
        self.n_samples = int(n_samples)
        self.required = int(required)
        self.detail = {"n_samples": int(n_samples), "required": int(required)}
        if window is not None:
            self.detail["window"] = int(window)
        if horizon is not None:
            self.detail["horizon"] = int(horizon)
        if tickers is not None:
            self.detail["tickers"] = list(tickers)
        message = (f"可用样本 {self.n_samples} 条，低于要求 {self.required} 条"
                   f"（window={window}，horizon={horizon}）——样本不足时不硬跑，"
                   f"请增大 limit 或放宽 window")
        super().__init__(message)


# ---------------------------------------------------------------------------
# 可选依赖探测（**只探测，不安装、不新增硬依赖**）
# ---------------------------------------------------------------------------
def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _load_module(name):
    """尽力导入可选库；任何失败返回 ``None``（调用方回落 numpy）。"""
    try:
        import importlib

        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 —— 可选依赖的任何失败都不该拖垮请求
        return None


def available_backends():
    """当前解释器里**真实可用**的可选后端：``{"sklearn": bool, "lightgbm": bool}``。

    每次调用都重新探测（不缓存），因此测试 monkeypatch 本函数即可模拟「依赖缺失」。
    本部署实测两者皆 ``False``——``impl`` 因此恒为 ``numpy-*``。
    """
    return {"sklearn": _module_available("sklearn"),
            "lightgbm": _module_available("lightgbm")}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _clamp_int(value, default, minimum, maximum):
    number = v3_math.to_float(value)
    if number is None:
        return default
    return max(minimum, min(maximum, int(number)))


def _clamp_float(value, default, minimum, maximum):
    number = v3_math.to_float(value)
    if number is None:
        return default
    if not math.isfinite(number):
        return default
    return max(minimum, min(maximum, float(number)))


def _num(value, default=0.0):
    """标量 → 有限 float（NaN/inf 一律回落 ``default``，绝不让 NaN 进 JSON）。"""
    number = v3_math.to_float(value)
    if number is None or not math.isfinite(number):
        return default
    return float(number)


def _clean_bars(bars):
    """K 线 → ``[(日期, 收盘价)]``：剔除非正/非数值收盘，按日期升序，同日期保留最后一条。

    只读 ``t``／``c`` 两个字段，因此自选数据源（含只有收盘价的假序列）也能用。
    """
    rows = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        close = v3_math.to_float(bar.get("c"))
        if close is None or not math.isfinite(close) or close <= 0:
            continue
        rows.append((str(bar.get("t") or "").strip(), float(close)))
    rows.sort(key=lambda item: item[0])
    out = []
    for date, close in rows:
        if out and out[-1][0] == date:
            out[-1] = (date, close)
        else:
            out.append((date, close))
    return out


def _skew(values):
    """总体偏度（``m3 / m2^1.5``）；样本不足或标准差为 0 → ``0.0``。"""
    values = np.asarray(values, dtype=float)
    if values.size < 3:
        return 0.0
    centered = values - values.mean()
    m2 = float(np.mean(centered ** 2))
    if m2 <= 0:
        return 0.0
    m3 = float(np.mean(centered ** 3))
    return m3 / (m2 ** 1.5)


def _rsi(rets, index, period=RSI_PERIOD):
    """``index`` 处的 RSI/100（Wilder 简化版：直接平均最近 ``period`` 日涨跌幅）。

    数据不足 → ``None``；全为涨（无跌）→ 1.0；全为跌 → 0.0；涨跌皆 0 → 0.5。
    """
    if index < period:
        return None
    window = np.asarray(rets[index - period + 1:index + 1], dtype=float)
    if window.size == 0:
        return None
    gains = float(np.mean(np.clip(window, 0.0, None)))
    losses = float(np.mean(np.clip(-window, 0.0, None)))
    if gains <= 0 and losses <= 0:
        return 0.5
    if losses <= 0:
        return 1.0
    if gains <= 0:
        return 0.0
    rs = gains / losses
    return rs / (1.0 + rs)


def _feature_matrix(closes, window):
    """``(n, len(FEATURE_NAMES))`` 的特征矩阵；第 ``i`` 行**只用** ``closes[:i+1]``。

    历史不足以定义某列时该格为 ``NaN``（调用方按行丢弃），**绝不用 0 或估计值顶替**。
    返回矩阵的第 ``i`` 行在数学上只依赖 ``closes[0..i]``——这是 PIT 无未来函数的全部依据。
    """
    n = int(closes.size)
    out = np.full((n, len(FEATURE_NAMES)), np.nan, dtype=float)
    if n < 2:
        return out
    rets = np.zeros(n, dtype=float)
    rets[1:] = closes[1:] / closes[:-1] - 1.0
    for index in range(1, n):
        lo = max(0, index - window + 1)
        win_ret = rets[lo:index + 1]
        win_close = closes[lo:index + 1]
        row = out[index]
        row[0] = rets[index]
        if index >= 5:
            row[1] = closes[index] / closes[index - 5] - 1.0
        if index >= window:
            row[2] = closes[index] / closes[index - window] - 1.0
        if win_ret.size >= 2:
            sd = float(np.std(win_ret, ddof=1))
            row[3] = sd
            if sd > 0:
                row[5] = (rets[index] - float(np.mean(win_ret))) / sd
            row[8] = _skew(win_ret)
        if win_close.size >= 2:
            mean_close = float(np.mean(win_close))
            if mean_close > 0:
                row[4] = closes[index] / mean_close - 1.0
            low = float(np.min(win_close))
            high = float(np.max(win_close))
            if high > low:
                row[7] = (closes[index] - low) / (high - low)
        rsi = _rsi(rets, index)
        if rsi is not None:
            row[6] = rsi
        if math.isfinite(row[1]) and math.isfinite(row[2]):
            row[9] = row[1] - row[2]
    return out


# ---------------------------------------------------------------------------
# 特征工程（PIT）
# ---------------------------------------------------------------------------
def build_features(bars_by_ticker, *, window=20, horizon=1):
    """把多标的日 K 变成**池化**的监督学习样本（PIT 严格）。

    契约::

        {"X": np.ndarray (n, p), "y": np.ndarray (n,),
         "dates": [特征日 t…], "tickers": [标的…], "feature_names": [...]}

    * ``X[i]`` 由 ``tickers[i]`` 在 ``dates[i]``（记为 ``t``）当日的**已知**信息构成：
      只用 ``≤ t`` 的收盘价（见 :func:`_feature_matrix`）；
    * ``y[i] = close(t + horizon) / close(t) − 1``，即 ``t`` 之后 ``horizon`` 日的真实收益；
    * 样本按 ``(日期, 标的)`` 升序排列（跨标的合并后仍是**时间序**，便于时序留出切分）；
    * 任一行出现 ``NaN``（历史长度不足）→ 丢弃该行，不插值、不清零。
    """
    window = _clamp_int(window, 20, 2, 500)
    horizon = _clamp_int(horizon, 1, 1, 250)
    warmup = max(window, RSI_PERIOD, 6)
    rows = []
    for ticker in sorted((bars_by_ticker or {}).keys()):
        series = _clean_bars((bars_by_ticker or {}).get(ticker))
        n = len(series)
        if n <= warmup + horizon:
            continue
        dates = [date for date, _ in series]
        closes = np.array([close for _, close in series], dtype=float)
        matrix = _feature_matrix(closes, window)
        for index in range(warmup, n - horizon):
            feature = matrix[index]
            if not np.all(np.isfinite(feature)):
                continue
            rows.append((dates[index], str(ticker), feature,
                         float(closes[index + horizon] / closes[index] - 1.0)))
    rows.sort(key=lambda item: (item[0], item[1]))
    matrix = (np.array([item[2] for item in rows], dtype=float) if rows
              else np.zeros((0, len(FEATURE_NAMES)), dtype=float))
    return {
        "X": matrix,
        "y": np.array([item[3] for item in rows], dtype=float),
        "dates": [item[0] for item in rows],
        "tickers": [item[1] for item in rows],
        "feature_names": list(FEATURE_NAMES),
    }


# ---------------------------------------------------------------------------
# 训练：标准化与 OLS 单变量校准（线性模型/动量基线共用）
# ---------------------------------------------------------------------------
def _standardizer(X):
    X = np.asarray(X, dtype=float)
    if X.size == 0:
        return np.zeros(X.shape[1] if X.ndim == 2 else 0), np.ones(X.shape[1] if X.ndim == 2 else 0)
    mean = X.mean(axis=0)
    std = X.std(axis=0, ddof=0)
    std = np.where(std > 0, std, 1.0)
    return mean, std


def _linear_model(coef_std, x_mean, x_std, y_mean, y_std, b_std, *, impl, params=None):
    """把**标准化空间**的系数打包成统一模型字典（``kind="linear"``）。

    ``intercept`` 汇报的是**原始量纲**的截距：``y_mean + y_std·(b_std − Σ coef·x_mean/x_std)``，
    这样调用方看到一个能直接对着 ``y`` 读的数，而不是标准化空间的 0。
    """
    coef_std = np.asarray(coef_std, dtype=float)
    x_mean = np.asarray(x_mean, dtype=float)
    x_std = np.asarray(x_std, dtype=float)
    raw_intercept = float(y_mean + y_std * (b_std - float(np.sum(coef_std * x_mean / x_std))))
    return {
        "kind": "linear",
        "impl": impl,
        "coef": [float(value) for value in coef_std],
        "intercept": raw_intercept,
        "nonzero": int(np.count_nonzero(np.abs(coef_std) > 1e-8)),
        "params": dict(params or {}),
        "_x_mean": [float(value) for value in x_mean],
        "_x_std": [float(value) for value in x_std],
        "_y_mean": float(y_mean),
        "_y_std": float(y_std),
        "_b_std": float(b_std),
    }


def _score_columns(Xs, ys):
    """单变量 OLS（含截距）的标准化系数 = Pearson 相关系数（``x`` 已标准化）。"""
    if Xs.size == 0:
        return 0.0
    if float(np.std(Xs)) <= 0 or float(np.std(ys)) <= 0:
        return 0.0
    return float(np.corrcoef(Xs, ys)[0, 1])


# ---------------------------------------------------------------------------
# 模型 1：Lasso（L1 正则）
# ---------------------------------------------------------------------------
def _soft(value, threshold):
    return np.sign(value) * np.clip(np.abs(value) - threshold, 0.0, None)


def _lasso_coordinate_descent(Xs, ys, alpha, epochs):
    """坐标下降解 ``min 1/(2n)‖y−Xb‖² + α‖b‖₁``（``Xs`` 每列已标准化 ``xᵀx/n = 1``）。

    每轮遍历全部坐标做**闭式软阈值更新**（``b_j ← soft(x_jᵀr/n + b_j, α)``），收敛快且无需
    学习率；``epochs`` 是最大遍历轮数，达到 ``1e-10`` 的系数变化即提前停。
    返回 ``(coef, sweeps)``。
    """
    n, p = Xs.shape
    coef = np.zeros(p, dtype=float)
    if n == 0 or p == 0:
        return coef, 0
    residual = ys - Xs @ coef
    sweeps = 0
    for _ in range(max(1, int(epochs))):
        sweeps += 1
        delta = 0.0
        for j in range(p):
            column = Xs[:, j]
            if float(column @ column) <= 0:
                continue
            rho = float(column @ residual) / n + coef[j]
            updated = float(_soft(rho, alpha))
            if updated != coef[j]:
                change = updated - coef[j]
                residual -= change * column
                delta = max(delta, abs(change))
                coef[j] = updated
        if delta < 1e-10:
            break
    return coef, sweeps


def _sklearn_lasso(Xs, ys, alpha, epochs):
    """可选后端：sklearn ``Lasso``（同 ``alpha`` 口径）。不可用/失败 → ``None``。"""
    module = _load_module("sklearn")
    if module is None:
        return None
    estimator = module.linear_model.Lasso(alpha=float(alpha), max_iter=int(epochs),
                                          fit_intercept=True, tol=1e-6)
    estimator.fit(Xs, ys)
    coef = np.asarray(estimator.coef_, dtype=float)
    # sklearn 在同一标准化空间里拟合；截距归一到我们的打包口径（b_std）。
    return coef, float(estimator.intercept_)


def train_lasso(X, y, *, alpha=0.01, epochs=300, lr=0.05):
    """Lasso 回归（L1）。

    契约::

        {"coef": [...], "intercept": float, "nonzero": int, "impl": "numpy-lasso" | "sklearn",
         "kind": "linear", "params": {...}}

    * ``coef`` 是**标准化特征空间**的系数（列已标准化，故 ``|coef|`` 之间可比，
      ``coef_top`` 的排序才有意义）；``intercept`` 是原始量纲截距；
    * ``alpha`` 与 sklearn ``Lasso`` 同口径（``min 1/(2n)‖y−Xb‖² + α‖b‖₁``）；
    * ``epochs`` 为坐标下降的最大遍历轮数；``lr`` 在**坐标下降下不需要**（闭式更新），
      本实现保留该参数是为契约一致，并原样记进 ``params`` 供调用方核对——**它不参与计算**，
      如实标注而不是假装用了它；
    * ``sklearn`` 存在时优先用它（``impl="sklearn"``），导入/拟合失败一律回落 numpy。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if X.ndim != 2 or y.ndim != 1 or X.shape[0] != y.shape[0]:
        raise ValueError("train_lasso 需要 X(n,p) 与 y(n) 且行数一致")
    alpha = _clamp_float(alpha, 0.01, 0.0, 1e6)
    epochs = _clamp_int(epochs, 300, 1, 100000)
    lr = _clamp_float(lr, 0.05, 0.0, 1.0)
    x_mean, x_std = _standardizer(X)
    Xs = (X - x_mean) / x_std if X.size else X
    y_mean = float(y.mean()) if y.size else 0.0
    y_std = float(y.std(ddof=0)) if y.size else 1.0
    if y_std <= 0:
        y_std = 1.0
    ys = (y - y_mean) / y_std if y.size else y
    params = {"alpha": alpha, "epochs": epochs, "lr": lr, "solver": "coordinate-descent"}

    backend = available_backends().get("sklearn")
    if backend:
        try:
            fitted = _sklearn_lasso(Xs, ys, alpha, epochs)
        except Exception:  # noqa: BLE001 —— 可选后端失败必须回落，绝不把请求打挂
            fitted = None
        if fitted is not None:
            coef, intercept = fitted
            return _linear_model(coef, x_mean, x_std, y_mean, y_std, intercept,
                                 impl="sklearn",
                                 params={**params, "solver": "sklearn.Lasso"})

    coef, sweeps = _lasso_coordinate_descent(Xs, ys, alpha, epochs)
    return _linear_model(coef, x_mean, x_std, y_mean, y_std, 0.0, impl="numpy-lasso",
                         params={**params, "sweeps": sweeps})


# ---------------------------------------------------------------------------
# 模型 2：GBDT-lite（numpy 实现的紧凑梯度提升树）
# ---------------------------------------------------------------------------
def _bin_edges(column, bins):
    """分位数分箱边界（去重、升序）；常数特征 → 空边界（该列无分裂价值）。"""
    if column.size == 0:
        return np.zeros(0, dtype=float)
    quantiles = np.linspace(0.0, 1.0, int(bins) + 1)[1:-1]
    edges = np.unique(np.quantile(column, quantiles))
    return np.asarray(edges, dtype=float)


def _bin_codes(X, edges):
    """把 ``X`` 按每列边界映射成整数箱号 ``(n, p)``。"""
    n, p = X.shape
    codes = np.zeros((n, p), dtype=np.int64)
    for j in range(p):
        column_edges = edges[j]
        if column_edges.size:
            codes[:, j] = np.searchsorted(column_edges, X[:, j], side="left")
    return codes


def _build_tree(grad, hess, codes, depth, l2, min_child, min_gain=1e-12):
    """递归建一棵 CART（XGBoost 风格增益）；返回 JSON 安全的嵌套字典。"""
    total_g = float(grad.sum())
    total_h = float(hess.sum())
    leaf = {"value": float(-total_g / (total_h + l2)), "n": int(grad.size)}
    if depth <= 0 or grad.size < 2 * min_child:
        return leaf
    n, p = codes.shape
    best = None
    for j in range(p):
        column = codes[:, j]
        n_bins = int(column.max()) + 1 if column.size else 0
        if n_bins < 2:
            continue
        bin_index = column
        g_sum = np.bincount(bin_index, weights=grad, minlength=n_bins)
        h_sum = np.bincount(bin_index, weights=hess, minlength=n_bins)
        cum_g = np.cumsum(g_sum)[:-1]
        cum_h = np.cumsum(h_sum)[:-1]
        left_h = cum_h
        right_h = total_h - cum_h
        valid = (left_h >= min_child) & (right_h >= min_child)
        if not valid.any():
            continue
        left_g = cum_g
        right_g = total_g - cum_g
        gain = (left_g ** 2 / (left_h + l2) + right_g ** 2 / (right_h + l2)
                - total_g ** 2 / (total_h + l2))
        gain = np.where(valid, gain, -np.inf)
        position = int(np.argmax(gain))
        score = float(gain[position])
        if not math.isfinite(score) or score <= min_gain:
            continue
        if best is None or score > best[0]:
            best = (score, j, position)
    if best is None:
        return leaf
    _, feature, position = best
    mask = codes[:, feature] <= position
    if not mask.any() or mask.all():
        return leaf
    return {
        "feature": int(feature),
        "bin": int(position),
        "gain": float(best[0]),
        "n": int(grad.size),
        "left": _build_tree(grad[mask], hess[mask], codes[mask], depth - 1,
                            l2, min_child, min_gain),
        "right": _build_tree(grad[~mask], hess[~mask], codes[~mask], depth - 1,
                             l2, min_child, min_gain),
    }


def _tree_predict(tree, codes):
    """一棵树对全部样本的叶子值（``codes`` 为 ``(n, p)`` 箱号）。"""
    out = np.zeros(codes.shape[0], dtype=float)
    if out.size == 0:
        return out

    def walk(node, mask):
        if "feature" not in node:
            out[mask] = float(node["value"])
            return
        left = mask & (codes[:, node["feature"]] <= node["bin"])
        walk(node["left"], left)
        walk(node["right"], mask & ~left)

    walk(tree, np.ones(out.size, dtype=bool))
    return out


def _lightgbm_gbdt(X, y, trees, depth, lr):
    """可选后端：LightGBM ``LGBMRegressor``。不可用/失败 → ``None``。"""
    module = _load_module("lightgbm")
    if module is None:
        return None
    estimator = module.LGBMRegressor(n_estimators=int(trees), max_depth=int(depth),
                                     learning_rate=float(lr), verbose=-1)
    estimator.fit(X, y)
    return estimator


def train_gbdt(X, y, *, trees=60, depth=2, lr=0.1):
    """梯度提升决策树（**numpy 紧凑实现**，非 LightGBM 官方库）。

    契约::

        {"trees": [...], "impl": "numpy-gbdt-lite" | "lightgbm", "kind": "gbdt", "params": {...}}

    实现要点（``numpy-gbdt-lite``）：
      * 平方损失 → 梯度 ``g = pred − y``、二阶导 ``h = 1``；
      * 每特征按**分位数直方图**分箱（``GBDT_BINS``），逐箱累计 ``G/H`` 后枚举箱边界分裂，
        增益 ``G_L²/(H_L+λ) + G_R²/(H_R+λ) − G²/(H+λ)``；
      * 树深 ``depth``（规格 1..3）、叶子值 ``−G/(H+λ)·lr``（学习率已烘焙进叶子，
        ``predict`` 只做求和），树数 ``trees``；
      * 无 GOSS/EFB/直方图减法等 LightGBM 特化优化——**速度与精度都不等价于官方库**，
        故 ``impl`` 如实写 ``numpy-gbdt-lite``。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if X.ndim != 2 or y.ndim != 1 or X.shape[0] != y.shape[0]:
        raise ValueError("train_gbdt 需要 X(n,p) 与 y(n) 且行数一致")
    trees = _clamp_int(trees, 60, 1, 2000)
    depth = _clamp_int(depth, 2, 1, 3)
    lr = _clamp_float(lr, 0.1, 1e-4, 1.0)
    n, p = X.shape
    if n == 0 or p == 0:
        raise ValueError("train_gbdt 需要非空样本")

    if available_backends().get("lightgbm"):
        try:
            estimator = _lightgbm_gbdt(X, y, trees, depth, lr)
        except Exception:  # noqa: BLE001 —— 可选后端失败回落
            estimator = None
        if estimator is not None:
            return {"kind": "gbdt", "impl": "lightgbm",
                    "trees": [{"estimator": "lightgbm.LGBMRegressor"}],
                    "params": {"trees": trees, "depth": depth, "lr": lr},
                    "_estimator": estimator}

    edges = [_bin_edges(X[:, j], GBDT_BINS) for j in range(p)]
    codes = _bin_codes(X, edges)
    base = float(y.mean())
    pred = np.full(n, base, dtype=float)
    forest = []
    for _ in range(trees):
        grad = pred - y
        hess = np.ones(n, dtype=float)
        tree = _build_tree(grad, hess, codes, depth, GBDT_L2, GBDT_MIN_CHILD)
        _scale_tree(tree, lr)
        pred = pred + _tree_predict(tree, codes)
        forest.append(tree)
    return {
        "kind": "gbdt",
        "impl": "numpy-gbdt-lite",
        "trees": forest,
        "base_pred": base,
        "bin_edges": [[float(value) for value in column] for column in edges],
        "params": {"trees": trees, "depth": depth, "lr": lr, "bins": GBDT_BINS, "l2": GBDT_L2},
    }


def _scale_tree(node, factor):
    """把学习率烘焙进叶子值（就地修改）。"""
    if "feature" not in node:
        node["value"] = float(node["value"]) * float(factor)
        return
    _scale_tree(node["left"], factor)
    _scale_tree(node["right"], factor)


# ---------------------------------------------------------------------------
# 模型 3：MLP（numpy 实现的单隐层网络）
# ---------------------------------------------------------------------------
def _mlp_forward(Xs, weights, activation):
    w1, b1, w2, b2 = weights
    z1 = Xs @ w1 + b1
    if activation == "relu":
        a1 = np.clip(z1, 0.0, None)
    else:
        a1 = np.tanh(z1)
    return a1, a1 @ w2 + b2


def _mlp_gradients(Xs, ys, weights, activation):
    w1, b1, w2, b2 = weights
    n = Xs.shape[0]
    a1, out = _mlp_forward(Xs, weights, activation)
    error = out - ys
    grad_w2 = a1.T @ error / n
    grad_b2 = np.array([float(error.mean())])
    if activation == "relu":
        derivative = (a1 > 0).astype(float)
    else:
        derivative = 1.0 - a1 ** 2
    delta = (error[:, None] * w2[None, :]) * derivative
    grad_w1 = Xs.T @ delta / n
    grad_b1 = delta.mean(axis=0)
    return [grad_w1, grad_b1, grad_w2, grad_b2], float(np.mean(error ** 2))


def _adam(weights, grads, state, step, lr, beta1=0.9, beta2=0.999, epsilon=1e-8):
    """Adam 更新（就地返回新参数列表）。"""
    updated = []
    for index, (weight, grad) in enumerate(zip(weights, grads)):
        moment = state["m"][index]
        velocity = state["v"][index]
        moment = beta1 * moment + (1 - beta1) * grad
        velocity = beta2 * velocity + (1 - beta2) * (grad ** 2)
        state["m"][index] = moment
        state["v"][index] = velocity
        m_hat = moment / (1 - beta1 ** step)
        v_hat = velocity / (1 - beta2 ** step)
        updated.append(weight - lr * m_hat / (np.sqrt(v_hat) + epsilon))
    return updated


def _sklearn_mlp(Xs, ys, hidden, epochs, lr, seed):
    """可选后端：sklearn ``MLPRegressor``。不可用/失败 → ``None``。

    返回**我方结构的权重**（``[W1, b1, W2, b2]`` + 激活名），使 ``predict`` 与 numpy 版
    走同一条前向路径——避免为可选后端再造第二套推理实现。
    """
    module = _load_module("sklearn")
    if module is None:
        return None
    estimator = module.neural_network.MLPRegressor(
        hidden_layer_sizes=(int(hidden),), activation="tanh", solver="adam",
        learning_rate_init=float(lr), max_iter=int(epochs), random_state=int(seed),
        tol=1e-8)
    estimator.fit(Xs, ys)
    w1 = np.asarray(estimator.coefs_[0], dtype=float)
    b1 = np.asarray(estimator.intercepts_[0], dtype=float)
    w2 = np.asarray(estimator.coefs_[1], dtype=float).reshape(-1)
    b2 = np.asarray(estimator.intercepts_[1], dtype=float).reshape(-1)
    return [w1, b1, w2, np.array([float(b2[0]) if b2.size else 0.0])]


def train_mlp(X, y, *, hidden=16, epochs=300, lr=0.01, seed=0):
    """单隐层 MLP 回归（**numpy 实现**，Adam 优化）。

    契约::

        {"weights": [...], "impl": "numpy-mlp" | "sklearn", "kind": "mlp", "params": {...}}

    * ``hidden`` 隐层宽度、``epochs`` 全批量迭代轮数、``lr`` Adam 学习率；
    * 激活 ``tanh``；输入/输出都标准化后训练，``predict`` 时反标准化回原始量纲；
    * ``seed`` 固定随机初始化 → 同一输入**可复现**（测试依赖这一点）；
    * ``weights`` 是 JSON 安全的 ``[W1(p,h), b1(h), W2(h), b2(1)]`` 嵌套列表。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if X.ndim != 2 or y.ndim != 1 or X.shape[0] != y.shape[0]:
        raise ValueError("train_mlp 需要 X(n,p) 与 y(n) 且行数一致")
    hidden = _clamp_int(hidden, 16, 1, 512)
    epochs = _clamp_int(epochs, 300, 1, 100000)
    lr = _clamp_float(lr, 0.01, 1e-6, 1.0)
    seed = _clamp_int(seed, 0, 0, 2 ** 31 - 1)
    n, p = X.shape
    if n == 0 or p == 0:
        raise ValueError("train_mlp 需要非空样本")
    x_mean, x_std = _standardizer(X)
    Xs = (X - x_mean) / x_std
    y_mean = float(y.mean())
    y_std = float(y.std(ddof=0))
    if y_std <= 0:
        y_std = 1.0
    ys = (y - y_mean) / y_std

    params = {"hidden": hidden, "epochs": epochs, "lr": lr, "activation": "tanh",
              "optimizer": "adam", "seed": seed}
    weights = None
    impl = "numpy-mlp"
    if available_backends().get("sklearn"):
        try:
            weights = _sklearn_mlp(Xs, ys, hidden, epochs, lr, seed)
        except Exception:  # noqa: BLE001 —— 可选后端失败回落
            weights = None
        if weights is not None:
            impl = "sklearn"
            params["optimizer"] = "sklearn.MLPRegressor(adam)"
    if weights is None:
        rng = np.random.default_rng(seed)
        weights = [
            rng.normal(0.0, math.sqrt(2.0 / p), size=(p, hidden)),
            np.zeros(hidden, dtype=float),
            rng.normal(0.0, math.sqrt(1.0 / hidden), size=hidden),
            np.zeros(1, dtype=float),
        ]
        state = {"m": [np.zeros_like(weight) for weight in weights],
                 "v": [np.zeros_like(weight) for weight in weights]}
        for step in range(1, epochs + 1):
            grads, _ = _mlp_gradients(Xs, ys, weights, "tanh")
            weights = _adam(weights, grads, state, step, lr)
    return {
        "kind": "mlp",
        "impl": impl,
        "weights": [np.asarray(weight, dtype=float).tolist() for weight in weights],
        "activation": "tanh",
        "hidden": hidden,
        "params": params,
        "_x_mean": [float(value) for value in x_mean],
        "_x_std": [float(value) for value in x_std],
        "_y_mean": y_mean,
        "_y_std": y_std,
    }


# ---------------------------------------------------------------------------
# 预测：三种模型统一入口
# ---------------------------------------------------------------------------
def predict(model, X):
    """统一预测入口：按 ``model["kind"]`` 分派（``linear`` / ``gbdt`` / ``mlp``）。

    ``model`` 只接受 :func:`train_lasso` / :func:`train_gbdt` / :func:`train_mlp` /
    :func:`momentum_baseline` 的返回值——它们自带反标准化所需的一切，**没有全局状态**。
    """
    X = np.asarray(X, dtype=float)
    if model.get("_estimator") is not None:  # 仅可选 lightgbm 后端会走到
        return np.asarray(model["_estimator"].predict(X), dtype=float)
    kind = model.get("kind")
    if X.shape[0] == 0:
        return np.zeros(0, dtype=float)
    if kind == "linear":
        x_mean = np.asarray(model["_x_mean"], dtype=float)
        x_std = np.asarray(model["_x_std"], dtype=float)
        coef = np.asarray(model["coef"], dtype=float)
        z = ((X - x_mean) / x_std) @ coef + float(model["_b_std"])
        return float(model["_y_mean"]) + float(model["_y_std"]) * z
    if kind == "gbdt":
        edges = [np.asarray(column, dtype=float) for column in model["bin_edges"]]
        codes = _bin_codes(X, edges)
        out = np.full(X.shape[0], float(model["base_pred"]), dtype=float)
        for tree in model["trees"]:
            if "feature" not in tree and "value" not in tree:
                continue  # lightgbm 后端走 _estimator 分支，不会是这种占位树
            out = out + _tree_predict(tree, codes)
        return out
    if kind == "mlp":
        x_mean = np.asarray(model["_x_mean"], dtype=float)
        x_std = np.asarray(model["_x_std"], dtype=float)
        Xs = (X - x_mean) / x_std
        w1 = np.asarray(model["weights"][0], dtype=float)
        b1 = np.asarray(model["weights"][1], dtype=float)
        w2 = np.asarray(model["weights"][2], dtype=float)
        b2 = np.asarray(model["weights"][3], dtype=float)
        _, out = _mlp_forward(Xs, [w1, b1, w2, b2], model.get("activation", "tanh"))
        return float(model["_y_mean"]) + float(model["_y_std"]) * out
    raise ValueError(f"未知模型类型：{kind!r}")


# ---------------------------------------------------------------------------
# 评估（所有模型/基线的**同一口径**）
# ---------------------------------------------------------------------------
def evaluate(model, X, y, *, cost_bps=0.0):
    """统一评估：``mse`` / ``ic`` / ``hit_rate`` / ``long_short_ann_pct`` / ``n``。

    口径（三个模型与动量基线**逐项一致**，这是「同台可比」的全部含义）：
      * ``mse`` = 预测值与真实 ``t+horizon`` 收益的均方误差；
      * ``ic`` = 预测值与真实收益的 Pearson 相关（**样本内**统计量，不做横截面分组）；
      * ``hit_rate`` = ``sign(pred) == sign(y)`` 的比例（``pred`` 恰为 0 记不中）；
      * ``long_short_ann_pct`` = ``sign(pred)`` 多空仓位的年化净收益百分比：
        ``mean(pos·y − turnover·cost) × (252/horizon) × 100``，``turnover = |pos_t − pos_{t−1}|``
        （首个样本从空仓建仓，故首笔也计一次换手）；``horizon`` 取 ``model["horizon"]``，
        缺省 1（日频）。
      * ``cost_bps`` 为**单边换手成本**（基点）；默认 0 → 纯毛收益，不做任何隐含扣费。

    调用方若要按标的分别计算（换手才有意义），应传入**单个标的**的切片——见
    :func:`_aggregate`；把多标的样本混在一起逐行算换手会高估成本，本函数不做这种假设。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n = int(y.size)
    if n == 0:
        return {"mse": None, "ic": None, "hit_rate": None, "long_short_ann_pct": None, "n": 0}
    pred = predict(model, X)
    pred = np.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
    errors = pred - y
    mse = float(np.mean(errors ** 2))
    if float(np.std(pred)) > 0 and float(np.std(y)) > 0:
        ic = float(np.corrcoef(pred, y)[0, 1])
    else:
        ic = 0.0
    hit_rate = float(np.mean((pred > 0) == (y > 0)))
    position = np.sign(pred)
    turnover = np.abs(np.diff(position, prepend=0.0))
    rate = _clamp_float(cost_bps, 0.0, 0.0, 1e6) / 10000.0
    net = position * y - turnover * rate
    horizon = _clamp_int(model.get("horizon"), 1, 1, 250)
    periods = TRADING_DAYS / horizon
    return {
        "mse": _num(mse),
        "ic": _num(ic),
        "hit_rate": _num(hit_rate),
        "long_short_ann_pct": _num(float(np.mean(net)) * periods * 100.0),
        "n": n,
    }


def _aggregate(rows):
    """把「逐标的的 :func:`evaluate` 结果」按样本数加权汇总（并保留逐标的明细）。

    ``long_short_ann_pct`` 的加权平均只在**同一 horizon** 下有意义——本函数的调用方
    （:func:`run_model_suite`）保证这一点。
    """
    usable = [row for row in rows if row["n"] > 0]
    if not usable:
        return {"mse": None, "ic": None, "hit_rate": None, "long_short_ann_pct": None, "n": 0}
    total = sum(row["n"] for row in usable)
    summary = {}
    for key in ("mse", "ic", "hit_rate", "long_short_ann_pct"):
        if any(row[key] is None for row in usable):
            summary[key] = None
            continue
        summary[key] = _num(sum(row[key] * row["n"] for row in usable) / total)
    summary["n"] = int(total)
    return summary


def _evaluate_grouped(model, X, y, tickers, *, cost_bps=0.0):
    """按标的切片分别 :func:`evaluate` 后汇总：返回 ``(加权汇总, 逐标的明细)``。

    为什么必须分组：``long_short_ann_pct`` 里的换手是「相邻样本之间」的仓位变化——多标的
    池化后相邻样本常属不同标的，逐行算换手会把跨标的切换误记成换手成本。分组后每个切片
    内部才是同一标的的真实时序。
    """
    rows = []
    per_ticker = {}
    for ticker in sorted(set(tickers)):
        mask = np.array([item == ticker for item in tickers], dtype=bool)
        if not mask.any():
            continue
        row = evaluate(model, X[mask], y[mask], cost_bps=cost_bps)
        rows.append(row)
        per_ticker[str(ticker)] = row
    return _aggregate(rows), per_ticker


# ---------------------------------------------------------------------------
# 动量基线（与现有策略同定义）
# ---------------------------------------------------------------------------
def momentum_baseline(X, y, *, feature_index, feature_names=None):
    """现有动量打分 ``c[t]/c[t−window] − 1`` 的**同口径**基线。

    打分即 :data:`MOMENTUM_FEATURE` 那一列（与 ``v3_math.backtest_momentum`` 的信号、
    ``v3_analytics._strategy_analysis`` 的 ``localMomentum`` 完全同一定义）。为了让 ``mse``
    与三个模型可比（原始打分与收益不同量纲），基线用**同一训练样本**做单变量 OLS 校准
    （含截距）；IC 与 hit_rate 因此只在「系数符号」上与原打分不同——若校准系数为负，
    说明动量在样本内是反向指标，这个结论如实呈现，不做方向修正。

    返回与 :func:`train_lasso` 同形的模型字典（``kind="linear"``、``impl="momentum-score"``）。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if X.shape[0] != y.shape[0]:
        raise ValueError("momentum_baseline 需要 X 与 y 行数一致")
    if X.shape[1] == 0:
        raise ValueError("momentum_baseline 需要至少一列特征")
    index = int(feature_index)
    x_mean, x_std = _standardizer(X)
    y_mean = float(y.mean()) if y.size else 0.0
    y_std = float(y.std(ddof=0)) if y.size else 1.0
    if y_std <= 0:
        y_std = 1.0
    column = (X[:, index] - x_mean[index]) / x_std[index] if X.size else X[:, index]
    beta = _score_columns(column, (y - y_mean) / y_std) if y.size else 0.0
    coef = np.zeros(X.shape[1], dtype=float)
    coef[index] = beta
    name = (feature_names or FEATURE_NAMES)[index] if index < len(feature_names or FEATURE_NAMES) \
        else MOMENTUM_FEATURE
    model = _linear_model(coef, x_mean, x_std, y_mean, y_std, 0.0,
                          impl="momentum-score",
                          params={"feature": name, "feature_index": index,
                                  "calibration": "univariate-ols"})
    model["name"] = "momentum"
    return model


# ---------------------------------------------------------------------------
# 统一模型字典 → 响应里的紧凑摘要（**不含权重明细**，避免把大对象塞进 JSON）
# ---------------------------------------------------------------------------
def _summary(model, *, coef_top=5, feature_names=None):
    """模型字典 → 响应摘要：``{name, impl, kind, params, coef_top?…}``（JSON 安全）。"""
    names = list(feature_names or FEATURE_NAMES)
    out = {
        "name": model.get("name") or model.get("impl"),
        "impl": model.get("impl"),
        "kind": model.get("kind"),
        "params": dict(model.get("params") or {}),
    }
    if model.get("kind") == "linear":
        coef = np.asarray(model.get("coef"), dtype=float)
        index = list(range(min(coef.size, len(names))))
        index.sort(key=lambda item: abs(coef[item]), reverse=True)
        out["intercept"] = _num(model.get("intercept"))
        out["nonzero"] = int(np.count_nonzero(np.abs(coef) > 1e-8))
        out["coef_top"] = [{"feature": names[item] if item < len(names) else f"f{item}",
                            "coef": _num(coef[item])} for item in index[:coef_top]]
    elif model.get("kind") == "gbdt":
        forest = model.get("trees") or []
        out["n_trees"] = len(forest)
        out["first_tree"] = forest[0] if forest and isinstance(forest[0], dict) else None
    elif model.get("kind") == "mlp":
        out["hidden"] = int(model.get("hidden") or 0)
        out["activation"] = model.get("activation")
        weights = model.get("weights") or []
        out["weight_shapes"] = [list(np.asarray(weight).shape) for weight in weights]
    return out


def _json_safe(value):
    """把嵌套结构里的 numpy 标量换成 Python 标量（信封要求纯 JSON）。"""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, np.ndarray):
        return [_json_safe(item) for item in value.tolist()]
    return value


# ---------------------------------------------------------------------------
# 编排：一趟跑完三模型 + 动量基线
# ---------------------------------------------------------------------------
def run_model_suite(bars_by_ticker, *, window=20, horizon=1, cost_bps=0.0,
                    train_ratio=0.7, seed=0):
    """特征 → 三模型 + 动量基线，**同一留出集、同一评估口径**。

    契约见 FR-STRAT-002；补充（本实现如实写出的部分）：

    * ``train_ratio``（缺省 0.7）：按 ``(日期, 标的)`` 升序**时序留出**——前 70% 训练、
      后 30% 评估；``models[].metrics`` / ``baseline.metrics`` 都是**样本外**指标，
      ``metrics_in_sample`` 另附训练集指标供对照（不拿它当结论）；
    * ``per_ticker``：样本外指标按标的的明细（换手口径见 :func:`evaluate`）；
    * ``n_samples < MIN_SAMPLES`` → 抛 :class:`InsufficientSample`（**不硬跑**）；
    * ``seed``：MLP 随机初始化种子（保证可复现）；
    * ``cost_bps``：单边换手成本（基点），同时用于三个模型与基线（同口径）。
    """
    window = _clamp_int(window, 20, 2, 500)
    horizon = _clamp_int(horizon, 1, 1, 250)
    cost_bps = _clamp_float(cost_bps, 0.0, 0.0, 1e6)
    train_ratio = _clamp_float(train_ratio, 0.7, 0.3, 0.9)
    built = build_features(bars_by_ticker, window=window, horizon=horizon)
    X, y = built["X"], built["y"]
    tickers = built["tickers"]
    names = built["feature_names"]
    n = int(X.shape[0])
    if n < MIN_SAMPLES:
        raise InsufficientSample(n, MIN_SAMPLES, window=window, horizon=horizon,
                                 tickers=sorted(set(tickers)))

    split = max(1, min(n - 1, int(round(n * train_ratio))))
    train_slice = slice(0, split)
    test_slice = slice(split, n)
    X_train, y_train = X[train_slice], y[train_slice]
    X_test, y_test = X[test_slice], y[test_slice]
    train_tickers = tickers[train_slice]
    test_tickers = tickers[test_slice]

    def finish(model):
        model = dict(model)
        model["horizon"] = horizon
        return model

    trained = [
        ("lasso", finish(train_lasso(X_train, y_train))),
        ("gbdt", finish(train_gbdt(X_train, y_train))),
        ("mlp", finish(train_mlp(X_train, y_train, seed=seed))),
    ]
    momentum_index = names.index(MOMENTUM_FEATURE) if MOMENTUM_FEATURE in names else 0
    baseline = finish(momentum_baseline(X_train, y_train, feature_index=momentum_index,
                                        feature_names=names))

    models = []
    for name, model in trained:
        metrics, per_ticker = _evaluate_grouped(model, X_test, y_test, test_tickers,
                                                cost_bps=cost_bps)
        in_sample, _ = _evaluate_grouped(model, X_train, y_train, train_tickers,
                                         cost_bps=cost_bps)
        summary = _summary(model, feature_names=names)
        summary["name"] = name
        models.append(_json_safe({**summary, "metrics": metrics,
                                  "metrics_in_sample": in_sample,
                                  "per_ticker": per_ticker}))

    base_metrics, base_per_ticker = _evaluate_grouped(baseline, X_test, y_test, test_tickers,
                                                      cost_bps=cost_bps)
    base_in_sample, _ = _evaluate_grouped(baseline, X_train, y_train, train_tickers,
                                          cost_bps=cost_bps)
    baseline_out = _json_safe({"name": "momentum",
                               "impl": baseline["impl"],
                               "params": baseline["params"],
                               "metrics": base_metrics,
                               "metrics_in_sample": base_in_sample,
                               "per_ticker": base_per_ticker})

    backends = available_backends()
    return _json_safe({
        "as_of": built["dates"][-1] if built["dates"] else None,
        "generated_at": _now_iso(),
        "window": window,
        "horizon": horizon,
        "cost_bps": cost_bps,
        "n_samples": n,
        "n_train": int(X_train.shape[0]),
        "n_test": int(X_test.shape[0]),
        "split": f"时序留出 {round(train_ratio * 100)}/{round((1 - train_ratio) * 100)}"
                 f"（按 (日期, 标的) 升序）",
        "tickers": sorted(set(tickers)),
        "feature_names": names,
        "models": models,
        "baseline": baseline_out,
        "backends": backends,
        "notes": _notes(window, horizon, cost_bps, backends, n, X_test.shape[0]),
    })


def _now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _notes(window, horizon, cost_bps, backends, n_samples, n_test):
    """如实写进响应的口径与限制说明（前端直接展示，不藏在文档里）。"""
    notes = [
        "PIT：t 日特征只用 ≤t 的收盘价；标签为 t+horizon 收益——无未来函数"
        "（tests/test_v3_ml.py::PITTests 用「打乱 t 之后的数据」逐元素钉住）",
        f"特征（{len(FEATURE_NAMES)} 列，全部由收盘价推出）：{'/'.join(FEATURE_NAMES)}；"
        f"window={window}、horizon={horizon}",
        f"样本：池化后 {n_samples} 条（跨标的），样本外留出 {n_test} 条；"
        "留出按 (日期, 标的) 升序而非随机——时间序列不能随机切分",
        "指标均为**样本外**；metrics_in_sample 仅作对照，不作为结论",
        "已知偏差（不掩盖）：未做 purging/embargo——相邻样本的 horizon 标签区间重叠，"
        "指标会偏乐观；未做横截面中性化；未做多重检验校正",
        f"交易成本按单边换手计，cost_bps={cost_bps}；long_short_ann_pct 为 "
        f"sign(pred) 多空仓位的年化净收益（252/horizon 年化），按标的切片后加权汇总",
        "动量基线 = 现有动量打分 c[t]/c[t-window]-1 的同口径评估"
        "（同一训练样本做单变量 OLS 校准以便与模型比 MSE；IC/hit_rate 的符号即校准符号）",
        f"可选后端探测：sklearn={backends['sklearn']}、lightgbm={backends['lightgbm']}；"
        "本部署没有这两个库 → 三个模型均为 numpy 实现（impl=numpy-*），"
        "numpy-gbdt-lite 不是 LightGBM 官方库，精度/速度均不等价",
        "只读：不触发任何写/交易端点，不落盘、不改前端、不启用策略",
    ]
    return notes
