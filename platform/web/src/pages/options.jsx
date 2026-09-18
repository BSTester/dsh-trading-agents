// 期权分析页：到期日列表（option_expiration）→ 期权链（option_chain）→ 期权筛选
// （option_screen）。数据源全部是富途实时直通端点（platform/server/futu_data.py，
// TTL 0），value 原样透传不做形状归一；页面按官方字段名防御式读取，缺字段一律 —，
// 每个区块折叠「原始返回」供核对——通道字段与文档不符时界面不编造数据。
// 到期日选择是**本地行过滤**：服务端 option_chain 的载荷白名单只有 code/field_filter
// （app.py FUTU_FIELDS / futu_data.option_chain），不接受到期日参数；链行自带的到期日
// 字段（expiration_date 等候选键）用于筛选展示。期权行是否有 Greeks 取决于上游字段面
// （REST 更全），行里出现哪些希腊字母键就展示哪些，不凭空造列。
// option_screen 必填陷阱（futu_data.option_screen 与 docs/TOOL-LIMITS 的实测事实）：filter
// 必须是非空对象，且必须带**非空 field_filter**（省略时上游只返回 4 个默认字段、其余全
// null），strategy 也是上游必填——缺了直接被拒，不浪费注定失败的往返。
// WP25（2026-09-18 用户需求：「能改成选项或输入框吗？而不是这种 json 格式的」）：筛选区
// 从「两个手写 JSON 框」改为**表单控件**（市场类别/指标条件/返回字段/条数上限/排序），
// 载荷组装全部下沉到 services/optionScreen.js（纯函数，node --test 直测）；原来那份手写
// JSON 没有删掉，而是收进「高级（JSON）」折叠面板当**逃生口**（可看表单生成的载荷，也可
// 切成手动编辑整份 filter，此时以 JSON 为准）。
import React from "react";
import {
  Alert, Button, Card, Collapse, Input, InputNumber, Radio, Select, Space, Switch, Table,
  Typography,
} from "antd";
import { callApi } from "../services/api.js";
import { useEndpoint } from "../services/hooks.js";
import { num } from "../services/format.jsx";
import { RawCollapse } from "../lib/raw-collapse.jsx";
import { SymbolInput } from "../components/SymbolInput.jsx";
import {
  dataplaneHint, exerciseProbabilitySummary, optionVolatilitySummary, strikeRows,
} from "../services/f10.js";
import {
  DEFAULT_OPTION_SCREEN_FORM, OPTION_FIELD_FILTER_FIELDS, OPTION_INDICATOR_TYPE_VERIFIED,
  OPTION_MARKET_CATEGORIES, buildOptionScreenFilter, formatOptionScreenFilter,
  parseOptionScreenJson,
} from "../services/optionScreen.js";

const { TextArea } = Input;

/** 多候选键防御读取：返回第一个非 null/undefined 的值，全部缺失返回 undefined（不补默认值）。 */
function pick(row, keys) {
  for (const key of keys) {
    if (row && row[key] !== null && row[key] !== undefined) return row[key];
  }
  return undefined;
}

/** 透传 value → 行对象数组：数组直接用；对象取全部数组型成员拼接（兼容裸数组与分组对象）。 */
function rowsOf(value) {
  if (Array.isArray(value)) return value.filter((row) => row && typeof row === "object");
  if (value && typeof value === "object") {
    return Object.values(value).flatMap((part) =>
      Array.isArray(part) ? part.filter((row) => row && typeof row === "object") : []);
  }
  return [];
}

/** 到期日候选：行内字符串、或行对象的 expiration_date/date 候选键，原样去重排序。 */
function expirationCandidates(value) {
  const rows = Array.isArray(value) ? value : rowsOf(value);
  const dates = rows.map((row) => (typeof row === "string"
    ? row
    : pick(row, ["expiration_date", "expire_date", "date", "strike_time"])))
    .filter((item) => typeof item === "string" && item);
  return [...new Set(dates)].sort();
}

