#!/usr/bin/env python3
"""
回补历史期权日线，用于重算历史 VIX。

背景
----
上交所 / 深交所官方接口只给「当日」快照，不提供历史；华创 HCVIX 等第三方
序列只覆盖 50ETF 与 300ETF。中证500 / 科创50 / 创业板的 IV 历史因此缺失。

东方财富是唯一提供 A 股 ETF 期权「历史日线」的免费源，但限流严苛：
实测约 50% 请求被服务端直接断连（HTTP 000 / RemoteDisconnected），
短时间密集请求后还会进入数分钟的「完全封禁」状态。

策略
----
1. 短间隔（1.5s）+ 单合约多轮重试 —— 被断连的多半重试一次就成功
2. 连续失败达阈值 → 判定进入封禁窗口，休眠后继续
3. 每个合约落盘一个 json，已存在即跳过（断点续传，可反复中断重跑）

用法
----
    python3 backfill.py                # 回补全部目标合约
    python3 backfill.py --limit 20     # 只跑前 20 个（验证用）
    python3 backfill.py --status       # 只看进度

输出
----
    ../data/history_raw/sse_{合约代码}.json
    ../data/history_raw/szse_{合约代码}.json
"""
import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.abspath(os.path.join(BASE, "..", "data", "history_raw"))
os.makedirs(RAW_DIR, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 Chrome/120 Safari/537.36")

# 东财 kline 镜像域名（数字前缀负载均衡，实测与主域名共享风控）
EM_HOSTS = ["push2his.eastmoney.com", "1.push2his.eastmoney.com",
            "2.push2his.eastmoney.com", "3.push2his.eastmoney.com",
            "7.push2his.eastmoney.com", "35.push2his.eastmoney.com",
            "62.push2his.eastmoney.com", "82.push2his.eastmoney.com"]

# 行权价筛选范围（相对标的收盘价）。
# VIX 计算中行权价 K 的权重 ∝ 1/K²，深度虚值合约贡献极小；
# 限幅后可显著减少请求数，代价是极端行情下轻微低估尾部风险。
STRIKE_BAND = 0.12

# 目标品种与到期月（回补窗口受「当前挂牌合约」限制：更早的合约已摘牌，查不到）
TARGET_UND = {"510500": "500ETF", "588000": "KCB50", "159915": "CYB"}
TARGET_EXPIRIES = {"20260923", "20261028", "20261223"}
BEG, END = "20260810", "20260918"


# ---------------------------------------------------------------- 通用抓取
def curl(url, headers=None, timeout=15):
    """用 curl 子进程请求。实测 curl 的 TLS 特征比 urllib 更容易通过东财风控。"""
    cmd = ["curl", "-s", "-m", str(timeout), "-A", UA]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return r.stdout
    except Exception:
        return ""


def em_kline(secid, retries=3, backoff=(0.0, 0.6, 1.6)):
    """
    东财历史日线。返回 [(日期, 收盘)]；无数据返回 []；彻底失败返回 None。

    镜像轮换：东财有 1./2./3.…/99. 的数字前缀镜像域名。实测主域名与镜像
    共享同一套风控，但轮换仍能小幅提高单次成功率，故保留。
    """
    hdr = {"Referer": "https://quote.eastmoney.com/"}
    for i in range(retries):
        host = EM_HOSTS[(i + int(time.time())) % len(EM_HOSTS)]
        url = (f"https://{host}/api/qt/stock/kline/get"
               f"?secid={secid}&fields1=f1,f2&fields2=f51,f53"
               f"&klt=101&fqt=0&beg={BEG}&end={END}")
        txt = curl(url, hdr)
        if txt:
            try:
                d = json.loads(txt)
                dd = d.get("data")
                if dd and dd.get("klines"):
                    out = []
                    for line in dd["klines"]:
                        p = line.split(",")
                        if len(p) >= 2:
                            try:
                                out.append((p[0], float(p[1])))
                            except ValueError:
                                pass
                    return out
                if dd is not None:          # 有 data 但无 klines = 该合约无成交记录
                    return []
            except json.JSONDecodeError:
                pass
        time.sleep(backoff[min(i, len(backoff) - 1)] + random.uniform(0, 0.3))
    return None


# ---------------------------------------------------------------- 合约清单
def sse_contracts():
    """上交所 ETF 期权合约清单（官方接口，不限流）"""
    url = ("http://query.sse.com.cn/commonQuery.do?isPagination=false&expireDate=&securityId="
           "&sqlId=SSE_ZQPZ_YSP_GGQQZSXT_XXPL_DRHY_SEARCH_L")
    txt = curl(url, {"Referer": "http://www.sse.com.cn/"}, timeout=30)
    raw = json.loads(txt).get("result") or []
    out = []
    for r in raw:
        if r.get("DELISTFLAG") == "是":
            continue
        name = r.get("SECURITYNAMEBYID") or ""
        m = re.search(r"\((\d{6})\)", name)
        if not m:
            continue
        und, cp = m.group(1), r.get("CALL_OR_PUT")
        if und not in TARGET_UND or cp not in ("认购", "认沽"):
            continue
        exp = str(r.get("EXPIRE_DATE") or "")
        if exp not in TARGET_EXPIRIES:
            continue
        strike = float(r.get("EXERCISE_PRICE") or 0)
        spot = float(r.get("UNDERLYING_CLOSEPX") or 0)
        if strike <= 0:
            continue
        # 行权价限幅：跳过深虚/深实合约
        if spot > 0 and abs(strike / spot - 1.0) > STRIKE_BAND:
            continue
        out.append({
            "market": "sse", "secid": f"10.{r['SECURITY_ID']}",
            "code": str(r["SECURITY_ID"]), "und_code": und,
            "und": TARGET_UND[und], "cp": "call" if cp == "认购" else "put",
            "strike": strike,
            "expiry": exp, "spot": spot,
        })
    return out


def szse_contracts():
    """深交所创业板 ETF 期权合约清单（官方报表接口，免费不限流）"""
    raw, page = [], 1
    while page <= 40:
        url = ("http://www.szse.cn/api/report/ShowReport/data"
               f"?SHOWTYPE=JSON&CATALOGID=ysplbrb&TABKEY=tab1&PAGENO={page}&random=0.5")
        txt = curl(url, timeout=25)
        try:
            d = json.loads(txt)
        except json.JSONDecodeError:
            break
        blk = (d[0] if isinstance(d, list) and d else {}) or {}
        rows = blk.get("data") or []
        if not rows:
            break
        raw.extend(rows)
        if page >= ((blk.get("metadata") or {}).get("pagecount") or 1):
            break
        page += 1
        time.sleep(0.25)

    out = []
    for r in raw:
        # bdmc 是标的中文名（如「易方达创业板ETF」），不是代码
        if "创业板" not in (r.get("bdmc") or ""):
            continue
        exp = re.sub(r"[-/]", "", str(r.get("xqrq") or ""))[:8]
        if exp not in TARGET_EXPIRIES:
            continue
        # 深交所列名: hydm 合约代码 / hymc 合约名称 / xqj 行权价
        nm = r.get("hymc") or ""
        cp = "call" if "购" in nm else ("put" if "沽" in nm else None)
        if not cp:
            continue
        out.append({
            "market": "szse", "secid": f"12.{r['hydm']}",
            "code": str(r["hydm"]), "und_code": "159915",
            "und": "CYB", "cp": cp,
            "strike": float(r.get("xqj") or 0),
            "expiry": exp, "spot": 0.0,
        })
    return out


# ---------------------------------------------------------------- 主流程
def load_done():
    return {f[:-5] for f in os.listdir(RAW_DIR) if f.endswith(".json")}


def status(contracts):
    done = load_done()
    tot = len(contracts)
    n = sum(1 for c in contracts if f"{c['market']}_{c['code']}" in done)
    by_und = {}
    for c in contracts:
        k = (c["und"], c["expiry"])
        by_und.setdefault(k, [0, 0])
        by_und[k][1] += 1
        if f"{c['market']}_{c['code']}" in done:
            by_und[k][0] += 1
    print(f"总进度: {n}/{tot} ({n*100//max(tot,1)}%)")
    for (und, exp), (a, b) in sorted(by_und.items()):
        print(f"  {und:<7} {exp}  {a}/{b}")
    return n, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个合约")
    ap.add_argument("--status", action="store_true", help="只显示进度")
    ap.add_argument("--market", default="all", choices=["all", "sse", "szse"],
                    help="只跑某个市场（默认全部）")
    ap.add_argument("--interval", type=float, default=2.5, help="请求最小间隔（秒）")
    ap.add_argument("--fail-pause", type=int, default=6, help="连续失败多少次后休眠")
    ap.add_argument("--pause-sec", type=int, default=240, help="首次休眠时长（秒，逐级翻倍）")
    args = ap.parse_args()

    print("[1/3] 取上交所合约清单 …", flush=True)
    cs = [] if args.market == "szse" else sse_contracts()
    print(f"      上交所 {len(cs)} 个（500ETF / KCB50，9·10·12 月）", flush=True)
    print("[2/3] 取深交所合约清单 …", flush=True)
    if args.market == "sse":
        cz = []
        print("      （已跳过）", flush=True)
    else:
        try:
            cz = szse_contracts()
            print(f"      深交所 {len(cz)} 个（创业板，9·10·12 月）", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"      深交所失败：{e}", flush=True)
            cz = []
    contracts = cs + cz
    if not contracts:
        sys.exit("无目标合约，退出。")

    if args.status:
        status(contracts)
        return

    print("[3/3] 开始回补", flush=True)
    done = load_done()
    todo = [c for c in contracts if f"{c['market']}_{c['code']}" not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"      待回补 {len(todo)} 个（已完成 {len(contracts)-len(todo)} 个）\n", flush=True)

    ok = empty = fail = 0
    consec_fail = 0
    pause_level = 0
    t0 = time.time()
    for i, c in enumerate(todo, 1):
        kl = em_kline(c["secid"])
        key = f"{c['market']}_{c['code']}"
        if kl is None:
            fail += 1
            consec_fail += 1
        else:
            rec = dict(c, klines=kl)
            with open(os.path.join(RAW_DIR, key + ".json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
            if kl:
                ok += 1
            else:
                empty += 1
            consec_fail = 0
            pause_level = 0

        if i % 10 == 0 or i == len(todo) or consec_fail >= args.fail_pause:
            el = time.time() - t0
            print(f"  [{i}/{len(todo)}] 有效{ok} 空{empty} 失败{fail}  "
                  f"用时{el/60:.1f}min  ETA{(el/i)*(len(todo)-i)/60:.1f}min", flush=True)

        if consec_fail >= args.fail_pause:
            pause = min(args.pause_sec * (2 ** pause_level), 900)
            print(f"      连续失败 {consec_fail} 次 → 疑似封禁窗口，休眠 {pause}s"
                  f"（第 {pause_level + 1} 级）", flush=True)
            time.sleep(pause)
            pause_level += 1
            consec_fail = 0

        time.sleep(args.interval + random.uniform(0, 0.5))

    print(f"\n完成：有效 {ok} / 空 {empty} / 失败 {fail}，总用时 {(time.time()-t0)/60:.1f} 分钟")
    status(contracts)


if __name__ == "__main__":
    main()
