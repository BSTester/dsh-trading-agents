// 标的代码归一 + 联想候选（WP24）。
//
// 为什么需要这一层：工作台的标的输入框此前全靠手打代码，用户记不住代码（用户需求原文：
// 「标的输入框现在是手打代码——如果也觉得『记不住』，可以给它们加上从关注池+当前持仓来的
// 联想候选（下拉可选，不强制）」）。候选是**帮忙**，不是白名单：输入框仍然接受任意代码，
// 候选里没有的值照常提交。
//
// 候选池的两个来源，写法完全不同：
//   * **平台关注池**——配置 ``~/.dsh/trading-platform.json`` 顶层 ``watchlist``，已是
//     ``SH.600519`` 这种带前缀写法（后端唯一实现在 ``trading_core.watchlist``，随
//     ``snapshot`` 载荷下发给前端）；
//   * **当前持仓**——``positions`` 端点的 ``groups[].positions[]`` 给的是**裸代码**
//     （实测 ``{symbol: "00100", name: "MINIMAX-W"}``，市场信息在账户组上，而在标的
//     代码上没有），必须在前端归一成带前缀写法才能与关注池去重。
//
// 归一的规则**在后端已有唯一实现**：``plugins/datasource/python/trading_datasource/market.py``
// 的 ``to_futu_symbol``。前端为什么还要抄一份：候选要在用户敲键的**当次渲染**里就要有值，
// 不能等一次服务端往返；而且候选值必须与用户最终提交的值同时过服务端的同一规则，两侧写法
// 不一致就会出现「点了候选反而查不到」。抄一份就有漂移风险——
// **漂移由 ``tests/test_wp24_symbol_mirror.py`` 锁**：它正则解析本文件的四个常量
// （``FUTU_PREFIXES`` / ``SH_FIRST_DIGITS`` / ``BJ_FIRST_DIGITS`` / ``HK_CODE_LENGTH``）与
// 各分支判据，按解析值重建规则，再与后端在同一张输入表上逐个比对结果；后端改规则、或本文件
// 改了常量而没跟，那张表先红。
import { symbolChain } from "./marketFilter.js";

/** 富途市场前缀；与后端 ``^(SH|SZ|BJ|HK|US)\.`` 同源（锁定测试按行为反查后端）。 */
export const FUTU_PREFIXES = ["SH", "SZ", "BJ", "HK", "US"];
/** 6 位 A 股代码的首位分档：6/9 → SH（后端 ``'SH' if text[0] in '69'``）。 */
export const SH_FIRST_DIGITS = "69";
/** 4/8 → BJ（后端 ``'BJ' if text[0] in '48'``）；其余 6 位 → SZ。 */
export const BJ_FIRST_DIGITS = "48";
/** 港股代码位数：1~5 位纯数字按港股处理并左侧补零（后端 ``text.zfill(5)``）。 */
export const HK_CODE_LENGTH = 5;
/** 下拉一次最多给多少条候选（空输入时就是「前 N 条」，点开即可选）。 */
export const SYMBOL_CANDIDATE_LIMIT = 20;

const PREFIX = FUTU_PREFIXES.join("|");
const PREFIXED = new RegExp(`^(${PREFIX})\\.[A-Z0-9.]+$`);
const SUFFIXED = new RegExp(`^([A-Z0-9.]+)\\.(${PREFIX})$`);

/**
 * 把各种写法归一为富途的 ``MARKET.CODE``（``to_futu_symbol`` 的逐条镜像）。
 *
 * 分支顺序与后端一致，**不可重排**：
 *   1. 已带前缀（``SH.600519``）→ 原样（大小写/空白先归一）；
 *   2. ``代码.市场``（``600519.SH``）→ 换序；
 *   3. 6 位纯数字 → 按首位分流（6/9→SH、4/8→BJ、其余→SZ）；
 *   4. 1~5 位纯数字 → ``HK.`` + 左侧补零到 5 位；
 *   5. 其余 → ``US.``（代码里的点号属于代码本身，如 ``BRK.B``）。
 *
 * 有意差异（仅一处，且比后端更保守）：``null``/``undefined`` 归一为 ``US.``，与后端的
 * ``str(None)`` → ``US.NONE`` 不同——候选构造会先滤掉空值，不会走到这里。
 */
export function toFutuSymbol(text) {
  const value = String(text ?? "").trim().toUpperCase();
  if (PREFIXED.test(value)) return value;
  const swapped = value.match(SUFFIXED);
  if (swapped) return `${swapped[2]}.${swapped[1]}`;
  if (/^\d{6}$/.test(value)) {
    const head = SH_FIRST_DIGITS.includes(value[0]) ? "SH"
      : BJ_FIRST_DIGITS.includes(value[0]) ? "BJ" : "SZ";
    return `${head}.${value}`;
  }
  if (/^\d{1,5}$/.test(value)) return `HK.${value.padStart(HK_CODE_LENGTH, "0")}`;
  return `US.${value}`;
}

