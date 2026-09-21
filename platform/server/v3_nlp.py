"""NLP 情绪引擎（规格 **FR-STRAT-003**）：把资讯变成可用的情绪因子与事件标签。

本模块是平台**唯一**的中文金融情绪实现（纯标准库 + 无三方依赖）。它做五件事：

  1. ``DEFAULT_LEXICON`` / ``NEGATORS`` / ``INTENSIFIERS`` —— **自研**中文金融情绪词典、
     否定词表、程度副词表（不抓取任何未授权词典，全部人工按公开市场语义构造）；
  2. ``segment(text)`` —— 轻量中文切分：**最大正向匹配 + 词典优先**，未命中词典的汉字
     串**退化到字符 bigram**（不需要 jieba，不引入任何分词依赖）；
  3. ``score_text`` / ``score_documents`` / ``sentiment_factor`` —— 可解释打分：命中词、
     否定翻转、程度放大、按时间半衰加权；
  4. ``classify_events`` / ``EVENT_RULES`` —— **事件识别**（FR-STRAT-003 的另一半）：
     33 类资本市场事件的触发词条表 + 与打分同一套否定/程度修饰口径，输出
     ``type/label/direction/matched/confidence``，零命中不硬造；
  5. ``sentiment_report`` + ``register`` —— 把上面四步接到一条资讯信封上，产出
     ``GET /api/v3/sentiment`` 的响应体（含 ``events`` 事件聚合与可选 ``doc_events``），
     并由本模块**自带 router** 注册该端点（``server/app.py`` 的 V3 子模块自动装配循环
     会 ``import server.v3_nlp`` 并调用 ``register(app, v3_run, home)``）。取资讯只
     **惰性复用** ``server.v3_sources`` 的公开函数（``detect_market`` /
     ``normalize_a_share_symbol`` / ``fetch_news`` / ``Deps``），本模块**不**新增第二份
     取数实现、也**不**改 ``v3_sources`` 的结构。

算法（口径逐条可核对，改口径必须同步改 ``docs/e2e-and-data-gaps.md`` 的 §十）::

    词条命中      w = polarity(∈[-1,1]) × 程度因子 × (否定 ? -NEGATION_DECAY : 1)
                  w 截断到 [-WEIGHT_CAP, +WEIGHT_CAP]
    单文档分      d = (Σw / Σ|w|) × (1 - e^(-Σ|w| / MASS_HALF))          ∈ (-1, 1)
                  ↑方向（有符号占比）        ↑置信（证据量的饱和函数）
    多文档分      s = Σ(d_i × 0.5^(age_i / half_life_hours)) / Σ(0.5^(age_i / half_life_hours))
                  只对**命中过词典**的文档求加权平均

三条纪律（与仓库「数据诚实」一致）：

  * **不返回 0 冒充中性**：没有任何命中 → ``score = None``（不是 0.0）。空输入、
    无新闻、词典全不命中都走这条路；调用方必须看 ``coverage`` 而不是把 None 读成 0。
  * **可解释**：每个文档分的 ``hits`` 都带 ``term/polarity/weight``，外加 ``negated`` /
    ``intensified`` 计数；多文档层汇总 ``top_terms``。响应能回答「为什么是这个分」。
  * **只读**：本模块不发任何网络请求、不写盘、不下单；取数由调用方注入
    （``sentiment_report(news_fetch, ...)`` 的 ``news_fetch``）。
"""
from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timedelta, timezone

try:  # pragma: no cover - 有 zoneinfo 就用真时区，没有退到固定 +08:00
    from zoneinfo import ZoneInfo

    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    CN_TZ = timezone(timedelta(hours=8))

#: 口径版本串：进响应 ``method`` 字段，改算法/词典必须同时改这个串（前端与文档按它对齐）。
METHOD = "lexicon-v1（自研词典+否定/程度修饰+时间半衰）"

# ── 词典 ───────────────────────────────────────────────────────────────────────
# 构造原则（可复核）：只收**公开市场语义明确**的中文金融词；一词一极性，量纲统一到
# [-1,1]；强词（涨停/立案调查/财务造假）靠 0.8~1.0，弱词（增长/上升/回落）靠 0.3~0.6。
# 极性方向按「对股东权益/现金流的含义」定，不按「字面褒贬」定——例如「减持」「解禁」
# 是负面（供给增加），「回购」「增持」是正面（供给收缩）。
#
# 复合词优先收**完整形态**：「不及预期」「超预期」是整体词条，因此不会被 ``不`` 拆成
# 「否定 + 预期」——中文里它们的否定语义不能靠单字规则复原。

#: 强正面（0.75~1.0）：确定性利好事件。
STRONG_POSITIVE = {
    "涨停": 0.90, "涨停板": 0.90, "涨停潮": 0.85, "一字涨停": 0.95,
    "大涨": 0.80, "暴涨": 0.85, "飙升": 0.85, "猛涨": 0.85, "飙涨": 0.85,
    "创新高": 0.85, "历史新高": 0.90, "创历史新高": 0.90,
    "业绩爆发": 0.85, "扭亏为盈": 0.85, "扭亏": 0.75, "摘帽": 0.75,
    "重大突破": 0.80, "重大利好": 0.95, "特大利好": 1.00, "重磅利好": 0.95,
    "超预期": 0.75, "超出预期": 0.75, "超市场预期": 0.75,
    "大幅预增": 0.85, "业绩预增": 0.75, "预增": 0.70,
    "供不应求": 0.75, "订单饱满": 0.75, "满产满销": 0.70, "量价齐升": 0.75,
    "重大合同": 0.70, "中标": 0.70, "获批": 0.60, "获批准": 0.60,
}

#: 中正面（0.5~0.74）：方向明确的经营/资金/评级利好。
POSITIVE = {
    "利好": 0.70, "净利润增长": 0.70, "高增长": 0.70, "快速增长": 0.70,
    "强劲增长": 0.80, "营收增长": 0.60, "订单增长": 0.65, "订单充足": 0.65,
    "订单增加": 0.60, "新增订单": 0.60, "毛利提升": 0.60, "毛利率提升": 0.65,
    "现金流改善": 0.60, "现金流充裕": 0.55, "市占率提升": 0.60,
    "景气度提升": 0.70, "估值修复": 0.55, "增持评级": 0.60, "买入评级": 0.70,
    "上调评级": 0.75, "上调盈利预测": 0.70, "上调目标价": 0.70, "推荐评级": 0.60,
    "回购": 0.60, "股份回购": 0.60, "增持": 0.60, "股东增持": 0.65, "高管增持": 0.65,
    "举牌": 0.65, "加仓": 0.50, "抢筹": 0.60, "主力流入": 0.60, "净流入": 0.55,
    "资金流入": 0.55, "北向资金净买入": 0.60, "领涨": 0.60, "放量上涨": 0.60,
    "高分红": 0.60, "分红": 0.50, "派息": 0.50, "高送转": 0.60, "高股息": 0.50,
    "战略合作": 0.55, "强强联合": 0.60, "政企合作": 0.45, "签署合同": 0.55,
    "受益": 0.55, "景气": 0.55, "政策支持": 0.60, "国产替代": 0.50,
    "需求旺盛": 0.70, "价格上涨": 0.55, "提价": 0.55, "涨价": 0.55, "扩产": 0.50,
}

#: 弱正面（0.3~0.49）：方向为正、但单独出现不足以定性的表述。
WEAK_POSITIVE = {
    "增长": 0.55, "增加": 0.40, "提升": 0.45, "改善": 0.50, "向好": 0.55,
    "盈利": 0.50, "净利润增加": 0.60, "减亏": 0.50, "转正": 0.50, "复苏": 0.55,
    "回暖": 0.50, "回升": 0.45, "反弹": 0.45, "走强": 0.50, "上涨": 0.50,
    "走高": 0.45, "攀升": 0.50, "突破": 0.55, "看好": 0.60, "乐观": 0.50,
    "买入": 0.55, "推荐": 0.50, "催化": 0.45, "龙头": 0.40, "低估": 0.50,
    "安全边际": 0.40, "补贴": 0.45, "降准": 0.45, "降息": 0.40, "减税": 0.45,
    "投产": 0.50, "量产": 0.50, "产能扩张": 0.50, "认证通过": 0.50, "专利": 0.35,
    "获奖": 0.35, "入选": 0.40, "纳入指数": 0.50, "首次覆盖": 0.35, "放量": 0.35,
    "增资": 0.35, "资产注入": 0.50, "引入战略投资者": 0.50, "合作": 0.40,
}