const CHAIN_BASE_COLUMNS = [
  { title: "代码", key: "code", render: (_f, row) => pick(row, ["code", "option_code", "ticker"]) ?? "—" },
  { title: "名称", key: "name", render: (_f, row) => pick(row, ["name", "option_name"]) ?? "—" },
  { title: "到期日", key: "expiration", render: (_f, row) => pick(row, ["expiration_date", "expire_date", "date"]) ?? "—" },
  { title: "行权价", key: "strike", align: "right", render: (_f, row) => num(pick(row, ["strike_price", "strike"])) },
  { title: "买价", key: "bid", align: "right", render: (_f, row) => num(pick(row, ["bid_price", "bid"])) },
  { title: "卖价", key: "ask", align: "right", render: (_f, row) => num(pick(row, ["ask_price", "ask"])) },
  { title: "成交量", key: "volume", align: "right", render: (_f, row) => num(pick(row, ["volume", "turnover_vol"]), 0) },
  { title: "持仓量", key: "open_interest", align: "right", render: (_f, row) => num(pick(row, ["open_interest", "position"]), 0) },
];

// Greeks 列（上游行里出现哪个键才追加哪列；rest 更全，MCP 通道可能全缺 → 无列不造列）。
const GREEK_KEYS = [
  { key: "delta", title: "Delta", keys: ["delta"] },
  { key: "gamma", title: "Gamma", keys: ["gamma"] },
  { key: "theta", title: "Theta", keys: ["theta"] },
  { key: "vega", title: "Vega", keys: ["vega"] },
  { key: "rho", title: "Rho", keys: ["rho"] },
  { key: "iv", title: "隐含波动率", keys: ["implied_volatility", "iv"] },
];

function chainColumns(rows) {
  const present = GREEK_KEYS.filter((greek) => rows.some((row) => pick(row, greek.keys) !== undefined));
  return [...CHAIN_BASE_COLUMNS,
    ...present.map((greek) => ({
      title: greek.title, key: greek.key, align: "right",
      render: (_f, row) => num(pick(row, greek.keys)),
    })),
    { title: "更新时间", key: "update_time", render: (_f, row) => pick(row, ["update_time", "update_date_time", "data_date"]) ?? "—" },
  ];
}

/** 原始返回折叠块见 src/lib/raw-collapse.jsx（WP12 任务 6 起与研究页共用）。 */

/** 期权链表：本地过滤到期日（服务端 option_chain 不收到期日参数，见文件头）。 */
function ChainTable({ chain }) {
  const allRows = rowsOf(chain.value);
  const [expiration, setExpiration] = React.useState("");
  const dates = expirationCandidates(chain.value);
  const rows = expiration ? allRows.filter((row) => {
    const rowDate = pick(row, ["expiration_date", "expire_date", "date"]);
    return rowDate === expiration || String(rowDate ?? "").startsWith(expiration);
  }) : allRows;
  if (chain.error) {
    return <Alert type="error" showIcon message={`期权链读取失败：${chain.error}`} />;
  }
  return (
    <>
      {dates.length > 0 && (
        <Space style={{ marginBottom: 8 }}>
          <Typography.Text type="secondary">到期日（本地过滤）：</Typography.Text>
          <Select style={{ width: 160 }}
            allowClear
            placeholder="全部到期日"
            value={expiration || undefined}
            onChange={(value) => setExpiration(value ?? "")}
            options={dates.map((date) => ({ value: date, label: date }))} />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            行 {expiration ? rows.length : allRows.length} / {allRows.length}
          </Typography.Text>
        </Space>)}
      <Table size="small"
        rowKey={(_row, index) => index}
        dataSource={rows}
        pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
        loading={chain.loading}
        scroll={{ x: "max-content" }}
        locale={{ emptyText: chain.loading ? "期权链加载中…" : "暂无期权链数据。" }}
        columns={chainColumns(rows)} />
      <RawCollapse value={chain.value} loading={chain.loading} />
    </>);
}

/** 表单行：定宽标签 + 控件（窄屏自动换行）；hint 是控件右侧的旁注。 */
function ScreenRow({ label, hint, children }) {
  return (
    <Space align="start" wrap size={8}>
      <Typography.Text type="secondary"
        style={{ width: 96, display: "inline-block", paddingTop: 5 }}>
        {label}
      </Typography.Text>
      <Space align="center" wrap size={8}>{children}</Space>
      {hint && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>{hint}</Typography.Text>)}
    </Space>);
}