/** 持仓端点载荷 → 持仓行数组（``{groups:[{positions:[…]}]}``；也接受已是行数组的写法）。 */
function positionRows(positions) {
  if (Array.isArray(positions)) return positions;
  const groups = positions?.groups;
  if (!Array.isArray(groups)) return [];
  const rows = [];
  for (const group of groups) {
    if (Array.isArray(group?.positions)) rows.push(...group.positions);
  }
  return rows;
}

/**
 * 候选列表：**关注池在前、持仓在后**，每项 ``{value, label, market}``。
 *
 * * ``value``：带前缀写法（``SH.600519``）——两侧来源都过 ``toFutuSymbol``，所以同一条
 *   标的无论来自关注池还是持仓都归到同一个值，去重才有意义；
 * * ``label``：知道名称时带名称（``SH.600519 贵州茅台``），否则就是代码本身；
 * * ``market``：``symbolChain`` 归出的市场链（``SH``/``HK``/``US``），归不到为 ``null``
 *   ——**不猜**，界面据此决定要不要标市场标签。
 *
 * 去重口径：同值只留一条，**位置保持首次出现处**（关注池优先的次序不被持仓打乱），
 * 名称取「有名称的那条」——关注池只给代码、持仓才有名称，所以关注池里的标的会原地
 * 升级成带名称的候选，而不是被持仓版本再插一条。
 */
export function symbolCandidates({ watchlist, positions } = {}) {
  const out = [];
  const index = new Map();
  const add = (rawSymbol, rawName) => {
    const text = String(rawSymbol ?? "").trim();
    if (!text) return;                       // 空代码不产出候选（不生成 "US." 这种垃圾值）
    const value = toFutuSymbol(text);
    const name = typeof rawName === "string" ? rawName.trim() : "";
    const seen = index.get(value);
    if (seen === undefined) {
      index.set(value, out.length);
      out.push({ value, label: name ? `${value} ${name}` : value, market: symbolChain(value) });
      return;
    }
    const row = out[seen];
    if (name && row.label === value) row.label = `${value} ${name}`;   // 原地补上名称
  };
  for (const item of Array.isArray(watchlist) ? watchlist : []) add(item);
  for (const row of positionRows(positions)) add(row?.symbol, row?.name);
  return out;
}

/**
 * 两个端点载荷 → 候选列表：``snapshot``（关注池在顶层 ``watchlist``，后端
 * ``server/app.py`` 的 snapshot 合并点写入）+ ``positions``（``{groups:[{positions}]}``）。
 *
 * 存在的理由是一次真实缺陷：调用点自己拼 ``{watchlist: …, positions: …}`` 时写错了键名
 * （关注池被塞进 ``pool``），关注池被静默丢弃、候选只剩持仓——纯函数测不到调用点的键名，
 * 于是接线收敛到这一个可直测的函数上（回归钉见 tests/symbols.test.mjs）。
 */
export function symbolCandidatesFromPayloads(snapshot, positions) {
  return symbolCandidates({ watchlist: snapshot?.watchlist, positions });
}

/**
 * 按输入过滤候选（大小写不敏感的子串匹配，同时看 ``value`` 与 ``label``：
 * 只输数字（``600``）命中 ``SH.600519``，带前缀（``hk.007``）命中 ``HK.00700``，
 * 输中文名（``茅台``）命中 ``SH.600519 贵州茅台``）。
 *
 * 空输入（或纯空白）**不隐藏**候选，按原序给前 ``limit`` 条——用户点开输入框就能选，
 * 不必先想起代码。``limit`` 缺省 20；非正数/非法值返回空数组（不返回「全部」，
 * 避免一处拼错把整池灌进下拉）。
 */
export function filterSymbolCandidates(candidates, input, limit = SYMBOL_CANDIDATE_LIMIT) {
  const rows = Array.isArray(candidates) ? candidates : [];
  const size = Number(limit);
  if (!Number.isFinite(size) || size <= 0) return [];
  const query = String(input ?? "").trim().toUpperCase();
  if (!query) return rows.slice(0, size);
  return rows
    .filter((row) => `${row?.value ?? ""} ${row?.label ?? ""}`.toUpperCase().includes(query))
    .slice(0, size);
}

/**
 * 多标的字段 ↔ 标的数组：逗号（中英文）/空白分隔、去空、大写归一、**去重保序**。
 *
 * 与页面既有 ``parseWatchlist`` 同一口径（那两处已收敛到这里，不再各写一份），
 * 只多一条去重：同一个标的重输两次会让服务端按两个标的算（因子相关性/IC 的分母就错了），
 * 而去重对用户是纯好事。
 */
export function splitSymbolList(text) {
  const out = [];
  const seen = new Set();
  for (const piece of String(text ?? "").split(/[,，\s]+/)) {
    const value = piece.trim().toUpperCase();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

/** 标的数组 → 逗号分隔字符串（与 ``splitSymbolList`` 互逆，往返稳定）。 */
export function joinSymbolList(list) {
  const text = Array.isArray(list) ? list.join(",") : list;
  return splitSymbolList(text).join(",");
}