#: 强负面（-0.75~-1.0）：退市/立案/违约/造假级事件。
STRONG_NEGATIVE = {
    "跌停": -0.90, "跌停板": -0.90, "一字跌停": -0.95, "大跌": -0.80,
    "暴跌": -0.85, "崩盘": -0.95, "闪崩": -0.85, "重挫": -0.80, "跳水": -0.75,
    "创新低": -0.80, "历史新低": -0.90, "创历史新低": -0.90, "新低": -0.70,
    "退市": -1.00, "退市风险": -0.95, "退市警示": -0.90, "暂停上市": -0.90,
    "终止上市": -0.90, "被ST": -0.90, "立案": -0.90, "立案调查": -0.95,
    "被立案": -0.95, "调查": -0.60, "处罚": -0.80, "行政处罚": -0.80,
    "违约": -0.95, "债务违约": -0.95, "逾期": -0.70, "债务逾期": -0.80,
    "资金链断裂": -0.95, "破产": -1.00, "破产重整": -0.90,
    "造假": -0.95, "财务造假": -1.00, "舞弊": -0.95, "内幕交易": -0.90,
    "操纵股价": -0.95, "爆雷": -0.90, "暴雷": -0.90, "爆仓": -0.90,
    "被执行": -0.70, "失信被执行人": -0.85, "失信": -0.80, "查封": -0.75,
}

#: 中负面（-0.5~-0.74）：业绩/治理/资金面的明确利空。
NEGATIVE = {
    "利空": -0.70, "亏损": -0.70, "巨亏": -0.90, "由盈转亏": -0.85, "预亏": -0.75,
    "业绩预亏": -0.80, "业绩预减": -0.75, "预减": -0.70, "净利润下降": -0.65,
    "营收下降": -0.60, "下滑": -0.60, "萎缩": -0.60, "不及预期": -0.70,
    "低于预期": -0.70, "逊于预期": -0.70, "未达预期": -0.70, "低于市场预期": -0.75,
    "减持": -0.60, "股东减持": -0.65, "高管减持": -0.65, "清仓式减持": -0.85,
    "减持计划": -0.60, "清仓": -0.70, "套现": -0.60, "质押": -0.50,
    "股权质押": -0.55, "高比例质押": -0.70, "平仓": -0.75, "强平": -0.80,
    "解禁": -0.50, "限售解禁": -0.55, "商誉减值": -0.80, "计提减值": -0.75,
    "资产减值": -0.70, "存货跌价": -0.60, "坏账": -0.70, "违规": -0.75,
    "违法违规": -0.85, "问询函": -0.60, "关注函": -0.50, "监管函": -0.60,
    "警示函": -0.65, "非标意见": -0.80, "保留意见": -0.70, "无法表示意见": -0.85,
    "停牌": -0.50, "终止重组": -0.60, "终止合作": -0.55, "解除合同": -0.60,
    "诉讼": -0.60, "被诉": -0.65, "败诉": -0.70, "仲裁": -0.50, "冻结": -0.70,
    "账户冻结": -0.80, "需求疲软": -0.70, "需求下滑": -0.70, "供过于求": -0.65,
    "产能过剩": -0.65, "价格战": -0.60, "库存高企": -0.60, "召回": -0.65,
}

#: 弱负面（-0.3~-0.49）：方向为负的常规表述。
WEAK_NEGATIVE = {
    "下降": -0.50, "减少": -0.40, "下跌": -0.50, "走低": -0.45, "回落": -0.45,
    "杀跌": -0.70, "破发": -0.60, "破净": -0.40, "计提": -0.50, "降价": -0.55,
    "跌价": -0.60, "主力流出": -0.60, "净流出": -0.55, "抛售": -0.70,
    "减仓": -0.50, "卖出评级": -0.75, "下调评级": -0.75, "下调目标价": -0.70,
    "下调盈利预测": -0.70, "看空": -0.65, "悲观": -0.55, "风险": -0.40,
    "风险提示": -0.45, "警示": -0.50, "承压": -0.50, "疲软": -0.55,
    "低迷": -0.60, "困境": -0.70, "危机": -0.80, "踩雷": -0.75,
    "停产": -0.75, "减产": -0.60, "限产": -0.50, "裁员": -0.60, "事故": -0.70,
    "安全事故": -0.80, "爆炸": -0.80, "起火": -0.70, "泄漏": -0.60,
    "环保处罚": -0.75, "罚款": -0.70, "警告": -0.50, "谴责": -0.60,
    "公开谴责": -0.70, "通报批评": -0.60, "辞职": -0.45, "离职": -0.40,
    "高管变动": -0.30, "董事长辞职": -0.55, "审计师变更": -0.50,
    "延期": -0.50, "中止": -0.55, "撤回": -0.50, "失败": -0.65, "未通过": -0.60,
    "被否": -0.70, "否决": -0.60, "取消订单": -0.75, "订单取消": -0.75,
    "丢单": -0.70, "补贴退坡": -0.60, "退坡": -0.50, "加征关税": -0.60,
    "制裁": -0.75, "实体清单": -0.80, "出口管制": -0.70, "禁止": -0.60,
}

#: 中文金融情绪词典：``词 → 极性 ∈ [-1,1]``（≥150 条，按极性分组构造；可整体替换）。
DEFAULT_LEXICON: dict[str, float] = {}
for _group in (STRONG_POSITIVE, POSITIVE, WEAK_POSITIVE,
               STRONG_NEGATIVE, NEGATIVE, WEAK_NEGATIVE):
    DEFAULT_LEXICON.update(_group)
del _group

#: 否定词：命中后对**后一个词典词条**翻转符号并衰减（``NEGATION_DECAY``）。
#: 全部是「让后文命题取反」的语法否定标记；「终止/取消」这类语义否定不在此表——
#: 它们作为独立的负面词条收在词典里（``终止合作`` / ``取消订单``），语义更准。
NEGATORS: tuple[str, ...] = (
    "不", "不再", "不会", "不是", "并非", "非", "未", "未能", "未获", "未予",
    "没有", "无", "无法", "难以", "缺乏", "缺少", "否认", "避免", "拒绝",
    "尚未", "不足以", "不足", "低于", "免于",
)

#: 程度副词：命中后放大/缩小**后一个词典词条**的权重。>1 放大，<1 削弱。
INTENSIFIERS: dict[str, float] = {
    # 放大
    "大幅": 1.60, "大幅度": 1.60, "显著": 1.50, "明显": 1.40, "急剧": 1.70,
    "剧烈": 1.60, "狂": 1.60, "猛": 1.50, "强劲": 1.50, "全面": 1.30,
    "充分": 1.25, "极大": 1.60, "极其": 1.70, "极为": 1.70, "极度": 1.75,
    "高度": 1.40, "严重": 1.70, "重大": 1.50, "巨额": 1.55, "进一步": 1.20,
    "持续": 1.15, "连续": 1.15, "加速": 1.35, "快速": 1.30, "高速": 1.35,
    "全面性": 1.30, "创纪录": 1.50, "史上": 1.40,
    # 「非常 / 相当 / 尤为」必须收进来：否则「非常」会被切成否定词 ``非`` + 未登录字，
    # 把「非常大幅增长」误判成负面（作用域限制见 v3_nlp 模块头与文档 §十）。
    "非常": 1.50, "相当": 1.30, "尤为": 1.50, "格外": 1.40,
    # 削弱
    "略微": 0.60, "小幅": 0.70, "轻微": 0.60, "微幅": 0.60, "略": 0.65,
    "稍": 0.65, "有所": 0.75, "个别": 0.70, "部分": 0.85, "暂时": 0.80,
}

#: 否定翻转的衰减系数：翻转后只保留 |w| × 0.65（「没增长」弱于「下滑」的确定性）。
NEGATION_DECAY = 0.65
#: 修饰词作用域（往后看的词条数）：只绑定**最近的一个**词典词条，遇到标点/空白即断开。
MODIFIER_WINDOW = 3
#: 单个命中权重的绝对值上限（防多重程度副词叠加把量纲打爆）。
WEIGHT_CAP = 1.5
#: 单文档置信饱和常数：``Σ|w| == MASS_HALF`` 时置信约 0.63。
MASS_HALF = 2.0
#: 正/负文档的判定带宽：|文档分| ≤ 该值为「中性」。
NEUTRAL_BAND = 0.05
#: 情绪因子默认时间半衰（小时）：48h 前的资讯权重减半。
DEFAULT_HALF_LIFE_HOURS = 48.0
#: 默认打分返回的 ``top_terms`` 条数。
TOP_TERMS = 8

