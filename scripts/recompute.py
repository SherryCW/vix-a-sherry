#!/usr/bin/env python3
"""
用回补的合约日线重算历史 VIX。

数据来源：backfill.py 落盘在 data/history_raw/ 的合约日线（东财）。
计算方法：与 vix.py 完全一致（CBOE / 上交所编制方案的方差互换法），
          因此重算结果与实时计算的数值可直接拼接成一条序列。

两个硬规则（与编制方案一致）：
  1. 剔除剩余期限 ≤ 7 天的到期月
  2. 若近月剩余 > 30 天，按方案注 3 直取近月，不做插值

用法：
    python3 recompute.py            # 重算并输出
    python3 recompute.py --save     # 同时写入 data/vix_history_backfill.csv
"""
import argparse
import csv
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.abspath(os.path.join(BASE, "..", "data", "history_raw"))
OUT_CSV = os.path.abspath(os.path.join(BASE, "..", "data", "vix_history_backfill.csv"))

RATE = 0.018          # 无风险利率，与 vix.py 保持一致
MIN_DAYS = 7          # 剔除剩余 ≤7 天的到期月
TARGET_DAYS = 30.0    # 目标期限

UND_NAME = {"510500": "中证500ETF", "588000": "科创50ETF", "159915": "创业板ETF"}


def term_variance(calls, puts, T, r=RATE):
    """单期限方差 σ²（小数）。逻辑与 vix.py 的 term_variance 一致。"""
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
    return sigma2 if sigma2 > 0 else None


def combine_30d(t1, s1, t2, s2, n_days=365.0):
    if abs(t2 - t1) < 1e-9:
        return None
    den = n_days * (t2 - t1)
    if den == 0:
        return None
    var = (t1 * s1 * (n_days * t2 - TARGET_DAYS) +
           t2 * s2 * (TARGET_DAYS - n_days * t1)) / den * (n_days / TARGET_DAYS)
    return var if var > 0 else None


def load_raw():
    """读回补的合约日线，组织成 books[und][date][expiry] = {call/put: {strike: price}}"""
    books = defaultdict(lambda: defaultdict(lambda: defaultdict(
        lambda: {"call": {}, "put": {}})))
    files = [f for f in os.listdir(RAW_DIR) if f.endswith(".json")]
    n_used = 0
    for fn in files:
        try:
            with open(os.path.join(RAW_DIR, fn), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        und, cp = rec.get("und_code"), rec.get("cp")
        strike, expiry = rec.get("strike"), rec.get("expiry")
        kl = rec.get("klines") or []
        if not (und and cp and strike and expiry and kl):
            continue
        exp_d = datetime.strptime(str(expiry), "%Y%m%d").date()
        used = False
        for d_str, close in kl:
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if close > 0:
                books[und][d][exp_d][cp][float(strike)] = float(close)
                used = True
        if used:
            n_used += 1
    return books, len(files), n_used


def compute_series(books):
    """对每个 (品种, 交易日) 计算 30 天 VIX"""
    out = []
    for und in sorted(books):
        for d in sorted(books[und]):
            terms = books[und][d]
            # 计算每个到期月的方差，同时剔除剩余 ≤MIN_DAYS 的
            cand = []
            for exp, sides in terms.items():
                days = (exp - d).days
                if days <= MIN_DAYS:
                    continue
                T = days / 365.0
                s2 = term_variance(sides["call"], sides["put"], T)
                if s2 is not None:
                    cand.append((days, T, s2))
            if not cand:
                continue
            cand.sort()

            if len(cand) >= 2:
                d1, t1, s1 = cand[0]
                d2, t2, s2 = cand[1]
                if d1 > TARGET_DAYS:
                    var = s1                      # 近月已超 30 天 → 直取
                    method = "near"
                else:
                    var = combine_30d(t1, s1, t2, s2)
                    method = "interp"
            else:
                d1, t1, s1 = cand[0]
                var = s1
                method = "single"

            if var is None or var <= 0:
                continue
            out.append({
                "date": d.isoformat(), "und_code": und,
                "name": UND_NAME.get(und, und),
                "vix": round(math.sqrt(var) * 100, 4),
                "near_days": d1, "method": method,
                "n_terms": len(cand),
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="写入 CSV")
    args = ap.parse_args()

    if not os.path.isdir(RAW_DIR) or not os.listdir(RAW_DIR):
        raise SystemExit(f"没有回补数据：{RAW_DIR}\n请先运行 python3 backfill.py")

    books, n_files, n_used = load_raw()
    print(f"合约文件 {n_files} 个，其中 {n_used} 个含有效数据\n")

    series = compute_series(books)
    if not series:
        raise SystemExit("未能算出任何结果（检查合约数据是否完整）")

    # 按品种打印
    by_und = defaultdict(list)
    for r in series:
        by_und[r["und_code"]].append(r)

    for und, rs in sorted(by_und.items()):
        rs.sort(key=lambda x: x["date"])
        print(f"■ {UND_NAME.get(und, und)}（{len(rs)} 个交易日）")
        print(f"  {'日期':<12}{'VIX':>8}{'近月剩余':>9}{'合成':>8}   区间")
        for r in rs:
            print(f"  {r['date']:<12}{r['vix']:>8.2f}{r['near_days']:>9}{r['method']:>8}")
        vs = [r["vix"] for r in rs]
        print(f"  → 区间 {min(vs):.2f} ~ {max(vs):.2f}，均值 {sum(vs)/len(vs):.2f}\n")

    if args.save:
        series.sort(key=lambda x: (x["date"], x["und_code"]))
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(series[0].keys()))
            w.writeheader()
            w.writerows(series)
        print(f"[已写入] {OUT_CSV}（{len(series)} 条）")


if __name__ == "__main__":
    main()
