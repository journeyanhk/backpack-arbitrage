# -*- coding: utf-8 -*-
"""
Backpack 美股（代币化股票）maker 交易脚本 v0.1 (2026-08-04)
=========================================================

设计要点（不复用 btc-yield-enhancer 逻辑）：
  btc-yield-enhancer 是给 24/7 连续市场设计的（RV 动态锚点 + 追价 + 冷静期）。
  Backpack 美股是「时段制」市场：订单簿只在非交易时段开放，交易时段走 RFQ，
  还有节假日、周末闭市。照搬 24/7 逻辑会在时段切换时出错（闭市还挂单、追价失败）。
  本脚本改为「时段感知状态机」，所有动作由当前时段决定，时段切换由引擎自动判定。

Backpack 美股交易规则（来自 docs.backpack.exchange/#section/Stock-Trading）：
  1. 只有带现货订单簿的股票（现 4 只：MU/SNDK/SKHY/SPCX 的 .US_USDC）能在
     非交易时段用标准订单端点挂单；交易时段（美东 9:30-16:00）只能走 RFQ。
  2. Taker Speed Bump：所有非 postOnly 订单统一 100ms 延迟，postOnly 豁免。
     -> 挂单必须 post_only=True。
  3. 代币化股票用 USDC 结算；数量受 securities 各时段 minQuantity/stepSize/maxQuantity 约束。
  4. 外部行情用 source=External（ticker/klines）；订单簿开放时用 depth 更实时。

当前版本策略（保守）：
  - 只在有订单簿的会话（PRE_MARKET / POST_MARKET / OVERNIGHT）做双侧 postOnly maker 挂单；
  - REGULAR（交易时段）撤单停摆，不自动走 RFQ（RFQ 延迟结算 + 询价制，第一版不碰）；
  - 周六 / 周日 20:00 前 / 节假日 -> SHUTDOWN，全部撤单。
  - 默认 DRY_RUN=True：只打印将要执行的动作，不真下单。配好 API key 且确认后置 False。

WebSocket 说明：本脚本是低频 maker 挂单（20s 一轮），行情用 REST depth/ticker 足够，
不属于行情轮询滥用；WS 化（depth 流 + account.rfqUpdate）留作后续升级项。

用法：
  set BPX_PUBLIC_KEY=xxx  BPX_SECRET_KEY=xxx   （未配置则只能 dry-run/只读）
  python bpx_trader.py --dry-run            # 演练
  python bpx_trader.py --live               # 实盘（需 key，谨慎）
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# 时区处理：优先 zoneinfo（Python 3.9+），Windows 需 tzdata 包；失败回退固定 DST 计算
try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
    _HAS_ZONEINFO = True
except Exception:
    NY_TZ = None
    _HAS_ZONEINFO = False

from bpx_stock import PublicStock
from bpx.account import Account

# ---------------- 配置 ----------------

DEFAULT_SYMBOLS = ["MON_USDC", "SOL_USDC", "HYPE_USDC"]
CHECK_INTERVAL = 20          # 主循环周期（秒）
BID_OFFSET = 0.002           # 买单低于参考价比例（0.2%）
ASK_OFFSET = 0.002           # 卖单高于参考价比例
REPRICE_THRESHOLD = 0.005    # 参考价漂移超过该比例则撤旧重挂（0.5%）
MAX_NOTIONAL_PER_SIDE = 500  # 单方向最大名义金额（USDC），风控硬顶
DEFAULT_QTY = 10             # 默认每方向股数（会被会话约束 clamp）
CLIENT_ID_PREFIX = 8402      # clientId 前缀，用于识别本脚本的订单
LOG_FILE = "bpx_trader.log"

SESSION_ORDERBOOK = {"US_EQUITIES_PRE_MARKET", "US_EQUITIES_POST_MARKET", "US_EQUITIES_OVERNIGHT", "CRYPTO_ACTIVE"}
SESSION_REGULAR = "US_EQUITIES_REGULAR"
SESSION_SHUTDOWN = "SHUTDOWN"


class SessionEngine:
    """时段引擎：按美东时间 + 节假日判定当前会话（不依赖 startWeekday 的猜测）。"""

    def __init__(self, pub: PublicStock, symbols: List[str]):
        self.pub = pub
        self.symbols = symbols
        # ★ 加密货币没有美股时段，直接返回 CRYPTO_ACTIVE
        self.is_crypto = all(".US_" not in s for s in symbols)
        self.holiday_dates = self._load_holidays() if not self.is_crypto else set()

    def _load_holidays(self) -> set:
        try:
            raw = self.pub.get_market_holidays()
            out = set()
            if isinstance(raw, list):
                for h in raw:
                    d = h.get("date")
                    if d:
                        out.add(str(d)[:10])
            logging.info("已加载休市日 %d 个", len(out))
            return out
        except Exception as e:
            logging.warning("拉取休市日失败（%s），回退仅按周末判定", e)
            return set()

    @staticmethod
    def _ny_now() -> datetime:
        """返回美东当前时间；zoneinfo 不可用时用简化 DST 规则（3月第2个周日~11月第1个周日，UTC-4，其余 UTC-5）。"""
        if _HAS_ZONEINFO:
            return datetime.now(NY_TZ)
        now = datetime.utcnow()
        year = now.year
        # 简化 DST：3月第二个周日 02:00 开始，11月第一个周日 02:00 结束（美东）
        def second_sunday(month: int) -> datetime:
            d = datetime(year, month, 1, 2, 0)
            days_ahead = (6 - d.weekday()) % 7
            return d.replace(day=1) + __import__("datetime").timedelta(days=days_ahead + 7)
        dst_start = second_sunday(3)
        dst_end = second_sunday(11)
        offset = -4 if dst_start <= now < dst_end else -5
        return now.replace(tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=offset)))

    def current_session(self) -> str:
        """返回当前会话名。加密货币 24/7 返回 CRYPTO_ACTIVE。"""
        if self.is_crypto:
            return "CRYPTO_ACTIVE"
        now = self._ny_now()
        # 节假日判定（用纽约日期）
        ny_date = now.strftime("%Y-%m-%d")
        if ny_date in self.holiday_dates:
            return SESSION_SHUTDOWN
        wd = now.weekday()  # 0=周一 ... 6=周日
        hhmm = now.strftime("%H:%M")
        if wd == 6:  # 周日
            # 周日 20:00 起进入 Overnight（周日 20:00 -> 周一 04:00）
            return "US_EQUITIES_OVERNIGHT" if hhmm >= "20:00" else SESSION_SHUTDOWN
        if wd == 5:  # 周六全天关闭
            return SESSION_SHUTDOWN
        if wd == 4 and hhmm >= "20:00":  # 周五晚 20:00 后无隔夜时段，市场关闭至周日 20:00
            return SESSION_SHUTDOWN
        # 周一~周四（及周五白天）
        if "04:00" <= hhmm < "09:30":
            return "US_EQUITIES_PRE_MARKET"
        if "09:30" <= hhmm < "16:00":
            return SESSION_REGULAR
        if "16:00" <= hhmm < "20:00":
            return "US_EQUITIES_POST_MARKET"
        return "US_EQUITIES_OVERNIGHT"  # 周一~周四 20:00-24:00 及 00:00-04:00（次日凌晨隔夜）


class MakerStrategy:
    """时段感知的 postOnly 双侧 maker。不复用 btc-yield-enhancer 的 RV 锚点/追价。"""

    def __init__(self, pub: PublicStock, symbols: List[str], dry_run: bool):
        self.pub = pub
        self.symbols = symbols
        self.dry_run = dry_run
        self.sessions = self._load_session_constraints()
        self.engine = SessionEngine(pub, symbols)
        self.last_ref = {}   # symbol -> (price, ts)
        self._filters: Dict[str, dict] = {}     # ★ N3: 精度过滤器缓存
        self._load_filters()

    def _load_filters(self):
        """★ N3: 从 get_markets() 加载 tickSize/stepSize"""
        try:
            ms = self.pub.get_markets()
            for m in ms if isinstance(ms, list) else []:
                sym = m.get("symbol", "")
                f = m.get("filters", {})
                if not sym or not f:
                    continue
                self._filters[sym] = {
                    "tickSize": float(f["price"]["tickSize"]),
                    "stepSize": float(f["quantity"]["stepSize"]),
                    "minQty": float(f["quantity"]["minQuantity"]),
                    "maxQty": float(f["quantity"].get("maxQuantity", 0) or 1e18),
                }
            logging.info("已加载 %d 个市场过滤器", len(self._filters))
        except Exception as e:
            logging.warning("加载过滤器失败: %s，回退 hardcoded 精度", e)

    def _quantize_price(self, symbol: str, price: float) -> float:
        f = self._filters.get(symbol, {})
        tick = f.get("tickSize")
        if not tick:
            return round(price, 4)
        return round(round(price / tick) * tick, 8)

    def _quantize_qty(self, symbol: str, qty: float) -> float:
        f = self._filters.get(symbol, {})
        step = f.get("stepSize")
        if not step:
            return round(qty, 4)
        q = round(qty / step) * step
        return max(f.get("minQty", step), min(f.get("maxQty", 1e18), q))
        self._filters: Dict[str, dict] = {}     # ★ N3: 精度过滤器缓存
        self._load_filters()

    def _load_session_constraints(self) -> Dict[str, dict]:
        """每只股票各会话的数量约束 {symbol: {SESSION: (min, max, step)}}。"""
        out = {}
        try:
            secs = self.pub.get_securities()
            for s in secs:
                asset = s.get("asset", "")
                sym = asset + "_USDC"
                if sym not in self.symbols:
                    continue
                cons = {}
                for sess in s.get("sessions", []):
                    cons[sess["name"]] = (
                        float(sess.get("minQuantity", 1)),
                        float(sess.get("maxQuantity", 1000)),
                        float(sess.get("stepSize", 1)),
                    )
                out[sym] = cons
        except Exception as e:
            logging.error("加载证券会话约束失败: %s", e)
        return out

    def clamp_qty(self, symbol: str, session: str, want: float) -> Optional[float]:
        """按会话约束 clamp 数量；无约束数据时用默认值。"""
        cons = self.sessions.get(symbol, {}).get(session)
        if not cons:
            logging.warning("%s 无 %s 会话约束，用默认数量", symbol, session)
            return DEFAULT_QTY
        mn, mx, step = cons
        qty = max(mn, min(mx, want))
        qty = int(qty / step) * step if step >= 1 else round(qty / step) * step
        return max(mn, qty)

    def get_ref_price(self, symbol: str) -> Optional[float]:
        """参考价：优先 depth 中价（订单簿开放时最实时），回退 External ticker lastPrice。
        ★ 修复: bids 数组升序，bids[-1] 才是买一"""
        try:
            d = self.pub.get_depth(symbol)
            if isinstance(d, dict) and d.get("asks") and d.get("bids"):
                best_ask = float(d["asks"][0][0])
                best_bid = float(d["bids"][-1][0])   # ★ bids[-1] = 买一
                if best_ask > 0 and best_bid > 0:
                    return (best_ask + best_bid) / 2
        except Exception as e:
            logging.warning("%s depth 获取失败: %s", symbol, e)
        try:
            t = self.pub.get_ticker(symbol, source="External")
            if isinstance(t, dict) and t.get("lastPrice"):
                return float(t["lastPrice"])
        except Exception as e:
            logging.warning("%s external ticker 失败: %s", symbol, e)
        return None

    def plan(self) -> dict:
        """决策一轮：返回 {symbol: {"action": ..., "session": ..., "price": ..., "detail": ...}}。"""
        session = self.engine.current_session()
        plan = {"session": session, "symbols": {}}
        for sym in self.symbols:
            if session not in SESSION_ORDERBOOK:
                plan["symbols"][sym] = {"action": "CANCEL_ALL", "session": session, "detail": "非订单簿时段，撤单停摆"}
                continue
            price = self.get_ref_price(sym)
            if not price:
                plan["symbols"][sym] = {"action": "WAIT", "session": session, "detail": "无参考价，跳过本轮"}
                continue
            # ★ N3: 按 tickSize 量化价格
            bid = self._quantize_price(sym, price * (1 - BID_OFFSET))
            ask = self._quantize_price(sym, price * (1 + ASK_OFFSET))
            drift = 0.0
            if sym in self.last_ref and self.last_ref[sym][0]:
                drift = abs(price - self.last_ref[sym][0]) / self.last_ref[sym][0]
            self.last_ref[sym] = (price, time.time())
            plan["symbols"][sym] = {
                "action": "REFRESH" if drift > REPRICE_THRESHOLD else "KEEP",
                "session": session,
                "price": price,
                "bid": bid,
                "ask": ask,
                "detail": f"参考价 {price:.2f}，漂移 {drift:.4f}，bid {bid} / ask {ask}",
            }
        return plan


class Trader:
    """执行层：REST 下单/撤单/查单。dry_run 时只打印。"""

    def __init__(self, symbols: List[str], dry_run: bool):
        pub_key = os.environ.get("BPX_PUBLIC_KEY", "")
        sec_key = os.environ.get("BPX_SECRET_KEY", "")
        self.symbols = symbols
        self.dry_run = dry_run
        self.account = None
        if not dry_run and pub_key and sec_key:
            self.account = Account(pub_key, sec_key, window=5000)
        elif not dry_run:
            raise SystemExit("实盘模式需要 BPX_PUBLIC_KEY / BPX_SECRET_KEY 环境变量")

    def _client_id(self) -> int:
        return int(f"{CLIENT_ID_PREFIX}{int(time.time()) % 100000000}")

    def get_open_orders(self, symbol: str) -> List[dict]:
        """★ 修复: get_open_orders(market_type) 第一个位置参数是 market_type，
        传 symbol 必须用关键字参数 symbol=symbol"""
        if not self.account:
            return []
        resp = self.account.get_open_orders(symbol=symbol)
        return resp if isinstance(resp, list) else []

    def cancel_all(self, symbol: str):
        """★ 修复: account.cancel_all_orders() 已执行 DELETE, 不再二次 http_client.delete"""
        action = f"撤单 {symbol}（全部）"
        if self.dry_run:
            logging.info("[DRY] %s", action)
            return
        self.account.cancel_all_orders(symbol)
        logging.info("%s 完成", action)

    def place_orders(self, symbol: str, bid: float, ask: float, qty: float, session: str, strat=None):
        """★ 修复: 精度按 filters 量化, 直接拿 execute_order 返回值"""
        # ★ N3: 从 strat 取量化方法（Trader 自己没有 _quantize_*）
        q_price = strat._quantize_price if strat and hasattr(strat, '_quantize_price') else lambda s,p: round(p,4)
        q_qty = strat._quantize_qty if strat and hasattr(strat, '_quantize_qty') else lambda s,q: round(q,4)
        for side, price in (("Bid", bid), ("Ask", ask)):
            action = f"挂单 {symbol} {side} {qty} @ {price}（postOnly, {session}）"
            if self.dry_run:
                logging.info("[DRY] %s", action)
                continue
            pq = q_qty(symbol, qty)
            pp = q_price(symbol, price)
            resp = self.account.execute_order(
                symbol=symbol,
                side=side,
                order_type="Limit",
                time_in_force="GTC",
                quantity=str(pq),
                price=str(pp),
                post_only=True,
                self_trade_prevention="RejectTaker",
                client_id=self._client_id(),
            )
            logging.info("%s -> %s", action, json.dumps(resp, ensure_ascii=False)[:200])


def main():
    ap = argparse.ArgumentParser(description="Backpack 美股 maker 交易脚本")
    ap.add_argument("--live", action="store_true", help="实盘模式（默认 dry-run）")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="逗号分隔的股票 symbol")
    ap.add_argument("--once", action="store_true", help="只跑一轮决策就退出")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
    )

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    dry_run = not args.live

    pub = PublicStock()
    trader = Trader(symbols, dry_run)
    strat = MakerStrategy(pub, symbols, dry_run)

    logging.info("启动：%s 模式，标的 %s，周期 %ss", "DRY-RUN" if dry_run else "LIVE", symbols, CHECK_INTERVAL)
    if not dry_run:
        logging.warning("实盘模式：请确认已核对策略与风控参数！")

    last_session = None
    while True:
        try:
            plan = strat.plan()
            session = plan["session"]
            if session != last_session:
                logging.info("时段切换：%s -> %s", last_session, session)
                last_session = session
            for sym, p in plan["symbols"].items():
                if p["action"] == "CANCEL_ALL":
                    trader.cancel_all(sym)
                elif p["action"] == "REFRESH":
                    trader.cancel_all(sym)
                    qty = strat.clamp_qty(sym, session, DEFAULT_QTY)
                    trader.place_orders(sym, p["bid"], p["ask"], qty, session, strat=strat)
                else:
                    logging.info("[%s] %s %s", session, sym, p["detail"])
        except KeyboardInterrupt:
            logging.info("收到中断，退出")
            break
        except Exception as e:
            logging.error("主循环异常: %s", e, exc_info=True)
        if args.once:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