#: 作用域断点（标点/空白）：否定与程度副词**不跨标点生效**。
PUNCTUATION = frozenset(
    "，。！？；：、,.!?;:（）()《》〈〉【】[]{}「」『』“”‘’\"'…—～~·|/\\*&@#^%+=<>"
)


def _vocabulary(lexicon):
    """切分用的词表 = 情绪词典 ∪ 否定词 ∪ 程度副词（词条形态必须被切出来才能修饰）。"""
    terms = set(lexicon) | set(NEGATORS) | set(INTENSIFIERS)
    return {term for term in terms if term}


def _build_index(lexicon):
    """首字 → 候选词表（按**长度降序**，即最大正向匹配的贪心顺序）。"""
    index: dict[str, list[str]] = {}
    for term in _vocabulary(lexicon):
        index.setdefault(term[0], []).append(term)
    for terms in index.values():
        terms.sort(key=lambda item: (-len(item), item))
    return index


_DEFAULT_INDEX = _build_index(DEFAULT_LEXICON)
_NEGATOR_SET = frozenset(NEGATORS)


def _text(value):
    """任意值 → 去空白字符串（``None``/NaN → ``""``，不产生 ``"nan"``）。"""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return ""
    text = str(value).strip()
    return "" if text in ("nan", "None", "NaT", "<NA>") else text


# ── 切分 ───────────────────────────────────────────────────────────────────────


def segment(text, lexicon=None):
    """轻量中文切分：**最大正向匹配 + 词典优先**，未命中的汉字串退化到**字符 bigram**。

    规则（按优先级）::

        1. 词典/否定词/程度副词的最长匹配（长度降序贪心，不需要 jieba）；
        2. 空白与标点各自成一个 token —— 它们同时是**否定/程度作用域的断点**；
        3. ASCII 字母数字（含 ``. % + -``）各自成串（``20%`` / ``1.6T`` 不被拆）；
        4. 其余（未命中词典的汉字）取当前位置的**字符 bigram** 并前进 1 个字符——
           这样"公司业绩大幅增长"里 ``大幅``/``增长`` 仍会被后续位置捕获，
           词典词条不会因为前面有未登录字而被吞掉；
        5. bigram 不跨空白/标点（避免把两个句子的字拼成一个假词）。

    ``lexicon=None`` 用 ``DEFAULT_LEXICON``（``segment(text)`` 即可直接调用）。
    """
    table = DEFAULT_LEXICON if lexicon is None else lexicon
    if lexicon is None:
        index = _DEFAULT_INDEX
    else:
        index = _build_index(table)
    raw = _text(text)
    tokens: list[str] = []
    size = len(raw)
    position = 0
    while position < size:
        char = raw[position]
        if char.isspace():
            tokens.append(char)
            position += 1
            continue
        if char in PUNCTUATION:
            tokens.append(char)
            position += 1
            continue
        matched = ""
        for term in index.get(char, ()):  # 同首字候选已按长度降序
            if raw.startswith(term, position):
                matched = term
                break
        if matched:
            tokens.append(matched)
            position += len(matched)
            continue
        if char.isascii() and (char.isalnum() or char in ".%+-"):
            end = position
            while end < size and raw[end].isascii() and (raw[end].isalnum() or raw[end] in ".%+-"):
                end += 1
            tokens.append(raw[position:end])
            position = end
            continue
        following = raw[position + 1] if position + 1 < size else ""
        if following and not following.isspace() and following not in PUNCTUATION:
            tokens.append(raw[position:position + 2])
            position += 1
        else:
            tokens.append(char)
            position += 1
    return tokens


# ── 打分 ───────────────────────────────────────────────────────────────────────


def _document_score(total, mass):
    """``Σw`` / ``Σ|w|`` → 有符号文档分（无证据返回 ``None``，**不返回 0**）。"""
    if mass <= 0:
        return None
    direction = total / mass
    confidence = 1.0 - math.exp(-mass / MASS_HALF)
    return round(direction * confidence, 6)


def score_text(text, *, lexicon=None):
    """给一段中文文本打分 → ``{score, hits, negated, intensified, mass}``。

    * ``score``：``None`` 表示**没有任何词典命中**（不返回 0 冒充中性）；否则 ∈ (-1,1)；
    * ``hits``：按出现顺序的命中明细 ``{term, polarity, weight}`` —— 这就是「为什么是
      这个分」的原始证据；
    * ``negated`` / ``intensified``：被否定翻转 / 被程度副词修饰的命中数；
    * ``mass``：``Σ|weight|``（证据量，多文档层与置信度都按它加权）。

    ``lexicon`` 可整体替换（默认 ``DEFAULT_LEXICON``）；否定词与程度副词表固定。
    """
    table = DEFAULT_LEXICON if lexicon is None else lexicon
    tokens = segment(text, lexicon=lexicon)
    hits: list[dict] = []
    negated = 0
    intensified = 0
    total = 0.0
    mass = 0.0
    for index, token in enumerate(tokens):
        polarity = table.get(token)
        if polarity is None:
            continue
        factor = 1.0
        flipped = False
        touched = False
        # 只绑定最近的一个词典词条；跨标点/空白断开（作用域纪律）
        for back in range(index - 1, max(-1, index - 1 - MODIFIER_WINDOW), -1):
            previous = tokens[back]
            if previous.isspace() or previous in PUNCTUATION:
                break
            if previous in table:
                break
            if previous in _NEGATOR_SET:
                flipped = True
                continue
            value = INTENSIFIERS.get(previous)
            if value is not None:
                factor *= value
                touched = True
        weight = polarity * factor * (-NEGATION_DECAY if flipped else 1.0)
        weight = max(-WEIGHT_CAP, min(WEIGHT_CAP, weight))
        hits.append({"term": token, "polarity": round(float(polarity), 4),
                     "weight": round(weight, 4)})
        total += weight
        mass += abs(weight)
        if flipped:
            negated += 1
        if touched:
            intensified += 1
    return {
        "score": _document_score(total, mass),
        "hits": hits,
        "negated": negated,
        "intensified": intensified,
        "mass": round(mass, 6),
    }


