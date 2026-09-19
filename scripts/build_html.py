#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成移动端友好的 A股波动率指数报告 HTML
================================================
- 自包含单文件，零外部依赖（无 CDN、无 JS 框架），手机流量下秒开
- 移动优先布局，同时兼容桌面
- 数据实时计算（复用 vix.py）

用法：
    python3 build_html.py                    生成到 ../output/vix_report.html
    python3 build_html.py -o /path/out.html  指定输出路径
    python3 build_html.py --from-history     用历史序列最后一天的数据（不请求接口）
"""

import argparse
import csv
import html
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from vix import (UNDERLYINGS, compute, sentiment, staleness_note,  # noqa: E402
                 fetch_hcvix, HCVIX_MAP, rv_history, HISTORY_CSV, DATA_DIR)

OUT_DEFAULT = os.path.join(os.path.dirname(HERE), "output", "vix_report.html")


def vix_color(v):
    """与 vix.py 的 sentiment() 分档严格对齐，改一处必须同步改另一处"""
    if v < 13:
        return "#60a5fa"      # 极度平静
    if v < 18:
        return "#34d399"      # 常态
    if v < 21:
        return "#fbbf24"      # 偏谨慎
    if v < 30:
        return "#fb923c"      # 紧张
    if v < 40:
        return "#f87171"      # 恐慌
    return "#ef4444"          # 极度恐慌


def term_svg(terms, color):
    """期限结构迷你折线图（SVG，自适应宽度）

    底部两行标签：到期天数 + 该期限计算 variance 所用的远期价。
    远期价必须显示——它是真正参与计算的价格，藏着会让数值无法核对。
    """
    if len(terms) < 2:
        return ""
    W, H = 320, 108
    pad_l, pad_r, pad_t, pad_b = 42, 42, 20, 40
    ivs = [t["iv"] for t in terms]
    lo, hi = min(ivs), max(ivs)
    span = (hi - lo) or 1.0
    lo_p, hi_p = lo - span * 0.25, hi + span * 0.25
    n = len(terms)
    def px(i):
        return pad_l + (W - pad_l - pad_r) * (i / (n - 1))
    def py(v):
        return pad_t + (H - pad_t - pad_b) * (1 - (v - lo_p) / (hi_p - lo_p))
    pts = " ".join(f"{px(i):.1f},{py(t['iv']):.1f}" for i, t in enumerate(terms))
    dots = "".join(
        f'<circle cx="{px(i):.1f}" cy="{py(t["iv"]):.1f}" r="3.5" fill="{color}"/>'
        for i, t in enumerate(terms))
    labels = "".join(
        f'<text x="{px(i):.1f}" y="{H-24}" text-anchor="middle" font-size="10" fill="#7c8798">{t["days"]}天</text>'
        for i, t in enumerate(terms))
    fwds = "".join(
        f'<text x="{px(i):.1f}" y="{H-8}" text-anchor="middle" font-size="10" fill="#5f6b7d">远期 {t["fwd"]:.4f}</text>'
        for i, t in enumerate(terms))
    vals = "".join(
        f'<text x="{px(i):.1f}" y="{py(t["iv"])-9:.1f}" text-anchor="middle" font-size="10" fill="#9aa4b2">{t["iv"]:.1f}</text>'
        for i, t in enumerate(terms))
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="期限结构">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"/>{dots}{vals}{labels}{fwds}</svg>')


def card(res, pct=None, rvpct=None, qhrb=None):
    color = vix_color(res["vix"])
    label = sentiment(res["vix"])
    rv = res.get("rv20")
    vrp = res.get("vrp")
    vrp_txt = "—"
    vrp_cls = "muted"
    if vrp is not None:
        vrp_txt = f"{vrp:+.2f}"
        vrp_cls = "warn" if vrp > 0 else "good"
    slope = res["terms"][-1]["iv"] - res["terms"][0]["iv"] if len(res["terms"]) >= 2 else None
    if slope is None:
        slope_txt, slope_desc, slope_cls = "—", "该数据源无期限结构明细", "muted"
    else:
        slope_txt = f"{slope:+.2f}"
        slope_desc = "近月更贵，短期风险定价高" if slope < 0 else "远月更贵，担忧偏中长期"
        slope_cls = "warn" if slope < 0 else "muted"
    nk_min = min((t["n_strikes"] for t in res["terms"]), default=99)
    thin = '<p class="alert">注意：行权价覆盖偏窄（最少 %d 个），该期限 IV 可能低估</p>' % nk_min if nk_min < 8 else ""
    pct_row = ""
    if pct is not None:
        pct_row += (f'<div class="pctbar"><span class="pctlabel">VIX 分位</span>'
                    f'<b class="pctval">{pct:.1f}%</b>'
                    f'<span class="pctnote">2015年至今 · 华创金工 HCVIX</span></div>'
                    f'<div class="pcttrack"><div class="pctfill" style="width:{max(pct, 1.5):.1f}%;'
                    f'background:{color}"></div></div>')
    elif qhrb is not None:
        qp = qhrb["pct"]
        qc = ("#f87171" if qp >= 80 else "#fb923c" if qp >= 65
              else "#fbbf24" if qp >= 40 else "#34d399")
        pct_row += (f'<div class="pctbar"><span class="pctlabel">近半年位置</span>'
                    f'<b class="pctval">{qp:.0f}%</b>'
                    f'<span class="pctnote">近半年 · 期货日报加权IV</span></div>'
                    f'<div class="pcttrack"><div class="pctfill" style="width:{max(qp, 1.5):.1f}%;'
                    f'background:{qc}"></div></div>')
    if rvpct is not None:
        pct_row += (f'<div class="pctbar"><span class="pctlabel">波动水平</span>'
                    f'<b class="pctval">{rvpct:.1f}%</b>'
                    f'<span class="pctnote">近3年 · 20日已实现波动率 RV</span></div>'
                    f'<div class="pcttrack"><div class="pctfill" style="width:{max(rvpct, 1.5):.1f}%;'
                    f'background:{color};opacity:.5"></div></div>')

    return f'''<article class="card">
  <div class="chead">
    <div>
      <h2>{html.escape(res["name"])}</h2>
      <span class="code">{res["code"]}</span>
    </div>
    <span class="tag" style="color:{color};border-color:{color}33;background:{color}14">{label}</span>
  </div>
  <div class="vixrow">
    <span class="vix" style="color:{color}">{res["vix"]:.2f}</span>
    <span class="vixunit">30天年化波动率</span>
  </div>
  <div class="stats">
    <div class="stat"><span>标的收盘</span><b>{res["spot"]}</b></div>
    <div class="stat"><span>20日已实现波动率</span><b>{f"{rv:.2f}" if rv is not None else "—"}</b></div>
    <div class="stat"><span>VRP（VIX−RV）</span><b class="{vrp_cls}">{vrp_txt}</b></div>
    <div class="stat"><span>期限斜率</span><b class="{slope_cls}">{slope_txt}</b></div>
  </div>
  {pct_row}
  <p class="hint">{slope_desc}</p>
  {term_svg(res["terms"], color)}
  {thin}
</article>'''


QHRB_MAP = {"500ETF": "510500", "KCB50": "588000", "CYB": "159915"}


def load_qhrb():
    """
    期货日报「加权隐含波动率」的历史位置，按标的代码返回。

    这是第三方序列，口径与本 skill 自算的方差互换 VIX 不同（按成交加权、
    以平值合约为主，数值系统性偏高），只用于判断「当前波动水平在近半年
    的相对位置」。渲染时用独立标签与来源说明区分，不与 VIX 数值并列比较。

    返回 {标的代码: {"pct", "cur", "lo", "hi", "n", "date"}}
    """
    import csv as _csv
    path = os.path.join(DATA_DIR, "qhrb_iv_history.csv")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    if not rows:
        return {}

    out = {}
    for key, code in QHRB_MAP.items():
        vals = [(r["date"], float(r[key])) for r in rows if r.get(key)]
        if len(vals) < 4:
            continue
        d, cur = vals[-1]
        arr = [v for _, v in vals]
        below = sum(1 for v in arr if v < cur)
        out[code] = {
            "pct": below / (len(arr) - 1) * 100 if len(arr) > 1 else 50.0,
            "cur": cur, "lo": min(arr), "hi": max(arr),
            "n": len(arr), "date": d,
        }
    return out


def build(results, data_date, stale_note=None, pcts=None, rvpcts=None, qhrb=None):
    pcts = pcts or {}
    rvpcts = rvpcts or {}
    qhrb = qhrb or {}
    cards = "\n".join(card(r, pcts.get(r["underlying"]), rvpcts.get(r["underlying"]),
                           qhrb.get(r["code"]))
                      for r in results)
    top = max(results, key=lambda r: r["vix"])
    low = min(results, key=lambda r: r["vix"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    gen_note = f"页面生成于 {now}"
    stale_html = (f'<div class="stalewarn"><b>数据尚未更新</b>　{html.escape(stale_note)}</div>'
                  if stale_note else "")

    summary = (f'<div class="summary">'
               f'<div><span class="slabel">波动最高</span>'
               f'<b style="color:{vix_color(top["vix"])}">{html.escape(top["name"])}'
               f'<span class="snum">{top["vix"]:.2f}</span></b></div>'
               f'<div><span class="slabel">波动最低</span>'
               f'<b style="color:{vix_color(low["vix"])}">{html.escape(low["name"])}'
               f'<span class="snum">{low["vix"]:.2f}</span></b></div>'
               f'</div>')

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0e1116">
<title>A股波动率指数 VIX-A-SHERRY · {data_date}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0e1116;color:#e8eaed;
 font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
 line-height:1.6;-webkit-font-smoothing:antialiased;
 padding:20px 14px calc(28px + env(safe-area-inset-bottom))}}
.wrap{{max-width:520px;margin:0 auto}}
h1{{font-size:19px;font-weight:600;letter-spacing:.2px;display:inline-block}}
.brand{{display:inline-block;margin-left:9px;padding:2px 9px;border-radius:6px;
 background:#16283a;border:1px solid #2f5a7d;color:#85b7eb;vertical-align:2px;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;
 font-weight:500;letter-spacing:1.5px}}
.sign{{margin-top:16px;text-align:center;color:#42505f;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
 font-size:11px;letter-spacing:2.4px}}
.sub{{font-size:12px;color:#7c8798;margin-top:5px}}
.warnbar{{margin-top:12px;padding:9px 12px;border-radius:9px;background:#3b2a12;border:1px solid #7c5a1e;
 font-size:11.5px;color:#f0c674;line-height:1.5}}
.stalewarn{{margin-top:8px;padding:9px 12px;border-radius:9px;background:#3a1c1c;border:1px solid #82302f;
 font-size:11.5px;color:#f09595;line-height:1.55}}
.stalewarn b{{color:#f7c1c1;font-weight:600}}
.summary{{display:flex;gap:10px;margin-top:14px}}
.summary>div{{flex:1;background:#171b22;border:1px solid #262c36;border-radius:11px;padding:11px 13px}}
.slabel{{display:block;font-size:11px;color:#7c8798;margin-bottom:3px}}
.snum{{margin-left:10px}}
.summary b{{font-size:14px;font-weight:600}}
.card{{background:#171b22;border:1px solid #262c36;border-radius:14px;
 padding:15px 15px 12px;margin-top:12px}}
.chead{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}}
h2{{font-size:14.5px;font-weight:600;display:inline}}
.code{{font-size:11px;color:#7c8798;margin-left:6px}}
.tag{{font-size:11px;padding:3px 9px;border-radius:20px;border:1px solid;white-space:nowrap}}
.vixrow{{display:flex;align-items:baseline;gap:9px;margin:11px 0 4px}}
.vix{{font-size:40px;font-weight:600;line-height:1;letter-spacing:-1px;
 font-variant-numeric:tabular-nums}}
.vixunit{{font-size:11.5px;color:#7c8798}}
.stats{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:13px}}
.stat{{background:#12161c;border-radius:9px;padding:8px 10px}}
.stat span{{display:block;font-size:11px;color:#7c8798}}
.stat b{{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}}
.hint{{font-size:11.5px;color:#7c8798;margin:9px 0 2px}}
.pctbar{{display:flex;align-items:baseline;gap:7px;margin:13px 0 5px}}
.pctlabel{{font-size:11.5px;color:#7c8798}}
.pctval{{font-size:15px;font-weight:600;color:#E8EAED;font-variant-numeric:tabular-nums}}
.pctnote{{font-size:10.5px;color:#5a6675;margin-left:auto}}
.pcttrack{{height:4px;border-radius:2px;background:#232a33;overflow:hidden}}
.pctfill{{height:100%;border-radius:2px}}
.warn{{color:#fb923c}}
.good{{color:#34d399}}
.muted{{color:#9aa4b2}}
.alert{{font-size:11.5px;color:#f0c674;margin-top:6px}}
svg{{display:block;margin-top:8px}}
.qsec{{margin-top:22px}}
.qh2{{font-size:13px;color:#9aa4b2;font-weight:500;margin-bottom:5px}}
.qnote{{font-size:11px;color:#6b7685;line-height:1.65;margin-bottom:12px}}
.qrow{{background:#161b22;border:1px solid #242b35;border-radius:11px;
 padding:11px 13px;margin-bottom:8px}}
.qhead{{display:flex;align-items:baseline;gap:8px;margin-bottom:7px}}
.qname{{font-size:12.5px;color:#c8cfd8}}
.qval{{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}}
.qpct{{font-size:11.5px;color:#7c8798;margin-left:auto}}
.qtrack{{height:4px;border-radius:2px;background:#232a33;overflow:hidden;margin-bottom:6px}}
.qfill{{height:100%;border-radius:2px}}
.qrange{{font-size:10.5px;color:#5a6675}}
footer{{margin-top:22px;padding-top:15px;border-top:1px solid #262c36;
 font-size:11px;color:#6b7685;line-height:1.75}}
footer b{{color:#9aa4b2;font-weight:500}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>A股波动率指数</h1>
    <div class="brand">VIX-A-SHERRY</div>
    <p class="sub">数据日期 {data_date} · {gen_note}</p>
    <div class="warnbar">非官方指数。上交所中国波指 iVIX 已于 2018-02 停发，本页基于上交所官方期权数据按方差互换法自行计算，与官方方案存在系统性口径差异，仅供研究参考。</div>
    {stale_html}
  </header>
  {summary}
  {cards}
  <footer>
    <b>怎么读</b><br>
    · VIX 反映期权市场对未来 30 天波动率的预期，数值越高市场越不安<br>
    · VRP = VIX − 20日已实现波动率。为正说明期权比实际波动贵（市场在买保险），为负则罕见，多出现在恐慌急跌后<br>
    · 期限斜率 = 远月 IV − 近月 IV。为负是常态，代表短期风险定价更高<br>
    · 品种间比较比看绝对值更有价值：50ETF 是权重蓝筹，中证500 是中盘，创业板与科创50 是高波动成长<br><br>
    <b>口径</b>：期权价格取上交所披露收盘价（官方方案用买卖价推算）；剔除剩余 ≤ 7 天合约；无风险利率 1.80%；未补虚拟行权价。覆盖上交所 5 个 ETF 期权品种，不含深交所与中金所股指期权。<br>
    <b>卡片底部的分位</b>有三种，口径不同，别混读：「VIX 分位」＝隐含波动率的历史位置（2015 年至今，仅 50ETF／300ETF 有）；「近半年位置」＝期货日报公布的加权隐含波动率（按成交加权、以平值合约为主，数值系统性偏高）；「波动水平」＝标的已实现波动率 RV（回头看实际波动了多少，不是预期）。
    <div class="sign">VIX-A-SHERRY</div>
  </footer>
</div>
</body>
</html>'''


def load_from_history():
    """离线模式：用历史序列最后一天的数据拼装（无期限结构明细）"""
    if not os.path.exists(HISTORY_CSV):
        return None
    rows = list(csv.DictReader(open(HISTORY_CSV, encoding="utf-8")))
    if not rows:
        return None
    last_date = max(r["date"] for r in rows)
    out = []
    for r in rows:
        if r["date"] != last_date:
            continue
        vix = float(r["vix"])
        rv = float(r["rv20"]) if r.get("rv20") else None
        out.append({
            "underlying": r["underlying"], "code": r["code"],
            "name": UNDERLYINGS.get(r["underlying"], {}).get("name", r["underlying"]),
            "data_date": r["date"], "vix": vix,
            "rv20": rv, "vrp": (vix - rv) if rv is not None else None,
            "spot": r["spot"], "rate": float(r["rate"]),
            "method": "历史序列快照", "terms": [],
        })
    return out, last_date


def main():
    ap = argparse.ArgumentParser(description="生成 A股VIX 移动端报告 HTML")
    ap.add_argument("-o", "--out", default=OUT_DEFAULT)
    ap.add_argument("--from-history", action="store_true", help="用历史序列快照，不请求接口")
    ap.add_argument("--rate", type=float, default=0.018)
    args = ap.parse_args()

    if args.from_history:
        loaded = load_from_history()
        if not loaded:
            raise SystemExit("历史序列为空，无法离线生成。先运行 vix.py calc --all --save")
        results, data_date = loaded
    else:
        results, data_date = [], None
        for alias in UNDERLYINGS:
            try:
                res = compute(alias, rate=args.rate)
            except SystemExit as e:
                print(f"[跳过] {alias}: {e}", file=sys.stderr)
                continue
            results.append(res)
            data_date = res["data_date"]
        if not results:
            raise SystemExit("所有品种均取数失败，未生成页面")

    results.sort(key=lambda r: r["vix"])
    note = staleness_note(data_date)

    # 历史分位（数据源：华创金工 HCVIX，仅部分品种有对应序列）
    pcts = {}
    try:
        hv = fetch_hcvix()
        for a in UNDERLYINGS:
            ser = HCVIX_MAP.get(a)
            if ser and ser in hv:
                vals = [v for _, v in hv[ser] if v is not None and v > 0.5]   # 过滤缺失值
                if len(vals) >= 20:
                    pcts[a] = sum(1 for v in vals if v <= vals[-1]) / len(vals) * 100
    except Exception as e:  # noqa: BLE001
        print(f"[历史分位不可用] {e}", file=sys.stderr)

    # 波动水平分位（RV 口径，覆盖全部品种，作为无 IV 历史时的过渡参照）
    rvpcts = {}
    for a in UNDERLYINGS:
        try:
            got = rv_history(UNDERLYINGS[a]["tx"])
            if got:
                vals = [v for v in got[0] if v > 0]
                if len(vals) >= 60:
                    rvpcts[a] = sum(1 for v in vals if v <= vals[-1]) / len(vals) * 100
        except Exception:  # noqa: BLE001
            pass

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(build(results, data_date, note, pcts, rvpcts, load_qhrb()))
    print(f"已生成：{args.out}")
    print(f"数据日期：{data_date}　品种数：{len(results)}　文件大小：{os.path.getsize(args.out)/1024:.1f} KB")
    if note:
        print(f"【数据尚未更新】{note}")


if __name__ == "__main__":
    main()
