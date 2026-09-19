#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股波动率指数（类 VIX）计算引擎　v1.0
================================================
主数据源：上海证券交易所官方期权披露接口（免费、权威、含收盘价与精确到期日、不限流）
辅数据源：腾讯财经日线（用于已实现波动率对比）

方法：CBOE / 上交所《上证50ETF波动率指数编制方案》方差互换法（model-free variance）

用法
----
    python3 vix.py list                     列出品种与到期月
    python3 vix.py calc 50ETF               计算 30 天 VIX（含 RV / VRP）
    python3 vix.py calc --all               计算全部品种
    python3 vix.py calc --all --save        计算并追加到历史序列
    python3 vix.py term 50ETF               期限结构
    python3 vix.py chain 50ETF              期权链明细（排查用）
    python3 vix.py history [50ETF]          查历史序列与分位
    python3 vix.py probe                    抓取日志分析，推断数据更新时点

口径说明（与官方编制的差异详见 references/methodology.md）
------------------------------------------------------------
1. 期权价格用上交所披露收盘价（官方方案用买卖价推算），存在已知偏差
2. 剔除剩余期限 <= 7 天的合约；近月 > 30 天时按编制方案直接取近月（注3）
3. 无风险利率默认 1.80%/年，--rate 可覆盖
4. 覆盖上交所 5 个 ETF 期权品种；深交所/中金所品种未纳入
"""

import argparse
import csv
import gzip
import json
import math
import os
import random
import re
import sys
import time
import urllib.request
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
HISTORY_CSV = os.path.join(DATA_DIR, "vix_history.csv")

# 年化基准：各年度 A 股实际的交易日数。
#
# 不能用美股惯例的 252 —— A 股交易日数不同，用错会系统性高估 RV（进而低估 VRP）。
# 也**不能跨年套用**：2025 年是 243 天、2026 年是 242 天，差 1 天就让 RV 差约 0.2%，
# 回补历史数据时用错年份会引入偏差。
#
# **每年年底补充下一年**（届时需向使用方确认，不要推测）。
TRADING_DAYS_BY_YEAR = {
    2025: 243,
    2026: 242,
}


def trading_days_in_year(d=None):
    """取某一天所在年度的 A 股交易日数。d 可为 date / ISO 字符串 / None（取当年）。"""
    if d is None:
        d = date.today()
    elif isinstance(d, str):
        d = _parse_date(d) or date.today()
    if isinstance(d, datetime):
        d = d.date()
    # 未收录的年份回退到表中最大值（当前 243）。仅影响历史重算，误差 < 0.5%；
    # 若要精确回补更早年份，请把该年数值补进 TRADING_DAYS_BY_YEAR。
    return TRADING_DAYS_BY_YEAR.get(d.year, max(TRADING_DAYS_BY_YEAR.values()))


FETCH_LOG = os.path.join(DATA_DIR, "fetch_log.csv")
HCVIX_CSV = os.path.join(DATA_DIR, "hcvix_history.csv")

# 华创证券金工公开的 HCVIX 历史序列（页面内嵌 JSON，2015-02-09 起）
# 用途：为自算序列提供长期历史参照（自算序列需自行逐日积累，初期样本不足）
# 引用请注明来源：华创证券金工 HCVIX
HCVIX_URL = "https://service.hcquant.com/production/hcvix_public.php"
HCVIX_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 Chrome/120 Safari/537.36"}
# 自算品种 -> 可对照的 HCVIX 序列名
HCVIX_MAP = {
    "50ETF":  "HCVIX50",
    "300ETF": "HCVIX300华泰柏瑞",      # 510300 是华泰柏瑞的沪深300ETF
}

SSE_URL = "http://query.sse.com.cn/commonQuery.do"
SSE_PARAMS = ("isPagination=false&expireDate=&securityId="
              "&sqlId=SSE_ZQPZ_YSP_GGQQZSXT_XXPL_DRHY_SEARCH_L")
SSE_HEADERS = {
    "Accept": "*/*", "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "http://www.sse.com.cn/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}
TX_HEADERS = {"User-Agent": SSE_HEADERS["User-Agent"], "Referer": "https://gu.qq.com/"}

# 深交所：官方接口只给合约元数据（行权价/到期日/认购认沽），不给价格
SZSE_URL = "http://www.szse.cn/api/report/ShowReport/data"
SZSE_HEADERS = {
    "Accept": "*/*",
    "Referer": "https://www.szse.cn/",
    "User-Agent": SSE_HEADERS["User-Agent"],
}
# 新浪：批量期权行情。字段 [2] = 收盘价
# （已用上交所官方收盘价批量验证：118 个合约全部精确吻合，零误差。改动前请重新验证）
SINA_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": SSE_HEADERS["User-Agent"],
}
SINA_PRICE_IDX = 2          # 期权行情里收盘价所在的字段下标
SINA_BATCH = 50             # 单次请求合约数上限（实测 50 稳定）

# 科创50 只保留华夏 588000：同日实测（2026-09-18）成交额 16.7 亿 vs 易方达 588080 的 1.73 亿，
# 相差 9.66 倍；且 588080 有 16 个合约全天零成交（零成交合约的价格是挂单价/结算价，会让 VIX 失真）。
# 如需恢复 588080：加回 {"code": "588080", "name": "科创50ETF易方达", "tx": "sh588080", "market": "SSE"} 即可。
UNDERLYINGS = {
    "50ETF":  {"code": "510050", "name": "上证50ETF",  "tx": "sh510050", "market": "SSE"},
    "300ETF": {"code": "510300", "name": "沪深300ETF", "tx": "sh510300", "market": "SSE"},
    "500ETF": {"code": "510500", "name": "中证500ETF", "tx": "sh510500", "market": "SSE"},
    "KCB50":  {"code": "588000", "name": "科创50ETF",  "tx": "sh588000", "market": "SSE"},
    "CYB":    {"code": "159915", "name": "创业板ETF",  "tx": "sz159915", "market": "SZSE"},
}
CODE2ALIAS = {v["code"]: k for k, v in UNDERLYINGS.items()}

CACHE_TTL = 600
_cache = {"ts": 0, "rows": None, "trade_date": None}


def _fetch(url, headers, retries=4, timeout=25, encoding="utf-8"):
    """带指数退避的 GET，返回文本。encoding 可选 utf-8 / gbk（新浪用 gbk）"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode(encoding, "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.2 * (2 ** i) + random.random())
    raise RuntimeError(f"请求失败：{url}\n原因：{last}")


