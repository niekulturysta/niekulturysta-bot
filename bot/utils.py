# bot/utils.py
TARGET_BANS = {
    "klatka": {"wiosł", "podciągan", "martwy"},
    "barki":  {"wiosł", "podciągan", "martwy"},
    # możesz dodać kolejne reguły, np. dla "nogi" itd.
}

def infer_targets(q: str):
    ql = q.lower()
    found = set()
    for t in TARGET_BANS.keys():
        if t in ql:
            found.add(t)
    # heurystyka: "trening" też traktujemy jako wskazanie, ale bez banów
    if "trening" in ql and not found:
        found.add("trening")
    return found

def violates_targets(text: str, targets: set[str]) -> bool:
    if not targets:
        return False
    tl = text.lower()
    bans = set()
    for t in targets:
        bans |= TARGET_BANS.get(t, set())
    return any(b in tl for b in bans)
