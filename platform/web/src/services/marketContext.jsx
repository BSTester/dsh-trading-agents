// 全局市场选择器的 React 上下文（与纯函数 marketFilter.js 分开：那一个要能在 node --test
// 里直测，不引 React）。
//
// 口径：
//   * 选择**全局**（一个页面只看一个市场链，或看全部）——用户要的是「按市场分类查看」，
//     而不是每个卡片各自一个过滤器；放在页头，切页不丢。
//   * 持久化到 localStorage：刷新/切页/重开浏览器都保留，避免每次重新点。
//   * 读回时**归一到合法取值**（normalizeMarketChoice）：旧版本写过的脏值不得让页面空白。
import React from "react";
import { MARKET_ALL, normalizeMarketChoice } from "./marketFilter.js";

const STORAGE_KEY = "dsh.workbench.market";

const MarketFilterContext = React.createContext({ market: MARKET_ALL, setMarket: () => {} });

function readStored() {
  try {
    return normalizeMarketChoice(window.localStorage.getItem(STORAGE_KEY));
  } catch (error) {
    // 隐私模式/存储被禁用：不影响功能，回落到默认「全部市场」
    return MARKET_ALL;
  }
}

export function MarketFilterProvider({ children }) {
  const [market, setMarketState] = React.useState(readStored);
  const setMarket = React.useCallback((next) => {
    const value = normalizeMarketChoice(next);
    setMarketState(value);
    try {
      window.localStorage.setItem(STORAGE_KEY, value);
    } catch (error) {
      // 写失败只是不持久化，本次会话内仍然生效
    }
  }, []);
  const value = React.useMemo(() => ({ market, setMarket }), [market, setMarket]);
  return <MarketFilterContext.Provider value={value}>{children}</MarketFilterContext.Provider>;
}

/** 页面消费入口：``const { market, setMarket } = useMarketFilter();`` */
export function useMarketFilter() {
  return React.useContext(MarketFilterContext);
}
