# -*- coding: utf-8 -*-
"""bpx_arb_ccxt.py v5.0 核心逻辑单元测试（mock 交易所，不连真实网络）。

覆盖审计"必须测试的故障场景"中可在单测层验证的部分：
  1. 追单/平仓价格方向（可成交限价 + 滑点上限）
  2. 永续平仓 reduceOnly
  3. 部分成交增量记账（不重不漏）
  4. OrderNotFound → UNKNOWN → 冻结币种（不伪造成交）
  5. 追单超时回滚已成交腿
  6. 费率硬门槛（负费率拒绝开仓）
  7. leverage 参与目标名义计算
  8. 平仓只关闭账本策略持仓（不动人工持仓）
  9. 借贷参数按意图（平仓卖现货禁止 autoBorrow）
  10. 撤单后重新确认最终成交量
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccxt

import bpx_arb_ccxt as bpx

bpx._init_db()  # 测试前确保账本表存在


def _reset_state():
    bpx._frozen.clear()
    bpx._exposed.clear()
    bpx._unknown_exposure = False
    bpx._task_results.clear()
    bpx._dry_seq = 0
    bpx._funding_rate_cache.clear()
    import sqlite3
    conn = sqlite3.connect(bpx.DB_PATH)
    for t in ("orders", "fills", "strategy_positions", "tasks"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


class FakeExchange:
    """可控 mock 交易所。"""

    def __init__(self):
        self.order_state = {}      # order_id -> dict(status, filled, amount)
        self.created = []          # (symbol, side, amount, price, params)
        self.cancelled = []
        self.order_not_found = set()
        self.network_err_orders = set()
        self.bbo = {}              # symbol -> (bid, ask)
        self.funding_rate = (0.001, 3600)   # (rate, interval)
        self.funding_fail = False
        self.positions = []
        self.collateral = {"collateral": [], "netEquityAvailable": 100000.0,
                           "assetsValue": 0, "marginFraction": 0}
        self._seq = 0
        self._dry_mode = False
        self.auto_fill = False   # True 时下单即全成（测试用）
        # ★ 模拟 backpack: 不支持 fetchOrder，支持 open orders/history/trades
        self.has = {"fetchOrder": False, "fetchOrders": True,
                    "fetchOpenOrders": True, "fetchMyTrades": True}

    def _order_view(self, oid):
        st = self.order_state[oid]
        return {"id": oid, "status": st["status"], "filled": st["filled"],
                "amount": st["amount"], "price": 0, "symbol": "?"}

    def fetch_open_orders(self, symbol=None):
        return [self._order_view(oid) for oid, st in self.order_state.items()
                if st["status"] == "open"]

    def fetch_orders(self, symbol=None):
        return [self._order_view(oid) for oid in self.order_state]

    def fetch_my_trades(self, symbol=None):
        return []

    def fetch_order_book(self, symbol, limit=5):
        if symbol not in self.bbo:
            raise Exception("no book")
        bid, ask = self.bbo[symbol]
        return {"bids": [[bid, 10.0]], "asks": [[ask, 10.0]]}

    def fetch_funding_rate(self, symbol):
        if self.funding_fail:
            raise Exception("net down")
        rate, interval = self.funding_rate
        return {"fundingRate": rate, "fundingInterval": interval}

    def create_order(self, symbol=None, type=None, side=None, amount=None,
                     price=None, params=None):
        self._seq += 1
        oid = f"oid-{self._seq}"
        rec = {"symbol": symbol, "side": side, "amount": amount, "price": price,
               "params": dict(params or {})}
        self.created.append(rec)
        if oid in self.order_not_found:
            raise ccxt.OrderNotFound(f"order not found {oid}")
        if self.auto_fill:
            self.order_state[oid] = {"status": "closed", "filled": amount, "amount": amount}
        else:
            self.order_state[oid] = {"status": "open", "filled": 0.0, "amount": amount}
        return {"id": oid, "status": self.order_state[oid]["status"],
                "filled": self.order_state[oid]["filled"], "amount": amount,
                "price": price, "average": None, "side": side, "symbol": symbol}

    def fetch_order(self, order_id, symbol=None):
        if order_id in self.order_not_found:
            raise ccxt.OrderNotFound(f"order not found {order_id}")
        if order_id in self.network_err_orders:
            raise ccxt.NetworkError("timeout")
        st = self.order_state.get(order_id)
        if not st:
            raise ccxt.OrderNotFound(f"order not found {order_id}")
        return {"id": order_id, "status": st["status"], "filled": st["filled"],
                "amount": st["amount"], "average": None, "price": 0, "symbol": symbol}

    def cancel_order(self, order_id, symbol=None):
        if order_id not in self.order_state and order_id not in self.order_not_found:
            raise ccxt.OrderNotFound(f"order not found {order_id}")
        self.cancelled.append(order_id)

    def fetch_positions(self):
        return self.positions

    def privateGetApiV1CapitalCollateral(self):
        return self.collateral

    def publicGetApiV1Collateral(self):
        return []

    # 测试辅助
    def fill(self, order_id, qty, status="closed"):
        st = self.order_state[order_id]
        st["filled"] = qty
        st["status"] = status

    def set_order_status(self, order_id, status):
        self.order_state[order_id]["status"] = status


def _install(exchange: FakeExchange):
    bpx.ex = exchange
    bpx.DRY_RUN = False
    bpx.has_key = True
    bpx.markets = {
        "MON/USDC": {"spot": True, "contractSize": 1.0},
        "MON/USDC:USDC": {"swap": True, "contractSize": 1.0},
    }
    exchange.bbo = {"MON/USDC": (0.99, 1.01), "MON/USDC:USDC": (0.98, 1.00)}
    exchange.funding_rate = (0.001, 3600)


class TestCrossPrice(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.ex = FakeExchange()
        _install(self.ex)

    def test_direction(self):
        """买入用卖一×1+滑点；卖出用买一×1-滑点（可成交方向）"""
        buy = bpx._cross_price("MON/USDC", "buy", 20)
        sell = bpx._cross_price("MON/USDC", "sell", 20)
        self.assertAlmostEqual(buy, 1.01 * 1.002, places=10)
        self.assertAlmostEqual(sell, 0.99 * 0.998, places=10)
        # 买入价必须 >= 卖一（穿过盘口）；卖出价必须 <= 买一
        self.assertGreaterEqual(buy, 1.01)
        self.assertLessEqual(sell, 0.99)


class TestCheckOrderStatus(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.ex = FakeExchange()
        _install(self.ex)

    def test_order_not_found_is_unknown(self):
        """未成交/历史/成交记录都查不到 → UNKNOWN，绝不伪造成交"""
        oid, _ = bpx._place_limit(bpx.ex, "MON/USDC", "buy", 500.0, 1.0, intent="open")
        bpx.ex.fetch_open_orders = lambda symbol=None: []
        bpx.ex.fetch_orders = lambda symbol=None: []
        status, filled, _ = bpx._check_order_filled(bpx.ex, "MON/USDC", oid)
        self.assertEqual(status, "unknown")
        self.assertEqual(filled, 0.0)

    def test_status_via_open_orders_and_history(self):
        """★ backpack 不支持 fetchOrder: 用 open orders + 订单历史确认状态与成交量"""
        self.ex.auto_fill = False
        oid, coid = bpx._place_limit(bpx.ex, "MON/USDC", "buy", 500.0, 1.0, intent="open")
        # 部分成交 200，仍在挂单
        self.ex.order_state[oid]["filled"] = 200.0
        status, filled, _ = bpx._check_order_filled(bpx.ex, "MON/USDC", oid)
        self.assertEqual(status, "open")
        self.assertEqual(filled, 200.0)
        # 全部成交 → 订单归档（不在 open，从历史确认 closed）
        self.ex.order_state[oid]["filled"] = 500.0
        self.ex.order_state[oid]["status"] = "closed"
        status, filled, _ = bpx._check_order_filled(bpx.ex, "MON/USDC", oid)
        self.assertEqual(status, "closed")
        self.assertEqual(filled, 500.0)
        # 撤销 → canceled
        self.ex.order_state[oid]["status"] = "canceled"
        self.ex.order_state[oid]["filled"] = 0.0
        status, filled, _ = bpx._check_order_filled(bpx.ex, "MON/USDC", oid)
        self.assertEqual(status, "canceled")


class TestExecutePair(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.ex = FakeExchange()
        _install(self.ex)
        self.sp = "MON/USDC"
        self.pp = "MON/USDC:USDC"

    def _orders(self, side=None, market=None):
        out = []
        for c in self.ex.created:
            if side and c["side"] != side:
                continue
            if market and market not in c["symbol"]:
                continue
            out.append(c)
        return out

    def test_both_maker_fill(self):
        """两腿 maker 均成交 → 成功，账本按量登记"""
        self.ex.auto_fill = True
        ok, s_f, p_f, msg = bpx.execute_pair("MON", 500.0, timeout_s=30)
        self.assertTrue(ok, msg)
        self.assertEqual(s_f, 500.0)
        self.assertEqual(p_f, 500.0)
        # maker 价格: 现货买@买一, 永续卖@卖一
        spot = [c for c in self.ex.created if c["side"] == "buy" and ":USDC" not in c["symbol"]][0]
        perp = [c for c in self.ex.created if c["side"] == "sell" and ":USDC" in c["symbol"]][0]
        self.assertEqual(spot["price"], 0.99)
        self.assertEqual(perp["price"], 1.00)
        self.assertTrue(spot["params"].get("postOnly"))
        pos = bpx._get_strategy_position("MON")
        self.assertAlmostEqual(pos["spot_qty"], 500.0)
        self.assertAlmostEqual(pos["perp_qty"], -500.0)

    def test_spot_filled_perp_chase(self):
        """现货先成交、永续未动 → 立即追单卖永续，追单价=买一×(1-滑点)（可成交方向）"""
        def create(symbol=None, type=None, side=None, amount=None, price=None, params=None):
            self.ex._seq += 1
            oid = f"oid-{self.ex._seq}"
            rec = {"symbol": symbol, "side": side, "amount": amount, "price": price,
                   "params": dict(params or {})}
            self.ex.created.append(rec)
            is_maker = bool((params or {}).get("postOnly"))
            if symbol == "MON/USDC" and side == "buy" and is_maker:
                # 现货 maker 立即成交
                self.ex.order_state[oid] = {"status": "closed", "filled": amount, "amount": amount}
            elif ":USDC" in symbol and side == "sell" and not is_maker:
                # 永续追单立即成交
                self.ex.order_state[oid] = {"status": "closed", "filled": amount, "amount": amount}
            else:
                self.ex.order_state[oid] = {"status": "open", "filled": 0.0, "amount": amount}
            return {"id": oid, "status": self.ex.order_state[oid]["status"],
                    "filled": self.ex.order_state[oid]["filled"], "amount": amount,
                    "price": price, "average": None, "side": side, "symbol": symbol}
        bpx.ex.create_order = create

        ok, s_f, p_f, msg = bpx.execute_pair("MON", 500.0, timeout_s=30)
        self.assertTrue(ok, msg)

        # 追单存在: 永续卖单，非 post_only，价格=买一×(1-滑点)=0.98×0.998（可成交）
        perp_sells = [c for c in self.ex.created if c["side"] == "sell" and ":USDC" in c["symbol"]]
        self.assertGreaterEqual(len(perp_sells), 2)  # maker + 追单
        chase = [c for c in perp_sells if not c["params"].get("postOnly")]
        self.assertEqual(len(chase), 1)
        self.assertAlmostEqual(chase[0]["price"], 0.98 * 0.998, places=10)
        self.assertLess(chase[0]["price"], 0.98)  # 穿过盘口
        # 追单数量为剩余未成交量（maker 未成交，全量追）
        self.assertAlmostEqual(chase[0]["amount"], 500.0)

    def test_partial_chase_then_full(self):
        """永续追单先部分成交 200 再全成 → 增量记账正确，无漏记/重复"""
        chase_state = {"filled": 0.0}
        self.ex._chase_oid = None

        def create(symbol=None, type=None, side=None, amount=None, price=None, params=None):
            self.ex._seq += 1
            oid = f"oid-{self.ex._seq}"
            self.ex.created.append({"symbol": symbol, "side": side, "amount": amount,
                                    "price": price, "params": dict(params or {})})
            if symbol == "MON/USDC" and side == "buy" and (params or {}).get("postOnly"):
                self.ex.order_state[oid] = {"status": "closed", "filled": amount, "amount": amount}
            elif ":USDC" in symbol and side == "sell" and not (params or {}).get("postOnly"):
                self.ex.order_state[oid] = {"status": "open", "filled": 0.0, "amount": amount}
                self.ex._chase_oid = oid
            else:
                self.ex.order_state[oid] = {"status": "open", "filled": 0.0, "amount": amount}
            return {"id": oid, "status": self.ex.order_state[oid]["status"],
                    "filled": self.ex.order_state[oid]["filled"], "amount": amount,
                    "price": price, "average": None, "side": side, "symbol": symbol}

        def open_orders_fake(symbol=None):
            # 追单订单分两次成交: 先 200 再全量（驱动新查询路径 2a/2b）
            chase_oid = getattr(self.ex, "_chase_oid", None)
            if chase_oid and chase_oid in self.ex.order_state:
                st = self.ex.order_state[chase_oid]
                if chase_state["filled"] == 0:
                    st["filled"] = 200.0
                    chase_state["filled"] = 200.0
                else:
                    st["filled"] = st["amount"]
                    st["status"] = "closed"
            return FakeExchange.fetch_open_orders(self.ex, symbol)

        bpx.ex.create_order = create
        bpx.ex.fetch_open_orders = open_orders_fake
        ok, s_f, p_f, msg = bpx.execute_pair("MON", 500.0, timeout_s=30)
        self.assertTrue(ok, msg)
        pos = bpx._get_strategy_position("MON")
        self.assertAlmostEqual(pos["spot_qty"], 500.0)
        self.assertAlmostEqual(pos["perp_qty"], -500.0)

    def test_partial_fill_incremental(self):
        """部分成交增量记账：先成交 200 再成交 300，账本合计 500 不重不漏"""
        # 直接驱动 _update_order_fill，模拟两次确认
        coid = bpx._record_order("MON", "perp", "sell", "open", 500.0, 1.0)
        bpx._update_order_fill("MON", "perp", "sell", "oid-1", coid, 200.0, "open")
        bpx._update_order_fill("MON", "perp", "sell", "oid-1", coid, 500.0, "closed")
        pos = bpx._get_strategy_position("MON")
        self.assertAlmostEqual(pos["perp_qty"], -500.0)
        import sqlite3
        conn = sqlite3.connect(bpx.DB_PATH)
        total = conn.execute("SELECT SUM(qty) FROM fills WHERE client_order_id=?", (coid,)).fetchone()[0]
        conn.close()
        self.assertAlmostEqual(total, 500.0)

    def test_unknown_freezes_symbol(self):
        """订单状态 UNKNOWN → 冻结币种，不开新单"""
        # 订单在 open/history/trades 中都查不到 → UNKNOWN
        bpx.ex.fetch_open_orders = lambda symbol=None: []
        bpx.ex.fetch_orders = lambda symbol=None: []
        ok, s_f, p_f, msg = bpx.execute_pair("MON", 500.0, timeout_s=30)
        self.assertFalse(ok)
        self.assertIn("MON", bpx._frozen)

    def test_rollback_on_chase_fail(self):
        """永续追单超时未成交 → 回滚已成交现货（卖出），不继续裸露"""
        created = []

        def create(symbol=None, type=None, side=None, amount=None, price=None, params=None):
            self.ex._seq += 1
            oid = f"oid-{self.ex._seq}"
            created.append((symbol, side, amount, price, dict(params or {})))
            # 追单（非 post_only 的合约卖单）永不成交
            if ":USDC" in symbol and side == "sell" and not (params or {}).get("postOnly"):
                self.ex.order_state[oid] = {"status": "open", "filled": 0.0, "amount": amount}
            else:
                self.ex.order_state[oid] = {"status": "open", "filled": 0.0, "amount": amount}
                if symbol == "MON/USDC" and side == "buy" and (params or {}).get("postOnly"):
                    # 现货 maker 立即成交
                    self.ex.order_state[oid] = {"status": "closed", "filled": amount, "amount": amount}
                if symbol == "MON/USDC" and side == "sell" and not (params or {}).get("postOnly"):
                    # 回滚卖单立即成交
                    self.ex.order_state[oid] = {"status": "closed", "filled": amount, "amount": amount}
            return {"id": oid, "status": self.ex.order_state[oid]["status"],
                    "filled": self.ex.order_state[oid]["filled"],
                    "amount": amount, "price": price, "average": None, "side": side,
                    "symbol": symbol}

        bpx.ex.create_order = create
        ok, s_f, p_f, msg = bpx.execute_pair("MON", 500.0, timeout_s=30)
        self.assertFalse(ok)
        self.assertEqual(s_f, 500.0)
        # 回滚卖单存在: 现货卖出，价格=买一×(1-滑点)，intent=close（无 autoBorrow）
        rollbacks = [c for c in created if c[0] == "MON/USDC" and c[1] == "sell"]
        self.assertEqual(len(rollbacks), 1)
        _, side, amount, price, params = rollbacks[0]
        self.assertAlmostEqual(amount, 500.0)
        self.assertAlmostEqual(price, 0.99 * 0.998, places=10)
        self.assertNotIn("autoBorrow", params)
        # 回滚成交后账本归零
        pos = bpx._get_strategy_position("MON")
        self.assertAlmostEqual(pos["spot_qty"], 0.0)


class TestClosePair(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.ex = FakeExchange()
        _install(self.ex)

    def test_close_direction_and_reduce_only(self):
        """平仓: 现货卖@买一×(1-滑点) 无 autoBorrow；合约买@卖一×(1+滑点) 带 reduceOnly"""
        self.ex.auto_fill = True
        # 预播种账本: 现货 +300 / 永续空 -300（与真实流程 close_position 从账本取量一致）
        coid = bpx._record_order("MON", "spot", "buy", "open", 300.0, 1.0)
        bpx._update_order_fill("MON", "spot", "buy", "oid-x", coid, 300.0, "closed")
        coid2 = bpx._record_order("MON", "perp", "sell", "open", 300.0, 1.0)
        bpx._update_order_fill("MON", "perp", "sell", "oid-y", coid2, 300.0, "closed")
        ok, s_f, p_f = bpx.close_pair("MON", 300.0, 300.0)
        self.assertTrue(ok)
        spot = [c for c in self.ex.created if ":USDC" not in c["symbol"]][0]
        perp = [c for c in self.ex.created if ":USDC" in c["symbol"]][0]
        self.assertAlmostEqual(spot["price"], 0.99 * 0.998, places=10)
        self.assertAlmostEqual(perp["price"], 1.00 * 1.002, places=10)
        self.assertNotIn("autoBorrow", spot["params"])
        self.assertNotIn("autoBorrow", perp["params"])
        self.assertIn("autoLendRedeem", spot["params"])
        self.assertTrue(perp["params"].get("reduceOnly"))
        pos = bpx._get_strategy_position("MON")
        self.assertAlmostEqual(pos["spot_qty"], 0.0)
        self.assertAlmostEqual(pos["perp_qty"], 0.0)


class TestOpenPosition(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.ex = FakeExchange()
        _install(self.ex)

    def _auto_fill(self):
        """下单即全成"""
        def create(symbol=None, type=None, side=None, amount=None, price=None, params=None):
            self.ex._seq += 1
            oid = f"oid-{self.ex._seq}"
            rec = {"symbol": symbol, "side": side, "amount": amount, "price": price,
                   "params": dict(params or {})}
            self.ex.created.append(rec)
            self.ex.order_state[oid] = {"status": "closed", "filled": amount, "amount": amount}
            return {"id": oid, "status": "closed", "filled": amount, "amount": amount,
                    "price": price, "average": None, "side": side, "symbol": symbol}
        bpx.ex.create_order = create

    def test_negative_funding_rejected(self):
        """费率为负 → 硬性拒绝开仓，不下单"""
        self.ex.funding_rate = (-0.0005, 3600)
        r = bpx.open_position("MON", 100, 2)
        self.assertFalse(r["ok"])
        self.assertIn("费率不达标", r["error"])
        self.assertEqual(len(self.ex.created), 0)

    def test_net_apy_negative_rejected(self):
        """年化 ≥ 门槛但净年化（扣成本缓冲）≤ 0 → 拒绝（成本覆盖不了不开仓）"""
        self.ex.funding_rate = (0.000012, 3600)  # 年化 ~10.5% ≥ 门槛
        bpx.EST_ROUND_TRIP_COST_APY = 12.0       # 加大缓冲 → 净年化 -1.5% ≤ 0
        try:
            r = bpx.open_position("MON", 100, 2)
            self.assertFalse(r["ok"])
            self.assertIn("净年化", r["error"])
        finally:
            bpx.EST_ROUND_TRIP_COST_APY = 5.0

    def test_low_apy_rejected(self):
        """门槛逻辑: 年化 ≥ 10% 且净年化 > 0 放行；年化 < 10% 或净年化 ≤ 0 拒绝"""
        self._auto_fill()
        # 年化 ~11%（0.0013%/h）→ 放行（用户实际遇到的场景：11% > 10% 应能开仓）
        self.ex.funding_rate = (0.0000127, 3600)  # ~11.1% gross，净 6.1% > 0 → 通过
        r = bpx.open_position("MON", 100, 2)
        self.assertTrue(r["ok"])
        # 年化 ~17.5% → 通过
        self.ex.funding_rate = (0.00002, 3600)
        r = bpx.open_position("MON", 100, 2)
        self.assertTrue(r["ok"])
        # 年化 ~7% < 10 → 拒绝
        self.ex.funding_rate = (0.000008, 3600)
        self.ex.created.clear()
        r = bpx.open_position("MON", 100, 2)
        self.assertFalse(r["ok"])
        self.assertIn("费率不达标", r["error"])
        self.assertEqual(len(self.ex.created), 0)  # 未下任何单

    def test_leverage_in_target_notional(self):
        """leverage 参与目标名义: 本金 100 × 2x = 200 → 拆 2 对"""
        self._auto_fill()
        r = bpx.open_position("MON", 100, 2, order_size=100)
        self.assertTrue(r["ok"])
        self.assertEqual(r["pairs_total"], 2)
        # 每对数量 = 100 / mid(1.00) = 100 个币
        spot_buys = [c for c in self.ex.created if c["side"] == "buy" and ":USDC" not in c["symbol"]]
        self.assertEqual(len(spot_buys), 2)
        self.assertAlmostEqual(spot_buys[0]["amount"], 100.0)

    def test_open_buy_borrows_usdc(self):
        """开仓买入现货允许 autoBorrow + autoLend"""
        self._auto_fill()
        bpx.open_position("MON", 100, 1, order_size=100)
        spot = [c for c in self.ex.created if c["side"] == "buy" and ":USDC" not in c["symbol"]][0]
        self.assertTrue(spot["params"].get("autoBorrow"))
        self.assertTrue(spot["params"].get("autoLend"))


class TestClosePositionLedgerOnly(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.ex = FakeExchange()
        _install(self.ex)

    def _auto_fill(self):
        def create(symbol=None, type=None, side=None, amount=None, price=None, params=None):
            self.ex._seq += 1
            oid = f"oid-{self.ex._seq}"
            self.ex.created.append({"symbol": symbol, "side": side, "amount": amount,
                                    "price": price, "params": dict(params or {})})
            self.ex.order_state[oid] = {"status": "closed", "filled": amount, "amount": amount}
            return {"id": oid, "status": "closed", "filled": amount, "amount": amount,
                    "price": price, "average": None, "side": side, "symbol": symbol}
        bpx.ex.create_order = create

    def test_close_only_strategy_qty(self):
        """账户总持有 1000，账本只 100 → 只平 100，不碰人工持仓"""
        self._auto_fill()
        # 账本播种 100 对
        coid = bpx._record_order("MON", "spot", "buy", "open", 100, 1.0)
        bpx._update_order_fill("MON", "spot", "buy", "oid-x", coid, 100, "closed")
        coid2 = bpx._record_order("MON", "perp", "sell", "open", 100, 1.0)
        bpx._update_order_fill("MON", "perp", "sell", "oid-y", coid2, 100, "closed")
        # 账户实际持有 1000（人工持仓），collateral 模拟
        self.ex.collateral["collateral"] = [
            {"symbol": "MON", "totalQuantity": 1000, "availableQuantity": 1000,
             "assetMarkPrice": 1.0},
            {"symbol": "USDC", "totalQuantity": 0, "borrowQuantity": 0},
        ]
        self.ex.positions = [{"symbol": "MON/USDC:USDC", "contracts": -100.0, "side": "short",
                              "markPrice": 1.0, "entryPrice": 1.0, "notional": 100.0,
                              "unrealizedPnl": 0}]
        r = bpx.close_position("MON")
        self.assertTrue(r["ok"])
        # 只平 100: 现货卖单与合约买单数量均为 100
        spot_sells = [c for c in self.ex.created if c["side"] == "sell" and ":USDC" not in c["symbol"]]
        self.assertAlmostEqual(sum(c["amount"] for c in spot_sells), 100.0)
        pos = bpx._get_strategy_position("MON")
        self.assertAlmostEqual(pos["spot_qty"], 0.0)
        self.assertAlmostEqual(pos["perp_qty"], 0.0)


class TestCancelReconfirm(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.ex = FakeExchange()
        _install(self.ex)

    def test_cancel_reconfirms_fill(self):
        """撤单时订单已被部分成交 → 重确认后按增量记账，剩余量才可补单"""
        # 创建订单并部分成交 120/300
        coid = bpx._record_order("MON", "spot", "buy", "open", 300.0, 1.0)
        bpx._update_order_fill("MON", "spot", "buy", "oid-1", coid, 120.0, "open")
        # 撤单：交易所 cancel 后订单状态变为 canceled 且 filled=120
        bpx.ex.order_state["oid-1"] = {"status": "open", "filled": 120.0, "amount": 300.0}

        def cancel(order_id, symbol=None):
            self.ex.order_state[order_id]["status"] = "canceled"
            self.ex.order_state[order_id]["filled"] = 120.0
        bpx.ex.cancel_order = cancel

        bpx._cancel_order("MON/USDC", "oid-1", coid, "MON", "spot", "buy")
        pos = bpx._get_strategy_position("MON")
        self.assertAlmostEqual(pos["spot_qty"], 120.0)  # 只记 120，无重复
        import sqlite3
        conn = sqlite3.connect(bpx.DB_PATH)
        total = conn.execute("SELECT SUM(qty) FROM fills WHERE client_order_id=?", (coid,)).fetchone()[0]
        conn.close()
        self.assertAlmostEqual(total, 120.0)


class TestOrderLookup(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def test_lookup_by_order_id_or_client_id(self):
        """订单行支持按 order_id 或 client_order_id 双键查找（DRY-RUN dry 订单可用）"""
        coid = bpx._record_order("MON", "spot", "buy", "open", 500.0, 1.0)
        bpx._bind_order_id(coid, "dry-123-1")
        row = bpx._get_order_row("dry-123-1")     # 按 order_id
        self.assertEqual(row["requested_amount"], 500.0)
        row2 = bpx._get_order_row(coid)           # 按 client_order_id
        self.assertEqual(row2["order_id"], "dry-123-1")
        # 模拟 dry 订单状态检查: 应返回已确认的请求量而非 0
        self.assertEqual(bpx._check_order_filled(bpx.ex, "MON/USDC", "dry-123-1")[1], 500.0)

    def test_client_id_is_uint32_integer(self):
        """★ Backpack 要求 clientId 为 uint32 整数，字符串会被 400 拒绝"""
        self.ex = FakeExchange()
        _install(self.ex)
        self.ex.auto_fill = True
        bpx.open_position("MON", 100, 1, order_size=100)
        for c in self.ex.created:
            cid = c["params"].get("clientId")
            self.assertIsInstance(cid, int, f"clientId 必须是整数，实际 {cid!r}")
            self.assertTrue(1 <= cid <= 0xFFFFFFFF)

    def test_reconcile_detects_phantom_ledger(self):
        """★ 对账并集检查：账本有持仓而交易所没有（幽灵持仓）也必须标记未知敞口"""
        self.ex = FakeExchange()
        _install(self.ex)
        self.ex.positions = []  # 交易所无任何持仓
        self.ex.collateral = {"collateral": [], "netEquityAvailable": 100000.0,
                              "assetsValue": 0, "marginFraction": 0}
        # 账本播种幽灵持仓（模拟 DRY-RUN 残留）
        coid = bpx._record_order("MON", "spot", "buy", "open", 39.0, 1.0)
        bpx._update_order_fill("MON", "spot", "buy", "oid-x", coid, 39.0, "closed")
        bpx._unknown_exposure = False
        bpx._reconcile_positions()
        self.assertTrue(bpx._unknown_exposure, "幽灵持仓必须被对账发现")
        bpx._unknown_exposure = False


class TestFundingInterval(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.ex = FakeExchange()
        _install(self.ex)

    def test_apy_uses_interval(self):
        """年化按实际结算周期计算（非硬编码小时）"""
        self.ex.funding_rate = (0.001, 3600)
        rate, apy = bpx._funding_rate_info("MON/USDC:USDC")
        self.assertAlmostEqual(apy, 0.001 * 24 * 365 * 100, places=6)
        self.ex.funding_rate = (0.001, 7200)  # 2 小时一次
        _, apy2 = bpx._funding_rate_info("MON/USDC:USDC")
        self.assertAlmostEqual(apy2, 0.001 * 12 * 365 * 100, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