/**
 * 期权筛选（WP25 表单化）：默认用**表单控件**生成载荷（组装规则全在
 * services/optionScreen.js，页面不做形状判断）；手写 JSON 收进「高级（JSON）」逃
 * 生口——那里能看表单生成的载荷，也能切到手动编辑整份 filter（打开后以 JSON 为准）。
 *
 * 控件 → 载荷映射（键名见 docs/TOOL-LIMITS.md 的最小可用载荷）：
 *   市场类别（多选）→ strategy.market_category_list（只给真机验证过的 7 个类别码）；
 *   加入指标条件 → strategy.filter_group_list（关 = []，官方接受的空形状；开 = 每组内
 *     option_list 一项，内含 indicator_type + indicator_value.value_list）；
 *   返回字段（自由标签）→ field_filter 的键（值一律 1，proto 占位规则）；
 *   条数上限 → limit（留空则不写该键）；
 *   启用排序 → sort_obj {sort_field, is_asc}（字段留空则不写该键）。
 * 「筛选」按钮沿用原行为：callApi("option_screen", { filter })，错误原样展示服务端原因。
 */
function OptionScreen() {
  const [form, setForm] = React.useState(() => ({ ...DEFAULT_OPTION_SCREEN_FORM }));
  const [manual, setManual] = React.useState(false);        // 高级：以手写 JSON 为准
  const [manualText, setManualText] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [result, setResult] = React.useState(null);         // { value } | { error }
  const patch = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));
  // 表单当前生成的载荷：既做「高级」面板的只读预览，也做按钮旁的即时校验提示。
  const built = React.useMemo(() => buildOptionScreenFilter(form), [form]);
  const payloadText = built.filter ? formatOptionScreenFilter(built.filter) : "";
  // 打开手动编辑时用**当前表单载荷**填充（每次打开都重新填充，覆盖上次编辑——逃生口的
  // 语义是「从表单这一份改起」，不是「再养一份独立状态」；这一点在界面文案里写明）。
  const toggleManual = (checked) => {
    setManual(checked);
    if (checked) setManualText(built.filter ? formatOptionScreenFilter(built.filter) : "");
  };
  const run = async () => {
    setResult(null);
    let filter;
    if (manual) {
      const parsed = parseOptionScreenJson(manualText);
      if (parsed.error) {
        setResult({ error: parsed.error });
        return;
      }
      filter = parsed.filter;
    } else {
      if (built.error) {
        setResult({ error: built.error });
        return;
      }
      filter = built.filter;
    }
    setBusy(true);
    try {
      // 载荷形状（futu_data.option_screen 的校验面）：filter = {strategy, field_filter,
      // limit?, sort_obj?} —— strategy 是 filter 内的非空对象键，不是 filter 的顶层展开。
      const value = await callApi("option_screen", { filter });
      setResult({ value });
    } catch (error) {
      setResult({ error: String(error.message || error) });
    } finally {
      setBusy(false);
    }
  };
  const rows = result?.value ? rowsOf(result.value) : [];
  return (
    <>
      <Alert type="info" showIcon style={{ marginBottom: 8 }} message={(
        <Typography.Text style={{ fontSize: 12 }}>
          「筛选」用下面的表单生成 option_screen 载荷。<b>市场类别</b>至少选一个（只列真机
          验证过的 {OPTION_MARKET_CATEGORIES.length} 个：非支持值会被上游静默忽略——回空列表
          + total=0，不报错，所以不给自由输入）；<b>返回字段</b>（field_filter）必填且非空：
          留空会被上游 -3 拒绝，本服务端也会拦下（省略时上游只返回 4 个默认字段、其余全
          null）。指标条件与排序可选，指标类型本仓库仅验证过 {OPTION_INDICATOR_TYPE_VERIFIED}。
          要看/改原始 JSON 用下方「高级（JSON）」——打开手动编辑后以 JSON 为准；参数校验失败
          时服务端直接拒绝，页面原样展示原因。口径见 docs/TOOL-LIMITS.md。
        </Typography.Text>)} />
      <Space direction="vertical" size="small" style={{ width: "100%", marginBottom: 8 }}>
        <ScreenRow label="市场类别"
          hint={`strategy.market_category_list；只列真机验证过的 ${OPTION_MARKET_CATEGORIES.length} 个（非支持值被上游静默忽略）`}>
          <Select mode="multiple" style={{ minWidth: 320 }}
            value={form.marketCategories}
            onChange={(value) => patch("marketCategories", value)}
            options={OPTION_MARKET_CATEGORIES}
            placeholder="至少选一个市场类别"
            aria-label="市场类别 market_category_list" />
        </ScreenRow>
        <ScreenRow label="指标条件" hint="可选；关闭时 strategy.filter_group_list 给空数组">
          <Switch checked={form.withIndicator}
            onChange={(checked) => patch("withIndicator", checked)}
            aria-label="加入指标条件" />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>加入指标条件</Typography.Text>
          <InputNumber style={{ width: 120 }} min={0} disabled={!form.withIndicator}
            value={form.indicatorType}
            onChange={(value) => patch("indicatorType", value)}
            aria-label="指标类型 indicator_type" />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            本仓库仅验证 {OPTION_INDICATOR_TYPE_VERIFIED}
          </Typography.Text>
          <Select mode="tags" style={{ minWidth: 200 }} disabled={!form.withIndicator}
            value={form.indicatorValues.map(String)}
            onChange={(value) => patch("indicatorValues", value)}
            tokenSeparators={[",", "，"]}
            placeholder="指标值（如 1）"
            aria-label="指标值 indicator_value.value_list" />
        </ScreenRow>
        <ScreenRow label="返回字段"
          hint="field_filter 的键；预置的 3 个经验证，其它字段名可自由输入（未验证）">
          <Select mode="tags" style={{ minWidth: 480 }}
            value={form.fieldFilter}
            onChange={(value) => patch("fieldFilter", value)}
            options={OPTION_FIELD_FILTER_FIELDS}
            tokenSeparators={[",", "，"]}
            placeholder="至少一个返回字段（留空会被上游 -3 拒绝）"
            aria-label="返回字段 field_filter" />
        </ScreenRow>
        <ScreenRow label="条数上限" hint="limit；留空则不带该键">
          <InputNumber style={{ width: 120 }} min={1} max={1000}
            value={form.limit}
            onChange={(value) => patch("limit", value)}
            aria-label="条数上限 limit" />
        </ScreenRow>
        <ScreenRow label="排序" hint="可选；字段留空则不写 sort_obj">
          <Switch checked={form.sortEnabled}
            onChange={(checked) => patch("sortEnabled", checked)}
            aria-label="启用排序" />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>启用排序</Typography.Text>
          {/* 真机形状 sort_obj={"sort_field":"volume","is_asc":false}（tests/test_wp7_futu_data.py）；
              字段用 tags+maxCount=1：单值但允许敲任意字段名（本仓库只验证过 volume）。 */}
          <Select mode="tags" maxCount={1} style={{ minWidth: 180 }} disabled={!form.sortEnabled}
            value={form.sortField ? [form.sortField] : []}
            onChange={(value) => patch("sortField", value.length > 0 ? value[value.length - 1] : "")}
            options={[{ value: "volume", label: "volume（成交量，已验证）" }]}
            placeholder="排序字段，如 volume"
            aria-label="排序字段 sort_field" />
          <Radio.Group value={form.sortAsc ? "asc" : "desc"} disabled={!form.sortEnabled}
            onChange={(event) => patch("sortAsc", event.target.value === "asc")}
            optionType="button"
            options={[{ label: "升序", value: "asc" }, { label: "降序", value: "desc" }]}
            aria-label="排序方向 is_asc" />
        </ScreenRow>
        <Space align="center" wrap size={8}>
          <Button type="primary" loading={busy} onClick={run}>筛选</Button>
          {!manual && built.error && (
            <Typography.Text type="danger" style={{ fontSize: 12 }}>{built.error}</Typography.Text>)}
        </Space>
        <Collapse size="small" items={[{
          key: "advanced",
          label: "高级（JSON）：查看表单生成的载荷 / 改为手动编辑",
          children: (
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                下面是表单当前生成的 filter 载荷（只读，可复制）。需要在原始 JSON 上直接改就用
                下面的开关——打开后会以这份载荷为初值，之后**以 JSON 为准**（表单不再参与）。
              </Typography.Text>
              <TextArea readOnly rows={6} value={payloadText || "（表单暂未通过校验，暂无载荷）"}
                aria-label="生成的 filter JSON（只读）" />
              <Space align="center" wrap size={8}>
                <Switch checked={manual} onChange={toggleManual} aria-label="改为手动编辑 JSON" />
                <Typography.Text style={{ fontSize: 12 }}>
                  改为手动编辑 JSON（打开时用当前表单载荷填充，覆盖上次编辑）
                </Typography.Text>
              </Space>
              {manual && (
                <TextArea rows={8} value={manualText}
                  onChange={(event) => setManualText(event.target.value)}
                  aria-label="filter JSON（原 strategy JSON / field_filter JSON）"
                  placeholder='整份 filter，如 {"strategy": {"market_category_list": [0]}, "field_filter": {"option_type": 1}}' />)}
            </Space>),
        }]} />
      </Space>
      {result?.error && <Alert type="error" showIcon style={{ marginBottom: 8 }} message={result.error} />}
      {result?.value && (
        <>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            返回行 {rows.length}
            {result.value.has_more === true ? " · 上游还有更多（has_more=true）" : ""}
            {result.value.total !== undefined && result.value.total !== null ? ` · total=${result.value.total}` : ""}
            {result.value.next_key ? ` · next_key=${result.value.next_key}` : ""}
          </Typography.Text>
          <Table size="small"
            rowKey={(_row, index) => index}
            dataSource={rows}
            pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
            scroll={{ x: "max-content" }}
            locale={{ emptyText: "上游返回了空结果。" }}
            columns={chainColumns(rows)} />
          <RawCollapse value={result.value} loading={false} />
        </>)}
    </>);
}

