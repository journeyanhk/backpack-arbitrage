# ⚡ Backpack Arbitrage / Backpack 资金费率套利

> A Backpack funding rate arbitrage trading bot. Opens spot-long + perpetual-short pairs to capture funding rate yield, with maker-first execution and automatic taker fallback.
>
> 一个在 Backpack Exchange 上运行的资金费率套利交易脚本。通过现货做多 + 永续做空的配对持仓，捕获资金费率收益。maker 优先执行，成交后自动 taker 兜底。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Flask](https://img.shields.io/badge/Flask-3.0+-green)
![ccxt](https://img.shields.io/badge/ccxt-4.5+-orange)

> ⚠️ **Important / 重要提示**
>
> This script uses a **spot-long + perpetual-short** delta-neutral arbitrage model, suitable for most **positive funding rate** scenarios. Negative funding rate scenarios are rare and **not monitored or traded** by this bot.
>
> 本脚本基于【买入现货 + 做空合约】的中性套利模式，适应大部分**正资金费率**场景。负资金费率场景较为罕见，**未纳入监测和交易**。

---

## 📖 Table of Contents / 目录

- [Strategy / 策略](#strategy--策略)
- [Getting Started / 快速开始](#getting-started--快速开始)
- [Web Dashboard / 仪表盘](#web-dashboard--仪表盘)
- [Architecture / 架构](#architecture--架构)
- [Risk Notes / 风险提示](#risk-notes--风险提示)
- [Support / 打赏](#support--打赏)
- [License / 许可](#license--许可)

---

## Strategy / 策略

### Core Logic / 核心逻辑

1. **Scan for opportunities** — Fetch all available perpetual funding rates. Filter by minimum APY threshold (default 10%).
2. **Open paired position** — Buy spot + sell perpetual (short) simultaneously:
   - Both legs start as **post-only maker** orders at the best bid/ask.
   - When **one leg fills**, the other immediately switches to **taker** (aggressive fill).
   - Timeout (default 3 min) → cancel remaining, re-quote at latest prices.
   - Max 3 retry cycles before failing.
3. **Capture funding payments** — Hold the delta-neutral position. The short perpetual leg receives funding payments periodically.
4. **Close position** — When ready to exit, close both legs: sell spot + buy perpetual. Both legs as taker orders. Real API positions are used (not memory accounting), ensuring positions are never lost across restarts.

开仓时现货买 + 合约卖两条腿同时 maker 挂单，一条腿成交后另一条改 taker 兜底，超时撤单重挂最多 3 次。平仓以 API 真实持仓为准，重启不丢仓。

### Symbol Selection / 币种选择

Only coins that appear in both the collateral list (available for spot trading) and the perpetual market are eligible. The dashboard sorts by current funding rate APY.

只有同时出现在抵押品列表和永续市场的币种才可选。仪表盘按资金费率年化排序。

---

## Getting Started / 快速开始

### Prerequisites / 前置条件

- **Python 3.10+**
- **A Backpack account** with API credentials
  - [Backpack Exchange](https://backpack.exchange/)
- API Key permissions: `Trade`, `Read`

### Installation / 安装

```bash
# 1. Clone the repo
git clone https://github.com/wepoets1107/backpack-arbitrage.git
cd backpack-arbitrage

# 2. Create virtual environment
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
# .\.venv\Scripts\activate

# 3. Install dependencies
pip install ccxt flask

# 4. Create .env file
echo BPX_PUBLIC_KEY=your_key_here > .env
echo BPX_SECRET_KEY=your_secret_here >> .env
echo BPX_LIVE=0 >> .env
```

### Configuration / 配置

Edit `.env` with your Backpack API credentials:

```bash
# .env — never commit this file!
BPX_PUBLIC_KEY=your_public_key_here
BPX_SECRET_KEY=your_secret_key_here
BPX_LIVE=0       # 0 = dry-run, 1 = live trading
BPX_WEB_USER=admin                 # 网页登录用户名（实盘必须配置）
BPX_WEB_PASSWORD=your_password     # 网页登录密码（实盘必须配置）
# BPX_PROXY=http://127.0.0.1:10808  # 可选：命令行直连交易所被墙时，填本机代理（ccxt 需显式配置）
```

> ⚠️ **Security / 安全（v5.1）**
>
> - 整个面板需要**用户名/密码登录**（`BPX_WEB_USER` / `BPX_WEB_PASSWORD`），未登录无法查看持仓或操作
> - 登录会话为 HttpOnly cookie；登录接口带防爆破（5 次失败锁定 60 秒）
> - 服务只监听 `127.0.0.1`，公网访问请用带 HTTPS 的反向代理（Caddy/Nginx）
> - `BPX_LIVE=1` 但缺少 API key 或登录凭据时直接拒绝启动
> - 平仓只关闭策略自有持仓（SQLite 账本记录），不会触碰人工持仓
>
> ⚠️ **Security**: `.env` is in `.gitignore` — your credentials will never be committed.
>
> ⚠️ **安全**：`.env` 已在 `.gitignore` 中，凭证不会提交到 Git。

### Run / 运行

```bash
python bpx_arb_ccxt.py
```

Open browser → **http://localhost:5055**

Start with `BPX_LIVE=0` (dry-run) to familiarize yourself with the dashboard. Set `BPX_LIVE=1` only when ready for live trading.

先用 `BPX_LIVE=0`（演练模式）熟悉面板，确认无误后再切 `BPX_LIVE=1` 实盘。

---

## Web Dashboard / 仪表盘

Runs at port 5055:

| Feature / 功能 | Description / 说明 |
|---|---|
| **Symbol list** | All eligible coins sorted by funding rate APY |
| **Open/Close/Cancel** | Manual position management with split-order support |
| **Live positions** | Real-time spot + perpetual holdings from API |
| **Active orders** | Currently open maker/taker orders |
| **Balances** | Collateral balances including lend/borrow status |
| **Margin ratio** | Account-level maintenance margin ratio |
| **Operation log** | Persistent log across restarts (from `bpx_arb.log`) |

操作：选币种 → 填金额 → 点开仓（现货买+合约卖同时执行）→ 持仓显示实时状态 → 点平仓一键退出。

---

## Architecture / 架构

```
backpack-arbitrage/
├── bpx_arb_ccxt.py          # Flask server + strategy logic (ccxt-based)
├── bpx_arb_server.py        # Legacy v0.3 (bpx-py SDK, kept for reference)
├── bpx_trader.py            # Legacy spot trader (kept for reference)
├── bpx_stock.py             # Backpack SDK patches (kept for reference)
├── templates/
│   └── bpx_arb.html         # Dashboard HTML (vanilla JS, no frameworks)
├── .env                     # API credentials (gitignored)
├── .gitignore               # Excludes .env, .venv, logs
├── bpx_arb.log              # Persistent operation log (gitignored)
└── README.md
```

### Data Flow / 数据流

```
Backpack Exchange
      ↕ (ccxt REST)
bpx_arb_ccxt.py (Flask + threading)
      ↕ (JSON API + auto-refresh)
bpx_arb.html (Dashboard)
```

### API Endpoints / 接口

| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard page |
| GET | `/api/state` | Current positions, orders, balances, logs, risk flags |
| GET | `/api/symbols` | Eligible coins with funding rates |
| GET | `/api/task/<id>` | Poll async open/close task result |
| POST | `/api/open` | Open a paired position (async, requires auth) |
| POST | `/api/close` | Close the strategy-owned position (async, requires auth) |
| POST | `/api/cancel` | Cancel all open orders for a symbol (requires auth) |

---

## Risk Notes / 风险提示

- **Funding rate can flip** — A positive funding rate today may be zero or negative tomorrow. v5.0 hard-blocks opening when the annualized rate is below the threshold, the rate is zero/negative, or the net yield (after a cost buffer) is not positive.
- **Auto-lend is enabled** — Spot holdings are automatically lent for extra yield. `autoLendRedeem` ensures smooth closing. Borrowing (`autoBorrow`) is only allowed on open-buy legs; closing spot sells can never borrow the base coin.
- **Reduce-only closes** — Perpetual close orders carry `reduceOnly` so repeated/retried closes can never flip a short into a long.
- **Order state machine** — `OrderNotFound` is treated as UNKNOWN (never assumed filled); unknown states freeze the symbol and require manual review.
- **Strategy ledger** — A SQLite ledger (`arb_ledger.db`) records orders, confirmed fills and strategy-owned positions. Only confirmed fill increments move the ledger; closes only touch strategy-owned quantity; a startup reconciliation blocks new opens if unknown exposure is detected.
- **Partial fills** — Any fill on one leg immediately triggers an aggressive chase on the other leg (marketable limit with slippage cap); if the chase fails, the filled leg is rolled back instead of staying exposed.
- **Slippage** — Taker fallback during fast markets may result in worse prices than maker.

- 资金费率随时可能逆转，持续关注；v5.0 费率为负、年化低于门槛或净年化≤0 将硬性拒绝开仓（门槛 MIN_NET_APY=10%，成本缓冲 EST_ROUND_TRIP_COST_APY=5%，可在 .env 同层常量调整）
- 现货自动借出赚息，平仓时自动赎回；仅开仓买入允许借入 USDC，平仓卖单禁止借币
- 永续平仓强制 reduceOnly，防止重复请求翻成反方向仓位
- 订单查不到视为状态未知并冻结币种，绝不伪造成交
- 账本记录订单/成交/策略持仓，重启可恢复；平仓只平策略自有持仓，不动人工持仓
- 部分成交立即追单，追单失败回滚已成交腿，避免长时间裸敞口
- 快速行情下 taker 兜底可能滑点，注意市场波动

---

## Support / 打赏

If this project helps you, consider supporting the community:

如果这个项目对你有帮助，欢迎打赏支持冰火岛社区发展：

```
EVM: 0x29f091DAA3dfee8100645ee24239bCC3ae174B42
```

---

## License / 许可

MIT License. See [LICENSE](LICENSE).

---

*Built for the community by [冰火岛](https://binghuodao.club). Use at your own risk — always test in dry-run mode first.*
*由冰火岛社区开发维护。请自行承担交易风险，务必先以演练模式测试。*