def _as_aware(value):
    """``None`` / ``datetime`` / epoch / ISO 串 → aware datetime（朴素时间按 ``CN_TZ``）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=CN_TZ)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # 毫秒时间戳
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    return parse_time(str(value))


_TIME_PATTERNS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y%m%d%H%M%S", "%Y%m%d",
)
_TIME_CLEAN = re.compile(r"\s+")


def parse_time(raw):
    """资讯时间串 → aware datetime；解析不出来返回 ``None``（**不猜时间**）。

    支持 ``2026-09-18 06:38:00``（AKShare 东财口径）、``2026-09-18T06:38:00Z``、
    ``2026/09/18``、``20260918`` 与 epoch（秒/毫秒）。**朴素时间按北京时间**
    （``CN_TZ``）解释——中文资讯源给的都是本地时间，按 UTC 读会偏 8 小时、把半衰算错。
    """
    text = _text(raw)
    if not text:
        return None
    candidate = _TIME_CLEAN.sub(" ", text).strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
    if parsed is None:
        for pattern in _TIME_PATTERNS:
            try:
                parsed = datetime.strptime(candidate, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CN_TZ)


def _doc_text(doc):
    """文档 → 待打分文本。支持 ``str`` 与含标题/摘要键的 ``dict``（中英文键都认）。"""
    if isinstance(doc, dict):
        parts = []
        seen = set()
        for key in ("title", "summary", "text", "content",
                    "新闻标题", "新闻内容", "摘要", "内容"):
            value = doc.get(key)
            if not isinstance(value, str):
                continue
            piece = value.strip()
            if piece and piece not in seen:
                seen.add(piece)
                parts.append(piece)
        return "。".join(parts)
    return _text(doc)


def _doc_time(doc):
    if isinstance(doc, dict):
        for key in ("published_at", "published", "time", "date", "datetime",
                    "发布时间", "时间", "日期"):
            value = doc.get(key)
            if value in (None, ""):
                continue
            parsed = parse_time(value) if not isinstance(value, datetime) else _as_aware(value)
            if parsed is not None:
                return parsed
        return None
    return None


def score_documents(docs, *, half_life_hours=DEFAULT_HALF_LIFE_HOURS, now=None, lexicon=None):
    """多文档情绪：**按时间半衰加权**的平均分 + 覆盖率 + 可解释榜单。

    聚合口径::

        w_i = 0.5 ^ (age_i / half_life_hours)          # age 为文档时间到 now 的小时数
        s   = Σ(d_i × w_i) / Σ(w_i)                    # 只对**命中过词典**的文档求和

    关键纪律（每条都有单测）:

      * 一篇都没命中 → ``score = None``（**不是 0**），``coverage = 0.0``；
      * ``coverage = scored / documents`` —— 命中词典的文档占比，回答「这个分代表多少
        资讯」；未命中的文档既不算正也不算负，它们只体现在 ``documents - scored`` 里；
      * 缺时间戳的文档按 ``as_of`` 计权（``w = 1.0``）并在 ``notes`` 里计数说明，
        **不静默丢弃、也不假装它们有历史时间**；
      * ``top_terms`` 按 ``|Σweight|`` 排序，是「为什么是这个分」的汇总证据。
    """
    try:
        half_life = float(half_life_hours)
    except (TypeError, ValueError):
        half_life = DEFAULT_HALF_LIFE_HOURS
    if not half_life or half_life <= 0:
        half_life = DEFAULT_HALF_LIFE_HOURS
    table = DEFAULT_LEXICON if lexicon is None else lexicon
    moment = _as_aware(now) or datetime.now(timezone.utc)

    items: list[dict] = []
    undated = 0
    for doc in docs or []:
        published = _doc_time(doc)
        if published is None:
            undated += 1
            age_hours = 0.0
        else:
            age_hours = max(0.0, (moment - published).total_seconds() / 3600.0)
        weight = 0.5 ** (age_hours / half_life)
        result = score_text(_doc_text(doc), lexicon=lexicon)
        items.append({
            "score": result["score"],
            "hits": result["hits"],
            "negated": result["negated"],
            "intensified": result["intensified"],
            "mass": result["mass"],
            "time_weight": weight,
            "age_hours": round(age_hours, 3),
            "published": published.isoformat() if published is not None else None,
        })

    documents = len(items)
    scored_items = [item for item in items if item["score"] is not None]
    scored = len(scored_items)
    coverage = round(scored / documents, 6) if documents else 0.0

    weight_sum = sum(item["time_weight"] for item in scored_items)
    if not scored_items or weight_sum <= 0:
        score = None
    else:
        score = round(
            sum(item["score"] * item["time_weight"] for item in scored_items) / weight_sum, 6
        )

    positive = sum(1 for item in scored_items if item["score"] > NEUTRAL_BAND)
    negative = sum(1 for item in scored_items if item["score"] < -NEUTRAL_BAND)
    neutral = scored - positive - negative

    tally: dict[str, dict] = {}
    for item in items:
        for hit in item["hits"]:
            entry = tally.get(hit["term"])
            if entry is None:
                entry = {"term": hit["term"], "polarity": hit["polarity"],
                         "count": 0, "weight": 0.0}
                tally[hit["term"]] = entry
            entry["count"] += 1
            entry["weight"] += hit["weight"]
    top_terms = [
        {"term": entry["term"], "polarity": round(entry["polarity"], 4),
         "count": entry["count"], "weight": round(entry["weight"], 4)}
        for entry in sorted(tally.values(), key=lambda e: (-abs(e["weight"]), -e["count"], e["term"]))
    ][:TOP_TERMS]

    stamps = [item["published"] for item in items if item["published"]]
    latest_at = max(stamps) if stamps else None

    notes: list[str] = []
    if documents == 0:
        notes.append("没有文档可打分（documents=0，score=null；不返回 0 冒充中性）")
    elif scored == 0:
        notes.append(f"{documents} 篇文档无一命中词典（score=null，coverage=0）")
    if undated:
        notes.append(f"{undated} 篇文档缺可解析时间戳，按 as_of 计权（w=1.0）并在 coverage 中计入")
    if documents and scored and coverage < 0.5:
        notes.append(f"词典覆盖率仅 {coverage:.0%}：分数只代表命中词典的 {scored} 篇，不代表全部资讯")
    notes.append(f"词典 {len(table)} 条（自研，未抓取网络词典）；否定/程度修饰与时间半衰见 method")
    if scored_items and any(item["negated"] for item in scored_items):
        notes.append(f"{sum(item['negated'] for item in scored_items)} 处命中被否定词翻转（衰减 {NEGATION_DECAY}）")
    if scored_items and any(item["intensified"] for item in scored_items):
        notes.append(f"{sum(item['intensified'] for item in scored_items)} 处命中被程度副词放大/削弱")

    return {
        "score": score,
        "documents": documents,
        "scored": scored,
        "coverage": coverage,
        "positive": positive,
        "negative": negative,
        "neutral": neutral,
        "undated": undated,
        "top_terms": top_terms,
        "latest_at": latest_at,
        "as_of": moment.isoformat(),
        "method": METHOD,
        "half_life_hours": half_life,
        "notes": notes,
    }


def _bar_date(bar):
    """K 线 → 时间。支持 dict（``date``/``time``/``datetime``/``日期``）与对象属性。"""
    if isinstance(bar, dict):
        for key in ("date", "time", "datetime", "trade_date", "日期"):
            if bar.get(key) not in (None, ""):
                return parse_time(bar.get(key))
        return None
    for key in ("date", "time", "datetime"):
        value = getattr(bar, key, None)
        if value not in (None, ""):
            parsed = _as_aware(value) if isinstance(value, datetime) else parse_time(value)
            if parsed is not None:
                return parsed
    return None


def _window_start(bars, window):
    """``window`` 根 K 线的最早一根时间 = 情绪因子的回看下界（无 K 线 → ``None``）。"""
    if not bars:
        return None
    try:
        span = int(window)
    except (TypeError, ValueError):
        span = 20
    if span <= 0:
        span = 20
    stamped = [(index, _bar_date(bar)) for index, bar in enumerate(bars)]
    ordered = sorted(
        (item for item in stamped if item[1] is not None),
        key=lambda item: (item[1], item[0]),
    )
    if not ordered:
        return None
    if len(ordered) > span:
        ordered = ordered[-span:]
    return ordered[0][1]


def sentiment_factor(bars_by_ticker, docs_by_ticker, *, window=20,
                     half_life_hours=DEFAULT_HALF_LIFE_HOURS, now=None, lexicon=None):
    """情绪因子：``{ticker: {score, coverage}}``（规格 FR-STRAT-003 的因子出口）。

    * ``docs_by_ticker``：``{ticker: [doc, ...]}``（doc 形状同 ``score_documents``）；
    * ``bars_by_ticker``：``{ticker: [bar, ...] }``，用来做 **PIT 截断**——只取最近
      ``window`` 根 K 线覆盖区间内的资讯，避免把因子算成「用了未来资讯」（无 K 线时
      不做截断，并在每只标的的 ``window_from`` 里如实写 ``null``）；
    * 同一份 ``score_documents`` 口径，因此 ``score`` 为 ``None``（无命中/无资讯）
      与 ``0``（正负相抵）**语义不同**，调用方必须区分。
    """
    moment = _as_aware(now) or datetime.now(timezone.utc)
    try:
        span = int(window)
    except (TypeError, ValueError):
        span = 20
    if span <= 0:
        span = 20
    tickers: dict[str, dict] = {}
    for ticker in sorted(set(docs_by_ticker or {}) | set(bars_by_ticker or {})):
        docs = list((docs_by_ticker or {}).get(ticker) or [])
        start = _window_start((bars_by_ticker or {}).get(ticker), span)
        inside = [
            doc for doc in docs
            if start is None or (_doc_time(doc) is None or _doc_time(doc) >= start)
        ]
        payload = score_documents(inside, half_life_hours=half_life_hours,
                                  now=moment, lexicon=lexicon)
        tickers[ticker] = {
            "score": payload["score"],
            "coverage": payload["coverage"],
            "documents": payload["documents"],
            "window_from": start.isoformat() if start is not None else None,
        }
    return {
        "tickers": tickers,
        "as_of": moment.isoformat(),
        "method": METHOD,
        "window": span,
        "half_life_hours": half_life_hours,
    }


# ── 事件识别（FR-STRAT-003 的另一半：极性之外，"这条新闻是什么事件"） ─────────────
#
# 与情绪打分**同一套机械**：复用 ``segment`` 的最大正向匹配与词典结构，把词典换成
# "事件触发词条"，复合词整体收的原则不变——``立案调查`` / ``重大资产重组`` /
# ``增持评级`` 都是整体词条，长词在切分里天然优先，所以 ``解除质押`` 不会被读成
# ``质押``，``增持评级`` 不会被读成 ``增持``（策略侧的增持信号不被评级词污染）。
#
#   * ``EVENT_RULES`` —— 33 类资本市场事件：``type``（英文键，策略消费的稳定契约）、
#     ``label``（中文）、``direction``（+1 利多 / -1 利空 / 0 中性）、``terms``
#     （触发词条 → 基础置信 ∈ (0,1]：公告体明确词给高位，一词多义/口语词给低位；
#     低位事件依然如实输出，置信高低留给策略侧取舍）；
#   * 否定/程度修饰与 ``score_text`` **同词表、同窗口、同语义**（口径见
#     ``classify_events`` 的 docstring）；
#   * ``_EVENT_STOP_TERMS`` —— 货币市场/再融资同形词（``质押式回购``/``定增``…）以
#     0 置信占位：先被整体切出，其内部的 ``质押``/``回购``/``增资`` 就不再是触发词
#     （语境消歧靠"更长的整体词条先占位"，不靠黑名单正则）。

#: 事件识别口径版本：进响应 ``event_method`` / ``event_version``；改规则表/口径必须同步改。
EVENT_METHOD = "rule-v1"
EVENT_VERSION = 1

#: 33 类资本市场事件规则表（全部人工按 A 股公告/新闻常用语汇构造，无网络词典）。
EVENT_RULES: tuple[dict, ...] = (
    {"type": "buyback", "label": "回购", "direction": 1, "terms": {
        "回购": 0.75, "股份回购": 0.85, "回购股份": 0.85, "回购方案": 0.8,
        "回购计划": 0.8, "回购注销": 0.85, "回购金额": 0.7}},
    {"type": "holder_increase", "label": "增持", "direction": 1, "terms": {
        "增持": 0.7, "股东增持": 0.8, "高管增持": 0.8, "增持计划": 0.75,
        "增持股份": 0.75, "举牌": 0.8, "被动增持": 0.55}},
    {"type": "holder_reduction", "label": "减持", "direction": -1, "terms": {
        "减持": 0.75, "股东减持": 0.8, "高管减持": 0.8, "减持计划": 0.8,
        "减持股份": 0.75, "清仓式减持": 0.9, "董监高减持": 0.8, "套现": 0.6}},
    {"type": "equity_pledge", "label": "股权质押", "direction": -1, "terms": {
        "质押": 0.6, "股权质押": 0.75, "股票质押": 0.7, "质押股份": 0.7,
        "补充质押": 0.7, "质押率": 0.6, "高比例质押": 0.8}},
    {"type": "pledge_release", "label": "解除质押", "direction": 1, "terms": {
        "解除质押": 0.65, "解除股权质押": 0.7, "解押": 0.6}},
    {"type": "investigation", "label": "立案调查", "direction": -1, "terms": {
        "立案调查": 0.95, "立案侦查": 0.9, "被立案": 0.9, "证监会立案": 0.9,
        "遭立案": 0.85, "立案告知": 0.8, "立案": 0.7}},
    {"type": "regulatory_penalty", "label": "监管处罚", "direction": -1, "terms": {
        "行政处罚": 0.85, "处罚": 0.8, "罚款": 0.7, "环保处罚": 0.7, "监管函": 0.6,
        "警示函": 0.65, "问询函": 0.6, "关注函": 0.5, "公开谴责": 0.75,
        "通报批评": 0.7, "责令改正": 0.7, "纪律处分": 0.75, "监管关注": 0.5}},
    {"type": "earnings_preincrease", "label": "业绩预增", "direction": 1, "terms": {
        "业绩预增": 0.8, "预增": 0.7, "预计增长": 0.55, "预计上升": 0.5}},
    {"type": "earnings_prereduce", "label": "业绩预减", "direction": -1, "terms": {
        "业绩预减": 0.8, "预减": 0.7, "业绩预亏": 0.85, "预亏": 0.8, "首亏": 0.8,
        "预计亏损": 0.75, "预计下降": 0.55, "由盈转亏": 0.85}},
    {"type": "earnings_turnaround", "label": "业绩扭亏", "direction": 1, "terms": {
        "扭亏为盈": 0.85, "扭亏": 0.75, "由亏转盈": 0.8, "预计扭亏": 0.75,
        "预盈": 0.6, "实现盈利": 0.6, "摘帽": 0.7, "撤销退市风险警示": 0.7}},
    {"type": "earnings_beat", "label": "业绩超预期", "direction": 1, "terms": {
        "超市场预期": 0.8, "大超预期": 0.85, "超出预期": 0.8, "超预期": 0.75,
        "好于预期": 0.75, "优于预期": 0.75, "高于预期": 0.7}},
    {"type": "earnings_miss", "label": "业绩不及预期", "direction": -1, "terms": {
        "差于预期": 0.75, "低于市场预期": 0.75, "不及预期": 0.75, "低于预期": 0.75,
        "逊于预期": 0.7, "未达预期": 0.7, "不达预期": 0.7, "弱于预期": 0.7}},
    {"type": "dividend", "label": "分红", "direction": 1, "terms": {
        "现金分红": 0.75, "每10股派": 0.75, "10派": 0.7, "分红": 0.7, "派息": 0.7,
        "派现": 0.7, "派发现金": 0.7, "分红方案": 0.7, "利润分配": 0.6, "股息": 0.55}},
    {"type": "stock_split", "label": "送转", "direction": 1, "terms": {
        "高送转": 0.7, "每10股转增": 0.65, "10转": 0.65, "送转": 0.6, "送股": 0.6,
        "转增股本": 0.6, "转增": 0.55}},
    {"type": "trading_halt", "label": "停牌", "direction": 0, "terms": {
        "临时停牌": 0.8, "紧急停牌": 0.8, "停牌": 0.75, "申请停牌": 0.7}},
    {"type": "trading_resume", "label": "复牌", "direction": 0, "terms": {
        "复牌": 0.7, "复牌公告": 0.7, "恢复交易": 0.6}},
    {"type": "ma_restructuring", "label": "并购重组", "direction": 1, "terms": {
        "重大资产重组": 0.85, "发行股份购买资产": 0.8, "借壳上市": 0.8, "要约收购": 0.8,
        "借壳": 0.75, "并购重组": 0.75, "并购": 0.7, "资产重组": 0.7, "重组": 0.65,
        "兼并": 0.65, "收购": 0.6}},
    {"type": "contract_win", "label": "中标", "direction": 1, "terms": {
        "中标": 0.8, "中标公告": 0.8, "中标金额": 0.75, "预中标": 0.7, "竞得": 0.6}},
    {"type": "major_contract", "label": "大额合同", "direction": 1, "terms": {
        "重大合同": 0.8, "大额订单": 0.75, "获得订单": 0.65, "独家供应": 0.6,
        "长期供货": 0.6, "签订合同": 0.6, "签署合同": 0.6, "签订协议": 0.5,
        "签署协议": 0.5, "框架协议": 0.55, "战略合作": 0.55}},
    {"type": "outbound_investment", "label": "对外投资", "direction": 1, "terms": {
        "对外投资": 0.7, "战略投资": 0.65, "投资设立": 0.65, "出资设立": 0.65,
        "设立子公司": 0.6, "增资扩股": 0.6, "产业投资": 0.55, "增资": 0.5}},
    {"type": "asset_sale", "label": "资产出售", "direction": 0, "terms": {
        "出售资产": 0.65, "资产出售": 0.65, "剥离资产": 0.65, "转让子公司": 0.65,
        "出售子公司": 0.65, "转让股权": 0.6, "股权转让": 0.6, "出售股权": 0.6,
        "挂牌转让": 0.6, "转让资产": 0.6}},
    {"type": "debt_default", "label": "债务违约", "direction": -1, "terms": {
        "债务违约": 0.9, "债券违约": 0.9, "交叉违约": 0.9, "资金链断裂": 0.9,
        "未按期兑付": 0.85, "无法兑付": 0.85, "违约": 0.8, "债务逾期": 0.75,
        "兑付风险": 0.75, "逾期": 0.6, "展期": 0.5}},
    {"type": "lawsuit", "label": "诉讼", "direction": -1, "terms": {
        "提起诉讼": 0.7, "被诉": 0.7, "法律纠纷": 0.55, "诉讼": 0.65, "起诉": 0.6,
        "应诉": 0.6, "一审判决": 0.5, "二审判决": 0.5, "纠纷": 0.5}},
    {"type": "arbitration", "label": "仲裁", "direction": -1, "terms": {
        "仲裁裁决": 0.7, "申请仲裁": 0.7, "仲裁委员会": 0.55, "仲裁": 0.65}},
    {"type": "management_change", "label": "高管变动", "direction": 0, "terms": {
        "董事长辞职": 0.7, "总经理辞职": 0.7, "高管变动": 0.6, "辞职": 0.6,
        "辞任": 0.6, "辞去": 0.6, "卸任": 0.6, "人事变动": 0.55, "离职": 0.5,
        "聘任": 0.5, "接任": 0.5, "换届": 0.5, "任命": 0.45, "新聘": 0.45}},
    {"type": "investor_relation", "label": "机构调研", "direction": 1, "terms": {
        "机构调研": 0.7, "接受调研": 0.65, "投资者关系活动": 0.6, "调研活动": 0.6,
        "接待机构": 0.6, "迎来调研": 0.6, "调研": 0.55}},
    {"type": "rating_up", "label": "评级上调", "direction": 1, "terms": {
        "上调评级": 0.85, "评级上调": 0.85, "调高评级": 0.8, "上调至买入": 0.8,
        "上调至增持": 0.8, "上调目标价": 0.75, "上调盈利预测": 0.75, "买入评级": 0.55,
        "增持评级": 0.5, "首次覆盖": 0.5, "推荐评级": 0.45}},
    {"type": "rating_down", "label": "评级下调", "direction": -1, "terms": {
        "下调评级": 0.85, "评级下调": 0.85, "调低评级": 0.8, "下调至卖出": 0.8,
        "下调目标价": 0.7, "下调盈利预测": 0.7, "卖出评级": 0.7, "减持评级": 0.6}},
    {"type": "lockup_expiry", "label": "限售解禁", "direction": -1, "terms": {
        "限售解禁": 0.75, "首发解禁": 0.7, "定增解禁": 0.7, "解除限售": 0.7,
        "解禁": 0.65, "解禁股": 0.65, "上市流通": 0.6}},
    {"type": "external_guarantee", "label": "对外担保", "direction": -1, "terms": {
        "对外担保": 0.7, "连带担保": 0.7, "提供担保": 0.65, "担保额度": 0.6,
        "担保余额": 0.6, "保证担保": 0.6, "担保": 0.55}},
    {"type": "fund_occupation", "label": "资金占用", "direction": -1, "terms": {
        "资金占用": 0.85, "非经营性占用": 0.85, "占用上市公司资金": 0.85, "占用资金": 0.75}},
    {"type": "clarification", "label": "新闻澄清", "direction": 0, "terms": {
        "澄清公告": 0.7, "郑重澄清": 0.7, "澄清说明": 0.65, "澄清": 0.65,
        "辟谣": 0.65, "不实报道": 0.55}},
    {"type": "delisting_risk", "label": "退市风险", "direction": -1, "terms": {
        "退市风险": 0.9, "面值退市": 0.9, "退市警示": 0.85, "暂停上市": 0.85,
        "终止上市": 0.85, "退市整理期": 0.85, "退市": 0.8}},
)

#: 类型目录：``type → {label, direction}``（策略/前端做方向映射的稳定目录；顺序无意义）。
EVENT_TYPES: dict[str, dict] = {
    rule["type"]: {"label": rule["label"], "direction": rule["direction"]}
    for rule in EVENT_RULES
}

#: 事件切分用词表 = 触发词条 ∪ 停用占位（``segment(lexicon=…)`` 会自动并入否定词/程度副词）。
_EVENT_LEXICON: dict[str, float] = {}
for _rule in EVENT_RULES:
    _EVENT_LEXICON.update(_rule["terms"])

#: 触发词条 → (type, label, direction)：识别期查表；导入期即校验词条不跨类型重复。
_EVENT_TRIGGER_INFO: dict[str, tuple[str, str, int]] = {}
for _rule in EVENT_RULES:
    for _term in _rule["terms"]:
        if _term in _EVENT_TRIGGER_INFO:
            raise ValueError(
                f"事件触发词条跨类型重复：{_term} → "
                f"{_EVENT_TRIGGER_INFO[_term][0]} / {_rule['type']}"
            )
        _EVENT_TRIGGER_INFO[_term] = (_rule["type"], _rule["label"], _rule["direction"])

#: 货币市场/再融资同形词占位（置信 0，**不是事件**）：整体收录后，其内部的
#: ``质押``/``回购``/``增资`` 不会再被切出（``定增`` 压住 ``增资`` 的误切）。
_EVENT_STOP_TERMS: dict[str, float] = {
    "质押式回购": 0.0, "正回购": 0.0, "逆回购": 0.0, "回购协议": 0.0,
    "回购利率": 0.0, "定增": 0.0,
}
for _stop in _EVENT_STOP_TERMS:
    if _stop in _EVENT_LEXICON or _stop in _EVENT_TRIGGER_INFO:
        raise ValueError(f"事件停用词与触发词冲突：{_stop}")
_EVENT_LEXICON.update(_EVENT_STOP_TERMS)
del _rule, _term, _stop


def _events_from_text(text):
    """一段文本 → 事件列表（每类取**最强触发**，confidence 降序；零命中 → ``[]``）。

    识别 = ``segment(text, lexicon=_EVENT_LEXICON)`` 切分后，落在触发词条上的 token
    记为事件；修饰语义与 ``score_text`` 完全一致（``MODIFIER_WINDOW`` 内、不跨标点/空白）：

      * 否定词（``NEGATORS``）→ 事件**方向取反**，置信 × ``NEGATION_DECAY``；
      * 程度副词（``INTENSIFIERS``）→ 置信 × 因子（``大幅`` 1.6 / ``小幅`` 0.7 …），
        最后截到 (0.05, 1.0]；
      * 置信 0 的停用占位（``质押式回购`` 等）不是事件，但会中断修饰窗口（与词典词一致）。
    """
    tokens = segment(text, lexicon=_EVENT_LEXICON)
    best: dict[str, dict] = {}
    for index, token in enumerate(tokens):
        base = _EVENT_LEXICON.get(token)
        if not base:  # 未登录 token 或停用占位（0.0）：都不是事件
            continue
        etype, elabel, edirection = _EVENT_TRIGGER_INFO[token]
        factor = 1.0
        flipped = False
        for back in range(index - 1, max(-1, index - 1 - MODIFIER_WINDOW), -1):
            previous = tokens[back]
            if previous.isspace() or previous in PUNCTUATION:
                break
            if previous in _EVENT_LEXICON:
                break
            if previous in _NEGATOR_SET:
                flipped = True
                continue
            value = INTENSIFIERS.get(previous)
            if value is not None:
                factor *= value
        confidence = base * factor
        direction = edirection
        if flipped:
            confidence *= NEGATION_DECAY
            direction = -direction  # 0 取反仍是 0：中性事件的否定只降置信
        candidate = {
            "type": etype,
            "label": elabel,
            "direction": direction,
            "matched": token,
            "confidence": round(max(0.05, min(1.0, confidence)), 4),
            "negated": flipped,
        }
        current = best.get(etype)
        if current is None or (candidate["confidence"], len(candidate["matched"])) > (
            current["confidence"],
            len(current["matched"]),
        ):
            best[etype] = candidate
    return sorted(best.values(), key=lambda item: (-item["confidence"], item["type"]))


def classify_events(docs, *, now=None):
    """文档列表 → 事件识别结果（FR-STRAT-003 事件识别出口，供策略侧守卫导入）。

    ``docs`` 形状与 ``score_documents`` 一致：``[{"title":..., "summary":...,
    "published_at":..., "source":...}, ...]``；纯字符串、缺字段、``None`` 字段都容忍
    （文本取 ``_doc_text`` 同一套键，时间取 ``_doc_time`` 同一套键，解析失败不猜时间）。

    返回（与 ``GET /api/v3/sentiment`` 的 ``events`` / ``doc_events`` 字段**同形**）::

        {"as_of", "method": EVENT_METHOD, "version": EVENT_VERSION, "documents": N,
         "events":    [ {type, label, direction, count, latest_at, first_seen}, ...],
         "doc_events":[ {index, dated, published_at, events: [ {type, label,
                        direction, matched, confidence, negated} ]}, ...]}

    口径（每条都有单测）：

      * **否定翻转**：``MODIFIER_WINDOW`` 内的否定词把事件方向取反并把置信 ×
        ``NEGATION_DECAY``（0.65）——"未增持"不是增持利多（方向 -1 的弱证据），
        "不存在减持"同理翻成 +1；这与情绪打分"翻转 + 衰减"同一口径。中性事件
        （direction=0，如停牌/复牌/澄清）取反仍是 0，只降置信；``negated=true``
        如实标注，策略侧可自行剔除；
      * **一篇文档可命中多类**：全部如实给出，每类取最强触发，按 confidence 降序；
      * **零命中 → ``events=[]``**：不硬造事件、不用"中性"占位；
      * **时间诚实**：``published_at`` 缺失或解析失败 → 该文档事件 ``dated=False``、
        ``published_at=null``（策略侧据此剔除，不猜时间）；``events`` 聚合里
        ``count`` 含这类文档，``latest_at`` / ``first_seen`` 只用时间可解析的文档算，
        全部不可解析时为 ``null``；
      * **归属诚实**：``doc_events`` 仅透传原文已有的 ``ticker`` / ``symbol``、
        ``source``、``title``，不以检索关键字推定公司归属，缺失字段不猜填；
      * **排序**：``events`` 按 ``count`` 降序 → 有新近时间者在前 → ``type`` 字典序
        （稳定可复现）；``doc_events`` 保持输入顺序（``index`` 对位）。
    """
    moment = _as_aware(now) or datetime.now(timezone.utc)
    doc_events: list[dict] = []
    tally: dict[str, dict] = {}
    for index, doc in enumerate(docs or []):
        published = _doc_time(doc)
        dated = published is not None
        events = _events_from_text(_doc_text(doc))
        doc_events.append({
            "index": index,
            "dated": dated,
            "published_at": published.isoformat() if dated else None,
            "events": events,
            **({key: doc[key] for key in ("ticker", "symbol", "source", "title") if key in doc}
               if isinstance(doc, dict) else {}),
        })
        for event in events:
            entry = tally.get(event["type"])
            if entry is None:
                entry = {"type": event["type"], "label": event["label"],
                         "direction": event["direction"], "count": 0, "_times": []}
                tally[event["type"]] = entry
            entry["count"] += 1
            if dated:
                entry["_times"].append(published)

    ranked = sorted(
        tally.values(),
        key=lambda entry: (
            -entry["count"],
            not entry["_times"],
            -(max(entry["_times"]).timestamp() if entry["_times"] else 0.0),
            entry["type"],
        ),
    )
    events_out = []
    for entry in ranked:
        times = entry["_times"]
        events_out.append({
            "type": entry["type"],
            "label": entry["label"],
            "direction": entry["direction"],
            "count": entry["count"],
            "latest_at": max(times).isoformat() if times else None,
            "first_seen": min(times).isoformat() if times else None,
        })
    return {
        "as_of": moment.isoformat(),
        "method": EVENT_METHOD,
        "version": EVENT_VERSION,
        "documents": len(doc_events),
        "events": events_out,
        "doc_events": doc_events,
    }


# ── 端点响应装配（取数由调用方注入，本模块不联网） ────────────────────────────────


def _clamp_int(raw, default, minimum=1, maximum=500):
    """查询串 → 整数（``server.v3_sources.to_int`` 的同语义本地实现：坏值回落默认、越界夹紧）。

    刻意**不** import ``v3_sources.to_int``：参数护栏是端点自身的行为，少一层跨模块耦合；
    两边语义必须一致（都是「不抛 422，回落到合法区间」）。
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _row_time(row):
    return _doc_time(row)


