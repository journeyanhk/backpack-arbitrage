# -*- coding: utf-8 -*-
"""
Backpack 资金费率套利信号器 v0.1 (2026-08-05)

扫描抵押品 ∩ 永续合约的币种，筛选一周平均年化 >= 10% 的标的，
输出终端表格 + HTML 文件。

用法：
  python bpx_signal.py                  # 单次扫描，输出终端 + HTML
  python bpx_signal.py --min-rate 15    # 自定义阈值
  python bpx_signal.py --watch 300      # 每 300 秒自动刷新
  python bpx_signal.py --output signal.html  # 指定 HTML 输出路径
"""
import argparse
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

# 复用行情补丁
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bpx_stock import PublicStock

# ---------------- 常量 ----------------
WEEK_HOURS = 24 * 7       # 168 条
MONTH_HOURS = 500         # 取满 500 条（约 20 天）
HOURS_PER_YEAR = 24 * 365
DEFAULT_MIN_RATE = 10.0   # 周均年化最低阈值
DEFAULT_OUTPUT = "BackPack资金费率套利信号器.html"


def annualize(rate: float) -> float:
    """单小时资金费率 → 年化百分数"""
    return rate * HOURS_PER_YEAR * 100


def fetch_funding_rates(pub: PublicStock, sym: str, limit: int = MONTH_HOURS) -> List[float]:
    """拉取资金费率历史，返回 rate 列表（从旧到新）"""
    try:
        data = pub.get_funding_interval_rates(sym, limit=limit)
        if isinstance(data, list) and data:
            return [float(item["fundingRate"]) for item in data]
    except Exception:
        pass
    return []


def build_signals(pub: PublicStock, min_rate: float) -> List[dict]:
    """构建信号列表"""
    # 1. 抵押品 ∩ 永续合约
    collateral = pub.get_collateral()
    markets = pub.get_markets()
    perp_map = {m["symbol"]: m for m in markets if m["symbol"].endswith("_PERP")}

    col_map = {item["symbol"]: item for item in collateral}

    candidates = []
    for sym in col_map:
        perp_sym = f"{sym}_USDC_PERP"
        if perp_sym in perp_map:
            candidates.append((sym, perp_sym, col_map[sym], perp_map[perp_sym]))

    print(f"[扫描] 抵押品 {len(col_map)} 个，永续合约交集 {len(candidates)} 个")

    # 2. 并发拉历史数据
    def worker(c):
        sym, perp_sym, col_item, perp_item = c
        rates = fetch_funding_rates(pub, perp_sym, MONTH_HOURS)
        if not rates:
            return None

        # 最新费率取最近 3 期均值，避免单点异常值（恰逢该小时费率脉冲反转）
        latest_3 = rates[-3:] if len(rates) >= 3 else rates[-1:]
        latest_rate = sum(latest_3) / len(latest_3)
        week = rates[-WEEK_HOURS:] if len(rates) >= WEEK_HOURS else rates
        month = rates

        imf_spot = float(col_item["imfFunction"]["base"])
        imf_perp = float(perp_item["imfFunction"]["base"])
        hc_base = float(col_item["haircutFunction"]["kind"].get("base", 0))

        spot_lev = round(1 / imf_spot, 1) if 0 < imf_spot < 100 else 0
        perp_lev = round(1 / imf_perp, 1) if 0 < imf_perp < 100 else 0

        return {
            "symbol": sym,
            "spot_leverage": spot_lev,
            "perp_leverage": perp_lev,
            "spot_leverage_str": f"{spot_lev:.1f}x" if spot_lev else "N/A",
            "perp_leverage_str": f"{perp_lev:.1f}x" if perp_lev else "N/A",
            "haircut_base": hc_base,
            "latest_rate": latest_rate,
            "latest_apy": annualize(latest_rate),
            "week_avg_raw": sum(week) / len(week),
            "week_avg_apy": annualize(sum(week) / len(week)),
            "month_avg_raw": sum(month) / len(month),
            "month_avg_apy": annualize(sum(month) / len(month)),
            "data_points": len(month),
        }

    results = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(worker, c): c for c in candidates}
        for f in as_completed(futures):
            r = f.result()
            if r and r["week_avg_apy"] >= min_rate:
                results.append(r)

    results.sort(key=lambda x: x["week_avg_apy"], reverse=True)
    return results


def terminal_table(signals: List[dict]):
    """终端表格输出"""
    if not signals:
        print("\n无符合条件的标的（周均年化 >= 阈值）")
        return

    print(f"\n{'币种':8s} {'现货杠杆':>8s} {'合约杠杆':>8s} {'最新费率':>18s} {'周均费率':>18s} {'月均费率':>18s} {'抵押权重':>8s}")
    print("-" * 100)
    for s in signals:
        latest = f"{s['latest_rate']:.8f}({s['latest_apy']:.0f}%APY)"
        week = f"{s['week_avg_raw']:.8f}({s['week_avg_apy']:.0f}%APY)"
        month = f"{s['month_avg_raw']:.8f}({s['month_avg_apy']:.0f}%APY)"
        print(f"{s['symbol']:8s} {s['spot_leverage_str']:>8s} {s['perp_leverage_str']:>8s} {latest:>18s} {week:>18s} {month:>18s} {s['haircut_base']:7.0%}")
    print(f"\n共 {len(signals)} 个标的 | 阈值：周均年化 >= {DEFAULT_MIN_RATE}% | {datetime.now().strftime('%Y-%m-%d %H:%M')}")


