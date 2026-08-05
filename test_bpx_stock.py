# -*- coding: utf-8 -*-
"""bpx_stock 补丁模块实测：验证美股外部行情与证券端点。"""
import json
import time

from bpx_stock import PublicStock

pub = PublicStock()


def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


# 1. 股票外部 ticker（补丁核心能力）
section("1. get_ticker('AAPL.US_USDC', source='External')")
r = pub.get_ticker("AAPL.US_USDC", source="External")
if isinstance(r, dict) and r.get("lastPrice"):
    print("OK  lastPrice:", r["lastPrice"], "| high:", r["high"], "| low:", r["low"], "| trades:", r["trades"])
else:
    print("FAIL 返回:", json.dumps(r, ensure_ascii=False)[:200])

# 2. 股票外部 K 线
section("2. get_klines('AAPL.US_USDC', '1d', source='External')")
start = int(time.time()) - 86400 * 7
r = pub.get_klines("AAPL.US_USDC", "1d", start, source="External")
print("OK  K 线条数:", len(r) if isinstance(r, list) else r)
if isinstance(r, list) and r:
    last = r[-1]
    print("    最近一根:", last.get("start"), "close:", last.get("close"), "volume:", last.get("volume"))

# 3. 证券列表
section("3. get_securities()")
secs = pub.get_securities()
if isinstance(secs, list):
    print("OK  证券数量:", len(secs))
    sample = [s.get("asset") for s in secs[:10]]
    print("    前 10 个:", ", ".join(sample))
else:
    print("FAIL:", str(secs)[:200])

# 4. 现货订单簿股票市场
section("4. get_stock_markets()")
ms = pub.get_stock_markets()
print("OK  STOCK 现货订单簿市场:", [m["symbol"] for m in ms])

# 5. 市场会话与休市日
section("5. get_market_sessions() / get_market_holidays()")
sess = pub.get_market_sessions()
hol = pub.get_market_holidays()
print("OK  会话数量:", len(sess) if isinstance(sess, list) else sess)
if isinstance(hol, list):
    print("OK  休市日数量:", len(hol), "| 前 3 个:", [h.get("date") for h in hol[:3]])

# 6. 兼容性回归：原有加密行为不变
section("6. 回归：get_ticker('SOL_USDC') 不加 source（原行为）")
r = pub.get_ticker("SOL_USDC")
if isinstance(r, dict) and r.get("lastPrice"):
    print("OK  SOL lastPrice:", r["lastPrice"])
else:
    print("FAIL:", json.dumps(r, ensure_ascii=False)[:200])

print("\n全部测试完成。")