def _day_key(published):
    return published.astimezone(CN_TZ).strftime("%Y-%m-%d")


def sentiment_report(news_fetch, symbol, *, market="", days=7, limit=20,
                     half_life_hours=DEFAULT_HALF_LIFE_HOURS, now=None,
                     source="akshare/stock_news_em", include_docs=False):
    """``GET /api/v3/sentiment`` 的响应体：**取资讯 → 打分 → 组装可解释信封**。

    ``news_fetch`` 是零参可调用对象，返回既有资讯信封（``server.v3_sources.fetch_news``
    的形状：``{ok, as_of, source, rows, attempts}`` 或 ``{ok:false, error}``）。**取数
    与打分刻意分离**：本模块不联网，端点层注入取数、测试注入假取数。

    返回契约（只增字段）::

        命中：{ok, symbol, market, as_of, source, days, limit, documents, scored,
               score, coverage, positive, negative, neutral, top_terms, per_day,
               latest_at, undated, method, notes, chain,
               events, event_method, event_version[, doc_events]}
        无资讯：{ok:true, score:null, documents:0, coverage:null, ..., events:[],
                 event_method, event_version, notes:["该标的近 N 天无资讯"]}
                                                                  ← **不是 0 分**
        取数失败：{ok:false, error:{code,message}, chain:[...], score:null, documents:0,
                   events:[], event_method, event_version}

    ``chain`` 是取数链留痕（数据源、是否成功、条数、AKShare 每次尝试），失败时同样带
    真实错误原文——前端可以逐级核对「为什么没有分」。

    ``events`` / ``doc_events`` / ``event_method`` / ``event_version`` 来自
    ``classify_events``（事件识别口径见其 docstring）：``events`` 是按类型的聚合
    （type/label/direction/count/latest_at/first_seen，count 降序、新近优先）；
    ``doc_events`` 是每文档明细，只在 ``include_docs=True`` 且 ``ok=true`` 时给出，
    避免默认响应膨胀。
    """
    moment = _as_aware(now) or datetime.now(timezone.utc)
    try:
        span = int(days)
    except (TypeError, ValueError):
        span = 7
    if span <= 0:
        span = 7
    base = {
        "ok": True,
        "symbol": _text(symbol),
        "market": _text(market),
        "as_of": moment.isoformat(),
        "source": source,
        "days": span,
        "limit": limit,
        "half_life_hours": half_life_hours,
    }

    try:
        envelope = news_fetch()
    except Exception as error:  # noqa: BLE001 —— 取数抛异常也走统一信封，不抛 500
        detail = f"{type(error).__name__}: {error}"[:300]
        return {
            **base,
            "ok": False,
            "score": None,
            "documents": 0,
            "scored": 0,
            "coverage": None,
            "positive": 0, "negative": 0, "neutral": 0,
            "top_terms": [],
            "per_day": [],
            "latest_at": None,
            "events": [],
            "event_method": EVENT_METHOD,
            "event_version": EVENT_VERSION,
            "error": {"code": "sentiment/news-fetch-failed", "message": detail},
            "chain": [{"source": source, "ok": False, "error": detail}],
            "notes": ["取资讯时抛异常，未做任何打分（score=null）"],
        }

    if not isinstance(envelope, dict):
        detail = f"资讯源返回非信封对象：{type(envelope).__name__}"
        return {
            **base,
            "ok": False,
            "score": None,
            "documents": 0,
            "scored": 0,
            "coverage": None,
            "positive": 0, "negative": 0, "neutral": 0,
            "top_terms": [],
            "per_day": [],
            "latest_at": None,
            "events": [],
            "event_method": EVENT_METHOD,
            "event_version": EVENT_VERSION,
            "error": {"code": "sentiment/news-fetch-failed", "message": detail},
            "chain": [{"source": source, "ok": False, "error": detail}],
            "notes": ["资讯源返回形状不合法，未做任何打分（score=null）"],
        }

    used_source = _text(envelope.get("source")) or source
    rows = list(envelope.get("rows") or [])
    attempts = envelope.get("attempts") or []

    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        error = {"code": _text(error.get("code")) or "sentiment/news-failed",
                 "message": _text(error.get("message")) or "资讯源失败且未提供错误明细"}
        return {
            **base,
            "ok": False,
            "source": used_source,
            "score": None,
            "documents": 0,
            "scored": 0,
            "coverage": None,
            "positive": 0, "negative": 0, "neutral": 0,
            "top_terms": [],
            "per_day": [],
            "latest_at": None,
            "events": [],
            "event_method": EVENT_METHOD,
            "event_version": EVENT_VERSION,
            "error": error,
            "chain": [{"source": used_source, "ok": False, "rows": 0,
                       "error": error, "attempts": attempts}],
            "notes": [f"资讯源失败（{error['code']}）：{error['message']}"],
        }

    cutoff = moment - timedelta(days=span)
    window_rows = []
    for row in rows:
        published = _row_time(row)
        if published is None:  # 时间不可解析：保留（进 undated 计数），不静默丢
            window_rows.append(row)
        elif published >= cutoff:
            window_rows.append(row)

    chain_row = {
        "source": used_source,
        "ok": True,
        "rows": len(rows),
        "in_window": len(window_rows),
        "window_days": span,
        "attempts": attempts,
    }

    if not window_rows:
        payload = {
            **base,
            "source": used_source,
            "documents": 0,
            "scored": 0,
            "score": None,
            "coverage": None,
            "positive": 0, "negative": 0, "neutral": 0,
            "top_terms": [],
            "per_day": [],
            "latest_at": None,
            "undated": 0,
            "events": [],
            "event_method": EVENT_METHOD,
            "event_version": EVENT_VERSION,
            "method": METHOD,
            "chain": [chain_row],
            "notes": [f"该标的近 {span} 天无资讯"
                      + (f"（上游返回 {len(rows)} 条，全部早于 {cutoff.isoformat()}）"
                         if rows else "（上游返回 0 条）")],
        }
        if include_docs:
            payload["doc_events"] = []
        return payload

    scored_payload = score_documents(window_rows, half_life_hours=half_life_hours,
                                     now=moment)
    per_day: dict[str, dict] = {}
    for row in window_rows:
        published = _row_time(row)
        result = score_text(_doc_text(row))
        key = _day_key(published) if published is not None else "unknown"
        bucket = per_day.setdefault(key, {"date": key, "count": 0, "_sum": 0.0, "_scored": 0})
        bucket["count"] += 1
        if result["score"] is not None:
            bucket["_sum"] += result["score"]
            bucket["_scored"] += 1
    days_out = []
    for key in sorted(per_day):
        bucket = per_day[key]
        days_out.append({
            "date": bucket["date"],
            "count": bucket["count"],
            "scored": bucket["_scored"],
            "score": round(bucket["_sum"] / bucket["_scored"], 6) if bucket["_scored"] else None,
        })

    notes = list(scored_payload["notes"])
    notes.insert(0, f"窗口 = 近 {span} 天（共 {len(window_rows)}/{len(rows)} 条资讯在窗口内）")

    events_payload = classify_events(window_rows, now=moment)
    if events_payload["events"]:
        summary = "、".join(
            f"{item['label']}×{item['count']}" for item in events_payload["events"][:5]
        )
        notes.append(f"事件识别（{EVENT_METHOD}）：{summary}"
                     + ("…" if len(events_payload["events"]) > 5 else ""))
    else:
        notes.append(f"事件识别（{EVENT_METHOD}）：零命中（events=[]，不硬造）")

    payload = {
        **base,
        "source": used_source,
        **{key: scored_payload[key] for key in
           ("documents", "scored", "score", "coverage", "positive", "negative",
            "neutral", "undated", "top_terms", "latest_at", "method")},
        "per_day": days_out,
        "events": events_payload["events"],
        "event_method": EVENT_METHOD,
        "event_version": EVENT_VERSION,
        "chain": [chain_row],
        "notes": notes,
    }
    if include_docs:
        payload["doc_events"] = events_payload["doc_events"]
    return payload


