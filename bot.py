import asyncio, websockets, json, telegram, httpx, sys
import pandas as pd
import pandas_ta as ta
from datetime import datetime

# ==================== CONFIG ====================
TELEGRAM_TOKEN = '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE'
CHAT_ID        = '1950462171'
SYMBOL         = 'SOLUSDT'

# ── Indicator parameters ─────────────────────────────────────
RSI_P  = 40    # RSI length
WMA_P  = 15    # WMA of RSI length

# ── Timeframe ────────────────────────────────────────────────
# FIX (live repainting): WebSocket stream AND indicator fetch
# must use the SAME timeframe.
# @kline_5m fires ONCE per 5M candle close — identical to TradingView.
# @kline_1m was firing 5× per 5M candle, reading a forming candle
# each time → crossover appeared/disappeared = live repainting.
INTERVAL  = '5m'
WS_STREAM = 'kline_5m'

# ── Fetch limit ──────────────────────────────────────────────
# FIX (RSI convergence): RSI(20) needs ~200+ candles to stabilize.
# 500 gives clean convergence matching TradingView values.
# With limit=100 RSI is still in initialization zone → wrong values.
FETCH_LIMIT = 500

# ==================== STAGE CONFIG ====================
STAGE1_R        = 1.5   # trail SL only, no exit
STAGE2_R        = 2.2   # exit 50%, trail SL
STAGE3_R        = 3.0   # exit remaining 50%, close trade
STAGE1_SL_TRAIL = 0.8   # SL → +0.8R after Stage 1
STAGE2_SL_TRAIL = 1.5   # SL → +1.5R after Stage 2

# ==================== STATS ====================
stats = {
    "balance"      : 00,
    "risk_percent" : 0.02,

    # outcome counters
    "total_trades" : 00,
    "win_s3"       : 00,    # full target (Stage 3 hit)
    "win_partial"  : 00,    # net positive but SL hit before Stage 3
    "loss_sl"      : 00,   # SL hit before any stage exit

    # stage reached counters (independent)
    "reached_s1"   : 0,
    "reached_s2"   : 0,
    "reached_s3"   : 0,

    # points (price distance, not USDT)
    "sl_points"    : 0.0,   # cumulative distance lost on SL trades
    "tp_points"    : 0.0,   # cumulative distance captured on exits
}

active_trade = None
http_client  = httpx.AsyncClient()

# ── Locks ────────────────────────────────────────────────────
# entry_lock  : prevents two simultaneous candle closes both
#               seeing active_trade=None and opening two trades
# closing_lock: prevents two simultaneous ticks both triggering
#               close_trade() at the same moment
entry_lock   = asyncio.Lock()
closing_lock = asyncio.Lock()

# ==================== LOGGER ====================

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(tag, msg):
    print(f"[{ts()}] [{tag:<7}] {msg}")

# ==================== INDICATORS ====================

async def fetch_indicators():
    """
    Three fixes applied here:

    FIX 1 — Timeframe match (live repainting root cause):
      INTERVAL='5m' matches the WebSocket stream @kline_5m.
      Signal now only evaluates on confirmed 5M candle close,
      identical to TradingView behaviour.

    FIX 2 — Correct candle index (forming candle repainting):
      iloc[-2] = just-closed confirmed candle  ← use this
      iloc[-3] = candle before that            ← use this
      iloc[-1] = new forming candle            ← NEVER use this
      After candle close event + HTTP round-trip delay, Binance
      REST already has a new forming candle at iloc[-1].

    FIX 3 — Sufficient warmup history (RSI convergence):
      FETCH_LIMIT=500 → RSI(20) converges after ~200 candles.
      With limit=100 values diverge from TradingView by 1-3 points.
    """
    try:
        url    = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': INTERVAL, 'limit': FETCH_LIMIT}

        log("INDIC", f"Fetching {SYMBOL} {INTERVAL} | limit={FETCH_LIMIT}")
        resp = await http_client.get(url, params=params)
        data = resp.json()
        log("INDIC", f"Received {len(data)} candles")

        df          = pd.DataFrame(data, columns=['ts','o','h','l','c','v','ts_e','q','n','tb','tq','i'])
        df['close'] = df['c'].astype(float)

        rsi = ta.rsi(df['close'], length=RSI_P)
        wma = ta.wma(rsi,         length=WMA_P)

        # Print last 3 confirmed candles — compare these with TradingView
        log("INDIC", "─── Last 3 confirmed candles (compare with TradingView) ───")
        for idx in [-4, -3, -2]:
            ct  = datetime.utcfromtimestamp(int(df['ts'].iloc[idx]) / 1000).strftime("%H:%M UTC")
            log("INDIC", f"  {ct} | close={float(df['c'].iloc[idx]):.4f} | RSI={rsi.iloc[idx]:.4f} | WMA={wma.iloc[idx]:.4f}")

        curr_rsi = rsi.iloc[-2]   # just-closed candle
        curr_wma = wma.iloc[-2]
        prev_rsi = rsi.iloc[-3]   # candle before that
        prev_wma = wma.iloc[-3]

        log("INDIC", "─── Crossover check ───")
        log("INDIC", f"  PREV: RSI={prev_rsi:.4f} WMA={prev_wma:.4f} | RSI<=WMA? {prev_rsi <= prev_wma}")
        log("INDIC", f"  CURR: RSI={curr_rsi:.4f} WMA={curr_wma:.4f} | RSI>WMA?  {curr_rsi > curr_wma}")
        log("INDIC", f"  CROSSOVER = {(prev_rsi <= prev_wma) and (curr_rsi > curr_wma)}")

        return curr_rsi, curr_wma, prev_rsi, prev_wma

    except Exception as e:
        log("ERROR", f"fetch_indicators failed: {e}")
        return None, None, None, None

