# 中国金融数据源实测报告 & cnfin.py 使用说明

[![CI](https://github.com/RenPsz/DingFeng_Data_Sources/actions/workflows/ci.yml/badge.svg)](https://github.com/RenPsz/DingFeng_Data_Sources/actions/workflows/ci.yml)

> 仓库：**https://github.com/RenPsz/DingFeng_Data_Sources**
> 当前版本：**v1.1.0**（生产审计修复版）　实测时间：2026-10-24　环境：Windows / Python 3.13.14
> 网络：家庭宽带 + 本地代理（Clash 7897）
> 测试标的：浦发银行 600000、平安银行 000001、上证指数、沪深300、300ETF 510300、创业板ETF 159915、北交所 430047
> 全部结论来自**真实调用**，原始数据见 `research/results/source_report.json`、`research/results/health.txt`
> 质量工程：离线测试 / 真实网络集成测试 / E2E / CI/CD 见 [`TESTING.md`](docs/TESTING.md)，审计结论见 [`AUDIT.md`](docs/AUDIT.md)

---

## 一、一句话结论

**没有"一个接口全搞定"的免费源。** 正确的做法是"**能力拆开、多源组合、自动降级**"。

本次实测下来最可靠的组合是：

| 数据 | 首选 | 次选 | 说明 |
|---|---|---|---|
| 实时行情 | **腾讯** `qt.gtimg.cn` | 新浪 / 东财 | 90–150ms，股票/指数/ETF/可转债全覆盖 |
| 日/周/月 K 线 | **腾讯** `web.ifzq.gtimg.cn` | 东财 / baostock / 新浪 | 支持前复权、后复权、不复权，可回溯到 2010 年 |
| 分钟 K 线 | **腾讯** `ifzq.gtimg.cn` | 东财 | 1/5/15/30/60 分钟 |
| 财务报表 / 财务指标 | **东财数据中心** | akshare（新浪口径）/ baostock | 165 个指标字段，含三大报表原文 |
| 宏观（统计局口径） | **东财数据中心镜像** | akshare | 19 个指标全部实测通过 |
| 股票列表 | 东财 / akshare / baostock | — | 首次约 7s，建议缓存一天 |
| 交易日历 | **上证指数日线推导** | baostock / akshare | 无需额外接口，零误差 |

这些全部封装进了 **`cnfin.py`（单文件，约 2000 行）**，调用方只面对统一字段。

---

## 二、实测矩阵（通过 / 总数）

| 能力 | 腾讯 | 新浪 | 东方财富 | baostock | akshare | efinance | adata |
|---|---|---|---|---|---|---|---|
| 实时行情 | ✅ 155ms | ✅ 91ms | ⚠️ 被网络阻断 | — | ⚠️ 依赖东财 | ❌ | — |
| 日/周/月K线 | ✅ 158ms | ✅ 130ms | ⚠️ 被网络阻断 | ✅ 4s | ⚠️ 依赖东财 | ❌ | ❌ |
| 分钟K线 | ✅ 138ms | — | ⚠️ 被网络阻断 | — | ⚠️ 依赖东财 | — | — |
| 股票列表 | — | — | ⚠️ 被网络阻断 | ✅ 12s | ✅ 7s | ❌ | ⚠️ 190s |
| 财务报表 | — | — | ✅ 166ms | — | ✅ 400–750ms | — | — |
| 财务指标 | — | — | ✅ 111ms | ✅ 3.5s | ✅ 496ms | — | ✅ 842ms |
| 分红送转 | — | — | ✅ 89ms | — | — | — | — |
| 宏观数据 | — | — | ✅ 54–250ms | — | ✅ 90–530ms | — | — |
| 交易日历 | ✅（推导） | — | — | ✅ | ✅ 204ms | — | — |
| 国家统计局官方 | — | — | — | — | — | — | — |

> ⚠️ "被网络阻断"不是接口本身失效：东财 `push2*.eastmoney.com` 在**本机网络**下 TCP 能连上、
> 但 TLS 握手后被立即 RST（代理和直连都一样）。换网络（比如手机热点、其他省份宽带）大概率正常。
> `cnfin.py` 对这种情况做了自动降级 + 熔断，不会拖慢主流程。

---

## 三、逐个数据源实测细节（含踩坑记录）

### 1. 腾讯财经 —— 本次最大赢家

| 接口 | 用途 | 实测 |
|---|---|---|
| `https://qt.gtimg.cn/q=sh600000,sz000001` | 实时行情（GBK 编码） | ✅ 90–150ms，一次可拉 40+ 只 |
| `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get` | 日/周/月 K 线 | ✅ 130ms |
| `https://ifzq.gtimg.cn/appstock/app/kline/mkline` | 分钟线 | ✅ 138ms |

**优点**：无需注册、无需 token、不限流、支持前/后复权、股票/指数/ETF/可转债全覆盖、可回溯到 2010 年。
**坑**：
- 返回 **GBK** 编码，不设置 `encoding='gbk'` 会乱码。
- 单次最多返回 **640 根**日线（传 count=5000 会直接报错）。取长历史必须按 `end` 日期向前翻页 —— 这个坑我在 `cnfin.py` 里已经处理（2010 至今 4032 根，一次调用拿到）。
- 字段是靠**位置索引**取的（53 个 `~` 分隔字段），腾讯改版会全崩。已封装并做了长度校验。
- **不返回成交额**，只有成交量。需要成交额请用 `prefer="baostock"` 或东财。

### 2. 新浪财经 —— 最好的备胎

| 接口 | 用途 | 实测 |
|---|---|---|
| `https://hq.sinajs.cn/list=sh600000` | 实时行情 | ✅ 91ms（必须带 `Referer: finance.sina.com.cn`） |
| `money.finance.sina.com.cn/.../CN_MarketData.getKLineData` | 日线 | ✅ 130ms |

**坑**：不带 Referer 返回 403；日线最多 1023 根；**复权口径不透明**（返回的是前复权价但接口没标），所以 `cnfin.py` 把它排在第 3 顺位，只在前面都挂了时兜底。

### 3. 东方财富 —— 数据最全，但分两套域名，命运不同

| 域名 | 用途 | 本机实测 |
|---|---|---|
| `datacenter.eastmoney.com` / `datacenter-web.eastmoney.com` | **财报、宏观、分红** | ✅ 稳定，50–250ms |
| `push2.eastmoney.com` / `push2his.eastmoney.com` | 行情、K线、股票列表 | ⚠️ TLS 后被 RST |

**这是本次最重要的发现**：很多人以为"东财挂了"，其实只是 `push2` 行情域名被挡，**数据中心那套域名一直是好的**，而中国最全的免费财报/宏观数据恰恰在数据中心里。

实测通过的关键报表（reportName）：

```
宏观：RPT_ECONOMY_CPI / PPI / GDP / PMI / CURRENCY_SUPPLY / CUSTOMS(进出口) /
      INDUS_GROW(工业增加值) / ASSET_INVEST(固投) / TOTAL_RETAIL(社零) /
      HOUSE_PRICE(70城房价) / GOLD_CURRENCY(外储) / RMB_LOAN(新增贷款) /
      DEPOSIT_RESERVE(存准率) / TAX(税收) / FDI / BOOM_INDEX(景气) / RPTA_WEB_RATE(LPR)
财务：RPT_F10_FINANCE_MAINFINADATA(主要指标165列) / RPT_F10_FINANCE_GINCOME(利润表203列) /
      RPT_DMSK_FN_BALANCE / RPT_DMSK_FN_INCOME / RPT_DMSK_FN_CASHFLOW
其他：RPT_SHAREBONUS_DET(分红送转)
```

**坑**：
- 价格字段是**分**（要 ÷100），涨跌幅是 ÷100 的百分比。
- 财报字段名是拼音缩写：`EPSJB`=基本每股收益、`ROEJQ`=加权ROE、`PARENTNETPROFIT`=归母净利、`TOTALOPERATEREVE`=营业总收入。已在 `cnfin.py` 里映射成英文标准名。
- 部分报表（如社融）根本没有，会返回 `报表配置不存在`。

### 4. baostock —— 历史行情和财报指标的稳定基石

✅ 日线（242 行/0.6s）、股票列表（7444 行/12s）、盈利能力、成长能力。
**坑**：需要 `login()/logout()` 维护 TCP 会话，打印 `login success!` 污染 stdout（已屏蔽）；单次查询有 5–10s 的固定开销；没有实时行情。

### 5. akshare —— 万能兜底，但别当主库

✅ 股票列表 5577 行、财务摘要、财务指标、20 个宏观函数全部可用。
**坑**：
- 它的行情接口全都走东财 `push2his`，东财一被挡，`stock_zh_a_hist` / `stock_zh_a_spot_em` / `index_zh_a_hist` **全部失败**。
- 内部 tqdm 进度条会污染日志。
- `stock_a_indicator_lg`（估值）在新版已被移除，网上老教程照抄会报 `AttributeError`。
- pandas 3.x 下部分接口有 `ArrowInvalid` 风险（本次用 3.1.0 未复现，但官方仍未完全适配）。

### 6. efinance / adata —— 本次全灭，不建议再用

- **efinance 0.5.9**：`get_quote_history` 走 `push2his`（被挡）；`get_realtime_quotes(['600000'])` 直接 `KeyError: 指定的行情参数不正确`（库自身参数校验 bug）。4 个测试全失败。
- **adata 2.9.5**：`stock.market.get_market` 返回空；`stock.info.all_code()` 跑了 **190 秒**；只有 `stock.finance.get_core_index` 可用（842ms）。
- 结论：这两个库维护活跃度下降，且都强依赖东财 push2，**不要作为生产依赖**。`cnfin.py` 里没有引入它们。

### 7. tushare —— 需 token，本次未测

代码层面保留但默认不启用。它的价值在于**财务因子、指数成分、龙虎榜、北向资金**这类结构化历史数据，质量比爬虫高，缺点是积分制。如果你有 token，建议这样接入：

```python
import tushare as ts
pro = ts.pro_api("你的token")
```

`cnfin.py` 的 `BaseSource` 已经预留了扩展位，加一个 `TushareSource` 类挂进 `self.sources` 即可。

### 8. 国家统计局 —— 官方接口已被 WAF 拦截

这是本次最需要说清楚的一件事：

- 统计局官网已从老版 `easyquery.htm` 改版为单页应用 `https://data.stats.gov.cn/dg/website/page.html`。
- 老的 `easyquery.htm` 数据接口**仍然存在**，但前面加了 WAF，返回：
  ```
  403 Forbidden
  reason: UrlACL
  Client IP: 117.154.106.219
  ```
- 我试过：GET / POST、带 Cookie、带 XHR 头、HTTP / HTTPS、访问首页拿 session —— **全部 403**。这是 IP + 路径级 ACL，不是请求头问题。

**解决方案（推荐）**：统计局的数据在**东财数据中心有一份完整镜像，口径、数值完全一致**（CPI 2026-09 同比 0.9%、GDP 前三季度 4.6%、PMI 50.1，都能对应上）。`cnfin.py` 的 `fin.macro("cpi")` 走的就是这条路，**19 个宏观指标全部实测通过**。

如果你确实需要直连官方站，`fin.nbs_query()` 保留了入口（带完整参数），换到没被 ACL 的 IP（如企业专线、云主机）后可直接用，返回 `returndata.datanodes` 结构。

---

## 四、`cnfin.py` 使用说明

### 安装

**方式一：从 GitHub 获取**

```bash
git clone https://github.com/RenPsz/DingFeng_Data_Sources.git
cd DingFeng_Data_Sources
pip install .
```

**方式二：按包安装（本地已有源码时）**

```bash
pip install .                       # 或 pip install dist/*.whl
```

**方式三：单文件拷贝**

```bash
pip install requests pandas          # 最小依赖，只有这两个
pip install baostock akshare         # 可选，装了就有更多备源
```

把 `cnfin.py` 拷到你项目的任意位置即可，无配置文件、无强依赖。

**代理配置（v1.1 起不再互相污染）**

```python
import cnfin

cnfin.set_proxy("http://127.0.0.1:7897")   # 影响之后创建的 CNFin 实例
fin1 = cnfin.CNFin()
fin2 = cnfin.CNFin(timeout=5)              # fin1/fin2 的 Session、熔断、限速彼此独立
```

显式代理只作用于代理会话；代理失败后的“直连回退”是真直连，不会带着代理配置绕圈。

### 5 分钟上手

```python
from cnfin import CNFin

fin = CNFin()                       # 默认开启本地缓存（~/.cnfin/cache）

# ---------- 行情 ----------
fin.quote("600000")
fin.quote(["600000", "000001", "sh000001"])     # 批量，指数带市场前缀
#   code  name    market  price  pct_chg  open  high  low  prev_close
#   volume amount turnover pe_ttm pb float_mv total_mv ts

# ---------- K 线 ----------
fin.kline("600000", start="2010-01-01")                     # 日线 前复权
fin.kline("600000", period="week", adjust="hfq")            # 周线 后复权
fin.kline("510300", limit=100)                              # ETF
fin.kline("000001", is_index=True)                          # 上证指数
fin.kline("600000", prefer="baostock")                      # 指定源（要成交额时用）
fin.minute("600000", freq=5, limit=240)                     # 5 分钟线

# ---------- 财务 ----------
fin.financial_summary("600000", periods=8)
#   report_date report_type eps bps revenue revenue_yoy net_profit
#   net_profit_yoy roe gross_margin net_margin debt_ratio

fin.financial_statement("600000", "income")      # 利润表
fin.financial_statement("600000", "balance")     # 资产负债表
fin.financial_statement("600000", "cashflow")    # 现金流量表
fin.statement_fields("income")                   # 常用科目中英文对照表

fin.dividend("600000")                           # 分红送转

# ---------- 宏观 ----------
fin.macro("cpi")                                 # 原始宽表
fin.macro("cpi", tidy=True)                      # 整洁长表 date/period/value/unit
fin.macro("social_financing", limit=36)
fin.macro_catalog()                              # 19 个指标的清单

# ---------- 工具 ----------
fin.trade_days("2024-01-01", "2024-12-31")       # 交易日列表
fin.is_trade_day("2026-10-01")                   # False
fin.health()                                     # 一键体检，排查"今天哪个接口挂了"
fin.last_trace()                                 # 最近路由明细（最多 40 条）
fin.reset_circuit()                              # 换网络后重置熔断
fin.clear_cache()                                # 清缓存
```

### 命令行

```bash
python cnfin.py health                            # 全源体检
python cnfin.py quote 600000 000001 sh000001
python cnfin.py kline 600000 --start 2024-01-01 --period week --adjust hfq
python cnfin.py fin 600000 --periods 12
python cnfin.py macro cpi --limit 12 --tidy
python cnfin.py catalog
python cnfin.py demo                              # 17 项全功能冒烟测试
```

### 返回值契约与失败语义（v1.1 强化）

| 能力 | 统一列 | 失败语义 |
|---|---|---|
| `quote` | `code/name/market/price/pct_chg/change/open/high/low/prev_close/volume/amount/turnover/pe_ttm/pb/float_mv/total_mv/ts` | 批量请求**不允许部分成功**：缺任意请求标的会降级，全源仍缺则抛 `AllSourcesFailed` |
| `kline` | `date/open/high/low/close/volume/amount/pct_chg` | 上游缺请求的复权口径、分页不完整、字段缺失都会报错并降级；`limit` 一律取**最新 N 根** |
| `minute` | `datetime/open/high/low/close/volume` | 参数白名单校验（1/5/15/30/60 分钟） |
| `financial_summary` | `report_date/report_type/eps/bps/revenue/revenue_yoy/net_profit/net_profit_yoy/roe/gross_margin/net_margin/debt_ratio` | `report_type` 按报告期月份重算，不信任上游字段；缺列补 NaN |
| `macro(tidy=True)` | `date/period/indicator/value/unit` | **只认目录里审定的数值列**；缺列时拒绝“猜第一个数值列”，转由备源 |
| `trade_days` / `is_trade_day` | 日期字符串列表 / bool | 周末直接返回 `False`，不发请求；非法日期抛 `ValueError` |

所有公开参数都有白名单和正整数校验，错误输入在**进入路由前**就抛 `ValueError`，不会污染源熔断状态。

### 支持的宏观指标

| key | 含义 | 单位 | 频率 | key | 含义 | 单位 |
|---|---|---|---|---|---|---|
| `cpi` | 居民消费价格指数 | 同比% | 月 | `trade` | 进出口总额 | 万美元 |
| `ppi` | 工业生产者出厂价格 | 同比% | 月 | `industrial` | 工业增加值 | 同比% |
| `gdp` | 国内生产总值 | 同比% | 季 | `fixed_asset` | 固定资产投资 | 同比% |
| `pmi` | 制造业 PMI | 指数 | 月 | `retail` | 社会消费品零售总额 | 同比% |
| `m2` | 货币供应量 | 亿元 | 月 | `house_price` | 70 城住宅价格 | 同比% |
| `lpr` | 贷款市场报价利率 | % | 日 | `forex` | 外汇/黄金储备 | 亿美元 |
| `social_financing` | 社会融资规模增量 | 亿元 | 月 | `rmb_loan` | 新增人民币贷款 | 亿元 |
| `deposit_reserve` | 存款准备金率 | % | 不定期 | `tax` | 全国税收收入 | 亿元 |
| `boom_index` | 企业景气指数 | 指数 | 季 | `fdi` | 外商直接投资 | 同比%（东财 2023 后停更） |
| `money_supply` | 货币供应量（别名） | 亿元 | 月 | | | |

### 设计要点

1. **能力优先，源其次**。调用方永远只面对统一字段，不关心底层是谁。
2. **自动降级**。每个能力配一条优先级链，单源超时/被墙/改版都不影响结果。
3. **熔断但不误伤**。网络/反爬失败计数并进入冷却；停牌、非交易日这类 `NoDataError` **不计入熔断**；`ValueError/TypeError` 这类程序错误直接抛出，不会被降级掩盖。
4. **结果可追溯**。每个 DataFrame 的 `.attrs` 里有 `source / capability / elapsed_ms / cached`；`fin.last_trace()` 可看每个源的成败与耗时。
5. **绝不静默造假**。所有源都失败时抛 `AllSourcesFailed`，附带每个源的失败原因：

```python
AllSourcesFailed: [quote] 所有数据源均失败 ->
  tencent: SourceError: ...; sina: SourceError: ...; eastmoney: SourceBlocked: ...
```

### 扩展一个新数据源

```python
from cnfin import BaseSource, CNFin
import pandas as pd

class MySource(BaseSource):
    name = "mysource"
    capabilities = ("quote",)          # 声明提供哪些能力

    def quote(self, symbols):
        ...                            # 返回 DataFrame，至少含 code/price
        return df

fin = CNFin()
fin.sources["mysource"] = MySource(fin.http)
fin.quote("600000", prefer="mysource")
```

---

## 五、缓存与性能

| 能力 | 默认 TTL | 说明 |
|---|---|---|
| quote | 10 秒 | 盘中高频调用也能扛 |
| kline | 1 小时 | 日线一天才变一次，1 小时足够 |
| minute | 不缓存 | 实时性要求高 |
| financial | 24 小时 | 财报按季更新 |
| macro | 6 小时 | 统计局按月/季发布 |
| 股票列表 | 24 小时 | 首次 7s，之后秒开 |

缓存目录 `~/.cnfin/cache`，`CNFin(cache=False)` 可关闭，`clear_cache()` 清空。

**v1.1 缓存安全与隔离**

- 缓存格式从 pickle 改为 **JSON（DataFrame split 结构 + dtype 元数据）**：磁盘被篡改时最多得到一次缓存 miss，**不会执行任意代码**。
- 缓存键使用“可读前缀 + 完整键 SHA-256 摘要”：市场、`prefer`、复权、周期、起止日期、limit 全部入键，长键不会互相碰撞。
- 写入使用 **临时文件 + `os.replace` 原子替换**，并发读写不会看到半截 JSON；旧版 `.pkl` 文件会被直接忽略。
- `DataFrame.attrs`（source / elapsed 等）随缓存保存，命中后 `cached=True`。

---

## 六、已知局限（务必知情）

1. **`amount`（成交额）在腾讯源下为 NaN**。腾讯 K 线只给成交量。要成交额：`fin.kline(..., prefer="baostock")` 或等东财通路恢复。
2. **股票列表首次较慢**（7s，akshare 分页拉取）。这是所有免费源的共同问题，已用 24h 缓存规避。
3. **免费网页接口无 SLA**。腾讯/新浪/东财随时可能改版，改版后需要更新字段映射。`fin.health()` 是你最快的排查工具。
4. **不要 7×24 高频裸跑**。所有免费接口都是爬虫性质，高频会被封 IP。`cnfin.py` 内置 0.12s/host 限速 + UA 轮换，但别拿它跑 tick 级策略。
5. **复权口径**：腾讯 `qfq` 是以**最新价为基准**的前复权，历史数据会随分红变化 —— 回测时务必每天重新拉，不要存死数据。新浪 K 线只支持**不复权**，v1.1 起请求 qfq/hfq 会直接报错降级，不会静默返回其他口径。
6. **北交所**已支持（`430047` / `920002` 实测可用），但流动性差，部分接口返回字段较少。
7. **`financial_statement` 的原始宽表列**随数据源不同而不同（东财英文原始 key / 新浪中文科目）。`attrs["source"]` 必读；需要稳定 schema 请用 `financial_summary`。
8. **宏观 `tidy=True` 只信任目录审定列**。某源 schema 改版导致审定列缺失时会显式报错，而不是拿第一个数字列凑数 —— 这是刻意的保守设计。

---

## 七、测试、CI/CD 与发布

完整策略见 [`TESTING.md`](docs/TESTING.md)，审计与剩余风险见 [`AUDIT.md`](docs/AUDIT.md)。速览：

| 层 | 范围 | 默认是否跑 |
|---|---|---|
| 单元测试 | mock HTTP / mock baostock、akshare，覆盖解析、单位、缓存、熔断、降级、输入校验 | ✅ 每次 push/PR |
| E2E（离线） | CLI 子进程、公开 API 工作流、包导入 | ✅ 每次 push/PR |
| 集成测试（真实网络） | 腾讯/东财/baostock 真实调用 + 数据契约断言 | ❌ 独立定时/手动，避免第三方波动阻塞发布 |
| 构建 | wheel + sdist + `twine check` + 干净环境安装冒烟 | ✅ 测试通过后 |
| 发布 | tag `v*` 触发，重跑测试、版本/tag 一致性校验、GitHub Release 附制品 | ✅ tag 触发 |

本地命令：

```bash
python -m pip install -e ".[dev]"

python -m pytest -m "not network"               # 离线全套（CI 默认门禁）
python -m pytest -m network                     # 真实网络集成（可选）
python -m pytest -m e2e                         # 端到端
python -m build                                 # 构建 wheel/sdist
```

---

## 八、生产环境建议

| 场景 | 建议 |
|---|---|
| 研究 / 一次性分析 | 直接用 `cnfin.py`，够用且免费 |
| 日频回测 | `cnfin.py` + 本地落库（Parquet），每天增量更新 |
| 实盘/无人值守 | 加一层 Tushare Pro 或付费源做主库，`cnfin.py` 作校验与兜底 |
| 需要 Level-2 / 逐笔 | 只能付费（Choice / iFinD / Wind），免费源无解 |
| 服务器部署 | 注意 IP 归属，机房 IP 被东财风控概率高于家宽；`cnfin.py` 的熔断能避免拖垮任务 |

---

## 九、项目结构与精简使用

**如果只是使用数据能力，你只需要 `cnfin.py` 一个文件**，把它复制到目标项目即可；其他文件都不是运行依赖。

| 路径 | 作用 | 什么时候需要 |
|---|---|---|
| **`cnfin.py`** | 全部数据能力都在这里 | 总是需要 |
| **`README.md`** | 唯一入口文档：用法、数据口径、失败语义 | 第一次接入时看 |
| `pyproject.toml` + `requirements.txt` | 打包与依赖声明 | 想 `pip install` 或跑测试时 |
| `tests/` | 85 项离线测试 + 5 项真实网络测试 | 改代码时 |
| `.github/workflows/` | CI、每周真实源巡检、tag 发布 | 推到 GitHub 后 |
| `docs/` | 审计、测试策略、Tushare 迁移方案、交付摘要 | 需要追责/交接时 |
| `research/` | 本次数据源调研脚本与原始证据（42 项探测、健康快照） | 想复跑调研时 |
| `dist/` | 已构建好的 wheel / sdist | 直接安装或分发时 |
| `.venv/` | 本地测试环境（约 288MB，可再生） | 跑测试时；只使用单文件可删 |

精简后的根目录只剩 **6 个可见文件 + 4 个目录**（另有 `.gitignore`、`.github` 等工程配置）。所有缓存、覆盖率报告、JUnit 快照、构建中间目录和临时验收环境均已清理，需要时由命令重新生成。

复跑验证：

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest -m "not network"
.venv\Scripts\python -m pytest -m network      # 可选：真实网络
.venv\Scripts\python cnfin.py health
.venv\Scripts\python cnfin.py demo
.venv\Scripts\python research\probe_sources.py
```

---

## 十、未来数据源升级：Tushare（规划中）

**当前 v1.1.0 尚未实现 Tushare 适配器**，不读取 `TUSHARE_TOKEN`，也不要在生产代码中假设它已可用。

已确定的未来方向：

- Tushare 作为**日频行情、复权、财务、分红、交易日历**的付费主源；
- 腾讯继续作为实时行情和分钟线主源；
- 免费源只做抽样校验；生产 `strict` 模式不静默降级到口径不同的源；
- 成交量/成交额、复权因子、财务公告日（PIT）必须显式标准化；
- 先用 2000 积分档验证，再决定是否升级，不直接购买高价套餐；
- Token 只从环境变量或 Secret 读取，不进入代码与公开仓库。

完整的接入边界、数据契约、成本、故障语义、测试与分阶段验收标准见
[`docs/TUSHARE.md`](docs/TUSHARE.md)。