# ── 端点注册（本模块自带 router；由 app.py 的 V3 自动装配循环加载） ──────────────
#
# 分工（对并发改动友好）：``v3_nlp`` 是情绪的唯一实现，也自己挂路由；
# ``server.v3_sources`` **只被惰性复用**它的公开取数面（``fetch_news`` / ``Deps`` /
# ``detect_market`` / ``normalize_a_share_symbol``），既不加第二份取数实现，
# 也不改动那个模块的结构（它同时被别的在途改动使用）。
#
# 关键字口径（实测 2026-09-20）：
#   * A 股 → 裸 6 位代码（``SH.600519`` → ``600519``）；
#   * 港股/美股 → 裸代码（``HK.00700`` → ``00700``，``US.NVDA`` → ``NVDA``）——
#     AKShare ``stock_news_em`` 是**东财关键字搜索**，实测 ``00700``/``NVDA`` 都能返回
#     对应标的的真实资讯（腾讯连续回购、英伟达高管减持），所以不限定 A 股；
#   * 识别不出的形态原样上送，让上游如实报错（不猜标的）。


def _bare_code(ticker):
    """``SH.600519`` / ``600519.SH`` → ``600519``；``AAPL`` → ``AAPL``。"""
    text = _text(ticker).upper()
    if "." in text:
        head, tail = text.split(".", 1)
        if head in ("SH", "SZ", "BJ", "HK", "US"):
            return tail
        if tail in ("SH", "SZ", "BJ", "HK", "US"):
            return head
    return text


