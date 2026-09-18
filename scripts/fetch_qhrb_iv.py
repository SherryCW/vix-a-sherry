#!/usr/bin/env python3
"""
抓取期货日报「期权市场」日报里的各品种加权隐含波动率，构建历史序列。

为什么用这个源
--------------
中证500 / 科创50 / 创业板没有第三方公开的 IV 历史序列（华创 HCVIX 只做
50ETF 与 300ETF，清华 CIMV 已停更）。期货日报每个交易日发布一篇期权市场
综述，正文包含全部 12 个品种的加权隐含波动率，是唯一能覆盖这三个品种的
公开历史来源。

口径提示（重要）
----------------
这里拿到的是**加权隐含波动率**（按成交/持仓加权、以平值附近合约为主），
与本 skill 自算的**方差互换 VIX** 不是同一口径，数值存在系统性差异。
它只能作「历史位置参照」使用，不能与自算序列混为一条曲线。

数据源
------
东方财富新闻搜索（search-api-web）找文章列表，再抓文章正文。
**这两个都是普通网页接口，不受 push2his 那套行情限流影响。**

用法
----
    python3 fetch_qhrb_iv.py                 # 抓最近若干页
    python3 fetch_qhrb_iv.py --pages 30      # 翻更多页（覆盖更长历史）
    python3 fetch_qhrb_iv.py --save          # 写入 data/qhrb_iv_history.csv
"""
import argparse
import csv
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.abspath(os.path.join(BASE, "..", "data", "qhrb_iv_history.csv"))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 Chrome/120 Safari/537.36")

KEYWORDS = [
    "创业板ETF期权加权隐含波动率",
    "华夏科创50ETF期权加权隐含波动率",
    "中证500ETF期权加权隐含波动率",
    "上证50ETF期权加权隐含波动率",     # 命中率最高：每篇完整版都会提到
    "期权市场 隐含波动率 期货日报",
]
MEDIA = {"期货日报", "期货日报网"}

# 每篇文章里三个目标品种的提取正则（容错：允许中间有 HTML 残留与空格）
PATTERNS = {
    "500ETF": [r"上交所\s*中证\s*500\s*ETF\s*期权加权隐含波动率为\s*([0-9.]+)",
               r"中证\s*500\s*ETF\s*期权加权隐含波动率为\s*([0-9.]+)"],
    "KCB50": [r"华夏科创50ETF期权加权隐含波动率为\s*([0-9.]+)",
              r"科创50ETF期权加权隐含波动率为\s*([0-9.]+)"],
    "CYB": [r"创业板ETF期权加权隐含波动率为\s*([0-9.]+)"],
}


def search(kw, page=1, size=30):
    """东财新闻搜索（JSONP）"""
    inner = {"uid": "", "keyword": kw, "type": ["cmsArticleWebOld"], "client": "web",
             "clientType": "web", "clientVersion": "curr",
             "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                            "pageIndex": page, "pageSize": size,
                                            "preTag": "", "postTag": ""}}}
    url = ("https://search-api-web.eastmoney.com/search/jsonp?"
           + urllib.parse.urlencode({"cb": "cb",
                                     "param": json.dumps(inner, separators=(",", ":"))}))
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://so.eastmoney.com/"})
    txt = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    d = json.loads(txt[txt.index("(") + 1:txt.rindex(")")])
    return (d.get("result") or {}).get("cmsArticleWebOld") or []


def fetch_article(url):
    """抓文章正文并提取三个品种的 IV"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    body = re.sub(r"<script[\s\S]*?</script>", "", h)
    body = re.sub(r"<style[\s\S]*?</style>", "", body)
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    out = {}
    for k, pats in PATTERNS.items():
        val = None
        for p in pats:
            m = re.search(p, txt)
            if m:
                try:
                    val = float(m.group(1))
                    break
                except ValueError:
                    pass
        out[k] = val
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=20, help="每个关键词翻多少页")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    print("[1/2] 搜索文章列表 …", flush=True)
    cands = {}
    for kw in KEYWORDS:
        for page in range(1, args.pages + 1):
            try:
                arts = search(kw, page=page)
            except Exception as e:  # noqa: BLE001
                print(f"      「{kw}」第 {page} 页失败：{type(e).__name__}", flush=True)
                break
            if not arts:
                break
            for a in arts:
                if a.get("mediaName") in MEDIA and "隐含波动率" in (a.get("title", "") + a.get("content", "")):
                    cands[a["url"]] = a.get("date", "")[:10]
            time.sleep(0.8)
        print(f"      「{kw}」累计候选 {len(cands)} 篇", flush=True)

    if not cands:
        raise SystemExit("未找到候选文章")

    print(f"\n[2/2] 逐篇提取（{len(cands)} 篇）…", flush=True)
    rows = []
    for i, (url, d) in enumerate(sorted(cands.items(), key=lambda x: x[1], reverse=True), 1):
        try:
            iv = fetch_article(url)
        except Exception as e:  # noqa: BLE001
            continue
        if any(v is not None for v in iv.values()):
            rows.append({"date": d, **iv, "url": url})
            mark = " ".join(f"{k}={v*100:.2f}%" if v is not None else f"{k}=—"
                            for k, v in iv.items())
            print(f"  [{i}/{len(cands)}] {d}  {mark}", flush=True)
        time.sleep(0.7)

    rows.sort(key=lambda r: r["date"])
    # 同一日期取最完整的一条
    best = {}
    for r in rows:
        d = r["date"]
        n = sum(1 for k in ("500ETF", "KCB50", "CYB") if r[k] is not None)
        if d not in best or n > sum(1 for k in ("500ETF", "KCB50", "CYB") if best[d][k] is not None):
            best[d] = r
    rows = sorted(best.values(), key=lambda r: r["date"])

    print(f"\n有效数据点：{len(rows)} 天")
    if rows:
        print(f"日期范围：{rows[0]['date']} ~ {rows[-1]['date']}")
        for k in ("500ETF", "KCB50", "CYB"):
            vs = [r[k] for r in rows if r[k] is not None]
            if vs:
                print(f"  {k:<7} {len(vs):>3} 个点  区间 {min(vs)*100:.2f}% ~ {max(vs)*100:.2f}%"
                      f"  均值 {sum(vs)/len(vs)*100:.2f}%")

    if args.save and rows:
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["date", "500ETF", "KCB50", "CYB", "url"])
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in ["date", "500ETF", "KCB50", "CYB", "url"]})
        print(f"\n[已写入] {OUT_CSV}")


if __name__ == "__main__":
    main()
