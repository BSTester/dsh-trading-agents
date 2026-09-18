// 标的输入框（WP24）：手打代码 + 联想候选（**下拉可选，不强制**）。
//
// 用户需求原文：「标的输入框现在是手打代码——如果也觉得『记不住』，可以给它们加上从
// 关注池+当前持仓来的联想候选（下拉可选，不强制）。」
//
// 三个约束，本组件逐条落实：
//   1. **仍然允许任意输入**：控件是 antd ``AutoComplete``（自由文本）与
//      ``Select mode="tags"``（自由标签），候选里没有的代码照常输入、照常提交；
//      组件不做任何校验，也不会因为「不在候选里」清空值；
//   2. **不改变提交行为**：``onPressEnter`` 该触发查询的仍然触发（Enter 语义见下面
//      两个组件各自的注释），宽度/占位/受控逻辑由调用方原样传入；
//   3. **候选来源只有一份**：``useSymbolCandidates()``（services/hooks.js）在模块级
//      共享「关注池 + 当前持仓」，页面里多个输入框不会各发一次请求。
//
// 候选**不按全局市场筛选**（既有决定）：一个输入框问的是「某一个标的」，与页面视角无关；
// 市场只作为每条候选上的标签展示（``MARKET_CHAIN_LABELS``）。
import React from "react";
import { AutoComplete, Select, Tag } from "antd";
import { useSymbolCandidates } from "../services/hooks.js";
import { MARKET_CHAIN_LABELS } from "../services/marketFilter.js";
import {
  SYMBOL_CANDIDATE_LIMIT, filterSymbolCandidates, joinSymbolList, splitSymbolList,
} from "../services/symbols.js";

/** 候选 → antd option：``label`` 带名称，市场链另起一个小标签（归不到市场就不标）。 */
function toOption(row) {
  const market = row?.market ? MARKET_CHAIN_LABELS[row.market] : null;
  return {
    value: row.value,
    label: market
      ? <span>{row.label} <Tag style={{ marginInlineStart: 6 }}>{market}</Tag></span>
      : row.label,
  };
}

function useAutoOptions(source, input, limit) {
  return React.useMemo(
    () => filterSymbolCandidates(source, input, limit).map(toOption),
    [source, input, limit]);
}

/**
 * 单标的输入框：``AutoComplete`` 包一层，其余 props 透传给 antd（``style``/``placeholder``…）。
 *
 * * ``value``/``onChange``：受控字符串（与原来的 ``Input`` 完全一致）；
 * * ``onPressEnter``：**收到当前要提交的文本**——页面的 ``onPressEnter`` 原来读自己的
 *   state，Enter 选中候选时那还是旧值；这里显式把「光标下的文本 / 刚选中的候选」传过去，
 *   页面据此发起查询（见各页接线）；
 * * ``options``：候选来源覆盖（缺省用共享的关注池+持仓）；测试与特殊页面可自备一份。
 *
 * Enter 语义（刻意收紧，避免抢掉原来的「回车即查询」）：``activeFirstOption={false}``
 * ——下拉打开时不预选第一条，所以回车**不改变**输入内容，直接按用户敲的代码查询；
 * 只有用户自己用方向键选到某条候选（或鼠标点选）时，才把候选值填进输入框。
 */
export function SymbolInput({
  value, onChange, onPressEnter, placeholder, style, ariaLabel, options,
  limit = SYMBOL_CANDIDATE_LIMIT, ...rest
}) {
  const shared = useSymbolCandidates();
  const source = Array.isArray(options) ? options : shared.candidates;
  const text = typeof value === "string" ? value : "";
  const antOptions = useAutoOptions(source, text, limit);
  // 提交值单独记一份：Enter 选中候选时 onChange 已经发出，但父组件的 state 要到下次
  // 渲染才更新，直接在 onKeyDown 里读 props.value 会提交**旧文本**。
  const emitted = React.useRef(text);
  if (emitted.current !== text) emitted.current = text;

  return (
    <AutoComplete
      value={text}
      options={antOptions}
      onChange={(next) => {
        const merged = String(next ?? "");
        emitted.current = merged;
        onChange?.(merged);
      }}
      onKeyDown={(event) => {
        if (event.key !== "Enter") return;
        onPressEnter?.(emitted.current);
      }}
      activeFirstOption={false}
      filterOption={false}
      placeholder={placeholder}
      style={style}
      aria-label={ariaLabel}
      {...rest}
    />
  );
}

/**
 * 多标的输入框（逗号分隔）↔ 标的数组。
 *
 * 用 ``Select mode="tags"``：逗号（中英文）即分隔符，任何输入都能成为标签（**自由输入
 * 不受候选限制**），候选也可点选；``onChange`` 回到调用方的是**逗号分隔字符串**，
 * 与页面原来的 ``input`` state 同类型，接线零改动。
 *
 * Enter 只做一件事：把「已提交的标签 + 光标下还没提交的文本」合并成新的一串，触发一次
 * 查询（``onPressEnter(merged)``）。之所以自己合并而不是等 antd 提交标签：下拉打开时
 * rc-select 的 Enter 走「选中当前高亮项」分支，不会提交搜索文本，等它就会出现
 * 「按了回车但刚敲的代码没进去」。
 */
export function SymbolListInput({
  value, onChange, onPressEnter, placeholder, style, ariaLabel, options,
  limit = SYMBOL_CANDIDATE_LIMIT, ...rest
}) {
  const shared = useSymbolCandidates();
  const source = Array.isArray(options) ? options : shared.candidates;
  const committed = React.useMemo(() => splitSymbolList(value), [value]);
  const [search, setSearch] = React.useState("");
  const searchRef = React.useRef("");
  const emitted = React.useRef(joinSymbolList(committed));
  const committedText = joinSymbolList(committed);
  if (emitted.current !== committedText) emitted.current = committedText;
  const antOptions = useAutoOptions(source, search, limit);

  const emit = (list) => {
    const next = joinSymbolList(list);
    emitted.current = next;
    onChange?.(next);
    return next;
  };

  return (
    <Select
      mode="tags"
      value={committed}
      options={antOptions}
      filterOption={false}
      tokenSeparators={[",", "，"]}
      onChange={(list) => { setSearch(""); searchRef.current = ""; emit(list); }}
      onSearch={(text) => {
        const merged = String(text ?? "");
        searchRef.current = merged;
        setSearch(merged);
      }}
      onKeyDown={(event) => {
        if (event.key !== "Enter") return;
        const merged = emit([...splitSymbolList(emitted.current),
                             ...splitSymbolList(searchRef.current)]);
        setSearch("");
        searchRef.current = "";
        onPressEnter?.(merged);
      }}
      placeholder={placeholder}
      style={style}
      aria-label={ariaLabel}
      {...rest}
    />
  );
}
