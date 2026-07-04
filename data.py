"""
Persistent data layer. Stores everything as ONE big dict, either in:
  - MongoDB Atlas (recommended - free forever, survives every Render
    restart/redeploy/sleep since it's a separate service, not local disk),
    used automatically when MONGODB_URI is set, or
  - a local data.json file (fallback for local dev - NOT durable on Render's
    free tier, since the local disk is wiped on every restart).
Thread-safe via a single RLock. Rest of the codebase is unaffected either
way - it only ever calls load() / save(d).
"""
import json, os, time, threading, uuid, secrets, hashlib
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")
_lock = threading.RLock()

# --- MongoDB Atlas backend (free forever, no card required - see README
#     "Persistent storage" for the 5-minute setup). Set MONGODB_URI on
#     Render and this kicks in automatically; nothing else to configure. ---
MONGODB_URI = os.environ.get("MONGODB_URI")
_mongo_collection = None
_mongo_last_error = None
_MONGO_DOC_ID = "app_state"
if MONGODB_URI:
    # IMPORTANT: constructing MongoClient with a mongodb+srv:// URI does an
    # EAGER DNS lookup immediately, before any query is ever made. If that
    # lookup fails - even transiently, e.g. right after creating a new
    # Atlas cluster or right after adding a network access rule, both of
    # which can take a minute or two to fully propagate - this would
    # otherwise crash the entire app at import time (the whole server
    # fails to start, not just the database). Wrapping it means a slow/
    # unlucky DNS moment degrades to local storage instead of taking down
    # the whole app; it'll pick Mongo back up automatically next request
    # once the connection actually succeeds, since we don't cache this
    # failure - every load()/save() call tries Mongo fresh.
    try:
        import pymongo
        _mongo_client = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=4000, connectTimeoutMS=4000)
        try:
            _mongo_db = _mongo_client.get_default_database()
            if _mongo_db is None:
                raise Exception("no default db in URI")
        except Exception:
            _mongo_db = _mongo_client["yosi_bingo"]
        _mongo_collection = _mongo_db["app_state"]
    except Exception as e:
        _mongo_last_error = str(e)
        print(f"[data.py] MongoDB client setup failed at startup, running on local file "
              f"until a request succeeds: {e}")

LOBBY_CONFIGS = [
    {"bet": 10,  "label": "10 ETB",  "jackpot_target": 500, "bonus": False},
    {"bet": 20,  "label": "20 ETB",  "jackpot_target": 500, "bonus": True},
    {"bet": 50,  "label": "50 ETB",  "jackpot_target": 500, "bonus": False},
    {"bet": 80,  "label": "80 ETB",  "jackpot_target": 500, "bonus": False},
    {"bet": 100, "label": "100 ETB", "jackpot_target": 500, "bonus": True},
    {"bet": 150, "label": "150 ETB", "jackpot_target": 500, "bonus": False},
    {"bet": 300, "label": "300 ETB", "jackpot_target": 500, "bonus": True},
]

DEFAULT_SETTINGS = {
    "commission_percent": 20,     # house cut of the total pot per game (e.g. 20 -> 2 ETB out of every 10 ETB bet)
    "jackpot_percent": 5,         # separate slice that feeds the progressive jackpot
    "signup_bonus": 10,           # ETB given once to every new player, non-withdrawable
    "min_players": 2,
    "countdown_seconds": 30,
    "draw_interval_seconds": 5,
    "win_patterns": ["row", "column", "diagonal", "corners"],  # which patterns count as a win
    "deposit_number": "0936414865",
    "deposit_name": "Amanuel Abiy",
    "maintenance_mode": False,
    "maintenance_message": "We're doing quick maintenance. Back soon!",
    "bots_enabled": True,   # master switch for lobby-filler bots (see bot_counts below)
    "big_win_threshold": 300,  # ETB - a single win at/above this fires a Telegram alert to admin
}