# ==================== TELEGRAM ====================

async def tg(bot, msg):
    await bot.send_message(CHAT_ID, msg, parse_mode='Markdown')

def _stats_footer():
    t  = stats['total_trades']
    w3 = stats['win_s3']
    wp = stats['win_partial']
    ls = stats['loss_sl']
    wr = ((w3 + wp) / t * 100) if t > 0 else 0
    return (
        f"\n━━━━━━━━━━━━━━━━━━\n"
        f"📊 *SESSION STATS*\n"
        f"├ 🎯 Full Win (S3): `{w3}`\n"
        f"├ 🔶 Partial Win:   `{wp}`\n"
        f"├ 🛑 SL Loss:       `{ls}`\n"
        f"├ 📈 Win Rate:      `{wr:.1f}%`\n"
        f"├ 🏦 Balance:       `${stats['balance']:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔢 *STAGE COUNTS*\n"
        f"├ S1 Reached: `{stats['reached_s1']}`\n"
        f"├ S2 Reached: `{stats['reached_s2']}`\n"
        f"└ S3 Reached: `{stats['reached_s3']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📐 *POINTS*\n"
        f"├ SL pts lost: `{stats['sl_points']:.4f}`\n"
        f"└ TP pts won:  `{stats['tp_points']:.4f}`"
    )

# ==================== TRADE ENGINE ====================

async def monitor_trade(price, bot):
    """
    Runs on every WebSocket tick while a trade is open.
    s1/s2 boolean flags guarantee each stage block fires exactly once.
    closing_lock guarantees close_trade() is called at most once.
    """
    global active_trade, stats

    if not active_trade:
        return
    if active_trade.get('closing'):
        return

    entry      = active_trade['entry']
    initial_sl = active_trade['initial_sl']
    risk_dist  = entry - initial_sl
    if risk_dist <= 0:
        return

    rr = (price - entry) / risk_dist

    # ── STAGE 1: 1.5R → trail SL to +0.8R, no position exit ──
    if not active_trade['s1'] and rr >= STAGE1_R:
        active_trade['s1'] = True
        active_trade['sl'] = entry + (risk_dist * STAGE1_SL_TRAIL)
        stats['reached_s1'] += 1
        log("TRADE", f"STAGE 1 | price={price:.4f} | rr={rr:.2f}R | new_sl={active_trade['sl']:.4f}")

        await tg(bot,
            f"🟢 *STAGE 1 HIT — {SYMBOL}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📍 Price:       `${price:.4f}`\n"
            f"🎯 Target hit:  `1.5R`\n"
            f"🛡️ SL trailed:  `+0.8R → ${active_trade['sl']:.4f}`\n"
            f"📦 Position:    `100% still open`\n"
            f"ℹ️ _Waiting for Stage 2 at 2.2R..._"
        )

    # ── STAGE 2: 2.2R → exit 50%, trail SL to +1.5R ──────────
    elif not active_trade['s2'] and rr >= STAGE2_R:
        active_trade['s2'] = True
        active_trade['sl'] = entry + (risk_dist * STAGE2_SL_TRAIL)

        realized                      = (active_trade['risk_usd'] * 0.5) * rr
        active_trade['realized_pnl']  = realized
        active_trade['s2_exit_price'] = price

        stats['balance']    += realized
        stats['tp_points']  += (price - entry) * 0.5
        stats['reached_s2'] += 1
        log("TRADE", f"STAGE 2 | price={price:.4f} | rr={rr:.2f}R | realized={realized:.2f} USDT | new_sl={active_trade['sl']:.4f}")

        await tg(bot,
            f"💰 *STAGE 2 HIT — {SYMBOL}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📍 Price:        `${price:.4f}`\n"
            f"🎯 Target hit:   `2.2R`\n"
            f"📤 Exited:       `50% of position`\n"
            f"💵 Realized PnL: `+{realized:.2f} USDT`\n"
            f"📐 Points:       `+{price - entry:.4f}`\n"
            f"🛡️ SL trailed:   `+1.5R → ${active_trade['sl']:.4f}`\n"
            f"📦 Remaining:    `50% still open`\n"
            f"ℹ️ _Waiting for Stage 3 at 3.0R..._"
        )

    # ── EXIT CONDITIONS ────────────────────────────────────────
    if rr >= STAGE3_R:
        async with closing_lock:
            if active_trade and not active_trade.get('closing'):
                active_trade['closing'] = True
                log("TRADE", f"STAGE 3 — closing | price={price:.4f} | rr={rr:.2f}R")
                await close_trade(price, "S3_TARGET", bot)

    elif price <= active_trade['sl']:
        async with closing_lock:
            if active_trade and not active_trade.get('closing'):
                active_trade['closing'] = True
                if active_trade['s2']:
                    reason = "SL_AFTER_S2"
                elif active_trade['s1']:
                    reason = "SL_AFTER_S1"
                else:
                    reason = "SL_PURE"
                log("TRADE", f"SL HIT — {reason} | price={price:.4f} | sl={active_trade['sl']:.4f} | rr={rr:.2f}R")
                await close_trade(price, reason, bot)


