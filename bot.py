import asyncio, websockets, json, telegram, httpx, sys, logging
import pandas as pd
import pandas_ta as ta

# ==================== CONFIG & LOGGING ====================
# REMINDER: Revoke your token via @BotFather and use environment variables for safety!
TELEGRAM_TOKEN = '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE'
CHAT_ID = '1950462171'
SYMBOL = 'SOLUSDT'

RSI_P, WMA_P = 20, 13

stats = {
    "balance": 63, 
    "risk_percent": 0.02, 
    "total_trades": 245,
    "wins_final": 21, 
    "wins_trailed": 67, 
    "losses": 157
}

active_trade = None
http_client = httpx.AsyncClient()

# ==================== DATA & SIGNAL ====================

async def fetch_indicators():
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': '5m', 'limit': 100}
        resp = await http_client.get(url, params=params)
        data = resp.json()
        
        df = pd.DataFrame(data, columns=['ts','o','h','l','c','v','ts_e','q','n','tb','tq','i'])
        df['close'] = df['close'].astype(float)
        
        rsi = ta.rsi(df['close'], length=RSI_P)
        wma = ta.wma(rsi, length=WMA_P)
        
        return rsi.iloc[-1], wma.iloc[-1], rsi.iloc[-2], wma.iloc[-2]
    except Exception as e:
        print(f"Error fetching indicators: {e}")
        return None, None, None, None

# ==================== ENGINE (STAGES) ====================

async def monitor_trade(price, bot):
    global active_trade, stats
    if not active_trade: return

    # Calculate R-Multiple
    # R = (Current Price - Entry) / (Entry - Initial Stop Loss)
    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    if risk_dist <= 0: return # Prevent division by zero
    rr = (price - active_trade['entry']) / risk_dist

    # --- STAGE 1: LOCK PROFIT (Trigger: 1.5R) ---
    # Moves SL into profit to guarantee a win.
    if not active_trade['s1'] and rr >= 1.5:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.8)
        active_trade['s1'] = True
        await bot.send_message(CHAT_ID, "🟢 *STAGE 1: PROFIT LOCKED*\nProgress: ▓▓▓▓▓░░░░░ 50%\n└ SL moved to +0.8R (Guaranteed Win)", parse_mode='Markdown')

    # --- STAGE 2: PARTIAL EXIT (Trigger: 2.2R) ---
    # Sells 50% of the position and trails remaining SL tighter.
    elif not active_trade['s2'] and rr >= 2.2:
        active_trade['s2'] = True
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 1.5)
        
        # Realize 50% profit based on current RR
        realized = (active_trade['risk_usd'] * 0.5) * rr
        active_trade['realized_pnl'] = realized
        stats['balance'] += realized
        
        await bot.send_message(CHAT_ID, f"💰 *STAGE 2: 50% EXIT*\nProgress: ▓▓▓▓▓▓▓░░░ 75%\n└ Realized: `+{realized:.2f} USDT`\n└ SL Trailed to +1.5R", parse_mode='Markdown')

    # --- STAGE 3 / FINAL EXIT CONDITIONS ---
    if rr >= 3.0:
        await close_trade(price, "🎯 TARGET HIT (3.0R)", bot)
    elif price <= active_trade['sl']:
        reason = "🛡️ TRAILED SL HIT" if active_trade['s1'] else "🛑 INITIAL SL HIT"
        await close_trade(price, reason, bot)

async def close_trade(exit_price, reason, bot):
    global active_trade, stats
    
    # If Stage 2 was hit, only 50% of the position remains to be closed
    mult = 0.5 if active_trade['s2'] else 1.0
    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    
    # Calculate PnL for the remaining portion
    pnl = (active_trade['risk_usd'] * mult) * ((exit_price - active_trade['entry']) / risk_dist)
    total_pnl = pnl + active_trade.get('realized_pnl', 0)
    
    stats['balance'] += pnl
    stats['total_trades'] += 1
    
    if "TARGET" in reason: 
        stats['wins_final'] += 1
    elif total_pnl > 0: 
        stats['wins_trailed'] += 1
    else: 
        stats['losses'] += 1

    msg = (f"🏁 *TRADE CLOSED: {reason}*\n━━━━━━━━━━━━━━━━━━\n"
           f"💵 *Total PnL:* `+{total_pnl:.2f} USDT`\n"
           f"🏦 *New Balance:* `${stats['balance']:.2f}`\n\n"
           f"📊 *Stats:* 🎯 {stats['wins_final']} | 🛡️ {stats['wins_trailed']} | 🛑 {stats['losses']}\n"
           f"📈 *Win Rate:* `{( (stats['wins_final']+stats['wins_trailed'])/stats['total_trades'] )*100:.1f}%`")
    
    await bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
    active_trade = None

# ==================== MAIN LOOP ====================

async def main():
    global active_trade
    async with telegram.Bot(TELEGRAM_TOKEN) as bot:
        # Connect to 1m stream for price updates, but trade on 5m candle closes
        url = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
        async with websockets.connect(url) as ws:
            print(f"Bot Started. Monitoring {SYMBOL}...")
            while True:
                raw_data = await ws.recv()
                data = json.loads(raw_data)
                
                if 'k' in data:
                    price = float(data['k']['c'])
                    
                    # 1. Monitor active trades on every tick
                    if active_trade: 
                        await monitor_trade(price, bot)
                    
                    # 2. Check for new signals only on Candle Close
                    if data['k']['x']: 
                        rsi, wma, prsi, pwma = await fetch_indicators()
                        
                        if rsi and not active_trade and prsi <= pwma and rsi > wma:
                            # Use 5m Low for SL calculation
                            api_res = await http_client.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=5m&limit=1")
                            low_val = float(api_res.json()[0][3]) * 0.9995
                            
                            active_trade = {
                                'entry': price, 
                                'initial_sl': low_val, 
                                'sl': low_val, 
                                'risk_usd': stats['balance'] * stats['risk_percent'],
                                's1': False, 
                                's2': False, 
                                'realized_pnl': 0
                            }
                            
                            await bot.send_message(CHAT_ID, f"🚀 *LONG SIGNAL: {SYMBOL}*\n💰 Entry: `${price:.2f}`\n🛑 Stop: `${low_val:.2f}`", parse_mode='Markdown')

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit()