def _default():
    return {
        "players": {},
        "deposit_requests": [],
        "withdraw_requests": [],
        "jackpots": {str(c["bet"]): 0 for c in LOBBY_CONFIGS},
        "jackpot_armed": {str(c["bet"]): False for c in LOBBY_CONFIGS},
        "games": {},
        "lobby_waiting": {str(c["bet"]): None for c in LOBBY_CONFIGS},
        "settings": dict(DEFAULT_SETTINGS),
        "commission_ledger": [],   # per-game commission records for reporting
        "anticheat": {
            "device_map": {},      # device_id -> [user_ids]
            "ip_map": {},          # ip -> [user_ids]
            "flags": [],           # list of flag records
        },
        "admin_sessions": {},      # token -> expiry_ts
        "broadcasts": [],
        "banned": {},               # user_id -> reason
        # Per-lobby count of "seat filler" bots. Bots exist ONLY to help a
        # lobby reach min_players faster - they never cost real players
        # anything and are never eligible to win the pot (see engine.py).
        # They are always labeled "Bot" in the UI so nobody is misled.
        "bot_counts": {str(c["bet"]): 0 for c in LOBBY_CONFIGS},
        # Editable via Admin -> Bots. Only the display NAME is editable -
        # there is no win-probability control (see engine.py / README).
        "bot_names": list(DEFAULT_BOT_NAMES),
        # Every finished game, kept separately from the live in-memory
        # engine.GameRoom objects so it survives restarts and can be
        # browsed/searched in the admin panel without touching live games.
        "game_history": [],
        # user_id -> last-seen unix timestamp, updated on basically every
        # API call the player's browser makes. Powers "online now" +
        # "active today" in the admin dashboard.
        "last_seen": {},
        # Every admin action (approve/reject, balance adjustment, ban/unban,
        # settings change...) - who did it, when, from what IP.
        "admin_audit_log": [],
    }


DEFAULT_BOT_NAMES = ["Abebe", "Kebede", "Selam", "Meron", "Yared", "Liya", "Dawit", "Hana",
                     "Nardos", "Bereket", "Sara", "Mekdes", "Yonas", "Ruth", "Solomon", "Tigist",
                     "Henok", "Betelhem", "Natnael", "Eden"]


_mongo_next_retry = 0  # unix timestamp - don't retry a failed Mongo connection on every single request


def _ensure_mongo():
    """Lazily (re)establish the Mongo connection if it's not up yet, with a
    30s cooldown between attempts. This means a transient DNS/network hiccup
    at startup (very common right after creating an Atlas cluster or adding
    a network access rule) heals itself automatically within the same
    running process, instead of being stuck on local storage until the next
    restart."""
    global _mongo_collection, _mongo_last_error, _mongo_next_retry
    if _mongo_collection is not None or not MONGODB_URI:
        return
    if time.time() < _mongo_next_retry:
        return
    _mongo_next_retry = time.time() + 30
    try:
        import pymongo
        client = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=4000, connectTimeoutMS=4000)
        try:
            mdb = client.get_default_database()
            if mdb is None:
                raise Exception("no default db in URI")
        except Exception:
            mdb = client["yosi_bingo"]
        coll = mdb["app_state"]
        coll.find_one({"_id": _MONGO_DOC_ID})  # force the connection to prove itself now
        _mongo_collection = coll
        _mongo_last_error = None
        print("[data.py] MongoDB connection established.")
    except Exception as e:
        _mongo_last_error = str(e)


def load():
    with _lock:
        _ensure_mongo()
        if _mongo_collection is not None:
            try:
                doc = _mongo_collection.find_one({"_id": _MONGO_DOC_ID})
                global _mongo_last_error
                _mongo_last_error = None
                if not doc:
                    d = _default()
                    save(d)
                    return d
                d = dict(doc)
                d.pop("_id", None)
                default = _default()
                for k, v in default.items():
                    if k not in d:
                        d[k] = v
                for k, v in DEFAULT_SETTINGS.items():
                    if k not in d.setdefault("settings", {}):
                        d["settings"][k] = v
                return d
            except Exception as e:
                # Don't take the whole app down if Mongo is briefly
                # unreachable (new cluster still starting, network rule
                # still propagating, transient blip, etc.) - fall back to
                # the local file for THIS request rather than 500ing every
                # single API call. Visible in system health + Render logs.
                _mongo_last_error = str(e)
                print(f"[data.py] MongoDB load() failed, falling back to local file: {e}")
        # --- local JSON fallback (only durable if DATA_DIR is a mounted
        #     persistent disk - see README) ---
        if not os.path.exists(DATA_FILE):
            d = _default()
            save(d)
            return d
        with open(DATA_FILE) as f:
            try:
                d = json.load(f)
                default = _default()
                for k, v in default.items():
                    if k not in d:
                        d[k] = v
                for k, v in DEFAULT_SETTINGS.items():
                    if k not in d.setdefault("settings", {}):
                        d["settings"][k] = v
                return d
            except Exception:
                return _default()