async def close_trade(exit_price, reason, bot):
    """
    All stat mutations happen synchronously before any await.
    active_trade = None is set BEFORE await tg() so any new candle
    close arriving during the Telegram send correctly sees no open trade.
    """
    global active_trade, stats

    entry          = active_trade['entry']
    initial_sl     = active_trade['initial_sl']
    risk_dist      = entry - initial_sl
    rr             = (exit_price - entry) / risk_dist
    remaining_mult = 0.5 if active_trade['s2'] else 1.0
    remaining_pnl  = (active_trade['risk_usd'] * remaining_mult) * rr
    total_pnl      = remaining_pnl + active_trade.get('realized_pnl', 0)

    # ── All mutations before any await ────────────────────────
    stats['balance']      += remaining_pnl
    stats['total_trades'] += 1

    if reason == "S3_TARGET":
        stats['win_s3']      += 1
        stats['reached_s3']  += 1
        stats['tp_points']   += (exit_price - entry) * 0.5
        outcome_emoji = "🎯"
        outcome_label = "FULL WIN — Stage 3 Target"

    elif reason == "SL_AFTER_S2":
        stats['win_partial'] += 1
        stats['sl_points']   += (entry - exit_price) * 0.5
        outcome_emoji = "🔶"
        outcome_label = "PARTIAL WIN — SL after Stage 2"

    elif reason == "SL_AFTER_S1":
        if total_pnl > 0:
            stats['win_partial'] += 1
            outcome_emoji = "🔶"
            outcome_label = "PARTIAL WIN — SL after Stage 1 (+0.8R)"
        else:
            stats['loss_sl']   += 1
            outcome_emoji = "🛑"
            outcome_label = "LOSS — SL after Stage 1"
        stats['sl_points'] += abs(entry - exit_price)

    else:  # SL_PURE
        stats['loss_sl']   += 1
        stats['sl_points'] += risk_dist
        outcome_emoji = "🛑"
        outcome_label = "LOSS — Initial SL Hit"

    log("TRADE", f"CLOSED — {outcome_label} | exit={exit_price:.4f} | rr={rr:.2f}R | pnl={total_pnl:+.2f} | balance={stats['balance']:.2f}")

    # ── Nullify BEFORE await ───────────────────────────────────
    active_trade = None

    pnl_sign = "+" if total_pnl >= 0 else ""
    await tg(bot,
        f"{outcome_emoji} *TRADE CLOSED — {outcome_label}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 Entry:        `${entry:.4f}`\n"
        f"🚪 Exit:         `${exit_price:.4f}`\n"
        f"📐 Points:       `{exit_price - entry:+.4f}`\n"
        f"📊 R achieved:   `{rr:.2f}R`\n"
        f"💵 Total PnL:    `{pnl_sign}{total_pnl:.2f} USDT`\n"
        f"🏦 New Balance:  `${stats['balance']:.2f}`"
        + _stats_footer()
    )

# ==================== ENTRY HANDLER ====================