def news_keyword(ticker):
    """标的 → AKShare 资讯检索关键字（裸代码；A 股/港股/美股同一口径）。"""
    from server import v3_sources  # 惰性：装配期不互相 import

    market = v3_sources.detect_market(ticker)
    if market in ("SH", "SZ", "BJ"):
        return v3_sources.normalize_a_share_symbol(ticker)
    if market in ("HK", "US"):
        return _bare_code(ticker)
    return _text(ticker).upper()


def sentiment_payload(deps, symbol, market="", days=7, limit=20, half_life_hours=None,
                      include_docs=False):
    """``GET /api/v3/sentiment`` 的同步实现：取资讯 → 打分 → 可解释信封。

    只读、不写盘、不触达任何交易端点。取数走 ``v3_sources.fetch_news``，因此自动继承
    ``retry_akshare`` 的退避重试与 ``attempts`` 留痕（含真实错误原文）。
    ``include_docs`` 透传给 ``sentiment_report``：只控制 ``doc_events`` 明细是否随响应返回。
    """
    from server import v3_sources

    options = {}
    if half_life_hours is not None:
        options["half_life_hours"] = half_life_hours
    symbol_text = _text(symbol)
    return sentiment_report(
        lambda: v3_sources.fetch_news(deps, news_keyword(symbol), limit),
        symbol_text,
        market=_text(market) or v3_sources.detect_market(symbol_text),
        days=days,
        limit=limit,
        include_docs=include_docs,
        **options,
    )