def save(data):
    with _lock:
        _ensure_mongo()
        if _mongo_collection is not None:
            try:
                doc = dict(data)
                doc["_id"] = _MONGO_DOC_ID
                _mongo_collection.replace_one({"_id": _MONGO_DOC_ID}, doc, upsert=True)
                global _mongo_last_error
                _mongo_last_error = None
                return
            except Exception as e:
                _mongo_last_error = str(e)
                print(f"[data.py] MongoDB save() failed, falling back to local file: {e}")
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DATA_FILE)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_settings():
    return load()["settings"]


def update_settings(patch):
    with _lock:
        d = load()
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS:
                d["settings"][k] = v
        save(d)
        return d["settings"]


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------
def get_or_create_player(user_id, name):
    uid = str(user_id)
    with _lock:
        d = load()
        if uid not in d["players"]:
            bonus = d["settings"].get("signup_bonus", 10)
            d["players"][uid] = {
                "name": name,
                "balance": bonus,
                "bonus_balance": bonus,   # portion of balance that can't be withdrawn until wagered
                "total_wins": 0,
                "total_winnings": 0,
                "games_played": 0,
                "transactions": [],
                "wins": [],
                "active_game": None,
                "language": "en",
                "call_lang": "en",   # voice-caller language - independently switchable from the live game screen
                "sound": True,
                "voice": True,
                "auto_mark": True,
                "device_id": None,
                "last_ip": None,
                "banned": False,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            if bonus:
                d["players"][uid]["transactions"].append({
                    "type": "credit", "amount": bonus, "note": "Signup bonus (one-time, non-withdrawable)",
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M")
                })
            save(d)
        else:
            # backfill any new fields for existing players
            p = d["players"][uid]
            changed = False
            for field, default in (("language", "en"), ("sound", True), ("voice", True),
                                    ("auto_mark", True), ("bonus_balance", 0), ("banned", False),
                                    ("device_id", None), ("last_ip", None)):
                if field not in p:
                    p[field] = default
                    changed = True
            if changed:
                save(d)
        return d["players"][uid]


def get_player(user_id):
    d = load()
    return d["players"].get(str(user_id))


def set_player_prefs(user_id, **prefs):
    uid = str(user_id)
    with _lock:
        d = load()
        if uid not in d["players"]:
            return None
        for k in ("language", "sound", "voice", "auto_mark", "call_lang"):
            if prefs.get(k) is not None:
                d["players"][uid][k] = prefs[k]
        save(d)
        return d["players"][uid]


def credit_balance(user_id, amount, note="deposit"):
    uid = str(user_id)
    with _lock:
        d = load()
        if uid not in d["players"]:
            return False
        d["players"][uid]["balance"] += amount
        d["players"][uid]["transactions"].append({
            "type": "credit", "amount": amount, "note": note,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
        save(d)
        return True


def debit_balance(user_id, amount, note="bet"):
    """Debits balance. Bonus balance is spent first (FIFO), so real cash stays
    protected the longest, then falls back to real balance."""
    uid = str(user_id)
    with _lock:
        d = load()
        if uid not in d["players"]:
            return False
        p = d["players"][uid]
        if p["balance"] < amount:
            return False
        p["balance"] -= amount
        bonus_used = min(p.get("bonus_balance", 0), amount)
        p["bonus_balance"] = p.get("bonus_balance", 0) - bonus_used
        p["transactions"].append({
            "type": "debit", "amount": amount, "note": note,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
        save(d)
        return True


def withdrawable_amount(user_id):
    """Real-cash balance only - bonus money can never be withdrawn."""
    p = get_player(user_id)
    if not p:
        return 0
    return max(0, p["balance"] - p.get("bonus_balance", 0))


def ban_player(user_id, reason="Violation of terms"):
    uid = str(user_id)
    with _lock:
        d = load()
        if uid in d["players"]:
            d["players"][uid]["banned"] = True
        d["banned"][uid] = reason
        save(d)


def unban_player(user_id):
    uid = str(user_id)
    with _lock:
        d = load()
        if uid in d["players"]:
            d["players"][uid]["banned"] = False
        d["banned"].pop(uid, None)
        save(d)


def is_banned(user_id):
    d = load()
    return d["players"].get(str(user_id), {}).get("banned", False)


def adjust_balance_admin(user_id, amount, note="Admin adjustment"):
    """Admin can add or remove funds directly (amount can be negative)."""
    uid = str(user_id)
    with _lock:
        d = load()
        if uid not in d["players"]:
            return False
        d["players"][uid]["balance"] = max(0, d["players"][uid]["balance"] + amount)
        d["players"][uid]["transactions"].append({
            "type": "credit" if amount >= 0 else "debit", "amount": abs(amount), "note": note,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
        save(d)
        return True


def list_players(search=None, limit=200):
    d = load()
    items = list(d["players"].items())
    if search:
        s = search.lower()
        items = [(uid, p) for uid, p in items if s in uid.lower() or s in p.get("name", "").lower()]
    items.sort(key=lambda x: x[1].get("created_at", ""), reverse=True)
    return [{"user_id": uid, **p} for uid, p in items[:limit]]


# ---------------------------------------------------------------------------
# Deposits / withdrawals (manual only - this is the ONLY way to add funds
# besides the one-time signup bonus)
# ---------------------------------------------------------------------------
def add_deposit_request(user_id, name, amount, method, reference):
    req = {
        "id": str(uuid.uuid4())[:8],
        "user_id": str(user_id),
        "name": name,
        "amount": amount,
        "method": method,
        "reference": reference,
        "status": "pending",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    with _lock:
        d = load()
        d["deposit_requests"].append(req)
        save(d)
    return req


def add_withdraw_request(user_id, name, amount, method, account):
    req = {
        "id": str(uuid.uuid4())[:8],
        "user_id": str(user_id),
        "name": name,
        "amount": amount,
        "method": method,
        "account": account,
        "status": "pending",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    with _lock:
        d = load()
        d["withdraw_requests"].append(req)
        save(d)
    return req


def get_pending_deposits():
    d = load()
    return [r for r in d["deposit_requests"] if r["status"] == "pending"]


def get_pending_withdrawals():
    d = load()
    return [r for r in d["withdraw_requests"] if r["status"] == "pending"]


def get_all_deposits(limit=200):
    d = load()
    return list(reversed(d["deposit_requests"]))[:limit]


def get_all_withdrawals(limit=200):
    d = load()
    return list(reversed(d["withdraw_requests"]))[:limit]


def deposits_total(date_from=None, date_to=None, status="approved"):
    d = load()
    rows = d["deposit_requests"]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if date_from:
        rows = [r for r in rows if r["time"][:10] >= date_from]
    if date_to:
        rows = [r for r in rows if r["time"][:10] <= date_to]
    return sum(r["amount"] for r in rows)


def withdrawals_total(date_from=None, date_to=None, status="approved"):
    d = load()
    rows = d["withdraw_requests"]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if date_from:
        rows = [r for r in rows if r["time"][:10] >= date_from]
    if date_to:
        rows = [r for r in rows if r["time"][:10] <= date_to]
    return sum(r["amount"] for r in rows)


def approve_deposit(req_id):
    with _lock:
        d = load()
        for r in d["deposit_requests"]:
            if r["id"] == req_id and r["status"] == "pending":
                r["status"] = "approved"
                save(d)
                credit_balance(r["user_id"], r["amount"], f"Deposit approved ({r['method']})")
                return r
    return None


def reject_deposit(req_id):
    with _lock:
        d = load()
        for r in d["deposit_requests"]:
            if r["id"] == req_id and r["status"] == "pending":
                r["status"] = "rejected"
                save(d)
                return r
    return None


def approve_withdrawal(req_id):
    with _lock:
        d = load()
        for r in d["withdraw_requests"]:
            if r["id"] == req_id and r["status"] == "pending":
                r["status"] = "approved"
                save(d)
                return r
    return None


def reject_withdrawal(req_id):
    """Rejecting refunds the already-debited amount back to the player."""
    with _lock:
        d = load()
        for r in d["withdraw_requests"]:
            if r["id"] == req_id and r["status"] == "pending":
                r["status"] = "rejected"
                save(d)
                credit_balance(r["user_id"], r["amount"], "Withdrawal rejected - refunded")
                return r
    return None


# ---------------------------------------------------------------------------
# Leaderboard / jackpot
# ---------------------------------------------------------------------------
def get_leaderboard(period="all"):
    """period: 'all' | 'daily' | 'weekly' | 'monthly'.
    For daily/weekly/monthly, wins are counted only within that rolling
    window (based on each win's own timestamp), not lifetime totals."""
    d = load()
    now = datetime.now()

    def in_period(time_str):
        if period == "all":
            return True
        try:
            wt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return False
        if period == "daily":
            return wt.date() == now.date()
        if period == "weekly":
            return (now - wt).days < 7 and (now - wt).total_seconds() >= 0
        if period == "monthly":
            return wt.year == now.year and wt.month == now.month
        return True

    players = []
    for uid, p in d["players"].items():
        if period == "all":
            total_wins = p["total_wins"]
            total_winnings = p["total_winnings"]
        else:
            wins_in_period = [w for w in p.get("wins", []) if in_period(w.get("time", ""))]
            if not wins_in_period:
                continue
            total_wins = len(wins_in_period)
            total_winnings = sum(w["amount"] for w in wins_in_period)
        if total_wins == 0 and total_winnings == 0:
            continue
        players.append({"name": p["name"], "total_wins": total_wins, "total_winnings": total_winnings})
    return sorted(players, key=lambda x: x["total_winnings"], reverse=True)[:20]


def get_jackpot(bet):
    d = load()
    return d["jackpots"].get(str(bet), 0)


def add_to_jackpot(bet, amount):
    with _lock:
        d = load()
        key = str(bet)
        d["jackpots"][key] = d["jackpots"].get(key, 0) + amount
        save(d)
        return d["jackpots"][key]


def reset_jackpot(bet):
    with _lock:
        d = load()
        d["jackpots"][str(bet)] = 0
        d.setdefault("jackpot_armed", {})[str(bet)] = False
        save(d)


# ---------------------------------------------------------------------------
# Jackpot payout rule: once a lobby's jackpot reaches its target, it becomes
# "armed" - the VERY NEXT bingo winner in that lobby wins the jackpot ON TOP
# OF their normal lobby prize. Persisted (not in-memory) so it survives
# restarts - an armed jackpot must not silently un-arm on a redeploy.
# ---------------------------------------------------------------------------
def set_jackpot_armed(bet, armed=True):
    with _lock:
        d = load()
        d.setdefault("jackpot_armed", {})[str(bet)] = armed
        save(d)


def is_jackpot_armed(bet):
    d = load()
    return d.get("jackpot_armed", {}).get(str(bet), False)


def pay_out_jackpot(bet):
    """Called exactly once, when the next winner after arming is paid.
    Returns the jackpot amount paid out and resets the pool + armed flag."""
    with _lock:
        d = load()
        key = str(bet)
        amount = d["jackpots"].get(key, 0)
        d["jackpots"][key] = 0
        d.setdefault("jackpot_armed", {})[key] = False
        save(d)
        return amount


# ---------------------------------------------------------------------------
# Commission ledger (for admin revenue reporting)
# ---------------------------------------------------------------------------
def record_commission(game_id, bet, player_count, total_pot, commission_amount, jackpot_amount):
    with _lock:
        d = load()
        d["commission_ledger"].append({
            "game_id": game_id, "bet": bet, "players": player_count,
            "total_pot": total_pot, "commission": commission_amount,
            "jackpot_cut": jackpot_amount,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        save(d)


def commission_summary(date_from=None, date_to=None):
    """With no args: today + all-time totals (original behavior, used by the
    dashboard). With date_from/date_to ('YYYY-MM-DD'): commission earned in
    that inclusive range only (used by the Commission tab's date filter)."""
    d = load()
    ledger = d["commission_ledger"]
    total = sum(r["commission"] for r in ledger)
    today_str = datetime.now().strftime("%Y-%m-%d")
    today = sum(r["commission"] for r in ledger if r["time"].startswith(today_str))
    result = {
        "total_commission": total,
        "today_commission": today,
        "total_games": len(ledger),
        "recent": list(reversed(ledger))[:50],
    }
    if date_from or date_to:
        rows = ledger
        if date_from:
            rows = [r for r in rows if r["time"][:10] >= date_from]
        if date_to:
            rows = [r for r in rows if r["time"][:10] <= date_to]
        result["range_commission"] = sum(r["commission"] for r in rows)
        result["range_games"] = len(rows)
        result["range_rows"] = list(reversed(rows))
    return result


def _period_bounds(period, date_str):
    """period: 'daily'|'weekly'|'monthly'. date_str: 'YYYY-MM-DD' anchor date
    (defaults to today). Returns (date_from, date_to) inclusive, both
    'YYYY-MM-DD' strings, for use with the *_total()/get_game_history()
    date filters above."""
    from datetime import timedelta
    anchor = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
    if period == "daily":
        d0 = d1 = anchor
    elif period == "weekly":
        d0 = anchor - timedelta(days=anchor.weekday())   # Monday
        d1 = d0 + timedelta(days=6)                       # Sunday
    elif period == "monthly":
        d0 = anchor.replace(day=1)
        if d0.month == 12:
            d1 = d0.replace(year=d0.year + 1, month=1) - timedelta(days=1)
        else:
            d1 = d0.replace(month=d0.month + 1) - timedelta(days=1)
    else:
        d0 = d1 = anchor
    return d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")


def finance_report(period="daily", date_str=None):
    """The Finance Report page: profit + deposits + withdrawals + house
    profit, all scoped to the chosen day/week/month via `date_str` (any
    date inside the desired period - defaults to today)."""
    date_from, date_to = _period_bounds(period, date_str)
    dep = deposits_total(date_from, date_to)
    wit = withdrawals_total(date_from, date_to)
    comm = commission_summary(date_from, date_to)
    games = get_game_history(date_from, date_to, limit=100000)
    return {
        "period": period, "date_from": date_from, "date_to": date_to,
        "deposits_total": dep,
        "withdrawals_total": wit,
        "house_profit": comm.get("range_commission", 0),  # commission IS the house's earnings
        "net_cashflow": dep - wit,
        "games_count": len(games),
        "deposit_history": [r for r in get_all_deposits(limit=100000)
                             if date_from <= r["time"][:10] <= date_to],
        "withdrawal_history": [r for r in get_all_withdrawals(limit=100000)
                                if date_from <= r["time"][:10] <= date_to],
    }


def player_totals(user_id, date_from=None, date_to=None):
    """Total deposits/withdrawals for one player - overall or date-scoped,
    for the admin Player detail view."""
    uid = str(user_id)
    d = load()
    dep_rows = [r for r in d["deposit_requests"] if r["user_id"] == uid and r["status"] == "approved"]
    wit_rows = [r for r in d["withdraw_requests"] if r["user_id"] == uid and r["status"] == "approved"]
    if date_from:
        dep_rows = [r for r in dep_rows if r["time"][:10] >= date_from]
        wit_rows = [r for r in wit_rows if r["time"][:10] >= date_from]
    if date_to:
        dep_rows = [r for r in dep_rows if r["time"][:10] <= date_to]
        wit_rows = [r for r in wit_rows if r["time"][:10] <= date_to]
    return {
        "total_deposits": sum(r["amount"] for r in dep_rows),
        "total_withdrawals": sum(r["amount"] for r in wit_rows),
        "deposit_count": len(dep_rows),
        "withdrawal_count": len(wit_rows),
    }


# ---------------------------------------------------------------------------
# Lobby-filler bots (fairness note: bots never receive prize money and never
# cost a real player anything - they only exist to help a quiet lobby reach
# min_players so real people don't wait around alone. See engine.py.)
# ---------------------------------------------------------------------------
def get_bot_settings():
    d = load()
    counts = dict(d.get("bot_counts", {}))
    for c in LOBBY_CONFIGS:
        counts.setdefault(str(c["bet"]), 0)
    return {"enabled": d["settings"].get("bots_enabled", True), "counts": counts,
            "names": get_bot_names()}


def update_bot_settings(enabled=None, counts=None):
    with _lock:
        d = load()
        if enabled is not None:
            d["settings"]["bots_enabled"] = bool(enabled)
        if counts:
            d.setdefault("bot_counts", {})
            for bet, n in counts.items():
                try:
                    d["bot_counts"][str(bet)] = max(0, int(n))
                except (TypeError, ValueError):
                    continue
        save(d)
    return get_bot_settings()


def get_bot_names():
    d = load()
    names = d.get("bot_names")
    return names if names else list(DEFAULT_BOT_NAMES)


def update_bot_names(names):
    """Admin-editable display names only - NOT win probability. See README."""
    with _lock:
        d = load()
        cleaned = [str(n).strip() for n in (names or []) if str(n).strip()]
        d["bot_names"] = cleaned if cleaned else list(DEFAULT_BOT_NAMES)
        save(d)
        return d["bot_names"]


# ---------------------------------------------------------------------------
# Anti-cheat: flag likely multi-accounting / suspicious behaviour
# ---------------------------------------------------------------------------
def register_device(user_id, device_id, ip):
    uid = str(user_id)
    with _lock:
        d = load()
        ac = d["anticheat"]
        if uid in d["players"]:
            d["players"][uid]["device_id"] = device_id
            d["players"][uid]["last_ip"] = ip

        new_flags = []
        if device_id:
            users = ac["device_map"].setdefault(device_id, [])
            if uid not in users:
                users.append(uid)
            if len(users) > 1:
                new_flags.append({
                    "type": "shared_device", "device_id": device_id, "user_ids": users[:],
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
        if ip:
            users = ac["ip_map"].setdefault(ip, [])
            if uid not in users:
                users.append(uid)
            if len(users) > 3:  # a handful of players sharing wifi is normal; many is suspicious
                new_flags.append({
                    "type": "shared_ip_many_accounts", "ip": ip, "user_ids": users[:],
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
        for f in new_flags:
            ac["flags"].append(f)
        save(d)
        return new_flags


def get_anticheat_flags(limit=100):
    d = load()
    return list(reversed(d["anticheat"]["flags"]))[:limit]


# ---------------------------------------------------------------------------
# Online presence - "last seen" heartbeat, updated on normal API traffic.
# Powers "online now" (admin) and "active today" (dashboard stat).
# ---------------------------------------------------------------------------
ONLINE_WINDOW_SECONDS = 90  # no heartbeat in this long => considered offline


def touch_online(user_id):
    uid = str(user_id)
    with _lock:
        d = load()
        if uid not in d["players"]:
            return
        d.setdefault("last_seen", {})[uid] = time.time()
        save(d)


def get_online_users():
    d = load()
    now = time.time()
    out = []
    for uid, ts in d.get("last_seen", {}).items():
        if now - ts <= ONLINE_WINDOW_SECONDS and uid in d["players"]:
            out.append({"user_id": uid, "name": d["players"][uid]["name"],
                        "seconds_ago": int(now - ts)})
    return sorted(out, key=lambda x: x["seconds_ago"])


def active_users_today():
    d = load()
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    for uid, p in d["players"].items():
        # "active" = touched their account today: logged a transaction,
        # a win, or was seen online today.
        seen_today = False
        last_seen_ts = d.get("last_seen", {}).get(uid)
        if last_seen_ts and datetime.fromtimestamp(last_seen_ts).strftime("%Y-%m-%d") == today:
            seen_today = True
        if not seen_today:
            for t in p.get("transactions", []):
                if t.get("time", "").startswith(today):
                    seen_today = True
                    break
        if seen_today:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Game history (separate from live engine.GameRoom objects - survives
# restarts, browsable/searchable in admin, never touches live games)
# ---------------------------------------------------------------------------
def record_game_history(game_id, bet, real_player_count, bot_count, total_pot,
                         commission, jackpot_cut, winners, started_at, finished_at, jackpot_paid=0):
    """winners: list of {name, user_id, card_number, prize, pattern, jackpot}.
    jackpot_cut: the slice of THIS game's pot that was added to the jackpot pool.
    jackpot_paid: the jackpot amount paid OUT this game (0 unless it was armed)."""
    with _lock:
        d = load()
        d.setdefault("game_history", []).append({
            "game_id": game_id, "bet": bet,
            "real_player_count": real_player_count, "bot_count": bot_count,
            "total_pot": total_pot, "commission": commission, "jackpot_cut": jackpot_cut,
            "jackpot_paid": jackpot_paid,
            "winners": winners,
            "started_at": started_at, "finished_at": finished_at,
        })
        save(d)


def get_game_history(date_from=None, date_to=None, bet=None, search=None, limit=200):
    """date_from/date_to: 'YYYY-MM-DD' strings, inclusive. search: matches
    game_id or any winner's name (case-insensitive)."""
    d = load()
    rows = list(reversed(d.get("game_history", [])))
    if date_from:
        rows = [r for r in rows if r["finished_at"][:10] >= date_from]
    if date_to:
        rows = [r for r in rows if r["finished_at"][:10] <= date_to]
    if bet:
        rows = [r for r in rows if str(r["bet"]) == str(bet)]
    if search:
        s = search.lower()
        rows = [r for r in rows if s in r["game_id"].lower()
                or any(s in (w.get("name") or "").lower() for w in r.get("winners", []))]
    return rows[:limit]


# ---------------------------------------------------------------------------
# Admin action audit log - who did what, when, from where.
# ---------------------------------------------------------------------------
def log_admin_action(admin_name, action, detail="", ip=""):
    with _lock:
        d = load()
        d.setdefault("admin_audit_log", []).append({
            "admin": admin_name or "admin", "action": action, "detail": detail,
            "ip": ip or "", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        save(d)


def get_admin_audit_log(limit=300):
    d = load()
    return list(reversed(d.get("admin_audit_log", [])))[:limit]


# ---------------------------------------------------------------------------
# Admin auth (simple token session, password comes from env ADMIN_PASSWORD)
# ---------------------------------------------------------------------------
def create_admin_session(admin_name="Admin", ttl_seconds=8 * 3600):
    token = secrets.token_hex(24)
    with _lock:
        d = load()
        now = time.time()
        # prune expired
        d["admin_sessions"] = {t: s for t, s in d["admin_sessions"].items()
                                if isinstance(s, dict) and s.get("expires", 0) > now}
        d["admin_sessions"][token] = {"expires": now + ttl_seconds, "name": admin_name or "Admin"}
        save(d)
    return token


def check_admin_session(token):
    if not token:
        return False
    d = load()
    s = d["admin_sessions"].get(token)
    if not s:
        return False
    if isinstance(s, dict):
        return bool(s.get("expires", 0) > time.time())
    return bool(s > time.time())  # backward compat with old plain-expiry sessions


def get_admin_name(token):
    d = load()
    s = d["admin_sessions"].get(token)
    if isinstance(s, dict):
        return s.get("name", "Admin")
    return "Admin"


def revoke_admin_session(token):
    with _lock:
        d = load()
        d["admin_sessions"].pop(token, None)
        save(d)


# ---------------------------------------------------------------------------
# Broadcasts (admin -> all players, sent via Telegram)
# ---------------------------------------------------------------------------
def log_broadcast(message):
    with _lock:
        d = load()
        d["broadcasts"].append({"message": message, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
        save(d)


def dashboard_stats():
    d = load()
    players = d["players"]
    total_players = len(players)
    total_balance = sum(p["balance"] for p in players.values())
    total_deposited = sum(t["amount"] for p in players.values() for t in p["transactions"]
                           if t["type"] == "credit" and "Deposit approved" in t["note"])
    pending_dep = len([r for r in d["deposit_requests"] if r["status"] == "pending"])
    pending_wit = len([r for r in d["withdraw_requests"] if r["status"] == "pending"])
    comm = commission_summary()
    return {
        "total_players": total_players,
        "active_today": active_users_today(),
        "online_now": len(get_online_users()),
        "total_games_played": len(d.get("game_history", [])),
        "total_wallet_balance": total_balance,
        "total_deposited": total_deposited,
        "pending_deposits": pending_dep,
        "pending_withdrawals": pending_wit,
        "total_commission": comm["total_commission"],
        "today_commission": comm["today_commission"],
        "anticheat_flags": len(d["anticheat"]["flags"]),
        "banned_players": len(d["banned"]),
    }
