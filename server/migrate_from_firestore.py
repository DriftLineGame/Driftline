"""
Driftline: copy everything from the old Firebase cloud into this server.

Run once, with the Driftline server window CLOSED:
    python migrate_from_firestore.py

Copies every player (username, PIN, bag, coins, friends, battle record),
battle, market listing and gift code from Firestore into data/driftline.db.

Safe to run again: players already on this server are skipped (so nobody's
new progress is overwritten) unless you add --overwrite.

Options:
  --dry-run          read and report, save nothing
  --overwrite        replace players that already exist here
  --from-file x.json import from a saved export instead of downloading
"""
import hashlib, json, os, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DRIFTLINE_NO_BACKUP", "1")
import driftline_server as S  # noqa: E402  (opens the same database)

PROJECT = "driftline-e4e88"
KEY = "AIzaSyD12UpqmtQFI8GoohpTLX0cYe1CeX2s7s0"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
ARGS = sys.argv[1:]
DRY = "--dry-run" in ARGS
OVERWRITE = "--overwrite" in ARGS
FROM_FILE = ARGS[ARGS.index("--from-file") + 1] if "--from-file" in ARGS else None
# retired with reeling in v3.0: unused ones are refunded at full price
REFUND = {"steady_hands": 70, "line_guard": 45, "lucky_charm": 130}