def _parse_date(s):
    """把 '2026-09-17' / '20260917' 解析为 date"""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def fetch_sse_chain(use_cache=True):
    """拉取上交所当日全部 ETF 期权合约 -> (rows, trade_date)"""
    now = time.time()
    if use_cache and _cache["rows"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["rows"], _cache["trade_date"]

    txt = _fetch(f"{SSE_URL}?{SSE_PARAMS}", SSE_HEADERS)
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        raise SystemExit("上交所接口返回非 JSON（接口变更或临时维护）")
    raw = data.get("result") or []
    if not raw:
        raise SystemExit("上交所接口返回空结果（可能非交易日或接口变更）")

    rows, trade_date = [], None
    for r in raw:
        if r.get("DELISTFLAG") == "是":
            continue
        name = r.get("SECURITYNAMEBYID") or ""
        m = re.search(r"\((\d{6})\)", name)
        if not m:
            continue
        und, cp = m.group(1), r.get("CALL_OR_PUT")
        if cp not in ("认购", "认沽"):
            continue
        try:
            strike = float(r.get("EXERCISE_PRICE") or 0)
            close = float(r.get("SECURITY_CLOSEPX") or 0)
            settle = float(r.get("SETTL_PRICE") or 0)
            und_close = float(r.get("UNDERLYING_CLOSEPX") or 0)
            expiry = datetime.strptime(str(r.get("EXPIRE_DATE")), "%Y%m%d").date()
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        price = close if close > 0 else settle
        if price <= 0:
            continue
        if trade_date is None:
            trade_date = str(r.get("TIMESAVE") or "")
        rows.append({"und_code": und, "cp": "call" if cp == "认购" else "put",
                     "strike": strike, "price": price,
                     "price_src": "close" if close > 0 else "settle",
                     "expiry": expiry, "und_close": und_close})

    _cache.update({"ts": now, "rows": rows, "trade_date": trade_date})
    return rows, trade_date


# ---------- 深交所：元数据来自深交所，价格来自新浪 ----------
# 深交所合约列表里的「标的名称」→ 标的代码（接口不直接给代码）
SZSE_UND_MAP = {
    "创业板ETF易方达": "159915",
    "深证100ETF易方达": "159901",
    "中证500ETF嘉实": "159922",
    "沪深300ETF嘉实": "159919",
}

_szse_cache = {"ts": 0, "rows": None, "trade_date": None}


def _sina_quotes(codes, batch=SINA_BATCH):  # noqa: D401
    """批量查新浪行情，返回 {代码: [字段, ...]}"""
    out = {}
    for i in range(0, len(codes), batch):
        txt = _fetch(SINA_URL + ",".join(codes[i:i + batch]), SINA_HEADERS,
                     retries=3, timeout=20, encoding="gbk")
        for line in txt.strip().split("\n"):
            m = re.match(r'var hq_str_(\w+)="(.*)";?', line.strip())
            if m:
                out[m.group(1)] = m.group(2).split(",")
        time.sleep(0.3)
    return out


def fetch_szse_chain(use_cache=True):
    """
    深交所 ETF 期权链。返回格式与 fetch_sse_chain 一致。

    为什么分两个源：深交所官方接口（CATALOGID=ysplbrb）只提供合约元数据
    （代码/行权价/认购认沽/到期日），**不含价格**；价格取自新浪财经，
    字段 [2] 为收盘价（已用上交所官方收盘价批量验证 118/118 精确吻合）。
    """
    now = time.time()
    if use_cache and _szse_cache["rows"] and (now - _szse_cache["ts"]) < CACHE_TTL:
        return _szse_cache["rows"], _szse_cache["trade_date"]

    raw, page, trade_date = [], 1, None
    while page <= 40:                       # 全量约 464 个合约，每页 20 条
        url = (f"{SZSE_URL}?SHOWTYPE=JSON&CATALOGID=ysplbrb&TABKEY=tab1"
               f"&PAGENO={page}&random=0.5")
        try:
            d = json.loads(_fetch(url, SZSE_HEADERS, retries=3, timeout=20))
        except Exception:  # noqa: BLE001
            break
        block = (d[0] if isinstance(d, list) and d else {}) or {}
        rows_page = block.get("data") or []
        if not rows_page:
            break
        raw.extend(rows_page)
        if trade_date is None:
            sub = (block.get("metadata") or {}).get("subname")
            if sub:
                trade_date = str(sub).strip()
        if page >= ((block.get("metadata") or {}).get("pagecount") or 1):
            break
        page += 1
        time.sleep(0.25)
    if not raw:
        raise SystemExit("深交所接口返回空（可能非交易日或接口变更）")

    quotes = _sina_quotes([f"CON_OP_{r['hydm']}" for r in raw])

    ucodes = sorted({SZSE_UND_MAP.get(r.get("bdmc", ""), "") for r in raw} - {""})
    spot = {}
    if ucodes:
        q = _sina_quotes([f"sz{c}" for c in ucodes])
        for c in ucodes:
            f = q.get(f"sz{c}")
            if f and len(f) > 3:
                try:
                    spot[c] = float(f[3])       # 股票行情字段 [3] = 收盘价
                except ValueError:
                    pass

    rows = []
    for r in raw:
        f = quotes.get(f"CON_OP_{r.get('hydm', '')}")
        if not f or len(f) <= SINA_PRICE_IDX:
            continue
        try:
            price = float(f[SINA_PRICE_IDX])
            strike = float(r.get("xqj") or 0)
            expiry = datetime.strptime(str(r.get("xqrq")), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if price <= 0 or strike <= 0:
            continue
        ucode = SZSE_UND_MAP.get(r.get("bdmc", ""), "")
        rows.append({"und_code": ucode,
                     "cp": "call" if r.get("hylx") == "认购" else "put",
                     "strike": strike, "price": price, "price_src": "sina",
                     "expiry": expiry, "und_close": spot.get(ucode, 0.0)})

    _szse_cache.update({"ts": now, "rows": rows, "trade_date": trade_date})
    return rows, trade_date


def get_chain(market="SSE", use_cache=True):
    """按市场分发取数"""
    if market == "SZSE":
        return fetch_szse_chain(use_cache)
    return fetch_sse_chain(use_cache)


def build_books(alias, rows, today):
    code = UNDERLYINGS[alias]["code"]
    books = {}
    for r in rows:
        if r["und_code"] != code:
            continue
        T = (r["expiry"] - today).days / 365.0
        if T <= 0:
            continue
        b = books.setdefault(r["expiry"], {"T": T, "call": {}, "put": {}, "und_close": r["und_close"]})
        b[r["cp"]][r["strike"]] = r["price"]
    return books


def term_variance(book, r):
    """单期限方差 σ²（小数）。返回 (sigma2, F, K0, n_strikes) 或 None"""
    calls, puts, T = book["call"], book["put"], book["T"]
    common = sorted(set(calls) & set(puts))
    if len(common) < 3:
        return None
    k_star = min(common, key=lambda k: abs(calls[k] - puts[k]))
    fwd = k_star + math.exp(r * T) * (calls[k_star] - puts[k_star])
    below = [k for k in common if k <= fwd]
    if not below:
        return None
    k0 = max(below)

    qs = []
    for k in sorted(set(calls) | set(puts)):
        if k < k0:
            q = puts.get(k)
        elif k == k0:
            c, p = calls.get(k), puts.get(k)
            q = (c + p) / 2.0 if (c is not None and p is not None) else (c or p)
        else:
            q = calls.get(k)
        if q and q > 0:
            qs.append((k, q))
    if len(qs) < 3:
        return None

    total, n = 0.0, len(qs)
    for i, (k, q) in enumerate(qs):
        if i == 0:
            dk = qs[1][0] - qs[0][0]
        elif i == n - 1:
            dk = qs[-1][0] - qs[-2][0]
        else:
            dk = (qs[i + 1][0] - qs[i - 1][0]) / 2.0
        total += dk / (k * k) * math.exp(r * T) * q
    sigma2 = (2.0 / T) * total - (1.0 / T) * (fwd / k0 - 1.0) ** 2
    return (sigma2, fwd, k0, n) if sigma2 > 0 else None


def combine_30d(t1, s1, t2, s2, n_days=365.0):
    if abs(t2 - t1) < 1e-9:
        return s1
    den = n_days * (t2 - t1)
    if den == 0:
        return None
    var = (t1 * s1 * (n_days * t2 - 30.0) + t2 * s2 * (30.0 - n_days * t1)) / den * (n_days / 30.0)
    return var if var > 0 else None


def realized_vol(tx_code, window=20, end_date=None):
    """
    腾讯日线 -> 年化已实现波动率（对数收益率标准差 × √当年实际交易天数）

    end_date：只取该日期（含）之前的行情。必须传入 VIX 的数据日期，
    因为两个数据源更新速度不同（腾讯及时、上交所滞后），
    若不锚定同一基准日，VIX 与 RV 会来自不同交易日，VRP 失去意义。
    """
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tx_code},day,,,{window + 10},qfq"
    try:
        d = json.loads(_fetch(url, TX_HEADERS, retries=2, timeout=15))
        node = d["data"][tx_code]
        kl = node.get("qfqday") or node.get("day") or []
        if end_date:
            kl = [x for x in kl if len(x) > 2 and str(x[0]) <= str(end_date)]
        closes = [float(x[2]) for x in kl if len(x) > 2]
        if len(closes) < window + 1:
            return None
        closes = closes[-(window + 1):]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        mu = sum(rets) / len(rets)
        var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
        return math.sqrt(var) * math.sqrt(trading_days_in_year(end_date)) * 100
    except Exception:  # noqa: BLE001
        return None


_last_trade_cache = {"ts": 0.0, "date": None}


def market_last_trade_date(tx_code="sh510050", use_cache=True):
    """
    取「市场最后一个有交易的日期」——用腾讯行情的时间戳字段。
    腾讯返回格式：v_sh510050="1~名称~代码~最新价~...~20260918161435~..."
    第 31 个字段（index 30）是该时刻时间戳。结果缓存 10 分钟，避免多品种重复请求。
    """
    now = time.time()
    if use_cache and _last_trade_cache["date"] and (now - _last_trade_cache["ts"]) < 600:
        return _last_trade_cache["date"]
    result = None
    try:
        req = urllib.request.Request(f"https://qt.gtimg.cn/q={tx_code}", headers=TX_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            txt = r.read().decode("gbk", "ignore")
        for seg in txt.split('"'):
            parts = seg.split("~")
            if len(parts) > 30 and parts[30][:8].isdigit():
                result = datetime.strptime(parts[30][:8], "%Y%m%d").date()
                break
    except Exception:  # noqa: BLE001
        pass
    _last_trade_cache.update({"ts": now, "date": result})
    return result


def staleness_note(data_date):
    """
    数据日期落后于市场最后交易日时，返回解释文案；否则返回 None。

    只在「确实滞后」时解释：若数据已是最后交易日（含非交易日复用上一交易日数据），
    属正常情况，不解释。
    """
    d = _parse_date(data_date)
    if not d:
        return None
    last = market_last_trade_date()
    if not last or d >= last:
        return None
    return (f"数据来自 {d}，而市场最后交易日已是 {last}。"
            f"原因是上交所日终数据发布有延迟，收盘后通常需数小时才更新，"
            f"在此期间接口仍返回上一交易日的快照。次日自动重跑时会补齐。")


def fetch_hcvix(force=False, max_age_hours=12):
    """
    抓取华创证券金工公开的 HCVIX 历史序列，缓存到 data/hcvix_history.csv。
    返回 {序列名: [(date, value), ...]}

    数据来自来源页面内嵌的完整 JSON（2015-02-09 期权上市首日至今，单品种约 2800 个交易日）。
    用途：本工具自算的 VIX 序列需要逐日积累，初期样本不足；HCVIX 提供长期历史参照。
    **引用请注明来源：华创证券金工 HCVIX。**
    """
    if not force and os.path.exists(HCVIX_CSV):
        if (time.time() - os.path.getmtime(HCVIX_CSV)) < max_age_hours * 3600:
            return _load_hcvix()

    txt = _fetch(HCVIX_URL, HCVIX_HEADERS, retries=3, timeout=60)
    i = txt.find('[{"id":"vix')
    if i < 0:
        raise SystemExit("华创页面结构已变化，未找到内嵌数据（接口维护中？）")
    depth, blob = 0, None
    for j in range(i, len(txt)):
        if txt[j] == "[":
            if depth == 0:
                start = j
            depth += 1
        elif txt[j] == "]":
            depth -= 1
            if depth == 0:
                blob = txt[start:j + 1]
                break
    if not blob:
        raise SystemExit("未能匹配到完整 JSON 数组")
    groups = json.loads(blob)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HCVIX_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["series", "date", "value"])
        for g in groups:
            for s in g.get("series", []):
                for ts, v in (s.get("data") or []):
                    if v is None:
                        continue
                    d = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                    w.writerow([s.get("name", ""), d, v])
    return _load_hcvix()


def _load_hcvix():
    if not os.path.exists(HCVIX_CSV):
        return {}
    out = {}
    with open(HCVIX_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["series"], []).append((r["date"], float(r["value"])))
    for k in out:
        out[k].sort()
    return out


def show_percentile(alias=None):
    """当前值在长期历史中的分位（数据源：华创证券金工 HCVIX）"""
    try:
        hv = fetch_hcvix()
    except SystemExit as e:
        print(f"无法获取历史序列：{e}", file=sys.stderr)
        return
    if not hv:
        print("历史序列为空。")
        return

    targets = [alias] if alias in UNDERLYINGS else list(UNDERLYINGS)
    print(f"{'品种':<9}{'序列':<20}{'样本':>7}{'当前':>8}{'分位':>8}{'中位':>8}{'最小值':>8}{'最大值':>8}")
    for a in targets:
        ser = HCVIX_MAP.get(a)
        if not ser or ser not in hv:
            print(f"{a:<9}{'（无对应序列）':<20}{'—':>7}{'—':>8}{'—':>8}{'—':>8}{'—':>8}")
            continue
        # 过滤源数据里的缺失值（HCVIX 用 0 或极小值表示无数据，会严重污染分位统计）
        vals = [v for _, v in hv[ser] if v is not None and v > 0.5]
        if len(vals) < 20:
            print(f"{a:<9}{ser:<20}{len(vals):>7}{'有效样本不足':>8}")
            continue
        cur = vals[-1]
        rank = sum(1 for v in vals if v <= cur) / len(vals) * 100
        med = sorted(vals)[len(vals) // 2]
        print(f"{a:<9}{ser:<20}{len(vals):>7}{cur:>8.2f}{rank:>7.1f}%{med:>8.2f}"
              f"{min(vals):>8.2f}{max(vals):>8.2f}")
    print("\n注：分位基于华创证券金工 HCVIX 历史序列（2015-02-09 起），非本工具自算序列。")
    print("    自算序列仍在逐日积累（data/vix_history.csv）。引用请注明来源。")
    print("    仅 50ETF 与 300ETF 有对应序列；创业板、中证500、科创50 暂无长期参照。")

    # ---- 波动水平分位（RV，覆盖全部品种，作为过渡期参照）----
    print("\n【波动水平历史分位】20日已实现波动率 · 腾讯日线（近 3 年）")
    print(f"{'品种':<9}{'标的':<12}{'当前RV':>9}{'分位':>8}{'中位':>8}{'样本':>7}  起始")
    for a in targets:
        got = rv_history(UNDERLYINGS[a]["tx"])
        if not got:
            print(f"{a:<9}{UNDERLYINGS[a]['name']:<12}{'取数失败':>9}")
            continue
        rvs, rd = got
        vals = [v for v in rvs if v > 0]
        if len(vals) < 60:
            print(f"{a:<9}{UNDERLYINGS[a]['name']:<12}{'样本不足':>9}")
            continue
        cur = vals[-1]
        rank = sum(1 for v in vals if v <= cur) / len(vals) * 100
        med = sorted(vals)[len(vals) // 2]
        start = rd[len(rd) - len(rvs)] if rd else ""
        print(f"{a:<9}{UNDERLYINGS[a]['name']:<12}{cur:>9.2f}{rank:>7.1f}%{med:>8.2f}"
              f"{len(vals):>7}  {start}")
    print("\n⚠️ RV 是标的「已实现波动率」（回头看过去实际波动了多少），")
    print("   不是期权「隐含波动率」（向前看市场预期要波动多少）。")
    print("   它能说明当前波动水平在历史上算高还是低，**不能称作「VIX 分位」**。")


SNAP_DIR = os.path.join(DATA_DIR, "snapshots")


def save_snapshots(rows_by_market, data_date):
    """
    保存当日原始期权链快照到 data/snapshots/YYYY-MM-DD.csv。

    **为什么必须做**：VIX 数值一旦算出就固定了，但口径可能变（利率、行权价处理、算法修正）。
    没有原始链就无法重算。而历史**无法回补**——实测东财历史K线接口每 3–4 个请求就封 IP、
    恢复需数十分钟，回补 250 个合约不可行；新浪无期权历史接口、腾讯不支持期权代码、
    上交所只给当日、optbbs 不可达。**错过即永久丢失。**
    """
    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, f"{data_date}.csv")
    if os.path.exists(path):
        return path, 0
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["market", "und_code", "cp", "strike", "price", "price_src",
                    "expiry", "und_close"])
        for mkt, rows in rows_by_market.items():
            for r in rows:
                w.writerow([mkt, r["und_code"], r["cp"], r["strike"], r["price"],
                            r.get("price_src", ""), r["expiry"], r["und_close"]])
                n += 1
    return path, n


def check_gaps(tx_ref="sh510050", count=400):
    """
    检测自算 VIX 序列是否漏了交易日。

    为什么必须检测：上交所只提供当日快照，**漏掉的交易日永久无法回补**
    （东财历史K线每 3–4 个请求就封 IP，其他源均无期权历史）。
    因此「知道漏了哪几天」比「静默缺数据」重要得多。

    交易日列表取自腾讯标的日线。返回 (缺失日期列表, 已有日期列表)
    """
    if not os.path.exists(HISTORY_CSV):
        return [], []
    rows = list(csv.DictReader(open(HISTORY_CSV, encoding="utf-8")))
    have = sorted({r["date"] for r in rows})
    if not have:
        return [], []
    try:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={tx_ref},day,,,{count},qfq")
        d = json.loads(_fetch(url, TX_HEADERS, retries=2, timeout=25))
        node = d["data"][tx_ref]
        kl = node.get("qfqday") or node.get("day") or []
        trading = [x[0] for x in kl if len(x) > 0]
    except Exception:  # noqa: BLE001
        return [], have
    span = [d for d in trading if have[0] <= d <= have[-1]]
    missing = [d for d in span if d not in have]
    return missing, have


def log_fetch(data_date, min_gap_sec=300):
    """
    记录每次抓取：运行时刻 + 数据日期 + 市场最后交易日。
    用于实证推断「上交所日终数据几点更新」——上交所未公开该时点，
    只能靠多次观测积累。同一分钟内重复运行只记一条。
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        now = datetime.now()
        if os.path.exists(FETCH_LOG):
            try:
                with open(FETCH_LOG, encoding="utf-8") as f:
                    last = list(csv.DictReader(f))
                if last:
                    prev = datetime.strptime(last[-1]["fetched_at"], "%Y-%m-%d %H:%M:%S")
                    if (now - prev).total_seconds() < min_gap_sec:
                        return
            except Exception:  # noqa: BLE001
                pass
        last_trade = market_last_trade_date()
        d, lt = _parse_date(data_date), last_trade
        stale = 1 if (d and lt and d < lt) else 0
        exists = os.path.exists(FETCH_LOG)
        with open(FETCH_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["fetched_at", "data_date", "market_last_trade", "stale"])
            w.writerow([now.strftime("%Y-%m-%d %H:%M:%S"), data_date,
                        lt.isoformat() if lt else "", stale])
    except Exception:  # noqa: BLE001
        pass          # 日志失败绝不能影响主流程


def show_probe():
    """汇总抓取日志，推断数据更新时点"""
    if not os.path.exists(FETCH_LOG):
        print("尚无抓取日志。每次运行本工具都会自动记录一条，积累后可再用本命令分析。")
        return
    rows = sorted(csv.DictReader(open(FETCH_LOG, encoding="utf-8")),
                  key=lambda r: r.get("fetched_at") or "")
    if not rows:
        print("日志为空。")
        return

    by_date = {}
    for r in rows:
        by_date.setdefault(r["data_date"], []).append(r)

    print(f"抓取日志：{len(rows)} 条记录，覆盖 {len(by_date)} 个数据日期")
    print(f"时间范围：{rows[0]['fetched_at']} ~ {rows[-1]['fetched_at']}\n")
    print(f"{'数据日期':<13}{'首次观测到该数据的时刻':<24}{'观测次数':>8}")
    for d in sorted(by_date):
        rs = sorted(by_date[d], key=lambda x: x["fetched_at"])
        first = rs[0]["fetched_at"]
        dd = _parse_date(d)
        lag = ""
        if dd:
            fd = _parse_date(first[:10])
            if fd and fd > dd:
                lag = f"（晚 {(fd - dd).days} 天）"
        print(f"{d:<13}{first + lag:<24}{len(rs):>8}")

    stale_rows = [r for r in rows if r.get("stale") == "1"]
    print(f"\n滞后抓取 {len(stale_rows)} 次"
          f"（拿到的是更早交易日的数据）")
    if stale_rows:
        times = sorted(r["fetched_at"][11:16] for r in stale_rows)
        print(f"  发生时刻：{', '.join(times)}")
    if len(by_date) < 5:
        print("\n※ 样本不足 5 个数据日期，暂不能推断更新窗口。继续积累。")
    else:
        print("\n※ 上表「首次观测时刻」即更新时点的上界——"
              "数据在该时刻之前已发布。多积累后可据此确定安全运行时间。")


def rv_history(tx_code, window=20, count=800):
    """
    标的的滚动已实现波动率历史序列（用于波动水平的长期分位参照）。
    腾讯单次约可返回 800 条（≈3.2 年）；超过约 2000 会返回空。

    ⚠️ 这是 RV（回头看，标的实际波动），不是 IV（向前看，期权隐含波动）。
    两者口径不同，**不可把它当作「VIX 历史分位」使用**。
    返回 (rvs, dates) 或 None
    """
    try:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={tx_code},day,,,{count},qfq")
        d = json.loads(_fetch(url, TX_HEADERS, retries=2, timeout=25))
        node = d["data"][tx_code]
        kl = node.get("qfqday") or node.get("day") or []
    except Exception:  # noqa: BLE001
        return None
    closes = [float(x[2]) for x in kl if len(x) > 2]
    dates = [x[0] for x in kl if len(x) > 2]
    if len(closes) < window + 30:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    rvs, rd = [], []
    for i in range(window, len(rets) + 1):
        seg = rets[i - window:i]
        mu = sum(seg) / window
        var = sum((x - mu) ** 2 for x in seg) / (window - 1)
        rvs.append(math.sqrt(var) * math.sqrt(trading_days_in_year(dates[i])) * 100)
        rd.append(dates[i])
    return rvs, rd


def rv_percentile(tx_code):
    """当前 RV 在自身历史中的分位。返回 (当前值, 分位, 样本数, 起始日期) 或 None"""
    got = rv_history(tx_code)
    if not got:
        return None
    rvs, rd = got
    vals = [v for v in rvs if v > 0]
    if len(vals) < 60:
        return None
    cur = vals[-1]
    rank = sum(1 for v in vals if v <= cur) / len(vals) * 100
    return cur, rank, len(vals), rd[-len(vals)] if rd else ""


def compute(alias, rate=0.018, min_days=7, with_rv=True):
    if alias not in UNDERLYINGS:
        raise SystemExit(f"未知品种 {alias}；可用：{', '.join(UNDERLYINGS)}")
    rows, trade_date = get_chain(UNDERLYINGS[alias].get("market", "SSE"))
    # 基准日必须用「数据自身的日期」，不能用系统日期：
    # 上交所日终数据存在滞后（收盘后一段时间内仍返回上一交易日快照），
    # 若用系统日期计算剩余期限，会对同一份数据算出不同结果，且破坏去重。
    today = _parse_date(trade_date) or date.today()
    log_fetch(trade_date)          # 记录观测时刻，用于推断数据更新时点（同 5 分钟内去重）
    books = build_books(alias, rows, today)
    if not books:
        raise SystemExit(f"{alias} 未取到期权数据")

    terms = []
    for exp, b in sorted(books.items()):
        days = (exp - today).days
        if days < min_days:
            continue
        res = term_variance(b, rate)
        if not res:
            continue
        s2, fwd, k0, nk = res
        terms.append({"expiry": exp, "T": b["T"], "days": days, "sigma2": s2,
                      "iv": 100 * math.sqrt(s2), "fwd": fwd, "k0": k0, "n_strikes": nk})
    if not terms:
        raise SystemExit(f"{alias} 无有效期限（剩余天数不足或行权价覆盖不足）")

    t30 = 30.0 / 365.0
    near = max([t for t in terms if t["T"] <= t30], key=lambda t: t["T"], default=None)
    far = min([t for t in terms if t["T"] >= t30], key=lambda t: t["T"], default=None)

    if near is not None and far is not None and near is not far:
        var = combine_30d(near["T"], near["sigma2"], far["T"], far["sigma2"])
        if var is None:
            raise SystemExit("30 天方差合成失败")
        vix = 100 * math.sqrt(var)
        method = f"{near['expiry']}({near['days']}天) + {far['expiry']}({far['days']}天) 加权插值"
    else:
        # 编制方案注3：近月剩余 > 30 天时，直接取近月波动率
        pick = terms[0]
        vix = 100 * math.sqrt(pick["sigma2"])
        method = f"单期限 {pick['expiry']}({pick['days']}天) — 近月>30天，按编制方案直取"

    rv = realized_vol(UNDERLYINGS[alias]["tx"], end_date=trade_date) if with_rv else None
    return {
        "underlying": alias, "code": UNDERLYINGS[alias]["code"], "name": UNDERLYINGS[alias]["name"],
        "data_date": trade_date or today.isoformat(), "vix": vix, "method": method,
        "terms": terms, "rv20": rv,
        "vrp": (vix - rv) if rv else None,
        "spot": next(iter(books.values()))["und_close"], "rate": rate,
    }


def sentiment(vix):
    """
    按 A 股自身历史校准的情绪分档。

    依据：官方中国波指 iVIX 停发前（2016-08 ~ 2018-02）的统计——
    均值 15.19，全区间 11.15~21.41，**80% 的时间落在 13.07~17.61**。
    因此「13–18」才是 A 股常态，不能照搬美股「12–16 平静、16–20 中性」的习惯
    （那会把 A 股最常见的区间误标成「平静」）。

    2015 年股灾实测参照：4 月顶 47、6 月顶 49、7/8 暴跌后 60、8/26 最低点 65+。

    详见 references/methodology.md 第九节。改动阈值前请先读那一节。
    """
    if vix < 13:
        return "极度平静"
    if vix < 18:
        return "常态"
    if vix < 21:
        return "偏谨慎"
    if vix < 30:
        return "紧张"
    if vix < 40:
        return "恐慌"
    return "极度恐慌"


def fmt_report(res):
    L = [f"标的：{res['name']}({res['code']})　　VIX：{res['vix']:.2f}　【{sentiment(res['vix'])}】",
         f"数据日期：{res['data_date']}　标的收盘：{res['spot']}　无风险利率：{res['rate']:.2%}"]
    note = staleness_note(res["data_date"])
    if note:
        L.append(f"【数据尚未更新】{note}")
    L.append(f"合成方式：{res['method']}")
    if res.get("rv20") is not None:
        L.append(f"20日已实现波动率：{res['rv20']:.2f}　→　波动率风险溢价 VRP：{res['vrp']:+.2f}"
                 f"（{'期权偏贵' if res['vrp'] > 0 else '期权偏便宜'}）")
    L += ["", "期限结构：",
          f"  {'到期日':<12}{'剩余天':>7}{'远期价':>10}{'K0':>9}{'行权价数':>9}{'年化IV':>9}"]
    for t in res["terms"]:
        L.append(f"  {t['expiry'].isoformat():<12}{t['days']:>7}{t['fwd']:>10.4f}"
                 f"{t['k0']:>9.2f}{t['n_strikes']:>9}{t['iv']:>9.2f}")
    if len(res["terms"]) >= 2:
        slope = res["terms"][-1]["iv"] - res["terms"][0]["iv"]
        L.append(f"\n期限斜率（远月-近月）：{slope:+.2f}　→　"
                 f"{'近月更贵，短期风险定价高' if slope < 0 else '远月更贵，不确定性偏中长期'}")
    return "\n".join(L)


def save_history(results):
    """
    追加到历史序列，按 (数据日期, 品种) 去重。
    同一交易日重复运行（或非交易日运行拿到上一交易日快照）不会产生重复记录。
    返回 (路径, 新增条数, 总条数)
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    exists = os.path.exists(HISTORY_CSV)
    existing = set()
    if exists:
        with open(HISTORY_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add((row.get("date"), row.get("underlying")))

    new_rows = [r for r in results if (r["data_date"], r["underlying"]) not in existing]
    if not new_rows:
        return HISTORY_CSV, 0, len(results)

    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["date", "underlying", "code", "vix", "rv20", "vrp", "spot", "rate"])
        for r in new_rows:
            w.writerow([r["data_date"], r["underlying"], r["code"],
                        f"{r['vix']:.4f}",
                        f"{r['rv20']:.4f}" if r.get("rv20") is not None else "",
                        f"{r['vrp']:.4f}" if r.get("vrp") is not None else "",
                        r["spot"], r["rate"]])
    return HISTORY_CSV, len(new_rows), len(results)


def show_history(alias=None):
    if not os.path.exists(HISTORY_CSV):
        print("尚无历史数据。先跑 `calc --all --save` 积累序列。")
        return
    rows = list(csv.DictReader(open(HISTORY_CSV, encoding="utf-8")))
    if alias:
        rows = [r for r in rows if r["underlying"] == alias]
    if not rows:
        print(f"历史中无 {alias} 记录。")
        return
    by_alias = {}
    for r in rows:
        if r["underlying"] not in UNDERLYINGS:
            continue          # 已下线的品种（如 588080）不再展示，历史记录保留在 csv 里
        by_alias.setdefault(r["underlying"], []).append(r)
    if not by_alias:
        print("历史中无当前跟踪品种的记录。")
        return
    print(f"{'品种':<9}{'样本数':>7}{'起始':<13}{'最新':<13}{'最新VIX':>9}{'区间最低':>9}{'最高':>9}{'当前分位':>9}")
    for a, rs in by_alias.items():
        vals = [float(x["vix"]) for x in rs]
        cur = vals[-1]
        rank = sum(1 for v in vals if v <= cur) / len(vals) * 100
        flag = "（样本<20，分位参考意义弱）" if len(vals) < 20 else ""
        print(f"{a:<9}{len(vals):>7}{rs[0]['date']:<13}{rs[-1]['date']:<13}"
              f"{cur:>9.2f}{min(vals):>9.2f}{max(vals):>9.2f}{rank:>8.1f}% {flag}")

    # 缺口检测：漏掉的交易日永久无法回补，必须让用户知道
    missing, have = check_gaps()
    if missing:
        print(f"\n⚠️ 缺失 {len(missing)} 个交易日：{', '.join(missing[:12])}"
              f"{' …' if len(missing) > 12 else ''}")
        print("   上交所只提供当日快照，缺失日期**无法回补**。")
        print("   50ETF / 300ETF 可改用 `percentile` 的华创 HCVIX 序列做近似参照（口径略有差异）。")
    elif have:
        print(f"\n✓ 交易日连续，覆盖 {have[0]} ~ {have[-1]}，无缺口。")


def main():
    ap = argparse.ArgumentParser(description="A股波动率指数（类VIX）")
    ap.add_argument("cmd", choices=["list", "calc", "term", "chain", "history", "probe",
                                    "percentile"])
    ap.add_argument("target", nargs="?", help="品种代码，如 50ETF")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--rate", type=float, default=0.018)
    ap.add_argument("--min-days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true", help="追加到历史序列")
    ap.add_argument("--no-rv", action="store_true", help="跳过已实现波动率计算（更快）")
    args = ap.parse_args()

    if args.cmd == "history":
        show_history(args.target)
        return
    if args.cmd == "probe":
        show_probe()
        return
    if args.cmd == "percentile":
        show_percentile(args.target)
        return

    if args.cmd == "list":
        chains = {}

        def _chain_for(mkt):
            if mkt not in chains:
                try:
                    chains[mkt] = get_chain(mkt)
                except SystemExit as e:
                    print(f"[{mkt} 取数失败] {e}", file=sys.stderr)
                    chains[mkt] = ([], None)
            return chains[mkt]

        print(f"{'品种':<9}{'标的':<13}{'代码':<8}{'市场':<6}{'到期月(剩余天)':<38}{'合约数':>7}")
        for alias, meta in UNDERLYINGS.items():
            rws, td = _chain_for(meta.get("market", "SSE"))
            tdy = _parse_date(td) or date.today()
            books = build_books(alias, rws, tdy)
            if not books:
                print(f"{alias:<9}{meta['name']:<13}{meta['code']:<8}"
                      f"{meta.get('market', 'SSE'):<6}{'无数据':<38}{0:>7}")
                continue
            n = sum(len(b['call']) + len(b['put']) for b in books.values())
            mons = " ".join(f"{e.strftime('%y-%m')}({(e-tdy).days}d)" for e in sorted(books))
            print(f"{alias:<9}{meta['name']:<13}{meta['code']:<8}"
                  f"{meta.get('market', 'SSE'):<6}{mons:<38}{n:>7}")
        return

    targets = list(UNDERLYINGS) if (args.all or args.target == "--all") else [args.target]
    if not targets or targets == [None]:
        raise SystemExit("请指定品种，或使用 --all")

    results = []
    if args.cmd == "calc" and not args.json:
        print("VIX-A-SHERRY ｜ A股波动率指数\n")
    for t in targets:
        try:
            res = compute(t, rate=args.rate, min_days=args.min_days, with_rv=not args.no_rv)
        except SystemExit as e:
            print(f"[跳过] {t}: {e}", file=sys.stderr)
            continue
        results.append(res)
        if args.json:
            continue
        if args.cmd == "calc":
            print(fmt_report(res)); print()
        elif args.cmd == "term":
            print(f"\n{res['name']}　VIX={res['vix']:.2f}　({res['data_date']})")
            for tt in res["terms"]:
                print(f"  {tt['expiry']}  剩余{tt['days']:>3}天  年化IV={tt['iv']:.2f}  远期={tt['fwd']:.4f}")
        elif args.cmd == "chain":
            rws, td = get_chain(UNDERLYINGS[t].get("market", "SSE"))
            tdy = _parse_date(td) or date.today()
            books = build_books(t, rws, tdy)
            for exp, b in sorted(books.items()):
                ks = sorted(set(b["call"]) | set(b["put"]))
                print(f"\n{UNDERLYINGS[t]['name']}　到期 {exp}（剩余 {(exp-tdy).days} 天）　标的收盘 {b['und_close']}")
                print(f"  {'行权价':>10}{'认购':>12}{'认沽':>12}")
                for k in ks:
                    c, p = b["call"].get(k), b["put"].get(k)
                    print(f"  {k:>10.4f}{(f'{c:.4f}' if c else '—'):>12}{(f'{p:.4f}' if p else '—'):>12}")

    if args.save and results:
        path, added, total = save_history(results)
        if added:
            print(f"[已追加 {added}/{total} 条] {path}", file=sys.stderr)
        else:
            print(f"[跳过保存] {total} 条记录均已存在（同一数据日期重复运行或非交易日）", file=sys.stderr)
        # 同时存档当日原始期权链（供未来口径变更时重算；历史不可回补）
        snap = {}
        for mkt in ("SSE", "SZSE"):
            try:
                rws, _ = get_chain(mkt)
                if rws:
                    snap[mkt] = rws
            except SystemExit:
                continue
        if snap:
            p, n = save_snapshots(snap, results[0]["data_date"])
            if n:
                print(f"[快照已存 {n} 条] {p}", file=sys.stderr)
            else:
                print(f"[快照已存在，跳过] {p}", file=sys.stderr)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