def html_page(signals: List[dict], min_rate: float, output_path: str):
    """生成 HTML 文件"""
    rows = ""
    for s in signals:
        rows += f"""
        <tr data-symbol="{s['symbol']}" data-spot="{s['spot_leverage']}" data-perp="{s['perp_leverage']}" data-latest-apy="{s['latest_apy']:.0f}" data-week-apy="{s['week_avg_apy']:.0f}" data-month-apy="{s['month_avg_apy']:.0f}" data-haircut="{s['haircut_base']}">
            <td><strong>{s['symbol']}</strong></td>
            <td>{s['spot_leverage_str']}</td>
            <td>{s['perp_leverage_str']}</td>
            <td>{s['latest_rate']:.8f}<br><span class="apy">({s['latest_apy']:.0f}% APY)</span></td>
            <td>{s['week_avg_raw']:.8f}<br><span class="apy">({s['week_avg_apy']:.0f}% APY)</span></td>
            <td>{s['month_avg_raw']:.8f}<br><span class="apy">({s['month_avg_apy']:.0f}% APY)</span></td>
            <td>{s['haircut_base']:.0%}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Backpack 资金费率套利信号器</title>
<style>
  body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; background: #f5f5f5; margin: 20px; }}
  .container {{ max-width: 1100px; margin: 0 auto; background: #fff; padding: 24px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  h2 {{ color: #333; margin-top: 0; }}
  .info {{ color: #888; font-size: 14px; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ background: #f0f0f0; padding: 10px 8px; text-align: center; border-bottom: 2px solid #ddd; cursor: pointer; user-select: none; }}
  th:hover {{ background: #e0e0e0; }}
  th .arrow {{ font-size: 10px; margin-left: 4px; opacity: 0.4; }}
  th .arrow.active {{ opacity: 1; }}
  td {{ padding: 10px 8px; text-align: center; border-bottom: 1px solid #eee; }}
  tr:hover {{ background: #fafafa; }}
  .apy {{ color: #e74c3c; font-weight: 600; font-size: 12px; }}
  .highlight {{ background: #fff3cd; }}
</style>
<meta http-equiv="refresh" content="300">
</head>
<body>
<div class="container">
<h2>Backpack 资金费率套利信号器</h2>
<p class="info">筛选条件：周均年化 >= {min_rate:.0f}% | 标的来源：抵押品 ∩ 永续合约 | 更新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<table>
<thead>
<tr>
  <th onclick="sortTable(0,'string')">币种 <span class="arrow">▲▼</span></th>
  <th onclick="sortTable(1,'num')">现货杠杆 <span class="arrow">▲▼</span></th>
  <th onclick="sortTable(2,'num')">合约杠杆 <span class="arrow">▲▼</span></th>
  <th onclick="sortTable(3,'num')">最新费率 <span class="arrow">▲▼</span></th>
  <th onclick="sortTable(4,'num')">一周平均 <span class="arrow">▲▼</span></th>
  <th onclick="sortTable(5,'num')">一月平均 <span class="arrow">▲▼</span></th>
  <th onclick="sortTable(6,'num')">抵押权重 <span class="arrow">▲▼</span></th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
<p style="color:#999;font-size:12px;text-align:center;margin-top:20px;">点击表头排序 | 所有资金费率为原始小时费率（年化 APY 括号标注）。<br>冰火岛社区出品</p>
</div>
<script>
const cols = ['symbol','spot','perp','latest-apy','week-apy','month-apy','haircut'];
let sortState = {{col: 4, asc: false}};  // 默认按周均降序

function sortTable(colIdx, type) {{
  const tbody = document.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const key = 'data-' + cols[colIdx];
  const asc = sortState.col === colIdx ? !sortState.asc : false;
  sortState = {{col: colIdx, asc: asc}};

  rows.sort((a, b) => {{
    let va = a.getAttribute(key), vb = b.getAttribute(key);
    if (type === 'num') {{ va = parseFloat(va)||0; vb = parseFloat(vb)||0; }}
    return asc ? (va > vb ? 1 : va < vb ? -1 : 0) : (va < vb ? 1 : va > vb ? -1 : 0);
  }});

  // 更新箭头
  document.querySelectorAll('th .arrow').forEach(el => el.classList.remove('active'));
  const arrow = document.querySelectorAll('th .arrow')[colIdx];
  if (arrow) {{ arrow.textContent = asc ? '▲' : '▼'; arrow.classList.add('active'); }}

  rows.forEach(r => tbody.appendChild(r));
}}

// 初始排序
sortTable(4, 'num');
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML 已输出: {os.path.abspath(output_path)}")


def main():
    ap = argparse.ArgumentParser(description="Backpack 资金费率套利信号器")
    ap.add_argument("--min-rate", type=float, default=DEFAULT_MIN_RATE, help=f"周均年化最低阈值（默认 {DEFAULT_MIN_RATE}%）")
    ap.add_argument("--output", default=DEFAULT_OUTPUT, help=f"HTML 输出路径（默认 {DEFAULT_OUTPUT}）")
    ap.add_argument("--watch", type=int, default=0, help="定时刷新间隔（秒），0=单次扫描")
    args = ap.parse_args()

    pub = PublicStock()

    while True:
        start = time.time()
        signals = build_signals(pub, args.min_rate)
        terminal_table(signals)
        html_page(signals, args.min_rate, args.output)

        if not args.watch:
            break
        elapsed = time.time() - start
        wait = max(0, args.watch - elapsed)
        print(f"\n>>> 下次刷新 {wait:.0f}s 后（Ctrl+C 退出）")
        time.sleep(wait)
        if args.watch:
            print("\033[2J\033[H", end="")  # 清屏（ANSI）


if __name__ == "__main__":
    main()