def decode(v):
    if v is None or "nullValue" in v:
        return None
    if "booleanValue" in v:
        return v["booleanValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "stringValue" in v:
        return v["stringValue"]
    if "timestampValue" in v:
        import datetime
        return int(datetime.datetime.fromisoformat(v["timestampValue"].replace("Z", "+00:00")).timestamp() * 1000)
    if "arrayValue" in v:
        return [decode(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: decode(x) for k, x in v["mapValue"].get("fields", {}).items()}
    return None


def fetch(name):
    docs, token = [], ""
    while True:
        url = f"{BASE}/{name}?pageSize=300&key={KEY}" + (f"&pageToken={urllib.parse.quote(token)}" if token else "")
        with urllib.request.urlopen(url, timeout=60) as r:
            js = json.loads(r.read())
        for d in js.get("documents", []):
            docs.append({"id": d["name"].split("/")[-1], **{k: decode(x) for k, x in d.get("fields", {}).items()}})
        print(f"\r  {name}: {len(docs)} downloaded", end="", flush=True)
        token = js.get("nextPageToken")
        if not token:
            print()
            return docs


def legacy_id(c):
    return "c_" + S.base36(int(c.get("ts") or 0)) + "_" + hashlib.sha1(f"{c['name']}|{c['tier']}|{c.get('ts')}".encode()).hexdigest()[:6]


def clean_shop(shop, rep):
    if not isinstance(shop, dict):
        return None
    s = dict(shop)
    if s.pop("subscriptions", None) or s.pop("subscription", None):
        rep["subs"] += 1
    for k in ("lineSlowUntil", "lineSlowAmount", "redGraceBoostUntil", "redGraceBoostAmount"):
        s.pop(k, None)
    inv = dict(s.get("inventory") or {})
    refund = 0
    for k, price in REFUND.items():
        n = max(0, int(inv.pop(k, 0) or 0))
        refund += n * price
        rep["items"] += n
    s["inventory"] = inv
    if refund:
        s["coinsSpent"] = (s.get("coinsSpent") or 0) - refund
        rep["coins"] += refund
    return s


def main():
    print("\nDriftline migration: old Firebase cloud -> this server" + (" (dry run, nothing saved)" if DRY else "") + "\n")
    if FROM_FILE:
        src = json.load(open(FROM_FILE, encoding="utf-8"))
    else:
        print("Downloading from Firestore…")
        src = {n: fetch(n) for n in ("players", "battles", "listings", "giftCodes")}
        os.makedirs(S.P("backups"), exist_ok=True)
        out = S.P("backups", "firestore-export.json")
        json.dump(src, open(out, "w", encoding="utf-8"))
        print(f"Saved a copy of the download to {out}")
    rep = {"players": 0, "skipped": 0, "replaced": 0, "nopin": 0, "subs": 0, "items": 0, "coins": 0, "battles": 0, "listings": 0, "gifts": 0}
    db = S.db
    if not DRY:
        db.execute("BEGIN")
    try:
        for d in src.get("players", []):
            bag = []
            for c in d.get("bag") or []:
                if not isinstance(c, dict) or not c.get("name") or not c.get("tier"):
                    continue
                c = dict(c, ts=float(c.get("ts") or 0), bonus=float(c.get("bonus") or 1))
                c["id"] = c.get("id") or legacy_id(c)
                bag.append(c)
            data = {"bag": bag, "shop": clean_shop(d.get("shop"), rep), "prawn": d.get("prawn"), "crab": d.get("crab"), "gold": d.get("gold"),
                    "battles": d.get("battles"), "friends": d.get("friends") or [], "incomingRequests": d.get("incomingRequests") or [],
                    "outgoingRequests": d.get("outgoingRequests") or []}
            uid = d["id"]
            if not d.get("pinHash"):
                rep["nopin"] += 1
            exists = S.q1("SELECT 1 FROM players WHERE uid=?", uid)
            if exists and not OVERWRITE:
                rep["skipped"] += 1
                continue
            if exists:
                rep["replaced"] += 1
            if not DRY:
                db.execute("""INSERT INTO players(uid, displayName, pinHash, pinScrypt, points, createdAt, updatedAt, lastBagSyncAt, data)
                              VALUES(?,?,?,NULL,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET displayName=excluded.displayName, pinHash=excluded.pinHash,
                              pinScrypt=NULL, points=excluded.points, updatedAt=excluded.updatedAt, data=excluded.data""",
                           (uid, d.get("displayName") or uid, d.get("pinHash"), S.compute_points(bag), int(d.get("createdAt") or S.now()),
                            int(d.get("updatedAt") or S.now()), S.now(), json.dumps(data, separators=(",", ":"))))
            rep["players"] += 1
        for b in src.get("battles", []):
            bid = b.pop("id")
            if not DRY:
                db.execute("INSERT OR IGNORE INTO battles VALUES(?,?,?,?,?)", (bid, b.get("player1Uid"), b.get("player2Uid"), b.get("status") or "pending", json.dumps(b)))
            rep["battles"] += 1
        for l in src.get("listings", []):
            lid = l.pop("id")
            if not DRY:
                db.execute("INSERT OR IGNORE INTO listings VALUES(?,?,?,?,?)", (lid, l.get("sellerUid") or "", l.get("status") or "cancelled", int(l.get("createdAt") or 0), json.dumps(l)))
            rep["listings"] += 1
        for g in src.get("giftCodes", []):
            if not DRY:
                db.execute("INSERT OR IGNORE INTO giftCodes VALUES(?,?,?,?,?,?,?)", (g["id"], int(g.get("amount") or 0), 1 if g.get("redeemed") else 0,
                           g.get("redeemedBy"), g.get("redeemedAt"), int(g.get("createdAt") or S.now()), g.get("createdBy")))
            rep["gifts"] += 1
        if not DRY:
            db.execute("COMMIT")
    except Exception:
        if not DRY:
            db.execute("ROLLBACK")
        raise
    print("\nDone." + (" (dry run: nothing saved)" if DRY else ""))
    print(f"  Players copied:          {rep['players']}" + (f" ({rep['replaced']} replaced)" if rep["replaced"] else ""))
    if rep["skipped"]:
        print(f"  Players skipped:         {rep['skipped']} (already on this server)")
    if rep["nopin"]:
        print(f"  Accounts without a PIN:  {rep['nopin']} (they can't sign in; reset their PIN on the admin page)")
    print(f"  Subscriptions ended:     {rep['subs']}")
    print(f"  Old items refunded:      {rep['items']} ({rep['coins']:,} Chum Coins returned)")
    print(f"  Battles:                 {rep['battles']}")
    print(f"  Market listings:         {rep['listings']}")
    print(f"  Gift codes:              {rep['gifts']}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nMigration stopped: {e}\nNothing was changed. Check the internet connection and try again.")
        sys.exit(1)