def register(app, v3_run, home, deps=None):
    """挂 ``GET /api/v3/sentiment``（本模块唯一路由，**只读**）。

    ``app.py`` 的 V3 自动装配循环按 ``register(app, v3_run, home)`` 调用；``deps`` 仅供
    测试注入（缺省按 ``home`` 建 ``v3_sources.Deps``，与 ``v3_sources.register`` 同一口径）。
    返回 ``deps``，便于测试断言注入生效。
    """
    from server import v3_sources

    if deps is None:
        deps = v3_sources.Deps(home=home)

    @app.get("/api/v3/sentiment")
    async def v3_sentiment(symbol: str = "", market: str = "", days: int = 7, limit: int = 20,
                           include_docs: bool = False):
        """个股资讯情绪因子 + 事件识别（规格 FR-STRAT-003）。**只读**：不写盘、不下单、不切模式。

        链路：``news_keyword(symbol)`` → ``akshare.stock_news_em``（经 ``retry_akshare``
        退避重试）→ ``sentiment_report`` 打分（自研词典 + 否定/程度修饰 + 时间半衰）与
        ``classify_events`` 事件识别（33 类触发词条 + 同一套否定/程度修饰口径）。

        ``days`` 是资讯时间窗（按发布时间过滤，缺时间戳的保留并计入 ``undated``）；
        ``limit`` 是取数上限（``stock_news_em`` 实测每页 10 条，limit 只影响请求条数）；
        ``include_docs=true`` 才附带每文档事件明细 ``doc_events``（默认不给，避免响应膨胀）。

        诚实口径（每条都有单测）:

          * 窗口内无资讯 → ``{ok:true, score:null, documents:0, events:[],
            notes:["该标的近 N 天无资讯"]}`` —— **不返回 0 分冒充中性**；
          * 有资讯但无一命中词典 → ``score:null`` + ``coverage:0``（同样不是 0 分）；
          * 事件零命中 → ``events:[]``（**不硬造事件**），``event_method``/``event_version``
            始终在场；
          * 上游失败 → ``{ok:false, error:{code,message}, chain:[...]}``，``message`` 是
            **真实错误原文**，``chain`` 里带 AKShare 每次尝试（``attempts``）；
          * ``top_terms`` + ``per_day`` + ``coverage`` + ``events`` 回答「这个分/这些事件
            是怎么来的」。
        """
        want_days = _clamp_int(days, 7, 1, 400)
        want_limit = _clamp_int(limit, 20, 1, 200)
        try:
            payload = await asyncio.to_thread(
                sentiment_payload, deps, symbol, market, want_days, want_limit,
                None, include_docs
            )
        except Exception as error:  # noqa: BLE001 —— 统一信封，不把栈透给前端
            detail = f"{type(error).__name__}: {error}"[:300]
            payload = {
                "ok": False,
                "symbol": _text(symbol),
                "market": _text(market),
                "as_of": datetime.now(timezone.utc).isoformat(),
                "source": "akshare/stock_news_em",
                "days": want_days,
                "limit": want_limit,
                "documents": 0,
                "scored": 0,
                "score": None,
                "coverage": None,
                "top_terms": [],
                "per_day": [],
                "events": [],
                "event_method": EVENT_METHOD,
                "event_version": EVENT_VERSION,
                "error": {"code": "sentiment/internal", "message": detail},
                "chain": [{"source": "akshare/stock_news_em", "ok": False, "error": detail}],
                "notes": ["情绪端点内部异常，未产出分数（score=null）"],
            }
        return payload

    app.state.v3_nlp = {"deps": deps, "routes": ("/api/v3/sentiment",)}
    return deps