async def handle_candle_close(price, closed_low, bot):
    """
    Called only on confirmed 5M candle close (data['k']['x'] = True).
    entry_lock prevents two simultaneous close events both opening a trade.

    FIX (candle low source):
      closed_low comes from data['k']['l'] in the WebSocket event —
      the low of the candle that JUST closed, final and accurate.
      Previously a separate REST call with limit=1 was used, which
      returned the FORMING candle's low (wrong candle, wrong value).
    """
    global active_trade

    log("CANDLE", f"5M candle closed | close={price:.4f} | low={closed_low:.4f}")

    async with entry_lock:
        log("LOCK", "entry_lock acquired")

        if active_trade:
            log("SIGNAL", f"SKIP — trade already open | entry={active_trade['entry']:.4f}")
            log("LOCK", "entry_lock released")
            return

        rsi, wma, prsi, pwma = await fetch_indicators()

        # FIX (None check): `rsi is not None` not `if rsi`
        # `if rsi` silently skips a valid signal when RSI == 0.0
        # because 0.0 is falsy in Python. `is not None` checks
        # only for fetch failure.
        if rsi is None:
            log("SIGNAL", "SKIP — indicator fetch returned None (API error)")
            log("LOCK", "entry_lock released")
            return

        # Crossover: RSI crossed UP through WMA on the just-closed candle
        # prev candle: RSI was below or equal to WMA  (prsi <= pwma)
        # curr candle: RSI is now above WMA            (rsi  >  wma)
        crossover = (prsi <= pwma) and (rsi > wma)

        log("SIGNAL", f"RSI={rsi:.4f} WMA={wma:.4f} | prevRSI={prsi:.4f} prevWMA={pwma:.4f}")
        log("SIGNAL", f"prsi<=pwma: {prsi <= pwma} | rsi>wma: {rsi > wma} | CROSSOVER: {crossover}")

        if not crossover:
            log("SIGNAL", "SKIP — no crossover this candle")
            log("LOCK", "entry_lock released")
            return

        low_val   = closed_low * 0.9995   # tiny buffer below candle low
        risk_dist = price - low_val

        if risk_dist <= 0:
            log("SIGNAL", f"SKIP — invalid risk distance ({risk_dist:.6f})")
            log("LOCK", "entry_lock released")
            return

        active_trade = {
            'entry'        : price,
            'initial_sl'   : low_val,
            'sl'           : low_val,
            'risk_usd'     : stats['balance'] * stats['risk_percent'],
            's1'           : False,
            's2'           : False,
            'closing'      : False,
            'realized_pnl' : 0.0,
            's2_exit_price': None,
        }

        log("TRADE", f"TRADE OPENED | entry={price:.4f} | sl={low_val:.4f} | risk={active_trade['risk_usd']:.2f} USDT")
        log("LOCK", "entry_lock released")

    # Send alert OUTSIDE lock so we don't hold it during network I/O
    await tg(bot,
        f"🚀 *LONG SIGNAL — {SYMBOL}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 Entry:       `${price:.4f}`\n"
        f"🛑 Stop Loss:   `${low_val:.4f}`\n"
        f"📐 Risk (pts):  `{risk_dist:.4f}`\n"
        f"💵 Risk (USDT): `${active_trade['risk_usd']:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Stage 1:     `${price + risk_dist * STAGE1_R:.4f}` (1.5R — SL trail)\n"
        f"🎯 Stage 2:     `${price + risk_dist * STAGE2_R:.4f}` (2.2R — exit 50%)\n"
        f"🎯 Stage 3:     `${price + risk_dist * STAGE3_R:.4f}` (3.0R — exit 50%)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 RSI: `{rsi:.2f}` | WMA: `{wma:.2f}`"
    )

# ==================== MAIN LOOP ====================

async def main():
    async with telegram.Bot(TELEGRAM_TOKEN) as bot:
        ws_url = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@{WS_STREAM}"

        # Outer loop handles reconnection automatically
        while True:
            try:
                log("WS", f"Connecting to {ws_url}...")
                async with websockets.connect(ws_url) as ws:
                    log("WS", f"Connected. Monitoring {SYMBOL} on {INTERVAL} timeframe.")
                    await tg(bot,
                        f"🤖 *Bot Started*\n"
                        f"├ Symbol:    `{SYMBOL}`\n"
                        f"├ Timeframe: `{INTERVAL}`\n"
                        f"├ RSI:       `{RSI_P}` | WMA: `{WMA_P}`\n"
                        f"└ Balance:   `${stats['balance']:.2f}`"
                    )

                    while True:
                        raw  = await ws.recv()
                        data = json.loads(raw)

                        if 'k' not in data:
                            continue

                        price    = float(data['k']['c'])
                        is_close = data['k']['x']

                        # Monitor open trade on every tick
                        if active_trade:
                            await monitor_trade(price, bot)

                        # Only act on confirmed 5M candle close
                        if is_close:
                            closed_low = float(data['k']['l'])
                            await handle_candle_close(price, closed_low, bot)

            except websockets.ConnectionClosed as e:
                log("WS", f"Connection closed: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                log("ERROR", f"Unexpected error: {e}. Reconnecting in 10s...")
                await asyncio.sleep(10)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("WS", "Bot stopped by user.")
        sys.exit()
