# Yosi Bingo — Setup

## What changed in THIS pass (card selection + requested fixes)
- **Pick-your-own-card**: players select a specific card by number (1..200, one per physical
  card in `cards_pool.json`) instead of getting a random one. Selecting instantly debits the
  lobby stake; deselecting instantly refunds it. Both are allowed freely until the game
  actually starts (waiting/countdown only — locked the moment it goes "running").
- **Max 2 cards per player**, and the 2nd card can only be picked after the 1st is already held.
- **Shared visibility**: every card's taken/free status is visible to EVERY player in the lobby —
  including players with zero balance. Taken cards render red; your own render gold. Balance only
  gates the *select* action, never the *view*.
- **Live sync**: card picks/releases broadcast over Socket.IO (`cards_changed`) so every open
  browser sees the grid update in real time, matching the server exactly.
- **State persistence on re-entry**: `get_state()` is always derived fresh from the server, so
  leaving the live screen and coming back re-renders your exact cards + the exact draw history —
  nothing is cached client-side that could go stale.
- **Leaderboard**: now has Daily / Weekly / Monthly / All-time tabs (`/api/leaderboard?period=`).
- **Deposit form**: the reference field now reads "Send the CBE Birr or Telebirr full text here"
  instead of "Transaction Reference / Screenshot ID".
- **Bot count hidden from players**: the player-facing game screen only ever shows one combined
  "Players" number — never a bots/real breakdown. That breakdown still exists for admins only
  (Admin → Live Games).
- **Bot name editor**: Admin → Bots → "Bot Names" lets you edit the pool of display names bots
  are randomly given. As before, there is deliberately no bot win-probability control.
- **Admin timestamps**: Live Games now shows Created/Started time for every game; the Players
  panel has a "View" button showing full transaction history and win history with timestamps.
- **Card grid is 1–200** (matches the full card pool), separate from the 1–75 ball-call board.

## What changed in the previous refinement
- **Admin dashboard** at `/admin.html` — the control center (see below)
- **Color palette** — navy (#02167f) + gold (#ffd014) + white, applied across both the player app and admin panel
- **Commission**: configurable %, defaults to 20% (2 ETB per 10 ETB bet) as you specified
- **Signup bonus**: 10 ETB, one-time, tracked separately so it can never be withdrawn (only deposited money can be)
- **Win patterns**: horizontal, vertical, diagonal, four corners — all toggleable per-lobby from admin settings
- **Real-time sync**: Socket.IO pushes draws/countdown/winners to every connected player instantly (falls back to 3s polling if a socket drops)
- **Number caller**: on-screen colored ball (B/I/N/G/O each has its own color) + optional voice-over using the browser's built-in text-to-speech
- **Auto/manual mark toggle**: cosmetic only — win detection always runs server-side off the true call history, so this setting can never cost anyone a win
- **Anti-cheat**: flags when the same device or the same IP is linked to multiple accounts (view under Admin → Anti-cheat)
- **Language setting**: English / Amharic toggle in Settings
- **Seat-filler bots**: admin-controlled count per lobby, house-funded, disclosed with a 🤖 label, and *never eligible to win the pot* — see note below

## Persistent storage (do this FIRST — free, no card required)

Without this, every restart/redeploy/idle-sleep on Render wipes your wallets,
transactions, and users, because Render's free tier has no durable local
disk. The fix costs nothing: point the app at a free **MongoDB Atlas**
database instead of the local disk. Atlas's free M0 tier (512MB) never
expires and isn't tied to Render at all, so it survives every restart.

**Setup (about 5 minutes):**
1. Go to https://www.mongodb.com/cloud/atlas/register and create a free account
2. Create a free **M0 cluster** (pick any nearby region)
3. Database Access → add a database user (username + password)
4. Network Access → Add IP Address → **Allow Access from Anywhere** (`0.0.0.0/0`) — needed since Render's outbound IP isn't fixed on the free tier
5. Click **Connect** → **Drivers** → copy the connection string, it looks like:
   `mongodb+srv://youruser:yourpassword@cluster0.xxxxx.mongodb.net/`
6. In Render → your service → **Environment** → add:
   - Key: `MONGODB_URI`
   - Value: the connection string from step 5 (fill in your real password)
7. Redeploy. That's it — `data.py` detects `MONGODB_URI` automatically and
   switches from the local file to Atlas. No other code changes needed.

**How to confirm it worked:** deposit a small test amount, then manually
restart the Render service (Manual Deploy → Deploy latest commit, or just
wait for it to idle out and wake back up). Check the wallet balance is
still there. If `MONGODB_URI` isn't set, the app quietly falls back to the
local `data.json` file — fine for testing on your own laptop, but **not**
durable on Render.

## Environment variables (set these in Render)
| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `WEBAPP_URL` | Your Render URL, e.g. `https://yosi-bingo.onrender.com` |
| `ADMIN_ID` | Your personal Telegram numeric user ID (for deposit/withdrawal alerts) |
| `ADMIN_PASSWORD` | Password to log into `/admin.html` — **change this from the default** |
| `SECRET_KEY` | Any random string, used for session signing |
| `MONGODB_URI` | **Strongly recommended** — see "Persistent storage" above. Without it, data is lost on every restart. |

## Render start command
```
python3 server.py
```

## Accessing the admin dashboard
Go to `https://YOUR-RENDER-URL/admin.html`, enter `ADMIN_PASSWORD`. From there you can:
- Approve/reject deposits & withdrawals (also still works via Telegram `/approve`, `/reject`, `/approvew`, `/pending`)
- Pause, resume, force-draw, or end any live game
- Ban/unban players, adjust balances directly
- Change commission %, bonus amount, countdown/draw timing, and which win patterns count
- Review anti-cheat flags
- Broadcast a message to every player via Telegram
- Turn seat-filler bots on/off and set how many join each lobby

## On the bots
You asked for bots you could control, including their win probability. I built the seat-filling part (they help
quiet lobbies reach the minimum player count) but left out win-probability control — letting the house dial in
which players' bots "win" real money games is rigging outcomes against paying players, which I won't build no
matter how it's framed. Bots here are purely cosmetic competition: they never pay in, and they can never be
declared the winner, so they have zero effect on real players' odds or payouts.

## Suggestions for "professional" polish beyond this pass
- **Provably-fair draw seed**: publish a hashed seed before each game and reveal it after, so players can verify the draw wasn't tampered with
- **KYC threshold**: require ID verification above a withdrawal amount (common in ETB gambling apps to reduce fraud/chargebacks)
- **Self-exclusion / spending limits**: let players cap their own daily deposit or set a cool-off period — reduces problem gambling and is often a legal requirement
- **Rate limiting on deposit/withdraw endpoints** to stop automated abuse
- **Automated deposit verification** (e.g. Telebirr/CBE API or SMS parsing) instead of manual reference-number review, once you have volume
- **Multi-admin roles** (e.g. a support-only admin who can't change commission settings)
- **Structured logging + error alerting** (Sentry or similar) so you hear about crashes before players do
