# A-Share Volatility Index · VIX-A-SHERRY

[中文](#中文) · [English](#english)

## 中文

计算并解读 A 股 ETF 期权的波动率指数（类 VIX）。

### 背景

A 股没有官方 VIX——上交所「中国波指 iVIX」（代码 000188）已于 2018-02 停发。
本项目用**上交所 / 深交所官方期权数据**，按 CBOE 与上交所编制方案的**方差互换法**
自行复现，并附已实现波动率（RV）对比与期限结构分析。

### 覆盖品种

| 别名 | 标的 | 代码 | 交易所 |
|---|---|---|---|
| `50ETF` | 上证50ETF | 510050 | 上交所 |
| `300ETF` | 沪深300ETF | 510300 | 上交所 |
| `500ETF` | 中证500ETF | 510500 | 上交所 |
| `KCB50` | 科创50ETF | 588000 | 上交所 |
| `CYB` | 创业板ETF | 159915 | 深交所 |

### 快速使用

```bash
cd scripts

python3 vix.py calc --all        # 计算全部品种
python3 vix.py term 50ETF        # 期限结构
python3 vix.py chain 50ETF       # 期权链明细
python3 vix.py history           # 历史序列与分位
python3 build_html.py            # 生成手机端报告页面
```

**仅依赖 Python 标准库**（Python 3.9+），无需安装任何第三方包。

### 输出

`output/vix_report.html` —— 自包含单文件页面，移动端优先布局，零外部资源依赖，
弱网或离线也能正常渲染。

每张品种卡片包含：VIX 数值、情绪分档、标的收盘价、20 日已实现波动率、
波动率风险溢价（VRP）、期限斜率与期限结构迷你图。

### 数据源

| 用途 | 来源 | 特性 |
|---|---|---|
| 主源（上交所期权链） | `query.sse.com.cn` 官方接口 | 一次返回当日全部合约，含收盘价与精确到期日；免费不限流 |
| 深交所期权链 | 深交所官方报表接口 + 新浪财经行情 | 官方仅给合约元数据，价格取自新浪（字段已批量校验） |
| 已实现波动率 | 腾讯财经日线 | 免费稳定 |
| 长期 IV 参照 | 华创证券金工 HCVIX | 仅覆盖 50ETF / 300ETF，回溯至 2015 年 |

### 重要说明

- **这是非官方指数**。编制口径与上交所官方方案存在系统性差异（使用收盘价而非
  买卖价推算、未补虚拟行权价等），对外引用时必须一并说明。
- 数据源取舍、算法细节、验证记录、全部已知局限，以及**历史数据回补方案**，
  都写在 `references/methodology.md`。

### 免责声明

仅供研究与教育用途，不构成任何投资建议。

## English

Compute and interpret the A-share ETF option volatility index (VIX-style).

### Background

A-shares have no official VIX — the SSE "China VIX" (iVIX, code 000188) was
discontinued in February 2018. This project reconstructs it from **official SSE /
SZSE option data** using the **model-free variance swap** method from the CBOE and
SSE methodology, and adds realized volatility (RV) comparison plus term structure
analysis.

### Coverage

| Alias | Underlying | Code | Exchange |
|---|---|---|---|
| `50ETF` | SSE 50 ETF | 510050 | SSE |
| `300ETF` | CSI 300 ETF | 510300 | SSE |
| `500ETF` | CSI 500 ETF | 510500 | SSE |
| `KCB50` | STAR 50 ETF | 588000 | SSE |
| `CYB` | ChiNext ETF | 159915 | SZSE |

### Quick start

```bash
cd scripts

python3 vix.py calc --all        # compute all underlyings
python3 vix.py term 50ETF        # term structure
python3 vix.py chain 50ETF       # option chain detail
python3 vix.py history           # historical series and percentile
python3 build_html.py            # build the mobile report page
```

**Standard library only** (Python 3.9+). No third-party packages required.

### Output

`output/vix_report.html` — a self-contained single-file page with a mobile-first
layout and zero external resource dependencies; renders fine on slow or offline
connections.

Each underlying card shows: VIX level, sentiment band, underlying close, 20-day
realized volatility, variance risk premium (VRP), term slope, and a mini term
structure chart.

### Data sources

| Purpose | Source | Notes |
|---|---|---|
| Primary (SSE option chain) | `query.sse.com.cn` official API | Full contract list with close prices and exact expiries; free, no rate limit |
| SZSE option chain | SZSE official report API + Sina Finance | Official API provides contract metadata only; prices come from Sina (fields validated in bulk) |
| Realized volatility | Tencent Finance daily bars | Free and stable |
| Long-run IV reference | Huachuang Securities HCVIX | Covers 50ETF / 300ETF only, back to 2015 |

### Notes

- **This is not an official index.** The methodology differs systematically from the
  SSE's official scheme (close prices instead of bid/ask midpoints, no virtual
  strikes added, and so on). Any external citation must state this.
- Data source trade-offs, algorithm details, validation records, all known
  limitations, and the **historical backfill approach** are documented in
  `references/methodology.md`.

### Disclaimer

For research and educational purposes only. Not investment advice.
