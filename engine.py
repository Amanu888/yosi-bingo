"""
Fully automated Bingo game engine.
- Players pick their OWN card(s) by number from a shared 1..N pool (max 2 per player)
- Selecting/deselecting a card instantly debits/refunds the lobby stake - allowed
  freely until the game actually starts (waiting or countdown status)
- Cards taken by other players are visible to EVERYONE in the lobby (red), even
  players with no balance - visibility is never gated by wallet balance
- Lobbies auto-start when MIN real players have at least one card selected
- Numbers auto-draw every DRAW_INTERVAL seconds
- Winners auto-detected server-side (client never decides a win - anti-cheat)
- Commission + jackpot cut are read live from admin settings
- Prizes auto-paid, lobby auto-resets for next round
"""
import random, time, threading, uuid, json
from datetime import datetime
from cards import COLUMNS, load_cards, card_all_numbers
import data as db

_games = {}             # game_id -> GameRoom
_lobby_lock = threading.Lock()
_on_draw_callbacks = []  # list of async funcs to notify Telegram / Socket.IO


def register_draw_callback(fn):
    _on_draw_callbacks.append(fn)


def _fire_callbacks(event, payload):
    import asyncio
    for fn in _on_draw_callbacks:
        try:
            asyncio.run_coroutine_threadsafe(fn(event, payload), _get_loop())
        except Exception:
            pass


_loop = None
def set_event_loop(loop):
    global _loop
    _loop = loop


def _get_loop():
    return _loop


DEFAULT_BOT_NAMES = ["Abebe", "Kebede", "Selam", "Meron", "Yared", "Liya", "Dawit", "Hana",
                     "Nardos", "Bereket", "Sara", "Mekdes", "Yonas", "Ruth", "Solomon", "Tigist",
                     "Henok", "Betelhem", "Natnael", "Eden"]

MAX_CARDS_PER_PLAYER = 2


def col_letter(n):
    if n <= 15: return "B"
    if n <= 30: return "I"
    if n <= 45: return "N"
    if n <= 60: return "G"
    return "O"


