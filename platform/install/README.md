# `platform/install/`：监控探测的**触发者**素材（规格 §8.3）

本目录只放**可直接安装的 systemd 单元素材**与说明，**本仓库没有安装、没有 enable/start
任何单元**（安装属运维动作）。

| 文件 | 作用 |
|---|---|
| `quant-v3-probe.service` | 一次运行：顺序触发两个**只读**探测端点，写两张缓存 |
| `quant-v3-probe.timer` | 定时器：每 30 分钟触发一次（`OnCalendar=*:0/30`） |
| `README.md` | 本文件 |
| `quant-headless/` | 另一个话题（headless profile 素材），与本定时器无关 |

---

## 一、为什么需要它：`/metrics` 抓取路径**故意不打上游**

`GET /metrics` 上的探测类指标全部来自**落盘缓存**，抓取路径**绝不发起外部调用**——
富途调用要过全局限流器（`v3_ratelimit`），用监控去触发「被限流」告警是自伤。
于是两张缓存必须由「别的人/别的东西探测后写入」：

| 缓存文件（`<DSH_HOME>/`） | 谁写 | 哪些指标读它 |
|---|---|---|
| `v3-datasource-probe.json` | `GET /api/v3/sources/status` | `quantwb_datasource_*` |
| `v3-risk-probe.json` | `POST /api/v3/metrics/probe/refresh` | `quantwb_risk_industry_*` |

**没有人跑这两个端点 ⇒ 下列告警会静默**（不是「没超限」，是「没有结论」）：

- `DataSourceChainUnavailable`、`DataSourceFellBackToSecondary`
- `RiskIndustryConcentrationBreached`、`RiskIndustryConcentrationApproaching`

这正是 `DataSourceProbeStale` 与 `RiskIndustryProbeStale` 两条 info 规则的存在理由：
**让「静默」本身可见**。本单元就是那个触发者。

> ⚠️ 一条容易忽略的边界：`*ProbeStale` 规则读的是
> `quantwb_datasource_probe_timestamp_seconds` / `quantwb_risk_industry_probe_timestamp_seconds`，
> 这两个指标**只在缓存文件存在时才导出**。也就是说：**从未探测过**时，连「过期告警」都不会响
> ——指标家族压根不出现在 `/metrics` 里。所以要么装本定时器，要么**至少手工跑一次**上面两个
> 端点把缓存建起来。

---

## 二、安装

### 方式 A：用户级（推荐，无需 root；跟随 `%h`）

```bash
mkdir -p ~/.config/systemd/user
cp platform/install/quant-v3-probe.service platform/install/quant-v3-probe.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now quant-v3-probe.timer

# 校验
systemctl --user list-timers quant-v3-probe.timer
systemctl --user status quant-v3-probe.service     # 最近一次运行的结果
journalctl --user -u quant-v3-probe.service -n 50  # 失败时看这里
```

用户级单元默认只在**有人登录**时运行；要让它在无人登录（例如只开机的机器）也跑：

```bash
loginctl enable-linger "$USER"
```

### 方式 B：系统级（需要 root；把 `%h` 换成实际路径）

```bash
sudo cp platform/install/quant-v3-probe.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quant-v3-probe.timer
```

> 服务里没有 `User=`/`WorkingDirectory=` 依赖，`curl` 是绝对路径（`/usr/bin/curl`），
> 所以两种方式都能直接跑；系统级下若平台服务以别的用户运行，只需保证 `127.0.0.1:8397`
> 可达（默认只绑回环，同机即可）。

### 手工演练（不装单元也能验）

```bash
curl -fsS -m 60  'http://127.0.0.1:8397/api/v3/sources/status'                    # 写降级链缓存
curl -fsS -m 120 -X POST 'http://127.0.0.1:8397/api/v3/metrics/probe/refresh'     # 写行业缓存
curl -s http://127.0.0.1:8397/metrics | grep -E 'datasource_probe_timestamp|risk_industry'
```

---

## 三、不装定时器会怎样（如实说明）

1. 缓存一直不刷新 → 超过 `QUANT_RISK_PROBE_MAX_AGE`（默认 6h）后
   `quantwb_risk_industry_pct` **不再导出**（宁可没有结论，也不拿旧值下结论）；
   超过 24h 后降级链取值类指标仍在，但规则的时间守卫会让它们不再触发；
2. `DataSourceProbeStale` / `RiskIndustryProbeStale` 转为 `firing`（info 级）——
   这是**设计如此**：让「监控不响」这件事本身可见；
3. 对应的 critical/warning 告警静默。**静默不等于正常**，值班看到 stale 告警就手工跑一次
   上面两条 curl。

## 四、代价（别装得太勤）

每次刷新都是**真实取数**，会消耗富途配额：

- `GET /api/v3/sources/status`：每条链按主源→降级源各做一次轻量探测，最坏 ~45s
  （`v3_fallback.PROBE_CHAIN_TIMEOUT`）；
- `POST /api/v3/metrics/probe/refresh`：默认对 SH/HK/US 各跑一次
  `v3_industry.industry_exposure`，按标的数打 `futu/info_owner_plate`
  （实测 SH 自选池 28 只；富途调用全部过 `v3_ratelimit` 限速/单飞/退避）；
- 30 分钟一次 ≈ 每天 48 次；若嫌多，把 `OnCalendar` 改成每日一次
  （`*-*-* 08:30:00`）仍能压住 24h 的 staleness 规则。

---

## 五、本次的校验（真跑，只读）

```console
$ systemd-analyze verify platform/install/quant-v3-probe.service
$ echo $?
0

$ systemd-analyze verify platform/install/quant-v3-probe.timer
$ echo $?
0
```

（工具本身可用性对照：故意写错的单元会报错并非零退出——
`/tmp/broken.service:5: Unknown key 'NotAKey' ...` / `Command /nonexistent/bin/foo is not
executable`，`exit=1`。本次未安装、未 enable、未 start 任何单元。）
