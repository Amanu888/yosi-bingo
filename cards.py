"""
Bingo card generation (75-ball, 5x5, B-I-N-G-O columns, free center).

IMPORTANT: Cards are generated ONCE and stored in cards_pool.json.
The pool is never regenerated automatically - every player always gets
one of these same fixed, unique cards. This file's __main__ block is
only ever run manually by the admin (e.g. to grow the pool), never by
the running server.
"""
import random
import json

COLUMN_RANGES = {
    "B": (1, 15),
    "I": (16, 30),
    "N": (31, 45),
    "G": (46, 60),
    "O": (61, 75),
}
COLUMNS = ["B", "I", "N", "G", "O"]


def generate_card():
    """Generate one 5x5 card as a dict of column -> list of 5 numbers (row order).
    Center of N column is 'FREE'."""
    card = {}
    for col in COLUMNS:
        lo, hi = COLUMN_RANGES[col]
        nums = random.sample(range(lo, hi + 1), 5)
        card[col] = nums
    card["N"][2] = "FREE"
    return card


def card_signature(card):
    """A hashable signature to detect duplicate cards regardless of generation order."""
    return tuple(tuple(card[c]) for c in COLUMNS)


def generate_unique_cards(n=200, existing=None, max_attempts=200000):
    """Generate n NEW unique bingo cards, avoiding any signature already in `existing`."""
    seen = set()
    if existing:
        for c in existing:
            seen.add(card_signature(c))
    cards = []
    attempts = 0
    while len(cards) < n and attempts < max_attempts:
        attempts += 1
        c = generate_card()
        sig = card_signature(c)
        if sig in seen:
            continue
        seen.add(sig)
        cards.append(c)
    if len(cards) < n:
        raise RuntimeError(f"Could only generate {len(cards)} unique cards out of {n} requested")
    return cards


def card_all_numbers(card):
    """Flat set of all numbers on a card (excluding FREE)."""
    nums = set()
    for col in COLUMNS:
        for v in card[col]:
            if v != "FREE":
                nums.add(v)
    return nums


def save_cards(cards, path="cards_pool.json"):
    with open(path, "w") as f:
        json.dump(cards, f)


def load_cards(path="cards_pool.json"):
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    # Manual admin utility only. Running this appends fresh unique cards
    # to the existing pool - it never removes or changes existing cards,
    # so every card already handed out to a player keeps the exact same
    # numbers forever. Usage: python3 cards.py 100   (adds 100 new cards)
    import sys
    random.seed()
    add_n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    try:
        existing = load_cards()
    except FileNotFoundError:
        existing = []
    new_cards = generate_unique_cards(add_n, existing=existing)
    pool = existing + new_cards
    save_cards(pool)
    print(f"Pool now has {len(pool)} unique cards ({len(new_cards)} newly added).")