class GameRoom:
    def __init__(self, bet, game_id=None):
        settings = db.get_settings()
        self.game_id = game_id or str(uuid.uuid4())[:8]
        self.bet = bet
        self.players = {}        # user_id -> PlayerSlot (see _new_slot)
        self.drawn = []
        self.remaining = list(range(1, 76))
        random.shuffle(self.remaining)
        self.status = "waiting"  # waiting | countdown | running | finished
        self.winners = []
        self.prize_per_winner = 0
        self.commission_amount = 0
        self.start_time = None
        self.created_at = time.time()
        self.countdown_end = None
        self.min_players = settings.get("min_players", 2)
        self.countdown_seconds = settings.get("countdown_seconds", 30)
        self.draw_interval = settings.get("draw_interval_seconds", 5)
        self.win_patterns = settings.get("win_patterns", ["row", "column", "diagonal", "corners"])
        self._draw_timer = None
        self._countdown_timer = None
        self._paused = False
        self._card_pool = load_cards()          # fixed list, index 0..N-1 -> card_number 1..N
        self.card_owner = {}                    # card_number(int) -> user_id (str)

    # -- card lookup --------------------------------------------------------
    def total_cards(self):
        return len(self._card_pool)

    def card_by_number(self, card_number):
        idx = card_number - 1
        if idx < 0 or idx >= len(self._card_pool):
            return None
        return self._card_pool[idx]

    def taken_cards_view(self):
        """Visible to EVERY player in the lobby regardless of balance - shows
        which card numbers are already claimed and by whom, so the client can
        render them red and disabled."""
        out = {}
        for num, uid in self.card_owner.items():
            slot = self.players.get(uid)
            if slot:
                out[str(num)] = {"user_id": uid, "name": slot["name"], "is_bot": slot.get("is_bot", False)}
        return out

    def _new_slot(self, user_id, name, is_bot=False):
        return {"user_id": user_id, "name": name, "cards": [], "marked": {}, "won": False,
                "winning_card": None, "is_bot": is_bot}

    # -- card selection / deselection (instant charge / refund) -------------
    def select_card(self, user_id, name, card_number):
        uid = str(user_id)
        if self.status not in ("waiting", "countdown"):
            raise ValueError("This game has already started - please wait for the next round.")
        card = self.card_by_number(card_number)
        if card is None:
            raise ValueError("Invalid card number.")
        if card_number in self.card_owner and self.card_owner[card_number] != uid:
            raise ValueError("That card is already taken by another player.")

        if db.is_banned(user_id):
            raise ValueError("Your account has been suspended. Contact support.")
        db.get_or_create_player(user_id, name)

        slot = self.players.get(uid)
        if slot is None:
            slot = self._new_slot(uid, name)
            self.players[uid] = slot

        if card_number in slot["cards"]:
            return slot  # already selected, nothing to do

        if len(slot["cards"]) >= MAX_CARDS_PER_PLAYER:
            raise ValueError(f"Maximum {MAX_CARDS_PER_PLAYER} cards per player.")

        # Instant debit - this is the "hold" on the lobby stake for this card.
        if not db.debit_balance(uid, self.bet, f"Card #{card_number} selected ({self.bet} ETB game)"):
            raise ValueError("Insufficient balance for this card.")

        slot["cards"].append(card_number)
        slot["marked"][str(card_number)] = []
        self.card_owner[card_number] = uid

        if self.status == "waiting":
            self._maybe_add_bots()
            real_ready = sum(1 for s in self.players.values() if not s.get("is_bot") and s["cards"])
            if real_ready >= self.min_players:
                self._begin_countdown()
        return slot

    def deselect_card(self, user_id, card_number):
        uid = str(user_id)
        if self.status not in ("waiting", "countdown"):
            raise ValueError("This game has already started - cards can no longer be changed.")
        slot = self.players.get(uid)
        if not slot or card_number not in slot["cards"]:
            raise ValueError("You don't have that card selected.")

        slot["cards"].remove(card_number)
        slot["marked"].pop(str(card_number), None)
        self.card_owner.pop(card_number, None)

        # Instant refund - the whole point of "change your mind before start".
        db.credit_balance(uid, self.bet, f"Card #{card_number} deselected - refunded")

        if not slot["cards"] and not slot.get("is_bot"):
            self.players.pop(uid, None)
        return True

    def player_cards(self, user_id):
        slot = self.players.get(str(user_id))
        if not slot:
            return []
        return [{"number": cn, "card": self.card_by_number(cn),
                 "marked": slot["marked"].get(str(cn), [])} for cn in slot["cards"]]

    def _maybe_add_bots(self):
        """Top up the lobby with house-funded 'seat filler' bots, per the
        admin's configured count for this bet tier. Bots never pay in and
        are never eligible to win (see _check_winners) - they exist purely
        so a lone real player isn't left waiting indefinitely. Bots take
        cards from the SAME shared pool, so they still show as taken/red."""
        cfg = db.get_bot_settings()
        if not cfg.get("enabled"):
            return
        target = int(cfg.get("counts", {}).get(str(self.bet), 0))
        current_bots = sum(1 for s in self.players.values() if s.get("is_bot"))
        idx = current_bots
        while current_bots < target and self.status == "waiting":
            idx += 1
            if not self._add_bot_slot(idx):
                break
            current_bots += 1

    def _add_bot_slot(self, idx):
        available = [n for n in range(1, self.total_cards() + 1) if n not in self.card_owner]
        if not available:
            return False
        card_number = random.choice(available)
        bot_names = db.get_bot_names() or DEFAULT_BOT_NAMES
        uid = f"bot_{self.game_id}_{idx}"
        name = f"{random.choice(bot_names)} 🤖"
        slot = self._new_slot(uid, name, is_bot=True)
        slot["cards"].append(card_number)
        slot["marked"][str(card_number)] = []
        self.card_owner[card_number] = uid
        self.players[uid] = slot
        return True

    def add_bots_now(self, count):
        """Admin-triggered manual top-up, on top of whatever the auto-fill already added."""
        current_bots = sum(1 for s in self.players.values() if s.get("is_bot"))
        for i in range(count):
            if not self._add_bot_slot(current_bots + i + 1):
                break
        if self.status == "waiting":
            real_ready = sum(1 for s in self.players.values() if not s.get("is_bot") and s["cards"])
            if real_ready >= self.min_players:
                self._begin_countdown()

    # -- lifecycle ----------------------------------------------------------
    def _begin_countdown(self):
        self.status = "countdown"
        self.countdown_end = time.time() + self.countdown_seconds
        _fire_callbacks("countdown", {"game_id": self.game_id, "bet": self.bet, "seconds": self.countdown_seconds})
        self._countdown_timer = threading.Timer(self.countdown_seconds, self._start_game)
        self._countdown_timer.daemon = True
        self._countdown_timer.start()

    def _start_game(self):
        # Safety: if everyone deselected during countdown, don't start an empty game.
        real_ready = sum(1 for s in self.players.values() if not s.get("is_bot") and s["cards"])
        if real_ready < self.min_players:
            self.status = "waiting"
            self.countdown_end = None
            return
        self.status = "running"
        self.start_time = time.time()
        settings = db.get_settings()
        real_player_count = sum(1 for s in self.players.values() if not s.get("is_bot"))
        real_card_count = sum(len(s["cards"]) for s in self.players.values() if not s.get("is_bot"))
        total_pot = self.bet * real_card_count
        commission_pct = settings.get("commission_percent", 20)
        jackpot_pct = settings.get("jackpot_percent", 5)
        commission_amount = round(total_pot * commission_pct / 100)
        jackpot_amount = round(total_pot * jackpot_pct / 100)
        self.commission_amount = commission_amount
        self.jackpot_cut = jackpot_amount
        self.total_pot = total_pot
        self.prize_per_winner = max(0, total_pot - commission_amount - jackpot_amount)
        new_jackpot_total = db.add_to_jackpot(self.bet, jackpot_amount)
        db.record_commission(self.game_id, self.bet, real_player_count, total_pot,
                              commission_amount, jackpot_amount)
        # Jackpot rule: once a lobby's progressive jackpot reaches its
        # target, it becomes ARMED - the very next bingo winner in that
        # lobby wins the whole jackpot ON TOP of their normal prize (see
        # _finish). Persisted via db.set_jackpot_armed so it survives a
        # restart between now and whenever the next winner happens.
        target = next((c["jackpot_target"] for c in db.LOBBY_CONFIGS if c["bet"] == self.bet), None)
        if target and new_jackpot_total >= target and not db.is_jackpot_armed(self.bet):
            db.set_jackpot_armed(self.bet, True)
            _fire_callbacks("jackpot_alert", {"bet": self.bet, "jackpot": new_jackpot_total, "target": target})
        _fire_callbacks("start", {"game_id": self.game_id, "bet": self.bet,
                                   "players": len(self.players), "prize": self.prize_per_winner,
                                   "commission": commission_amount})
        self._schedule_draw()

    def _schedule_draw(self):
        self._draw_timer = threading.Timer(self.draw_interval, self._auto_draw)
        self._draw_timer.daemon = True
        self._draw_timer.start()

    def _auto_draw(self):
        if self._paused:
            self._schedule_draw()
            return
        if self.status != "running" or not self.remaining:
            self._finish()
            return
        num = self.remaining.pop()
        self.drawn.append(num)
        # server is the single source of truth for marking - client display
        # preference (auto/manual mark) never changes what actually counts.
        for slot in self.players.values():
            for cn in slot["cards"]:
                card = self.card_by_number(cn)
                if card and num in card_all_numbers(card):
                    slot["marked"].setdefault(str(cn), []).append(num)
        _fire_callbacks("draw", {"game_id": self.game_id, "bet": self.bet, "number": num,
                                  "col": col_letter(num), "drawn_count": len(self.drawn),
                                  "drawn": self.drawn[:]})
        new_winners = self._check_winners()
        if new_winners:
            self._finish(new_winners)
        elif not self.remaining:
            self._finish()
        else:
            self._schedule_draw()

    def force_draw_now(self):
        """Admin control: trigger the next draw immediately."""
        if self._draw_timer:
            self._draw_timer.cancel()
        self._auto_draw()

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    # -- win checking ---------------------------------------------------------
    def _check_winners(self):
        new_winners = []
        for slot in self.players.values():
            if slot["won"] or slot.get("is_bot"):
                continue
            for cn in slot["cards"]:
                if self._has_bingo(slot, cn):
                    slot["won"] = True
                    slot["winning_card"] = cn
                    new_winners.append(slot)
                    break
        return new_winners

    def _has_bingo(self, slot, card_number):
        marked = set(slot["marked"].get(str(card_number), []))
        card = self.card_by_number(card_number)

        def is_marked(col, row):
            v = card[col][row]
            return v == "FREE" or v in marked

        if "row" in self.win_patterns:
            for row in range(5):
                if all(is_marked(c, row) for c in COLUMNS):
                    return True
        if "column" in self.win_patterns:
            for c in COLUMNS:
                if all(is_marked(c, r) for r in range(5)):
                    return True
        if "diagonal" in self.win_patterns:
            if all(is_marked(COLUMNS[i], i) for i in range(5)):
                return True
            if all(is_marked(COLUMNS[i], 4 - i) for i in range(5)):
                return True
        if "corners" in self.win_patterns:
            corners = [("B", 0), ("O", 0), ("B", 4), ("O", 4)]
            if all(is_marked(c, r) for c, r in corners):
                return True
        if "blackout" in self.win_patterns:
            if all(is_marked(c, r) for c in COLUMNS for r in range(5)):
                return True
        return False

    def winning_pattern_name(self, slot):
        """Best-effort label of which pattern this player won with, for UI display."""
        cn = slot.get("winning_card") or (slot["cards"][0] if slot["cards"] else None)
        if cn is None:
            return "Bingo"
        marked = set(slot["marked"].get(str(cn), []))
        card = self.card_by_number(cn)

        def is_marked(col, row):
            v = card[col][row]
            return v == "FREE" or v in marked

        for row in range(5):
            if all(is_marked(c, row) for c in COLUMNS):
                return "Horizontal Line"
        for c in COLUMNS:
            if all(is_marked(c, r) for r in range(5)):
                return "Vertical Line"
        if all(is_marked(COLUMNS[i], i) for i in range(5)) or all(is_marked(COLUMNS[i], 4 - i) for i in range(5)):
            return "Diagonal"
        corners = [("B", 0), ("O", 0), ("B", 4), ("O", 4)]
        if all(is_marked(c, r) for c, r in corners):
            return "Four Corners"
        return "Bingo"

    # -- finish ---------------------------------------------------------------
    def _finish(self, winners=None):
        self.status = "finished"
        if self._draw_timer:
            self._draw_timer.cancel()
        winners_detail = []
        prize_each = 0
        jackpot_won_total = 0
        if winners:
            self.winners = winners
            prize_each = self.prize_per_winner // len(winners)
            # Jackpot payout rule: if this lobby's jackpot was armed (reached
            # its target on a previous game), the winner(s) of THIS game -
            # the very next bingo after arming - take the whole jackpot on
            # top of their normal prize. Split evenly if there's a tie,
            # same as the normal prize. Pays out exactly once, then the
            # jackpot resets to 0 and re-arms from scratch.
            if db.is_jackpot_armed(self.bet):
                jackpot_won_total = db.pay_out_jackpot(self.bet)
            jackpot_share = (jackpot_won_total // len(winners)) if jackpot_won_total else 0
            for w in winners:
                pattern = self.winning_pattern_name(w)
                total_payout = prize_each + jackpot_share
                w["jackpot_won"] = jackpot_share
                note = f"Bingo win ({self.bet} ETB game - {pattern})"
                if jackpot_share:
                    note += f" + JACKPOT {jackpot_share} ETB"
                db.credit_balance(w["user_id"], total_payout, note)
                d = db.load()
                uid = w["user_id"]
                if uid in d["players"]:
                    d["players"][uid]["total_wins"] += 1
                    d["players"][uid]["total_winnings"] += total_payout
                    d["players"][uid]["games_played"] += 1
                    d["players"][uid]["wins"].append({
                        "amount": total_payout, "bet": self.bet, "pattern": pattern,
                        "card_number": w.get("winning_card"), "jackpot": jackpot_share,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M")
                    })
                    db.save(d)
                winners_detail.append({"name": w["name"], "user_id": uid,
                                        "card_number": w.get("winning_card"),
                                        "pattern": pattern, "prize": total_payout,
                                        "jackpot": jackpot_share})
            _fire_callbacks("win", {
                "game_id": self.game_id, "bet": self.bet,
                "winners": [{"name": w["name"], "user_id": w["user_id"],
                             "card_number": w.get("winning_card"),
                             "pattern": self.winning_pattern_name(w)} for w in winners],
                "prize_each": prize_each + jackpot_share,
                "jackpot_share": jackpot_share,
            })
            if jackpot_won_total:
                _fire_callbacks("jackpot_paid", {
                    "bet": self.bet, "amount": jackpot_won_total,
                    "winners": [w["name"] for w in winners],
                })
            # Big-winner alert - configurable threshold (ETB), defaults to 300.
            big_win_threshold = db.get_settings().get("big_win_threshold", 300)
            if (prize_each + jackpot_share) >= big_win_threshold:
                _fire_callbacks("big_win", {
                    "bet": self.bet, "prize_each": prize_each + jackpot_share,
                    "winners": [w["name"] for w in winners],
                })
        else:
            _fire_callbacks("no_winner", {"game_id": self.game_id, "bet": self.bet})
        for slot in self.players.values():
            if not slot["won"] and not slot.get("is_bot"):
                d = db.load()
                uid = slot["user_id"]
                if uid in d["players"]:
                    d["players"][uid]["games_played"] += 1
                db.save(d)

        # Game history - survives restarts, browsable in admin, completely
        # separate from this in-memory GameRoom (which disappears in 10s).
        # Only log games that actually started (start_time set) - a room
        # ended by an admin while still "waiting" never really played.
        if self.start_time:
            real_player_count = sum(1 for s in self.players.values() if not s.get("is_bot"))
            bot_count = sum(1 for s in self.players.values() if s.get("is_bot"))
            db.record_game_history(
                game_id=self.game_id, bet=self.bet,
                real_player_count=real_player_count, bot_count=bot_count,
                total_pot=getattr(self, "total_pot", 0),
                commission=self.commission_amount, jackpot_cut=getattr(self, "jackpot_cut", 0),
                jackpot_paid=jackpot_won_total,
                winners=winners_detail,
                started_at=datetime.fromtimestamp(self.start_time).strftime("%Y-%m-%d %H:%M:%S"),
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        threading.Timer(10, lambda: _reset_lobby(self.bet)).start()

    def get_state(self, user_id=None):
        """Every field here is safe to show to ANY player in the lobby, whether
        or not they have a card or a balance - visibility is never gated by
        money (see requirement: everyone can see the live game & every card's
        taken/free status). Only `my_cards` is personal to the requester."""
        uid = str(user_id) if user_id else None
        my_cards = self.player_cards(uid) if uid else []
        won = any(c["number"] == self.players.get(uid, {}).get("winning_card")
                   for c in my_cards) if uid and self.players.get(uid) else False
        return {
            "game_id": self.game_id,
            "bet": self.bet,
            "status": self.status,
            "player_count": len(self.players),          # combined real + bots, never split for players
            "min_players": self.min_players,
            "total_cards": self.total_cards(),
            "taken_cards": self.taken_cards_view(),      # {card_number: {name}} - visible to everyone
            "drawn": self.drawn,
            "countdown_left": max(0, int(self.countdown_end - time.time())) if self.countdown_end else 0,
            "prize": self.prize_per_winner,
            "commission": self.commission_amount,
            "winners": [{"name": w["name"], "pattern": self.winning_pattern_name(w),
                         "card_number": w.get("winning_card"),
                         "jackpot_won": w.get("jackpot_won", 0)} for w in self.winners],
            "my_cards": my_cards,
            "won": bool(self.players.get(uid, {}).get("won")) if uid else False,
            "max_cards": MAX_CARDS_PER_PLAYER,
            "paused": self._paused,
        }


def _reset_lobby(bet):
    with _lobby_lock:
        key = str(bet)
        _games.pop(_get_lobby_game_id(bet), None)
        d = db.load()
        d["lobby_waiting"][key] = None
        db.save(d)
        _fire_callbacks("lobby_reset", {"bet": bet})


def _get_lobby_game_id(bet):
    d = db.load()
    return d["lobby_waiting"].get(str(bet))


def get_or_create_lobby(bet):
    with _lobby_lock:
        gid = _get_lobby_game_id(bet)
        if gid and gid in _games:
            return _games[gid]
        room = GameRoom(bet)
        _games[room.game_id] = room
        d = db.load()
        d["lobby_waiting"][str(bet)] = room.game_id
        db.save(d)
        return room


def get_game(game_id):
    return _games.get(game_id)


def _check_device_flags(user_id, device_id, ip):
    new_flags = db.register_device(user_id, device_id, ip)
    for f in new_flags or []:
        _fire_callbacks("suspicious_activity", {"user_id": str(user_id), **f})


def enter_lobby(user_id, name, bet, device_id=None, ip=None):
    """View a lobby (live state, card grid, taken cards) - NEVER charges
    anything and NEVER requires balance. Anyone can look; only select_card
    actually spends money."""
    if db.is_banned(user_id):
        raise ValueError("Your account has been suspended. Contact support.")
    db.get_or_create_player(user_id, name)
    if device_id or ip:
        _check_device_flags(user_id, device_id, ip)
    return get_or_create_lobby(bet)


def select_card(user_id, name, bet, card_number, device_id=None, ip=None):
    if device_id or ip:
        _check_device_flags(user_id, device_id, ip)
    room = get_or_create_lobby(bet)
    return room, room.select_card(user_id, name, card_number)


def deselect_card(user_id, bet, card_number):
    room = get_or_create_lobby(bet)
    room.deselect_card(user_id, card_number)
    return room


def all_lobby_states():
    states = []
    d = db.load()
    for cfg in db.LOBBY_CONFIGS:
        bet = cfg["bet"]
        gid = d["lobby_waiting"].get(str(bet))
        room = _games.get(gid) if gid else None
        jackpot = db.get_jackpot(bet)
        states.append({
            "bet": bet,
            "label": cfg["label"],
            "bonus": cfg["bonus"],
            "jackpot": jackpot,
            "jackpot_target": cfg["jackpot_target"],
            "player_count": len(room.players) if room else 0,  # combined, never split
            "status": room.status if room else "waiting",
            "prize": room.prize_per_winner if room else 0,
            "game_id": gid,
        })
    return states


# ---------------------------------------------------------------------------
# Admin controls over live games (admin view IS allowed to see the real/bot
# split and timestamps - only the player-facing state hides it)
# ---------------------------------------------------------------------------
def admin_list_active_games():
    out = []
    for gid, room in _games.items():
        out.append({
            "game_id": gid, "bet": room.bet, "status": room.status,
            "player_count": len(room.players),
            "real_player_count": sum(1 for s in room.players.values() if not s.get("is_bot")),
            "bot_count": sum(1 for s in room.players.values() if s.get("is_bot")),
            "drawn_count": len(room.drawn), "paused": room._paused,
            "created_at": datetime.fromtimestamp(room.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            "started_at": datetime.fromtimestamp(room.start_time).strftime("%Y-%m-%d %H:%M:%S") if room.start_time else "-",
        })
    return out


def admin_pause_game(game_id):
    room = _games.get(game_id)
    if room:
        room.pause()
        return True
    return False


def admin_resume_game(game_id):
    room = _games.get(game_id)
    if room:
        room.resume()
        return True
    return False


def admin_force_draw(game_id):
    room = _games.get(game_id)
    if room and room.status == "running":
        room.force_draw_now()
        return True
    return False


def admin_add_bots(game_id, count):
    room = _games.get(game_id)
    if room and room.status == "waiting":
        room.add_bots_now(count)
        return True
    return False


def admin_end_game(game_id):
    room = _games.get(game_id)
    if room:
        room._finish()
        return True
    return False


def admin_reset_jackpot(bet):
    """Manual override: wipes a lobby's jackpot pool and un-arms it WITHOUT
    paying anyone - use only to correct a mistake, not as normal flow. The
    normal flow is automatic: pay_out_jackpot() fires from _finish() the
    moment the next winner appears after arming."""
    db.reset_jackpot(bet)
    return True