// ---------------------------------------------------------------------------
// 期权波动率与行权概率（derivative_detail，WP12 任务 6）
// ---------------------------------------------------------------------------
// 载荷契约（app.py derivative_detail 白名单 + futu_data.FutuData.derivative_detail）：
//   {code, section, params?}——字段名是 code；section 白名单 4 项，本区用其中两项。
// **合约要求**（锁定表 §C.7 官方 -3）：这两个 section 的 symbol 必须是**期权合约**，
// 传正股会被上游直接拒绝——所以本区用独立的「合约代码」输入，不与正股输入共用。
// 权限语义：行权概率官方 -9 = 用户无期权数据查询权限（与标的无关），由 dataplaneHint 说明。

/** 键值行（缺值由纯函数给 —）。 */
function DerivativeRows({ rows }) {
  return (
    <Space direction="vertical" size={2} style={{ width: "100%" }}>
      {rows.map((row) => (
        <Space key={row.label} size={8} align="start">
          <Typography.Text type="secondary" style={{ minWidth: 180, display: "inline-block" }}>
            {row.label}
          </Typography.Text>
          <Typography.Text>{row.value}</Typography.Text>
        </Space>))}
    </Space>);
}

/** 单 section 查询卡：错误给服务端原因 + 下一步；空对象给如实说明。 */
function DerivativeCard({ title, section, code, summarize, withStrikes = false }) {
  const query = useEndpoint("derivative_detail", code ? { code, section } : null, [code, section]);
  const summary = query.value ? summarize(query.value) : null;
  const strikes = withStrikes ? strikeRows(summary?.strikes) : [];
  const hint = query.error ? dataplaneHint(query.error) : "";
  return (
    <Card type="inner" title={title}>
      {query.error && (
        <Alert type="error" showIcon message={`读取失败：${query.error}`}
          description={hint || undefined} />)}
      {summary?.note && <Alert type="info" showIcon message={summary.note} />}
      {summary && !summary.note && (
        <>
          <DerivativeRows rows={summary.rows} />
          {strikes.length > 0 && (
            <>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                行权概率 {summary.strikes.length} 条（键名按上游原样展示，完整内容见下方原始返回）：
              </Typography.Text>
              {strikes.map((row) => (
                <Space key={row.index} wrap size={12} style={{ marginTop: 4 }}>
                  <Typography.Text type="secondary">#{row.index}</Typography.Text>
                  {row.cells.map((cell) => (
                    <Typography.Text key={cell.label}>
                      <Typography.Text type="secondary">{cell.label}=</Typography.Text>{cell.value}
                    </Typography.Text>))}
                </Space>))}
            </>)}
        </>)}
      <RawCollapse value={query.value} loading={query.loading} />
    </Card>);
}

/** 衍生品区：独立合约输入 + 两张卡（各自独立失败）。 */
function DerivativeDetail() {
  const [contract, setContract] = React.useState("");
  const [code, setCode] = React.useState("");
  return (
    <Card type="inner" title="期权波动率与行权概率（derivative_detail）"
      extra={(
        <Space>
          {/* 期权合约代码（US.AAPL260116C00200000）与标的代码是**两种格式**：合约里嵌了
              到期日/看跌看涨/行权价，关注池与持仓都不产出它，套标的候选只会给出一堆按下去
              必被上游拒绝的值——故此处保持普通 Input，不加候选（WP24 明确不接）。 */}
          <Input placeholder="期权合约代码，如 US.AAPL260116C00200000" style={{ width: 300 }}
            value={contract}
            onChange={(event) => setContract(event.target.value)}
            onPressEnter={() => setCode(contract.trim().toUpperCase())} />
          <Button onClick={() => setCode(contract.trim().toUpperCase())}>查询</Button>
        </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          两个接口要求传期权合约代码（传正股会被上游拒绝）；数据经富途 OpenAPI 数据面。
        </Typography.Text>
        {!code && <Typography.Text type="secondary">输入期权合约代码后查询。</Typography.Text>}
        {code && (
          <DerivativeCard title="期权波动率" section="option_volatility" code={code}
            summarize={optionVolatilitySummary} />)}
        {code && (
          <DerivativeCard title="行权概率" section="option_exercise_probability" code={code}
            summarize={exerciseProbabilitySummary} withStrikes />)}
      </Space>
    </Card>);
}

export default function OptionsPage() {
  const [ticker, setTicker] = React.useState("");
  const [query, setQuery] = React.useState("");
  const expirations = useEndpoint("option_expiration", query ? { code: query } : null, [query]);
  const chain = useEndpoint("option_chain", query ? { code: query } : null, [query]);
  return (
    <Card title="期权分析" extra={(
      <Space>
        <SymbolInput placeholder="标的代码，如 HK.00700 / US.AAPL" style={{ width: 240 }} value={ticker}
          onChange={setTicker}
          onPressEnter={(text) => setQuery(text.trim().toUpperCase())} />
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {!query && (
          <Typography.Text type="secondary">
            输入标的代码后查询；富途实时直通（TTL 0），期权限级决定数据面。
          </Typography.Text>)}
        {query && (
          <Card type="inner" title="到期日列表（option_expiration）">
            {expirations.error && (
              <Alert type="error" showIcon message={`到期日读取失败：${expirations.error}`} />)}
            {!expirations.error && (
              <Space size={4} wrap>
                {expirationCandidates(expirations.value).map((date) => (
                  <Typography.Text key={date} code>{date}</Typography.Text>))}
                {expirations.loading && <Typography.Text type="secondary">到期日加载中…</Typography.Text>}
                {!expirations.loading && expirationCandidates(expirations.value).length === 0 && (
                  <Typography.Text type="secondary">上游未返回到期日（见原始返回）。</Typography.Text>)}
              </Space>)}
            <RawCollapse value={expirations.value} loading={expirations.loading} />
          </Card>)}
        {query && (
          <Card type="inner" title="期权链（option_chain）">
            {/* key=query：换标的时重建组件，到期日本地筛选不残留上一个标的的选择 */}
            <ChainTable key={query} chain={chain} />
          </Card>)}
        <Card type="inner" title="期权筛选（option_screen）">
          <OptionScreen />
        </Card>
        <DerivativeDetail />
      </Space>
    </Card>);
}
