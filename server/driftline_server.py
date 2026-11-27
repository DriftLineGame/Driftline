"""
Driftline game server · Powered by SCODE
- Public game API on 0.0.0.0:8766 (reached through a Cloudflare quick tunnel)
- Admin page on 127.0.0.1:5051 (this computer only, never tunnelled)
Run with:  python run.py              (starts this server and the tunnel)
       or: python driftline_server.py (server only)

Built to be light on a capped home connection: phones send only new
catches (never the whole bag), leaderboards are totalled here instead of
shipping every player's bag, and bigger replies are gzip-compressed.
"""
import asyncio, datetime, hashlib, hmac, json, math, os, random, re, secrets, sqlite3, sys, time
from collections import defaultdict

from aiohttp import web, ClientSession, ClientTimeout

VERSION = "3.0"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
P = lambda *a: os.path.join(HERE, *a)
START_TIME = time.time()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ======================= CONFIG =======================
DEFAULT_CONFIG = {
    "port": 8766,
    "admin_port": 5051,
    "allowed_origins": ["https://driftlinegame.github.io"],
    "site_url": "https://driftlinegame.github.io/Driftline/",
    "admin_username": "admin",
    "use_overpass": True,
    "overpass_url": "https://overpass-api.de/api/interpreter",
    "backup_keep_days": 14,
    "repo_dir": "..",
    "auto_push_server_json": True,
    "cloudflared_path": "",
}


def load_config():
    path = P("config.json")
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            cfg.update(json.load(open(path, encoding="utf-8")))
        except Exception as e:
            log("!! config.json could not be read:", e)
    else:
        json.dump(DEFAULT_CONFIG, open(path, "w", encoding="utf-8"), indent=2)
    if os.environ.get("DRIFTLINE_PORT"):
        cfg["port"] = int(os.environ["DRIFTLINE_PORT"])
    if os.environ.get("DRIFTLINE_ADMIN_PORT"):
        cfg["admin_port"] = int(os.environ["DRIFTLINE_ADMIN_PORT"])
    if os.environ.get("DRIFTLINE_NO_OVERPASS"):
        cfg["use_overpass"] = False
    if os.environ.get("DRIFTLINE_OVERPASS_URL"):
        cfg["overpass_url"] = os.environ["DRIFTLINE_OVERPASS_URL"]
    return cfg


CFG = load_config()


def admin_password():
    path = P("admin-password.txt")
    if not os.path.exists(path):
        pw = secrets.token_urlsafe(12)
        open(path, "w").write(pw)
        print(f"\n*** Admin password created: {pw}  (saved in admin-password.txt)\n", flush=True)
    return open(path).read().strip()


# ======================= FISH DATA =======================
FISH = json.load(open(P("fish_data.json"), encoding="utf-8"))
TIERS = ["common", "hard", "rare", "superrare", "ultimate", "abyssal"]
FISH_SET = {(n, t) for t in TIERS for n in FISH.get(t, [])}
try:
    CATALOG = json.load(open(P("shop_catalog.json"), encoding="utf-8"))
except Exception:
    CATALOG = {"cosmetics": {}}
    log("!! shop_catalog.json missing: skin purchases will be refused.")

# ======================= DATABASE =======================
DB_PATH = os.environ.get("DRIFTLINE_DB") or P("data", "driftline.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS players(
  uid TEXT PRIMARY KEY, displayName TEXT NOT NULL, pinHash TEXT, pinScrypt TEXT,
  points REAL NOT NULL DEFAULT 0, createdAt INTEGER NOT NULL, updatedAt INTEGER NOT NULL,
  lastBagSyncAt INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_players_points ON players(points DESC);
CREATE INDEX IF NOT EXISTS idx_players_updated ON players(updatedAt DESC);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, uid TEXT NOT NULL, createdAt INTEGER NOT NULL, lastSeen INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_uid ON sessions(uid);
CREATE TABLE IF NOT EXISTS battles(id TEXT PRIMARY KEY, p1 TEXT, p2 TEXT, status TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listings(id TEXT PRIMARY KEY, sellerUid TEXT NOT NULL, status TEXT NOT NULL, createdAt INTEGER NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status, createdAt DESC);
CREATE INDEX IF NOT EXISTS idx_listings_seller ON listings(sellerUid, createdAt DESC);
CREATE TABLE IF NOT EXISTS giftCodes(code TEXT PRIMARY KEY, amount INTEGER NOT NULL, redeemed INTEGER NOT NULL DEFAULT 0,
  redeemedBy TEXT, redeemedAt INTEGER, createdAt INTEGER NOT NULL, createdBy TEXT);
CREATE TABLE IF NOT EXISTS dock_cells(cell TEXT PRIMARY KEY, fetchedAt INTEGER NOT NULL, ok INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS docks(id TEXT PRIMARY KEY, cell TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_docks_cell ON docks(cell);
CREATE TABLE IF NOT EXISTS spots(dockId TEXT PRIMARY KEY, crew TEXT, strength INTEGER NOT NULL DEFAULT 0, updatedAt INTEGER NOT NULL, lastByUid TEXT);
CREATE TABLE IF NOT EXISTS raids(id TEXT PRIMARY KEY, dockId TEXT NOT NULL, win INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS traffic(day TEXT PRIMARY KEY, bytes_out INTEGER NOT NULL DEFAULT 0, bytes_in INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0);
""")


class tx:
    """BEGIN IMMEDIATE ... COMMIT. Handlers are synchronous and the server is
    single-threaded, so this is a real atomic transaction."""
    def __enter__(self):
        db.execute("BEGIN IMMEDIATE")

    def __exit__(self, et, ev, tb):
        db.execute("COMMIT" if et is None else "ROLLBACK")
        return False


def q(sql, *a):
    return db.execute(sql, a).fetchall()


def q1(sql, *a):
    return db.execute(sql, a).fetchone()


# ======================= HELPERS =======================
class ApiError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def bad(msg, code=None):
    return ApiError(400, msg, code)


def now():
    return int(time.time() * 1000)


def rid(prefix, n=6):
    return prefix + secrets.token_hex(n)


def server_catch_id():
    return "s_" + base36(now()) + "_" + secrets.token_hex(4)


def base36(n):
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    n = int(n)
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = chars[r] + out
        if n == 0:
            return out


def hash01(s):
    """Deterministic number in [0,1) from a string (buoys, raid schedule)."""
    return int.from_bytes(hashlib.sha256(s.encode()).digest()[:4], "big") / 4294967296


def pick_weighted(entries, r=None):
    r = random.random() if r is None else r
    total = sum(w for _, w in entries)
    x = r * total
    for v, w in entries:
        if x < w:
            return v
        x -= w
    return entries[-1][0]


def pick_fish(tier):
    lst = FISH.get(tier) or FISH["common"]
    return random.choice(lst)


def norm_username(u):
    return re.sub(r"[^a-z0-9_]", "", str(u or "").strip().lower())


def legacy_pin_hash(pin):
    # exactly how v2.x hashed PINs in the browser, so migrated players keep their PIN
    return hashlib.sha256(("driftline-salt-v1-" + pin).encode()).hexdigest()


def scrypt_hash(pin):
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(str(pin).encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + key.hex()


def scrypt_verify(pin, stored):
    parts = str(stored or "").split("$")
    if len(parts) != 3:
        return False
    want = bytes.fromhex(parts[2])
    got = hashlib.scrypt(str(pin).encode(), salt=bytes.fromhex(parts[1]), n=16384, r=8, p=1, dklen=len(want))
    return hmac.compare_digest(want, got)


def dist_m(a, b):
    R = 6371000
    dlat = math.radians(b["lat"] - a["lat"])
    dlng = math.radians(b["lng"] - a["lng"])
    s = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a["lat"])) * math.cos(math.radians(b["lat"])) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(s))


def num(v, name):
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise bad(f"{name} must be a number.")
    if not math.isfinite(n):
        raise bad(f"{name} must be a number.")
    return n


def latlng(body):
    lat, lng = num(body.get("lat"), "lat"), num(body.get("lng"), "lng")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise bad("That location is out of range.")
    return {"lat": lat, "lng": lng}


def tier_index(t):
    return TIERS.index(t) if t in TIERS else -1


def fish_points(c):
    return (1 + max(0, tier_index(c.get("tier")))) * (c.get("bonus") or 1)


def compute_points(bag):
    # must match computePoints() in the game page
    return sum(fish_points(c) for c in (bag or []) if c)


ROD_BASE = [10, 50, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000]


def rod_threshold(i):
    if i < len(ROD_BASE):
        return ROD_BASE[i]
    last = ROD_BASE[-1]
    for _ in range(len(ROD_BASE), i + 1):
        last = round(last * 1.5 / 50) * 50
    return last


def rod_level(p):
    l = 0
    while p >= rod_threshold(l) and l < 200:
        l += 1
    return l


def utc_day(t_ms=None):
    return datetime.datetime.fromtimestamp((t_ms or now()) / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")


# ======================= PLAYER MODEL =======================
def default_play():
    return {"crew": None, "crewChangedAt": 0, "bait": {"worm": 5, "shrimp": 2, "glow": 0},
            "eggs": [], "eggProgressAt": 0, "dockSpins": {}, "raidPasses": {"day": None, "used": 0, "bought": 0},
            "raidLastHitAt": 0, "inbox": [],
            "stats": {"spins": 0, "eggsHatched": 0, "raidsWon": 0, "docksClaimed": 0, "kmWalked": 0}}


def load_player(uid):
    row = q1("SELECT * FROM players WHERE uid=?", uid)
    if not row:
        return None
    p = dict(row)
    d = json.loads(p["data"])
    dp = default_play()
    play = {**dp, **(d.get("play") or {})}
    for k in ("stats", "bait", "raidPasses"):
        play[k] = {**dp[k], **((d.get("play") or {}).get(k) or {})}
    d["play"] = play
    p["data"] = d
    return p


def save_player(p):
    p["points"] = compute_points(p["data"].get("bag"))
    p["updatedAt"] = now()
    db.execute("UPDATE players SET displayName=?, pinHash=?, pinScrypt=?, points=?, updatedAt=?, lastBagSyncAt=?, data=? WHERE uid=?",
               (p["displayName"], p.get("pinHash"), p.get("pinScrypt"), p["points"], p["updatedAt"], p.get("lastBagSyncAt") or 0,
                json.dumps(p["data"], separators=(",", ":")), p["uid"]))


def coins_of(p):
    return math.floor(compute_points(p["data"].get("bag")) - ((p["data"].get("shop") or {}).get("coinsSpent") or 0))


def adjust_spent(p, delta):
    shop = p["data"].setdefault("shop", {}) or {}
    p["data"]["shop"] = shop
    shop["coinsSpent"] = (shop.get("coinsSpent") or 0) + delta
    shop["updatedAt"] = now()


def equipped(d, key, dflt):
    return ((d.get("shop") or {}).get("equipped") or {}).get(key) or dflt


def public_summary(uid, name, points, d):
    return {"uid": uid, "displayName": name, "points": points or 0,
            "avatar": equipped(d, "avatar", "default"), "banner": equipped(d, "banner", "none"),
            "crew": (d.get("play") or {}).get("crew")}


def refresh_passes(play):
    day = utc_day()
    if play["raidPasses"].get("day") != day:
        play["raidPasses"]["day"] = day
        play["raidPasses"]["used"] = 0


def play_payload(p):
    play = p["data"]["play"]
    refresh_passes(play)
    return {"crew": play["crew"], "crewChangedAt": play["crewChangedAt"], "bait": play["bait"], "eggs": play["eggs"],
            "stats": play["stats"],
            "raidPasses": {"free": 0 if play["raidPasses"]["used"] >= 1 else 1, "bought": play["raidPasses"]["bought"]},
            "coinsSpent": (p["data"].get("shop") or {}).get("coinsSpent") or 0,
            "points": compute_points(p["data"].get("bag"))}


def player_payload(p):
    d = p["data"]
    return {"uid": p["uid"], "displayName": p["displayName"], "points": compute_points(d.get("bag")),
            "bag": d.get("bag") or [], "shop": d.get("shop"), "prawn": d.get("prawn"), "crab": d.get("crab"),
            "gold": d.get("gold"), "battles": d.get("battles"), "play": play_payload(p)}


def is_admin(uid):
    return uid == norm_username(CFG["admin_username"])


def sanitize_catch(c):
    if not isinstance(c, dict):
        return None
    name = c.get("name") if isinstance(c.get("name"), str) else None
    tier = c.get("tier") if c.get("tier") in TIERS else None
    if not name or not tier:
        return None

    def fnum(v, d):
        try:
            v = float(v)
            return v if math.isfinite(v) else d
        except (TypeError, ValueError):
            return d
    out = {"id": c["id"] if isinstance(c.get("id"), str) and len(c["id"]) <= 60 else None,
           "name": name[:80], "tier": tier, "ts": fnum(c.get("ts"), now()),
           "lat": fnum(c.get("lat"), 0), "lng": fnum(c.get("lng"), 0),
           "bonus": min(10, max(1, fnum(c.get("bonus"), 1) or 1))}
    if isinstance(c.get("src"), str):
        out["src"] = c["src"][:16]
    return out


def stable_legacy_id(c):
    return "c_" + base36(math.floor(c["ts"])) + "_" + hashlib.sha1(f"{c['name']}|{c['tier']}|{c['ts']}".encode()).hexdigest()[:6]


# ======================= SESSIONS + LIMITS =======================
SESSION_TTL = 180 * 24 * 3600 * 1000
LAST_SEEN = {}  # uid -> ms (for the admin page)


def create_session(uid):
    t = secrets.token_hex(32)
    db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (t, uid, now(), now()))
    return t


def auth_uid(request):
    m = re.fullmatch(r"Bearer ([a-f0-9]{64})", request.headers.get("Authorization", ""))
    if not m:
        return None
    s = q1("SELECT * FROM sessions WHERE token=?", m.group(1))
    if not s:
        return None
    if now() - s["lastSeen"] > SESSION_TTL:
        db.execute("DELETE FROM sessions WHERE token=?", (m.group(1),))
        return None
    if now() - s["lastSeen"] > 3600 * 1000:
        db.execute("UPDATE sessions SET lastSeen=? WHERE token=?", (now(), m.group(1)))
    LAST_SEEN[s["uid"]] = now()
    return s["uid"]


class Limiter:
    def __init__(self):
        self.hits = defaultdict(list)

    def ok(self, key, n, window):
        t = time.time()
        h = [x for x in self.hits[key] if x > t - window]
        if len(h) >= n:
            self.hits[key] = h
            return False
        h.append(t)
        self.hits[key] = h
        return True

    def count(self, key, window):
        t = time.time()
        self.hits[key] = [x for x in self.hits[key] if x > t - window]
        return len(self.hits[key])

    def add(self, key):
        self.hits[key].append(time.time())

    def clear(self, key):
        self.hits.pop(key, None)

    def prune(self):
        t = time.time()
        for k in list(self.hits):
            self.hits[k] = [x for x in self.hits[k] if x > t - 3600]
            if not self.hits[k]:
                del self.hits[k]


LIMIT = Limiter()
FAILS = Limiter()

# ======================= ACCOUNTS =======================
COSMETIC_TYPES = {"avatar": "avatars", "hook": "hooks", "marker": "markers", "line": "lines", "banner": "banners", "aura": "auras"}
DEFAULT_OWNED = {"avatars": ["default"], "hooks": ["classic"], "markers": ["classic"], "lines": ["classic"], "banners": ["none"], "auras": ["none"]}


def cosmetic_price(type_, id_):
    for c in (CATALOG.get("cosmetics") or {}).get(type_, []):
        if c["id"] == id_:
            return c["price"]
    return None


def sanitize_guest_bag(bag):
    # fish caught before making an account come along, within reason
    allowed = {"common", "hard", "rare", "superrare"}
    out = []
    for raw in bag if isinstance(bag, list) else []:
        c = sanitize_catch(raw)
        if not c or c["tier"] not in allowed or (c["name"], c["tier"]) not in FISH_SET:
            continue
        c["id"] = "c_" + base36(now()) + "_" + secrets.token_hex(3)
        c["bonus"] = min(c["bonus"], 2)
        out.append(c)
        if len(out) >= 50:
            break
    return out


def api_signup(body, ip):
    username = str(body.get("username") or "").strip()
    pin = str(body.get("pin") or "")
    uid = norm_username(username)
    if not 3 <= len(uid) <= 20:
        raise bad("Username must be 3-20 characters (letters, numbers, underscore).")
    if not re.fullmatch(r"\d{6}", pin):
        raise bad("PIN must be exactly 6 digits.")
    if not LIMIT.ok(("signup", ip), 5, 3600):
        raise ApiError(429, "Too many new accounts from this connection. Try again later.")
    if q1("SELECT 1 FROM players WHERE uid=?", uid):
        raise bad("That username is already taken.")
    t = now()
    bag = sanitize_guest_bag(body.get("guestBag"))
    shop = {"coinsSpent": 0, "ownedCosmetics": json.loads(json.dumps(DEFAULT_OWNED)),
            "equipped": {"avatar": "default", "hook": "classic", "marker": "classic", "line": "classic", "banner": "none", "aura": "none"},
            "updatedAt": t}
    gs = body.get("guestShop") if isinstance(body.get("guestShop"), dict) else None
    if gs:
        budget, cost = compute_points(bag), 0
        owned = gs.get("ownedCosmetics") or {}
        for type_, key in COSMETIC_TYPES.items():
            for cid in owned.get(key) or []:
                price = cosmetic_price(type_, cid)
                if price is None or cid in shop["ownedCosmetics"][key] or cost + price > budget:
                    continue
                cost += price
                shop["ownedCosmetics"][key].append(cid)
        try:
            gspent = float(gs.get("coinsSpent") or 0)
        except (TypeError, ValueError):
            gspent = 0
        shop["coinsSpent"] = min(budget, max(cost, gspent))
        for type_, key in COSMETIC_TYPES.items():
            want = (gs.get("equipped") or {}).get(type_)
            if want and want in shop["ownedCosmetics"][key]:
                shop["equipped"][type_] = want
    data = {"bag": bag, "shop": shop, "prawn": None, "crab": None, "gold": None, "battles": None,
            "friends": [], "incomingRequests": [], "outgoingRequests": [], "play": default_play()}
    db.execute("INSERT INTO players VALUES(?,?,?,?,?,?,?,?,?)",
               (uid, username, None, scrypt_hash(pin), compute_points(bag), t, t, t, json.dumps(data, separators=(",", ":"))))
    return {"token": create_session(uid), "player": player_payload(load_player(uid))}


def api_signin(body, ip):
    uid = norm_username(body.get("username"))
    pin = str(body.get("pin") or "")
    if FAILS.count(("u", uid), 900) >= 6:
        raise ApiError(429, "Too many wrong PINs for this username. Wait 15 minutes, then try again.")
    if FAILS.count(("ip", ip), 3600) >= 30:
        raise ApiError(429, "Too many sign-in attempts from this connection. Wait an hour, then try again.")
    p = load_player(uid)
    if not p:
        FAILS.add(("ip", ip))
        raise bad("No account found with that username.")
    ok = scrypt_verify(pin, p["pinScrypt"]) if p.get("pinScrypt") else (bool(p.get("pinHash")) and hmac.compare_digest(legacy_pin_hash(pin), p["pinHash"]))
    if not ok:
        FAILS.add(("u", uid))
        FAILS.add(("ip", ip))
        raise bad("Incorrect PIN.")
    FAILS.clear(("u", uid))
    if not p.get("pinScrypt"):
        # first sign-in after moving from the old cloud: upgrade the hash
        p["pinScrypt"], p["pinHash"] = scrypt_hash(pin), None
        db.execute("UPDATE players SET pinHash=NULL, pinScrypt=? WHERE uid=?", (p["pinScrypt"], uid))
    LAST_SEEN[uid] = now()
    return {"token": create_session(uid), "player": player_payload(p)}


# ======================= CATCHES (deltas only) =======================
def sold_catch_ids(uid):
    out = set()
    for r in q("SELECT data FROM listings WHERE sellerUid=? AND status='sold'", uid):
        try:
            out.add(json.loads(r["data"]).get("catchId"))
        except Exception:
            pass
    return out


def api_catches(uid, body):
    """The phone sends only catches the server hasn't seen yet."""
    incoming = body.get("catches")
    if not isinstance(incoming, list) or len(incoming) > 600:
        raise bad("Bad catch list.")
    with tx():
        p = load_player(uid)
        bag = p["data"].get("bag") or []
        have = {c.get("id") for c in bag}
        sold = sold_catch_ids(uid)
        elapsed = max(0, (now() - (p.get("lastBagSyncAt") or 0)) / 1000)
        allowance = min(500, 25 + int(elapsed / 3))  # a phone can only catch so fast
        accepted, rejected, fresh = [], [], []
        for raw in incoming:
            c = sanitize_catch(raw)
            if not c:
                continue
            if not c["id"]:
                c["id"] = stable_legacy_id(c)
            if c["id"] in have:
                accepted.append(c["id"])  # already saved (a retried send)
                continue
            if c["id"].startswith("s_") or c["id"] in sold or (c["name"], c["tier"]) not in FISH_SET or len(fresh) >= allowance:
                rejected.append(c["id"])
                continue
            c["ts"] = min(c["ts"], now() + 60000)
            fresh.append(c)
            have.add(c["id"])
            accepted.append(c["id"])
        if fresh:
            bag = sorted(fresh + bag, key=lambda c: -(c.get("ts") or 0))
            p["data"]["bag"] = bag
        p["lastBagSyncAt"] = now()
        save_player(p)
        return {"points": compute_points(bag), "accepted": accepted, "rejected": rejected}


SHOP_SKIP = {"coinsSpent", "ownedCosmetics", "subscriptions", "subscription", "equipped"}


def api_sync_shop(uid, body):
    incoming = body.get("shop") if isinstance(body.get("shop"), dict) else {}
    with tx():
        p = load_player(uid)
        shop = p["data"].get("shop") or {}
        for k, v in incoming.items():
            if k in SHOP_SKIP or not re.fullmatch(r"[a-zA-Z]{1,40}", k):
                continue
            if len(json.dumps(v)) > 20000:
                continue
            if k == "bonusRodLevels":
                # rod tokens are only ever bought, so a phone with an old copy can't lower them
                try:
                    v = max(int(float(v)), int(shop.get(k) or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
            elif k == "unlockedAchievements" and isinstance(v, list):
                v = list(shop.get(k) or []) + [a for a in v if a not in (shop.get(k) or [])]
            shop[k] = v
        if isinstance(shop.get("pointBoostMultiplier"), (int, float)):
            shop["pointBoostMultiplier"] = min(3, max(1, shop["pointBoostMultiplier"]))
        owned = shop.get("ownedCosmetics") or DEFAULT_OWNED
        eq_in = incoming.get("equipped") or {}
        shop["equipped"] = shop.get("equipped") or {}
        for type_, key in COSMETIC_TYPES.items():
            want = eq_in.get(type_)
            if not want or want == shop["equipped"].get(type_):
                continue
            if want in (owned.get(key) or []) or want in DEFAULT_OWNED[key]:
                shop["equipped"][type_] = want
        p["data"]["shop"] = shop
        save_player(p)
        return {"ok": True, "equipped": shop["equipped"]}


def api_spend(uid, body):
    # only spending goes through here; the page can never add coins
    amount = math.floor(num(body.get("amount"), "amount"))
    if amount <= 0 or amount > 10_000_000:
        raise bad("Invalid amount.")
    with tx():
        p = load_player(uid)
        adjust_spent(p, amount)
        save_player(p)
        return {"coinsSpent": p["data"]["shop"]["coinsSpent"]}


def api_buy_cosmetic(uid, body):
    type_, cid = str(body.get("type") or ""), str(body.get("id") or "")
    key = COSMETIC_TYPES.get(type_)
    if not key:
        raise bad("Unknown item type.")
    price = cosmetic_price(type_, cid)
    if price is None:
        raise bad("That item isn’t in the shop.")
    with tx():
        p = load_player(uid)
        shop = p["data"].setdefault("shop", {}) or {}
        p["data"]["shop"] = shop
        owned = shop.setdefault("ownedCosmetics", json.loads(json.dumps(DEFAULT_OWNED)))
        owned.setdefault(key, list(DEFAULT_OWNED[key]))
        if cid in owned[key]:
            return {"ok": True, "alreadyOwned": True, "coinsSpent": shop.get("coinsSpent") or 0, "ownedCosmetics": owned}
        if coins_of(p) < price:
            raise bad("Not enough Chum Coins.")
        adjust_spent(p, price)
        owned[key].append(cid)
        save_player(p)
        return {"ok": True, "coinsSpent": shop["coinsSpent"], "ownedCosmetics": owned}


def sanitize_trap(kind, t):
    if t is None:
        return None
    if not isinstance(t, dict):
        raise bad("Bad trap data.")
    out = {"lat": num(t.get("lat"), "lat"), "lng": num(t.get("lng"), "lng"),
           "deployedAt": num(t.get("deployedAt"), "deployedAt"), "resolveAt": num(t.get("resolveAt"), "resolveAt")}
    if kind != "gold":
        out["odds"] = min(95, max(0, num(t.get("odds"), "odds")))
        out["wasHotspot"] = bool(t.get("wasHotspot"))
        out["attempts"] = min(10 if kind == "crab" else 5, max(1, math.floor(num(t.get("attempts"), "attempts"))))
    return out


def api_sync_trap(uid, body):
    kind = str(body.get("kind") or "")
    if kind not in ("prawn", "crab", "gold"):
        raise bad("Unknown trap.")
    with tx():
        p = load_player(uid)
        trap = sanitize_trap(kind, body.get("trap"))
        if kind == "gold" and trap:
            cur = p["data"].get("gold")
            if cur and abs(cur["deployedAt"] - trap["deployedAt"]) < 1000:
                min_resolve = cur["resolveAt"] - 30 * 60000  # one Trap Accelerator per sync
            else:
                min_resolve = now() + 3 * 3600000 - 30 * 60000 - 60000
            trap["resolveAt"] = max(trap["resolveAt"], min_resolve)
        p["data"][kind] = trap
        save_player(p)
        return {"ok": True, "trap": trap}


def api_collect_gold(uid):
    with tx():
        p = load_player(uid)
        g = p["data"].get("gold")
        if not g:
            raise bad("You don’t have a gold pot out.")
        if now() < g["resolveAt"] - 5000:
            raise bad("That gold pot isn’t ready yet.")
        reward = random.randint(5, 20)
        adjust_spent(p, -reward)
        p["data"]["gold"] = None
        save_player(p)
        return {"reward": reward, "coinsSpent": p["data"]["shop"]["coinsSpent"]}


# ======================= GIFT CODES =======================
def api_gift_verify(uid, body):
    if not is_admin(uid):
        raise ApiError(403, "Only the admin account can make gift codes.")
    if not hmac.compare_digest(str(body.get("passcode") or "").encode(), admin_password().encode()):
        raise ApiError(403, "Incorrect passcode.")
    return {"ok": True}


def make_gift(amount, by):
    chars = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    for _ in range(5):
        code = "-".join("".join(secrets.choice(chars) for _ in range(4)) for _ in range(3))
        if not q1("SELECT 1 FROM giftCodes WHERE code=?", code):
            db.execute("INSERT INTO giftCodes(code, amount, redeemed, createdAt, createdBy) VALUES(?,?,0,?,?)", (code, amount, now(), by))
            return code
    raise ApiError(500, "Could not make a unique code. Try again.")


def api_gift_create(uid, body):
    api_gift_verify(uid, body)
    amount = math.floor(num(body.get("amount"), "amount"))
    if amount <= 0 or amount > 1_000_000:
        raise bad("Invalid amount.")
    return {"code": make_gift(amount, uid)}


def api_gift_redeem(uid, body):
    code = str(body.get("code") or "").strip().upper()
    with tx():
        g = q1("SELECT * FROM giftCodes WHERE code=?", code)
        if not g:
            raise bad("That code doesn’t exist. Double check it and try again.")
        if g["redeemed"]:
            raise bad("That code has already been used.")
        db.execute("UPDATE giftCodes SET redeemed=1, redeemedBy=?, redeemedAt=? WHERE code=?", (uid, now(), code))
        p = load_player(uid)
        adjust_spent(p, -g["amount"])
        save_player(p)
        return {"amount": g["amount"], "coinsSpent": p["data"]["shop"]["coinsSpent"]}


# ======================= LEADERBOARDS (totalled here) =======================
LB_CACHE = {}


def api_leaderboard(query):
    limit = min(200, max(1, int(num(query.get("limit") or 50, "limit"))))
    return [public_summary(r["uid"], r["displayName"], r["points"], json.loads(r["data"]))
            for r in q("SELECT uid, displayName, points, data FROM players ORDER BY points DESC LIMIT ?", limit)]


def api_leaderboard_periods(query):
    """Day/week/month totals worked out here, so phones download a short
    list instead of every player's bag. Period points = 1 + tier per fish
    (same as the page's computePeriodPoints)."""
    limit = min(500, max(1, int(num(query.get("limit") or 300, "limit"))))
    starts = {}
    for k in ("day", "week", "month"):
        try:
            starts[k] = int(float(query.get(k)))
        except (TypeError, ValueError):
            starts[k] = None
    key = (limit, starts["day"], starts["week"], starts["month"])
    hit = LB_CACHE.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    oldest = min([v for v in starts.values() if v] or [now() - 32 * 86400000])
    out = []
    for r in q("SELECT uid, displayName, points, data FROM players ORDER BY updatedAt DESC LIMIT ?", limit):
        d = json.loads(r["data"])
        per = {k: 0 for k in starts}
        for c in d.get("bag") or []:
            ts = c.get("ts") or 0
            if ts < oldest:
                continue
            v = 1 + max(0, tier_index(c.get("tier")))
            for k, s in starts.items():
                if s and ts >= s:
                    per[k] += v
        row = public_summary(r["uid"], r["displayName"], r["points"], d)
        row["periods"] = per
        out.append(row)
    if len(LB_CACHE) > 50:
        LB_CACHE.clear()
    LB_CACHE[key] = (time.time(), out)
    return out


def api_battle_leaderboard(query):
    limit = min(200, max(1, int(num(query.get("limit") or 50, "limit"))))
    out = []
    for r in q("SELECT uid, displayName, points, data FROM players ORDER BY updatedAt DESC LIMIT ?", limit):
        d = json.loads(r["data"])
        rec = (d.get("battles") or {}).get("record") or {"wins": 0, "losses": 0, "draws": 0, "gamesPlayed": 0}
        out.append({**public_summary(r["uid"], r["displayName"], r["points"], d), **rec})
    return out


def api_player(query):
    p = load_player(norm_username(query.get("uid")))
    if not p:
        return None
    summary = public_summary(p["uid"], p["displayName"], compute_points(p["data"].get("bag")), p["data"])
    if query.get("lite"):
        return summary
    sh = p["data"].get("shop") or {}
    return {**summary, "catchCount": len(p["data"].get("bag") or []),
            "shop": {"equipped": sh.get("equipped") or {}, "bonusRodLevels": sh.get("bonusRodLevels") or 0,
                     "unlockedAchievements": sh.get("unlockedAchievements") or []}}


# ======================= FRIENDS =======================
def api_friend_request(uid, body):
    with tx():
        me = load_player(uid)
        target = norm_username(body.get("username"))
        if not target:
            raise bad("Enter a username.")
        if target == uid:
            raise bad("You can't friend yourself.")
        them = load_player(target)
        if not them:
            raise bad("No account found with that username.")
        md, td = me["data"], them["data"]
        for d in (md, td):
            for k in ("friends", "incomingRequests", "outgoingRequests"):
                d[k] = d.get(k) or []
        if uid in td["friends"]:
            raise bad("You’re already friends.")
        if any(r["uid"] == uid for r in td["incomingRequests"]):
            raise bad("Request already sent.")
        if any(r["uid"] == target for r in md["incomingRequests"]):
            md["incomingRequests"] = [r for r in md["incomingRequests"] if r["uid"] != target]
            md["friends"] = sorted(set(md["friends"]) | {target})
            td["friends"] = sorted(set(td["friends"]) | {uid})
            td["outgoingRequests"] = [r for r in td["outgoingRequests"] if r["uid"] != uid]
            save_player(me)
            save_player(them)
            return {"displayName": them["displayName"], "autoAccepted": True}
        if len(td["incomingRequests"]) >= 100:
            raise bad("That player has too many pending requests right now.")
        t = now()
        td["incomingRequests"].append({"uid": uid, "displayName": me["displayName"], "sentAt": t})
        md["outgoingRequests"].append({"uid": target, "displayName": them["displayName"], "sentAt": t})
        save_player(me)
        save_player(them)
        return {"displayName": them["displayName"]}


def api_friend_respond(uid, body):
    frm, accept = norm_username(body.get("uid")), bool(body.get("accept"))
    with tx():
        me = load_player(uid)
        me["data"]["incomingRequests"] = [r for r in me["data"].get("incomingRequests") or [] if r["uid"] != frm]
        them = load_player(frm)
        if accept and them:
            me["data"]["friends"] = sorted(set(me["data"].get("friends") or []) | {frm})
            them["data"]["friends"] = sorted(set(them["data"].get("friends") or []) | {uid})
        if them:
            them["data"]["outgoingRequests"] = [r for r in them["data"].get("outgoingRequests") or [] if r["uid"] != uid]
            save_player(them)
        save_player(me)
        return {"ok": True}


def api_friend_remove(uid, body):
    other = norm_username(body.get("uid"))
    with tx():
        me = load_player(uid)
        me["data"]["friends"] = [u for u in me["data"].get("friends") or [] if u != other]
        me["data"]["outgoingRequests"] = [r for r in me["data"].get("outgoingRequests") or [] if r["uid"] != other]
        save_player(me)
        them = load_player(other)
        if them:
            them["data"]["friends"] = [u for u in them["data"].get("friends") or [] if u != uid]
            them["data"]["incomingRequests"] = [r for r in them["data"].get("incomingRequests") or [] if r["uid"] != uid]
            save_player(them)
        return {"ok": True}


def api_friends(uid):
    d = load_player(uid)["data"]
    return {"friends": d.get("friends") or [], "incomingRequests": d.get("incomingRequests") or [], "outgoingRequests": d.get("outgoingRequests") or []}


# ======================= BATTLES =======================
def battle_state(p):
    b = p["data"].get("battles") or {}
    return {"incoming": b.get("incoming") or [], "outgoing": b.get("outgoing") or [], "active": b.get("active"),
            "record": b.get("record") or {"wins": 0, "losses": 0, "draws": 0, "gamesPlayed": 0}}


def save_battle(bid, b):
    db.execute("INSERT INTO battles VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
               (bid, b.get("player1Uid"), b.get("player2Uid"), b.get("status"), json.dumps(b, separators=(",", ":"))))


def api_battle_challenge(uid, body):
    target = norm_username(body.get("opponentUid"))
    hours = num(body.get("durationHours"), "durationHours")
    hours = int(hours) if hours == int(hours) else round(hours, 2)  # 0.5 = the 30-minute battle
    if not 0.5 <= hours <= 168:
        raise bad("Invalid battle length.")
    vis = body.get("visibility") if body.get("visibility") in ("hidden", "halfway", "transparent") else "hidden"
    with tx():
        me, them = load_player(uid), load_player(target)
        if not them:
            raise bad("No account found with that username.")
        if target not in (me["data"].get("friends") or []):
            raise bad("You can only challenge friends.")
        bid = f"{'_'.join(sorted([uid, target]))}_{now()}"
        b = {"player1Uid": uid, "player1DisplayName": me["displayName"], "player2Uid": target, "player2DisplayName": them["displayName"],
             "durationHours": hours, "visibility": vis, "status": "pending", "createdAt": now(), "acceptedAt": None, "endAt": None,
             "player1StartPoints": None, "player2StartPoints": None, "player1FinalPoints": None, "player2FinalPoints": None,
             "result": None, "forfeitedBy": None}
        save_battle(bid, b)
        mb, tb = battle_state(me), battle_state(them)
        mb["outgoing"].append({"battleId": bid, "opponentUid": target, "opponentDisplayName": them["displayName"], "durationHours": hours, "visibility": vis, "sentAt": now()})
        tb["incoming"].append({"battleId": bid, "opponentUid": uid, "opponentDisplayName": me["displayName"], "durationHours": hours, "visibility": vis, "sentAt": now()})
        me["data"]["battles"], them["data"]["battles"] = mb, tb
        save_player(me)
        save_player(them)
        return {"battleId": bid}


def api_battle_respond(uid, body):
    bid, accept = str(body.get("battleId") or ""), bool(body.get("accept"))
    with tx():
        me = load_player(uid)
        mb = battle_state(me)
        entry = next((b for b in mb["incoming"] if b["battleId"] == bid), None)
        if not entry:
            raise bad("That challenge is no longer available.")
        mb["incoming"] = [b for b in mb["incoming"] if b["battleId"] != bid]
        row = q1("SELECT data FROM battles WHERE id=?", bid)
        if not row:
            raise bad("That challenge no longer exists.")
        b = json.loads(row["data"])
        opp = load_player(entry["opponentUid"])
        ob = battle_state(opp) if opp else None
        if ob:
            ob["outgoing"] = [x for x in ob["outgoing"] if x["battleId"] != bid]
        if not accept:
            b["status"] = "declined"
            save_battle(bid, b)
            me["data"]["battles"] = mb
            save_player(me)
            if opp:
                opp["data"]["battles"] = ob
                save_player(opp)
            return {"accepted": False}
        if mb["active"]:
            raise bad("Finish your current battle first.")
        if ob and ob["active"]:
            raise bad("That player is already in a battle.")
        t = now()
        end = int(t + b["durationHours"] * 3600000)
        mine, theirs = compute_points(me["data"].get("bag")), compute_points(opp["data"].get("bag")) if opp else 0
        p1 = b["player1Uid"] == uid
        b.update({"status": "active", "acceptedAt": t, "endAt": end,
                  "player1StartPoints": mine if p1 else theirs, "player2StartPoints": theirs if p1 else mine})
        save_battle(bid, b)
        mb["active"] = {"battleId": bid, "opponentUid": entry["opponentUid"], "opponentDisplayName": entry["opponentDisplayName"],
                        "startAt": t, "endAt": end, "visibility": b["visibility"], "myStartPoints": mine, "opponentStartPoints": theirs}
        me["data"]["battles"] = mb
        save_player(me)
        if opp:
            ob["active"] = {"battleId": bid, "opponentUid": uid, "opponentDisplayName": me["displayName"], "startAt": t, "endAt": end,
                            "visibility": b["visibility"], "myStartPoints": theirs, "opponentStartPoints": mine}
            opp["data"]["battles"] = ob
            save_player(opp)
        return {"accepted": True, "endAt": end, "opponentDisplayName": entry["opponentDisplayName"]}


def resolve_battle(bid, forced, forfeiter, caller):
    with tx():
        row = q1("SELECT data FROM battles WHERE id=?", bid)
        if not row:
            raise bad("Battle not found.")
        b = json.loads(row["data"])
        if caller not in (b["player1Uid"], b["player2Uid"]):
            raise ApiError(403, "Not your battle.")

        def summary():
            return {k: b.get(k) for k in ("player1Uid", "player2Uid", "player1DisplayName", "player2DisplayName",
                                          "player1FinalPoints", "player2FinalPoints", "result")} | {"forfeited": bool(b.get("forfeitedBy"))}
        if b["status"] == "completed":
            return {"alreadyCompleted": True, **summary()}
        if b["status"] != "active":
            raise bad("This battle isn’t active.")
        if not forced and now() < b["endAt"] - 5000:
            raise bad("This battle hasn’t finished yet.")
        p1, p2 = load_player(b["player1Uid"]), load_player(b["player2Uid"])
        f1 = (compute_points(p1["data"].get("bag")) if p1 else 0) - (b.get("player1StartPoints") or 0)
        f2 = (compute_points(p2["data"].get("bag")) if p2 else 0) - (b.get("player2StartPoints") or 0)
        result = forced or ("player1" if f1 > f2 else "player2" if f2 > f1 else "draw")
        b.update({"status": "completed", "player1FinalPoints": f1, "player2FinalPoints": f2, "result": result, "completedAt": now()})
        if forfeiter:
            b["forfeitedBy"] = forfeiter
        save_battle(bid, b)
        for pl, me in ((p1, "player1"), (p2, "player2")):
            if not pl:
                continue
            st = battle_state(pl)
            rec = st["record"]
            if result == me:
                rec["wins"] += 1
            elif result == "draw":
                rec["draws"] += 1
            else:
                rec["losses"] += 1
            rec["gamesPlayed"] += 1
            st["active"] = None
            pl["data"]["battles"] = st
            save_player(pl)
        return {"alreadyCompleted": False, **summary()}


def api_battle_forfeit(uid, body):
    bid = str(body.get("battleId") or "")
    row = q1("SELECT data FROM battles WHERE id=?", bid)
    if not row:
        raise bad("Battle not found.")
    b = json.loads(row["data"])
    return resolve_battle(bid, "player2" if b["player1Uid"] == uid else "player1", uid, uid)


# ======================= FISH MARKET =======================
def save_listing(lid, l):
    db.execute("INSERT INTO listings VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
               (lid, l["sellerUid"], l["status"], l["createdAt"], json.dumps(l, separators=(",", ":"))))


def listing_out(r):
    return {"id": r["id"], **json.loads(r["data"])}


def api_list(uid, body):
    ask = math.floor(num(body.get("askPrice"), "askPrice"))
    if not 1 <= ask <= 10_000_000:
        raise bad("Pick a price between 1 and 10,000,000.")
    cid = str(body.get("catchId") or "")
    with tx():
        p = load_player(uid)
        fish = next((c for c in p["data"].get("bag") or [] if c.get("id") == cid), None)
        if not fish:
            raise bad("That fish isn’t on the server yet. Wait a moment for it to save, then try again.")
        for r in q("SELECT data FROM listings WHERE sellerUid=? AND status='active'", uid):
            if json.loads(r["data"]).get("catchId") == cid:
                raise bad("That fish is already listed.")
        lid = f"l_{uid}_{now()}_{secrets.token_hex(3)}"
        l = {"sellerUid": uid, "sellerDisplayName": p["displayName"], "catchId": cid, "fishName": fish["name"], "fishTier": fish["tier"],
             "fishPointValue": fish_points(fish), "askPrice": ask, "marketValueAtListing": body.get("marketValueAtListing"),
             "status": "active", "createdAt": now(), "lastPriceChangeAt": now(), "buyerUid": None, "buyerDisplayName": None, "soldAt": None}
        save_listing(lid, l)
        return {"listingId": lid}


def my_listing(uid, lid):
    r = q1("SELECT data FROM listings WHERE id=?", lid)
    if not r:
        raise bad("Listing not found.")
    l = json.loads(r["data"])
    if l["sellerUid"] != uid:
        raise ApiError(403, "Not your listing.")
    if l["status"] != "active":
        raise bad("That listing is no longer active.")
    return l


def api_cancel(uid, body):
    lid = str(body.get("listingId") or "")
    with tx():
        l = my_listing(uid, lid)
        l["status"] = "cancelled"
        save_listing(lid, l)
        return {"ok": True}


def api_reprice(uid, body):
    lid = str(body.get("listingId") or "")
    price = math.floor(num(body.get("newPrice"), "newPrice"))
    if not 1 <= price <= 10_000_000:
        raise bad("Pick a price between 1 and 10,000,000.")
    with tx():
        l = my_listing(uid, lid)
        if now() - l["lastPriceChangeAt"] < 7 * 86400000:
            raise bad("You can only change a listing’s price once per week.")
        l["askPrice"], l["lastPriceChangeAt"] = price, now()
        save_listing(lid, l)
        return {"ok": True}


def api_buy(uid, body):
    lid = str(body.get("listingId") or "")
    with tx():
        r = q1("SELECT data FROM listings WHERE id=?", lid)
        if not r:
            raise bad("Listing not found.")
        l = json.loads(r["data"])
        if l["status"] != "active":
            raise bad("That fish was already sold.")
        if l["sellerUid"] == uid:
            raise bad("You can’t buy your own listing.")
        seller, buyer = load_player(l["sellerUid"]), load_player(uid)
        if not seller or not buyer:
            raise bad("Account not found.")
        if coins_of(buyer) < l["askPrice"]:
            raise bad("Not enough Chum Coins.")
        sbag = seller["data"].get("bag") or []
        idx = next((i for i, c in enumerate(sbag) if c.get("id") == l["catchId"]), -1)
        if idx < 0:
            l["status"] = "cancelled"
            save_listing(lid, l)
            raise bad("That fish is no longer available.")
        fish = sbag.pop(idx)
        pv = fish_points(fish)
        new_fish = {**fish, "id": server_catch_id(), "ts": now()}
        buyer["data"]["bag"] = [new_fish] + (buyer["data"].get("bag") or [])
        # seller keeps the coin value they had (offset) plus the price; buyer pays exactly the price
        adjust_spent(seller, -pv - l["askPrice"])
        adjust_spent(buyer, l["askPrice"] + pv)
        l.update({"status": "sold", "buyerUid": uid, "buyerDisplayName": buyer["displayName"], "soldAt": now()})
        save_listing(lid, l)
        save_player(seller)
        save_player(buyer)
        return {"fishName": l["fishName"], "fishTier": l["fishTier"], "price": l["askPrice"], "sellerDisplayName": l["sellerDisplayName"],
                "catch": new_fish, "coinsSpent": buyer["data"]["shop"]["coinsSpent"]}


# ======================= DOCKS =======================
OSM_CELL = 0.1
BUOY_CELL = 0.01
BUOY_PER_CELL = 2
DOCK_RANGE = 120
SPIN_COOLDOWN = 5 * 60000
BUOY_NAMES = ["Kelp Buoy", "Gull Rock Buoy", "Driftwood Buoy", "Tidepool Buoy", "Herring Buoy", "Lantern Buoy", "Foghorn Buoy", "Seaglass Buoy",
              "Anchor Buoy", "Barnacle Buoy", "Current Buoy", "Otter Buoy", "Starfish Buoy", "Moonwater Buoy", "Saltmarsh Buoy", "Breakwater Buoy"]
KIND_LABEL = {"pier": "Pier", "marina": "Marina", "slipway": "Boat Launch", "fishing": "Fishing Spot", "beach": "Beach", "ferry": "Ferry Dock",
              "tackle": "Bait & Tackle", "dock": "Dock", "buoy": "Drift Buoy"}


def buoy_at(cx, cy, i):
    r1, r2, r3 = hash01(f"buoy:{cx}:{cy}:{i}:a"), hash01(f"buoy:{cx}:{cy}:{i}:b"), hash01(f"buoy:{cx}:{cy}:{i}:n")
    return {"id": f"b:{cx}:{cy}:{i}", "lat": (cy + 0.1 + r1 * 0.8) * BUOY_CELL, "lng": (cx + 0.1 + r2 * 0.8) * BUOY_CELL,
            "name": BUOY_NAMES[int(r3 * len(BUOY_NAMES))], "kind": "buoy"}


def deg_box(center, radius):
    dlat = radius / 111320
    dlng = radius / (111320 * max(0.1, math.cos(math.radians(center["lat"]))))
    return dlat, dlng


def buoys_near(center, radius):
    r = min(radius, 3000)
    dlat, dlng = deg_box(center, r)
    out = []
    for y in range(math.floor((center["lat"] - dlat) / BUOY_CELL), math.floor((center["lat"] + dlat) / BUOY_CELL) + 1):
        for x in range(math.floor((center["lng"] - dlng) / BUOY_CELL), math.floor((center["lng"] + dlng) / BUOY_CELL) + 1):
            for i in range(BUOY_PER_CELL):
                out.append(buoy_at(x, y, i))
    return out


def classify(tags):
    if tags.get("shop") in ("fishing", "bait"):
        return "tackle"
    if tags.get("man_made") == "pier":
        return "pier"
    if tags.get("leisure") == "marina":
        return "marina"
    if tags.get("leisure") == "slipway":
        return "slipway"
    if tags.get("leisure") == "fishing":
        return "fishing"
    if tags.get("natural") == "beach":
        return "beach"
    if tags.get("amenity") == "ferry_terminal":
        return "ferry"
    if tags.get("waterway") == "dock":
        return "dock"
    return None


PENDING_CELLS = {}
HTTP = None


async def fetch_osm_cell(cell):
    cx, cy = map(int, cell.split(":"))
    s, w = cy * OSM_CELL, cx * OSM_CELL
    bb = f"{s:.4f},{w:.4f},{s + OSM_CELL:.4f},{w + OSM_CELL:.4f}"
    query = f"""[out:json][timeout:25];(
  nwr["man_made"="pier"]({bb});
  nwr["leisure"~"^(marina|slipway|fishing)$"]({bb});
  nwr["natural"="beach"]["name"]({bb});
  node["amenity"="ferry_terminal"]({bb});
  node["shop"~"^(fishing|bait)$"]({bb});
  nwr["waterway"="dock"]({bb});
);out center 400;"""
    try:
        async with HTTP.post(CFG["overpass_url"], data={"data": query}, headers={"User-Agent": "Driftline/3.0 (game server)"},
                             timeout=ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            js = await r.json(content_type=None)
        rows = []
        for el in js.get("elements", []):
            tags = el.get("tags") or {}
            kind = classify(tags)
            if not kind:
                continue
            lat = el.get("lat", (el.get("center") or {}).get("lat"))
            lng = el.get("lon", (el.get("center") or {}).get("lon"))
            if lat is None or lng is None:
                continue
            rows.append((f"osm:{el['type'][0]}{el['id']}", cell, lat, lng, str(tags.get("name") or KIND_LABEL[kind])[:60], kind))
        with tx():
            db.executemany("INSERT INTO docks VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, name=excluded.name, kind=excluded.kind", rows)
            db.execute("INSERT INTO dock_cells VALUES(?,?,1) ON CONFLICT(cell) DO UPDATE SET fetchedAt=excluded.fetchedAt, ok=1", (cell, now()))
    except Exception as e:
        db.execute("INSERT INTO dock_cells VALUES(?,?,0) ON CONFLICT(cell) DO UPDATE SET fetchedAt=excluded.fetchedAt, ok=0", (cell, now()))
        log(f"docks: map lookup for area {cell} failed ({e}); drift buoys cover it for now.")


def osm_cells(center, radius):
    dlat, dlng = deg_box(center, radius)
    cells = []
    for y in range(math.floor((center["lat"] - dlat) / OSM_CELL), math.floor((center["lat"] + dlat) / OSM_CELL) + 1):
        for x in range(math.floor((center["lng"] - dlng) / OSM_CELL), math.floor((center["lng"] + dlng) / OSM_CELL) + 1):
            cells.append(f"{x}:{y}")
    return cells[:12]


async def ensure_cells(cells):
    if not CFG["use_overpass"]:
        return
    waits = []
    for cell in cells:
        c = q1("SELECT * FROM dock_cells WHERE cell=?", cell)
        fresh = c and (now() - c["fetchedAt"] < (7 * 86400000 if c["ok"] else 3600000))
        if fresh:
            continue
        if cell not in PENDING_CELLS:
            task = asyncio.ensure_future(fetch_osm_cell(cell))
            PENDING_CELLS[cell] = task
            task.add_done_callback(lambda _t, c=cell: PENDING_CELLS.pop(c, None))
        waits.append(PENDING_CELLS[cell])
    if waits:
        # don't keep the player waiting on a slow map server; buoys still work
        await asyncio.wait(waits, timeout=6)


def lookup_dock(did):
    if did.startswith("b:"):
        try:
            _, cx, cy, i = did.split(":")
            cx, cy, i = int(cx), int(cy), int(i)
        except ValueError:
            return None
        return buoy_at(cx, cy, i) if 0 <= i < BUOY_PER_CELL else None
    r = q1("SELECT * FROM docks WHERE id=?", did)
    return {"id": r["id"], "lat": r["lat"], "lng": r["lng"], "name": r["name"], "kind": r["kind"]} if r else None


def spot_state(did):
    s = q1("SELECT * FROM spots WHERE dockId=?", did)
    if not s or not s["crew"]:
        return {"crew": None, "strength": 0}
    decayed = s["strength"] - math.floor((now() - s["updatedAt"]) / 86400000) * 5  # -5 a day if nobody visits
    return {"crew": s["crew"], "strength": decayed} if decayed > 0 else {"crew": None, "strength": 0}


def set_spot(did, crew, strength, by):
    db.execute("INSERT INTO spots VALUES(?,?,?,?,?) ON CONFLICT(dockId) DO UPDATE SET crew=excluded.crew, strength=excluded.strength, updatedAt=excluded.updatedAt, lastByUid=excluded.lastByUid",
               (did, crew, strength, now(), by))


# ---- raids ----
RAID_WINDOW = 2 * 3600000
RAID_CHANCE = 0.12  # about 1 in 8 docks has a boss in any 2-hour window
RAID_COOLDOWN = 6000


def raid_window(t):
    return t // RAID_WINDOW


def raid_schedule(did, win):
    if hash01(f"raid:{did}:{win}") >= RAID_CHANCE:
        return None
    tier = "ultimate" if hash01(f"raidtier:{did}:{win}") < 0.25 else "superrare"
    lst = FISH[tier]
    boss = lst[int(hash01(f"raidboss:{did}:{win}") * len(lst))]
    return {"id": f"{did}@{win}", "dockId": did, "win": win, "boss": boss, "tier": tier,
            "maxHp": 2400 if tier == "ultimate" else 1200, "startsAt": win * RAID_WINDOW, "endsAt": (win + 1) * RAID_WINDOW}


def raid_state(did, t):
    sched = raid_schedule(did, raid_window(t))
    if not sched:
        return None
    r = q1("SELECT data FROM raids WHERE id=?", sched["id"])
    st = json.loads(r["data"]) if r else {"hp": sched["maxHp"], "contributions": {}, "crewDamage": {}, "defeatedAt": None}
    return {**sched, **st}


def raid_public(r, uid):
    if not r:
        return None
    return {"id": r["id"], "boss": r["boss"], "tier": r["tier"], "hp": max(0, r["hp"]), "maxHp": r["maxHp"], "endsAt": r["endsAt"],
            "defeated": bool(r.get("defeatedAt")), "attackers": len(r.get("contributions") or {}),
            "myDamage": (r.get("contributions") or {}).get(uid, 0) if uid else 0, "crewDamage": r.get("crewDamage") or {}}


async def api_docks(uid, query):
    center = latlng(query)
    radius = min(10000, max(200, num(query.get("radius") or 1500, "radius")))
    cells = osm_cells(center, radius)
    await ensure_cells(cells)
    seen = {}
    for cell in cells:
        for r in q("SELECT * FROM docks WHERE cell=?", cell):
            seen[r["id"]] = {"id": r["id"], "lat": r["lat"], "lng": r["lng"], "name": r["name"], "kind": r["kind"]}
    for b in buoys_near(center, radius):
        seen[b["id"]] = b
    p = load_player(uid) if uid else None
    t = now()
    near = sorted(((dist_m(center, d), d) for d in seen.values()), key=lambda x: x[0])
    out = []
    for dist, d in near[:80]:
        if dist > radius:
            break
        spot = spot_state(d["id"])
        last = (p["data"]["play"]["dockSpins"].get(d["id"]) or 0) if p else 0
        out.append({"id": d["id"], "lat": round(d["lat"], 6), "lng": round(d["lng"], 6), "name": d["name"], "kind": d["kind"],
                    "label": KIND_LABEL.get(d["kind"], "Dock"), "crew": spot["crew"], "strength": spot["strength"],
                    "readyAt": last + SPIN_COOLDOWN if last else 0, "raid": raid_public(raid_state(d["id"], t), uid)})
    return {"range": DOCK_RANGE, "spinCooldownMs": SPIN_COOLDOWN, "docks": out}


BAIT = {"worm": 3, "shrimp": 8, "glow": 20}  # price each
EGG_KINDS = {1: [("common", 50), ("hard", 35), ("rare", 15)], 3: [("hard", 30), ("rare", 50), ("superrare", 20)],
             5: [("rare", 35), ("superrare", 50), ("ultimate", 15)]}
MAX_EGGS, INCUBATORS = 9, 3
CREWS = ("coral", "tide", "storm")
CREW_CACHE = {"at": 0, "data": None}


def crew_on_dock(p, did, amount):
    """Your visit strengthens your crew's hold, or wears down a rival's until you take it."""
    crew = p["data"]["play"]["crew"]
    if not crew:
        return None
    cur = spot_state(did)
    if not cur["crew"]:
        nxt = {"crew": crew, "strength": 25, "claimed": True}
    elif cur["crew"] == crew:
        nxt = {"crew": crew, "strength": min(100, cur["strength"] + amount)}
    else:
        s = cur["strength"] - round(amount * 1.5)
        nxt = {"crew": cur["crew"], "strength": s} if s > 0 else {"crew": crew, "strength": 25, "claimed": True, "tookFrom": cur["crew"]}
    set_spot(did, nxt["crew"], nxt["strength"], p["uid"])
    if nxt.get("claimed"):
        p["data"]["play"]["stats"]["docksClaimed"] += 1
        CREW_CACHE["at"] = 0
    return nxt


def api_spin(uid, body):
    did = str(body.get("dockId") or "")
    at = latlng(body)
    dock = lookup_dock(did)
    if not dock:
        raise bad("That dock doesn’t exist any more. Refresh the map.")
    if dist_m(at, dock) > DOCK_RANGE + 30:
        raise bad(f"Get within {DOCK_RANGE} m of {dock['name']} to visit it.")
    with tx():
        p = load_player(uid)
        play = p["data"]["play"]
        last = play["dockSpins"].get(did) or 0
        if now() - last < SPIN_COOLDOWN:
            raise bad(f"{dock['name']} is restocking. Come back in {math.ceil((last + SPIN_COOLDOWN - now()) / 60000)} min.")
        play["dockSpins"][did] = now()
        play["dockSpins"] = {k: v for k, v in play["dockSpins"].items() if now() - v < 86400000}
        spot = spot_state(did)
        home = bool(play["crew"] and spot["crew"] == play["crew"])
        count = random.randint(2, 4) + (3 if dock["kind"] == "tackle" else 0) + (1 if home else 0)
        got = {"worm": 0, "shrimp": 0, "glow": 0}
        for _ in range(count):
            got[pick_weighted([("worm", 65), ("shrimp", 28), ("glow", 7)])] += 1
        for k, v in got.items():
            play["bait"][k] = play["bait"].get(k, 0) + v
        egg = None
        if len(play["eggs"]) < MAX_EGGS and random.random() < 0.3:
            km = pick_weighted([(1, 60), (3, 30), (5, 10)])
            egg = {"id": rid("e_", 5), "km": km, "walked": 0, "receivedAt": now(), "dockName": dock["name"]}
            play["eggs"].append(egg)
        coins = 0
        if random.random() < 0.3:
            coins = random.randint(1, 3)
            adjust_spent(p, -coins)
        crew = crew_on_dock(p, did, 10)
        play["stats"]["spins"] += 1
        save_player(p)
        return {"dock": {"id": did, "name": dock["name"]}, "bait": got, "egg": egg, "coins": coins, "crew": crew, "homeDock": home,
                "play": play_payload(p), "readyAt": now() + SPIN_COOLDOWN}


def api_bait_use(uid, body):
    t = str(body.get("type") or "")
    if t not in BAIT:
        raise bad("Unknown bait.")
    with tx():
        p = load_player(uid)
        if (p["data"]["play"]["bait"].get(t) or 0) < 1:
            raise bad("You’re out of that bait.")
        p["data"]["play"]["bait"][t] -= 1
        save_player(p)
        return {"bait": p["data"]["play"]["bait"]}


def api_bait_buy(uid, body):
    t = str(body.get("type") or "")
    qty = math.floor(num(body.get("qty"), "qty"))
    if t not in BAIT:
        raise bad("Unknown bait.")
    if qty not in (1, 5, 10, 25):
        raise bad("Pick a pack size.")
    price = BAIT[t] * qty
    with tx():
        p = load_player(uid)
        if coins_of(p) < price:
            raise bad("Not enough Chum Coins.")
        adjust_spent(p, price)
        p["data"]["play"]["bait"][t] = p["data"]["play"]["bait"].get(t, 0) + qty
        save_player(p)
        return {"bait": p["data"]["play"]["bait"], "coinsSpent": p["data"]["shop"]["coinsSpent"], "price": price}


MAX_WALK_KMH = 15


def api_egg_progress(uid, body):
    km = max(0, min(5, num(body.get("km"), "km")))
    with tx():
        p = load_player(uid)
        play = p["data"]["play"]
        # walking only counts at walking/jogging speed; time with the game closed doesn't count
        since = min(15 * 60000, now() - play["eggProgressAt"]) if play["eggProgressAt"] else 0
        add = min(km, max(0, since / 3600000 * MAX_WALK_KMH))
        play["eggProgressAt"] = now()
        play["stats"]["kmWalked"] = round((play["stats"].get("kmWalked") or 0) + add, 3)
        for e in play["eggs"][:INCUBATORS]:
            e["walked"] = min(e["km"], round(e["walked"] + add, 3))
        save_player(p)
        return {"added": add, "eggs": play["eggs"], "kmWalked": play["stats"]["kmWalked"]}


def api_egg_hatch(uid, body):
    eid = str(body.get("eggId") or "")
    with tx():
        p = load_player(uid)
        play = p["data"]["play"]
        idx = next((i for i, e in enumerate(play["eggs"]) if e["id"] == eid), -1)
        if idx < 0:
            raise bad("That egg isn’t in your hatchery.")
        egg = play["eggs"][idx]
        if egg["walked"] + 1e-9 < egg["km"]:
            raise bad(f"Walk {max(0, egg['km'] - egg['walked']):.2f} km more to hatch this egg.")
        tier = pick_weighted(EGG_KINDS.get(egg["km"], EGG_KINDS[1]))
        try:
            lat, lng = float(body.get("lat") or 0), float(body.get("lng") or 0)
        except (TypeError, ValueError):
            lat = lng = 0
        fish = {"id": server_catch_id(), "name": pick_fish(tier), "tier": tier, "ts": now(), "lat": lat, "lng": lng, "bonus": 2, "src": "egg"}
        play["eggs"].pop(idx)
        play["stats"]["eggsHatched"] += 1
        p["data"]["bag"] = [fish] + (p["data"].get("bag") or [])
        save_player(p)
        return {"catch": fish, "eggs": play["eggs"], "points": compute_points(p["data"]["bag"])}


def api_egg_discard(uid, body):
    eid = str(body.get("eggId") or "")
    with tx():
        p = load_player(uid)
        p["data"]["play"]["eggs"] = [e for e in p["data"]["play"]["eggs"] if e["id"] != eid]
        save_player(p)
        return {"eggs": p["data"]["play"]["eggs"]}


def api_crew_join(uid, body):
    crew = str(body.get("crew") or "")
    if crew not in CREWS:
        raise bad("Pick Coral, Tide or Storm.")
    with tx():
        p = load_player(uid)
        play = p["data"]["play"]
        if play["crew"] == crew:
            return {"crew": crew, "play": play_payload(p)}
        if play["crew"] and now() - play["crewChangedAt"] < 7 * 86400000:
            raise bad(f"You can switch crews once a week. Try again in {math.ceil((play['crewChangedAt'] + 7 * 86400000 - now()) / 86400000)} day(s).")
        play["crew"], play["crewChangedAt"] = crew, now()
        save_player(p)
        CREW_CACHE["at"] = 0
        return {"crew": crew, "play": play_payload(p)}


def api_crews():
    if CREW_CACHE["data"] and time.time() - CREW_CACHE["at"] < 30:
        return CREW_CACHE["data"]
    out = {c: {"docks": 0, "strength": 0, "members": 0, "points": 0} for c in CREWS}
    for s in q("SELECT dockId FROM spots WHERE crew IS NOT NULL"):
        st = spot_state(s["dockId"])
        if st["crew"] in out:
            out[st["crew"]]["docks"] += 1
            out[st["crew"]]["strength"] += st["strength"]
    for r in q("SELECT points, json_extract(data, '$.play.crew') AS crew FROM players"):
        if r["crew"] in out:
            out[r["crew"]]["members"] += 1
            out[r["crew"]]["points"] += r["points"] or 0
    CREW_CACHE.update(at=time.time(), data=out)
    return out


RAID_PASS_PRICE = 40


def api_raid_get(uid, query):
    did = str(query.get("dockId") or "")
    dock = lookup_dock(did)
    if not dock:
        raise bad("Unknown dock.")
    return {"dock": {"id": did, "name": dock["name"], "lat": dock["lat"], "lng": dock["lng"]}, "raid": raid_public(raid_state(did, now()), uid)}


def api_raid_pass(uid):
    with tx():
        p = load_player(uid)
        if coins_of(p) < RAID_PASS_PRICE:
            raise bad("Not enough Chum Coins.")
        adjust_spent(p, RAID_PASS_PRICE)
        p["data"]["play"]["raidPasses"]["bought"] += 1
        save_player(p)
        return {"play": play_payload(p), "coinsSpent": p["data"]["shop"]["coinsSpent"]}


def api_raid_attack(uid, body):
    did = str(body.get("dockId") or "")
    at = latlng(body)
    dock = lookup_dock(did)
    if not dock:
        raise bad("Unknown dock.")
    if dist_m(at, dock) > DOCK_RANGE + 30:
        raise bad(f"Get within {DOCK_RANGE} m of {dock['name']} to join the raid.")
    bait = body.get("bait") if body.get("bait") in BAIT else None
    with tx():
        t = now()
        r = raid_state(did, t)
        if not r:
            raise bad("There’s no boss at this dock right now.")
        if r.get("defeatedAt"):
            raise bad("This boss has already been beaten. Watch for the next one!")
        p = load_player(uid)
        play = p["data"]["play"]
        if t - (play["raidLastHitAt"] or 0) < RAID_COOLDOWN - 300:
            raise bad("Reel in your line first, then cast again.", "cooldown")
        refresh_passes(play)
        pass_used = None
        r["contributions"] = r.get("contributions") or {}
        if uid not in r["contributions"]:
            if play["raidPasses"]["used"] < 1:
                play["raidPasses"]["used"], pass_used = 1, "free"
            elif play["raidPasses"]["bought"] > 0:
                play["raidPasses"]["bought"] -= 1
                pass_used = "bought"
            else:
                raise bad("You need a Raid Pass to join. You get one free every day, or buy one in the Tackle Shop.", "no_pass")
        if bait:
            if (play["bait"].get(bait) or 0) < 1:
                raise bad("You’re out of that bait.")
            play["bait"][bait] -= 1
        rod = min(20, rod_level(compute_points(p["data"].get("bag"))) + ((p["data"].get("shop") or {}).get("bonusRodLevels") or 0))
        dmg = 30 + random.randint(0, 40) + rod * 3 + {"worm": 10, "shrimp": 25, "glow": 60}.get(bait, 0)
        crit = random.random() < 0.1
        if crit:
            dmg *= 2
        dmg = min(dmg, r["hp"])
        r["hp"] -= dmg
        r["contributions"][uid] = r["contributions"].get(uid, 0) + dmg
        r["crewDamage"] = r.get("crewDamage") or {}
        if play["crew"]:
            r["crewDamage"][play["crew"]] = r["crewDamage"].get(play["crew"], 0) + dmg
        play["raidLastHitAt"] = t
        my_reward = None
        if r["hp"] <= 0:
            r["hp"], r["defeatedAt"] = 0, t
            # everyone who helped gets the boss (worth 3x) plus coins for their share
            for cuid, cdmg in r["contributions"].items():
                cp = p if cuid == uid else load_player(cuid)
                if not cp:
                    continue
                share = min(1, cdmg / r["maxHp"])
                coins = 15 + round(40 * share)
                fish = {"id": server_catch_id(), "name": r["boss"], "tier": r["tier"], "ts": t, "lat": dock["lat"], "lng": dock["lng"], "bonus": 3, "src": "raid"}
                cp["data"]["bag"] = [fish] + (cp["data"].get("bag") or [])
                adjust_spent(cp, -coins)
                cp["data"]["play"]["stats"]["raidsWon"] += 1
                reward = {"type": "raid", "boss": r["boss"], "tier": r["tier"], "coins": coins, "catch": fish, "dockName": dock["name"], "at": t}
                if cuid == uid:
                    my_reward = reward
                else:
                    cp["data"]["play"]["inbox"] = (cp["data"]["play"].get("inbox") or [])[-19:] + [reward]
                    save_player(cp)
            if r["crewDamage"]:
                best = max(r["crewDamage"].items(), key=lambda kv: kv[1])[0]
                set_spot(did, best, 60, uid)  # top-damage crew takes the dock
                CREW_CACHE["at"] = 0
        db.execute("INSERT INTO raids VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                   (r["id"], did, r["win"], json.dumps({"hp": r["hp"], "contributions": r["contributions"], "crewDamage": r["crewDamage"],
                                                        "defeatedAt": r.get("defeatedAt")})))
        save_player(p)
        return {"damage": dmg, "crit": crit, "passUsed": pass_used, "raid": raid_public(r, uid), "reward": my_reward, "play": play_payload(p)}


def api_play(uid):
    with tx():
        p = load_player(uid)
        inbox = p["data"]["play"].get("inbox") or []
        if inbox:
            p["data"]["play"]["inbox"] = []
            save_player(p)
        return {**play_payload(p), "inbox": inbox}


# ======================= HTTP =======================
def client_ip(request):
    peer = request.remote or ""
    if peer in ("127.0.0.1", "::1") and request.headers.get("CF-Connecting-IP"):
        return request.headers["CF-Connecting-IP"][:64]
    return peer


def origin_ok(origin):
    if not origin:
        return False
    if origin in CFG["allowed_origins"]:
        return True
    return bool(re.fullmatch(r"http://(localhost|127\.0\.0\.1)(:\d+)?", origin))


TRAFFIC = {"day": None, "out": 0, "in": 0, "n": 0}


def count_traffic(n_in, n_out):
    day = datetime.date.today().isoformat()
    if TRAFFIC["day"] != day:
        flush_traffic()
        TRAFFIC.update(day=day, out=0, n=0)
        TRAFFIC["in"] = 0
    TRAFFIC["out"] += n_out
    TRAFFIC["in"] += n_in
    TRAFFIC["n"] += 1


def flush_traffic():
    if TRAFFIC["day"] and TRAFFIC["n"]:
        db.execute("""INSERT INTO traffic VALUES(?,?,?,?) ON CONFLICT(day) DO UPDATE SET
                      bytes_out=bytes_out+excluded.bytes_out, bytes_in=bytes_in+excluded.bytes_in, requests=requests+excluded.requests""",
                   (TRAFFIC["day"], TRAFFIC["out"], TRAFFIC["in"], TRAFFIC["n"]))
        TRAFFIC.update(out=0, n=0)
        TRAFFIC["in"] = 0


# (method, path) -> (handler(ctx), needs_auth)
ROUTES = {
    ("GET", "/api/health"): (lambda c: {"ok": True, "version": VERSION, "time": now(), "online": sum(1 for v in LAST_SEEN.values() if now() - v < 600000)}, False),
    ("POST", "/api/signup"): (lambda c: api_signup(c["body"], c["ip"]), False),
    ("POST", "/api/signin"): (lambda c: api_signin(c["body"], c["ip"]), False),
    ("GET", "/api/me"): (lambda c: player_payload(load_player(c["uid"])), True),
    ("GET", "/api/play"): (lambda c: api_play(c["uid"]), True),
    ("POST", "/api/catches"): (lambda c: api_catches(c["uid"], c["body"]), True),
    ("POST", "/api/sync/shop"): (lambda c: api_sync_shop(c["uid"], c["body"]), True),
    ("POST", "/api/sync/trap"): (lambda c: api_sync_trap(c["uid"], c["body"]), True),
    ("POST", "/api/gold/collect"): (lambda c: api_collect_gold(c["uid"]), True),
    ("POST", "/api/coins/spend"): (lambda c: api_spend(c["uid"], c["body"]), True),
    ("POST", "/api/shop/cosmetic"): (lambda c: api_buy_cosmetic(c["uid"], c["body"]), True),
    ("POST", "/api/gift/verify"): (lambda c: api_gift_verify(c["uid"], c["body"]), True),
    ("POST", "/api/gift/create"): (lambda c: api_gift_create(c["uid"], c["body"]), True),
    ("POST", "/api/gift/redeem"): (lambda c: api_gift_redeem(c["uid"], c["body"]), True),
    ("GET", "/api/leaderboard"): (lambda c: api_leaderboard(c["query"]), False),
    ("GET", "/api/leaderboard/periods"): (lambda c: api_leaderboard_periods(c["query"]), False),
    ("GET", "/api/leaderboard/battles"): (lambda c: api_battle_leaderboard(c["query"]), False),
    ("GET", "/api/player"): (lambda c: api_player(c["query"]), False),
    ("POST", "/api/friends/request"): (lambda c: api_friend_request(c["uid"], c["body"]), True),
    ("POST", "/api/friends/respond"): (lambda c: api_friend_respond(c["uid"], c["body"]), True),
    ("POST", "/api/friends/remove"): (lambda c: api_friend_remove(c["uid"], c["body"]), True),
    ("GET", "/api/friends"): (lambda c: api_friends(c["uid"]), True),
    ("POST", "/api/battles/challenge"): (lambda c: api_battle_challenge(c["uid"], c["body"]), True),
    ("POST", "/api/battles/respond"): (lambda c: api_battle_respond(c["uid"], c["body"]), True),
    ("POST", "/api/battles/complete"): (lambda c: resolve_battle(str(c["body"].get("battleId") or ""), None, None, c["uid"]), True),
    ("POST", "/api/battles/forfeit"): (lambda c: api_battle_forfeit(c["uid"], c["body"]), True),
    ("GET", "/api/battles"): (lambda c: battle_state(load_player(c["uid"])), True),
    ("POST", "/api/market/list"): (lambda c: api_list(c["uid"], c["body"]), True),
    ("POST", "/api/market/cancel"): (lambda c: api_cancel(c["uid"], c["body"]), True),
    ("POST", "/api/market/reprice"): (lambda c: api_reprice(c["uid"], c["body"]), True),
    ("POST", "/api/market/buy"): (lambda c: api_buy(c["uid"], c["body"]), True),
    ("GET", "/api/market/active"): (lambda c: [listing_out(r) for r in q("SELECT * FROM listings WHERE status='active' ORDER BY createdAt DESC LIMIT ?", min(200, int(num(c["query"].get("limit") or 50, "limit"))))], False),
    ("GET", "/api/market/mine"): (lambda c: [listing_out(r) for r in q("SELECT * FROM listings WHERE sellerUid=? ORDER BY createdAt DESC LIMIT 30", c["uid"])], True),
    ("GET", "/api/docks"): (lambda c: api_docks(c["uid"], c["query"]), False),
    ("POST", "/api/docks/spin"): (lambda c: api_spin(c["uid"], c["body"]), True),
    ("POST", "/api/bait/use"): (lambda c: api_bait_use(c["uid"], c["body"]), True),
    ("POST", "/api/bait/buy"): (lambda c: api_bait_buy(c["uid"], c["body"]), True),
    ("POST", "/api/eggs/progress"): (lambda c: api_egg_progress(c["uid"], c["body"]), True),
    ("POST", "/api/eggs/hatch"): (lambda c: api_egg_hatch(c["uid"], c["body"]), True),
    ("POST", "/api/eggs/discard"): (lambda c: api_egg_discard(c["uid"], c["body"]), True),
    ("POST", "/api/crew/join"): (lambda c: api_crew_join(c["uid"], c["body"]), True),
    ("GET", "/api/crews"): (lambda c: api_crews(), False),
    ("GET", "/api/raid"): (lambda c: api_raid_get(c["uid"], c["query"]), False),
    ("POST", "/api/raid/attack"): (lambda c: api_raid_attack(c["uid"], c["body"]), True),
    ("POST", "/api/raid/pass"): (lambda c: api_raid_pass(c["uid"]), True),
}


def json_resp(request, data, status=200):
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    resp = web.Response(body=raw, status=status, content_type="application/json", headers={"Cache-Control": "no-store"})
    if len(raw) > 1400 and "gzip" in request.headers.get("Accept-Encoding", ""):
        resp.enable_compression()
    return resp


async def api_handler(request):
    key = (request.method, request.path)
    if request.path == "/api/signout" and request.method == "POST":
        m = re.fullmatch(r"Bearer ([a-f0-9]{64})", request.headers.get("Authorization", ""))
        if m:
            db.execute("DELETE FROM sessions WHERE token=?", (m.group(1),))
        return json_resp(request, {"ok": True})
    route = ROUTES.get(key)
    if not route:
        return json_resp(request, {"error": "Unknown endpoint."}, 404)
    fn, needs_auth = route
    try:
        uid = auth_uid(request)
        if needs_auth and not uid:
            raise ApiError(401, "Please sign in again.", "auth")
        if uid and not q1("SELECT 1 FROM players WHERE uid=?", uid):
            raise ApiError(401, "Please sign in again.", "auth")
        body = {}
        if request.method == "POST":
            raw = await request.read()
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    raise bad("Invalid JSON.")
                if not isinstance(body, dict):
                    raise bad("Invalid JSON.")
        ctx = {"uid": uid, "body": body, "query": dict(request.query), "ip": client_ip(request)}
        result = fn(ctx)
        if asyncio.iscoroutine(result):
            result = await result
        return json_resp(request, {"ok": True} if result is None and key[1] != "/api/player" else result)
    except ApiError as e:
        return json_resp(request, {"error": e.message, "code": e.code}, e.status)
    except Exception as e:
        log("!! error in", request.path, repr(e))
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        return json_resp(request, {"error": "Something went wrong on the server. Try again in a moment."}, 500)


STATIC_OK = re.compile(r"^/[A-Za-z0-9_\-./]*$")


async def static_handler(request):
    # The server can also host the game page itself (handy for testing through the tunnel).
    path = request.path
    if path in ("", "/"):
        path = "/index.html"
    if not STATIC_OK.match(path) or "/." in path or ".." in path or path.startswith("/server"):
        return web.Response(status=404, text="Not found")
    file = os.path.normpath(os.path.join(REPO, path.lstrip("/")))
    if not file.startswith(REPO) or not os.path.isfile(file):
        return web.Response(status=404, text="Not found")
    return web.FileResponse(file, headers={"Cache-Control": "no-cache" if file.endswith(".html") else "public, max-age=86400"})


@web.middleware
async def mw(request, handler):
    origin = request.headers.get("Origin")
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        ip = client_ip(request)
        if request.path.startswith("/api/") and not LIMIT.ok(("api", ip), 240, 60):
            resp = json_resp(request, {"error": "Slow down a little: too many requests."}, 429)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as e:
                resp = e
    if origin_ok(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    if request.path.startswith("/api/") and isinstance(resp, web.Response):
        try:
            # (uncompressed size: real traffic is lower for the gzipped replies)
            count_traffic(request.content_length or 0, len(resp.body or b""))
        except TypeError:
            pass
    return resp


# ======================= UPKEEP =======================
def backup():
    try:
        bdir = P("backups")
        os.makedirs(bdir, exist_ok=True)
        target = os.path.join(bdir, f"driftline-{datetime.date.today().isoformat()}.db")
        if not os.path.exists(target):
            db.execute("VACUUM INTO ?", (target,))
            log("backup saved:", target)
        files = sorted(f for f in os.listdir(bdir) if re.fullmatch(r"driftline-\d{4}-\d{2}-\d{2}\.db", f))
        while len(files) > CFG["backup_keep_days"]:
            os.remove(os.path.join(bdir, files.pop(0)))
        return target
    except Exception as e:
        log("!! backup failed:", e)
        return None


async def housekeeping():
    while True:
        try:
            db.execute("DELETE FROM sessions WHERE lastSeen < ?", (now() - SESSION_TTL,))
            db.execute("DELETE FROM raids WHERE win < ?", (raid_window(now()) - 12,))
            flush_traffic()
            LIMIT.prune()
            FAILS.prune()
            if not os.environ.get("DRIFTLINE_NO_BACKUP"):
                backup()
        except Exception as e:
            log("!! housekeeping:", e)
        await asyncio.sleep(600)


# ======================= ADMIN (127.0.0.1 only) =======================
def make_admin_app():
    PW = admin_password()
    tokens, fails = {}, []

    def authed(request):
        t = request.cookies.get("adm")
        return bool(t and tokens.get(t, 0) > time.time())

    @web.middleware
    async def guard(request, handler):
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return web.Response(status=403, text="Local only")
        if request.path in ("/", "/login") or authed(request):
            return await handler(request)
        return web.json_response({"ok": False, "error": "Sign in"}, status=401)

    r = web.RouteTableDef()

    @r.get("/")
    async def page(request):
        return web.FileResponse(P("admin.html"))

    @r.post("/login")
    async def login(request):
        t0 = time.time()
        fails[:] = [f for f in fails if f > t0 - 300]
        if len(fails) >= 10:
            return web.json_response({"ok": False, "error": "Too many tries. Wait 5 minutes."}, status=429)
        d = await request.json()
        if not hmac.compare_digest(str(d.get("password", "")).encode(), PW.encode()):
            fails.append(t0)
            return web.json_response({"ok": False, "error": "Wrong password"}, status=401)
        t = secrets.token_urlsafe(24)
        tokens[t] = t0 + 12 * 3600
        resp = web.json_response({"ok": True})
        resp.set_cookie("adm", t, httponly=True, samesite="Strict", max_age=12 * 3600)
        return resp

    @r.post("/logout")
    async def logout(request):
        tokens.pop(request.cookies.get("adm", ""), None)
        return web.json_response({"ok": True})

    @r.get("/api/overview")
    async def overview(request):
        flush_traffic()
        try:
            link = json.load(open(os.path.join(REPO, "server.json"), encoding="utf-8")).get("server")
        except Exception:
            link = None
        today = datetime.date.today().isoformat()
        traffic = [dict(x) for x in q("SELECT * FROM traffic ORDER BY day DESC LIMIT 14")]
        online = sorted(((u, t) for u, t in LAST_SEEN.items() if now() - t < 600000), key=lambda x: -x[1])
        return web.json_response({
            "version": VERSION, "uptime": int(time.time() - START_TIME), "link": link,
            "players": q1("SELECT COUNT(*) c FROM players")["c"],
            "active_today": q1("SELECT COUNT(*) c FROM players WHERE updatedAt >= ?", now() - 86400000)["c"],
            "online": [{"uid": u, "ago": int((now() - t) / 1000)} for u, t in online],
            "traffic": traffic, "today": next((x for x in traffic if x["day"] == today), None),
            "listings": q1("SELECT COUNT(*) c FROM listings WHERE status='active'")["c"],
            "docks_cached": q1("SELECT COUNT(*) c FROM docks")["c"],
            "gifts": [dict(g) for g in q("SELECT * FROM giftCodes ORDER BY createdAt DESC LIMIT 20")],
        })

    @r.get("/api/players")
    async def players(request):
        qs = (request.query.get("q") or "").strip().lower()
        rows = q("SELECT uid, displayName, points, createdAt, updatedAt, data FROM players WHERE uid LIKE ? ORDER BY points DESC LIMIT 100", f"%{qs}%")
        out = []
        for x in rows:
            d = json.loads(x["data"])
            out.append({"uid": x["uid"], "name": x["displayName"], "points": x["points"], "coins": math.floor(x["points"] - ((d.get("shop") or {}).get("coinsSpent") or 0)),
                        "fish": len(d.get("bag") or []), "crew": (d.get("play") or {}).get("crew"), "created": x["createdAt"], "updated": x["updatedAt"],
                        "online": now() - LAST_SEEN.get(x["uid"], 0) < 600000})
        return web.json_response({"ok": True, "players": out})

    @r.post("/api/players/{uid}/pin")
    async def reset_pin(request):
        uid, d = request.match_info["uid"], await request.json()
        pin = str(d.get("pin", ""))
        if not re.fullmatch(r"\d{6}", pin):
            return web.json_response({"ok": False, "error": "PIN must be 6 digits"})
        db.execute("UPDATE players SET pinScrypt=?, pinHash=NULL WHERE uid=?", (scrypt_hash(pin), uid))
        db.execute("DELETE FROM sessions WHERE uid=?", (uid,))
        return web.json_response({"ok": True})

    @r.post("/api/players/{uid}/coins")
    async def give_coins(request):
        uid, d = request.match_info["uid"], await request.json()
        try:
            amount = int(d.get("amount"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Amount must be a whole number"})
        with tx():
            p = load_player(uid)
            if not p:
                return web.json_response({"ok": False, "error": "No such player"})
            adjust_spent(p, -amount)
            save_player(p)
        return web.json_response({"ok": True})

    @r.post("/api/players/{uid}/signout")
    async def kick(request):
        db.execute("DELETE FROM sessions WHERE uid=?", (request.match_info["uid"],))
        return web.json_response({"ok": True})

    @r.post("/api/gift")
    async def gift(request):
        d = await request.json()
        try:
            amount = int(d.get("amount"))
        except (TypeError, ValueError):
            amount = 0
        if not 0 < amount <= 1_000_000:
            return web.json_response({"ok": False, "error": "Pick an amount from 1 to 1,000,000"})
        return web.json_response({"ok": True, "code": make_gift(amount, "admin-page")})

    @r.post("/api/backup")
    async def backup_now(request):
        target = backup()
        return web.json_response({"ok": bool(target), "file": target})

    app = web.Application(middlewares=[guard])
    app.add_routes(r)
    return app


async def main():
    global HTTP
    HTTP = ClientSession()
    app = web.Application(middlewares=[mw], client_max_size=2 * 1024 * 1024)
    app.router.add_route("*", "/api/{tail:.*}", api_handler)
    app.router.add_route("GET", "/{tail:.*}", static_handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CFG["port"]).start()
    admin_runner = web.AppRunner(make_admin_app(), access_log=None)
    await admin_runner.setup()
    await web.TCPSite(admin_runner, "127.0.0.1", CFG["admin_port"]).start()
    n = q1("SELECT COUNT(*) c FROM players")["c"]
    log(f"Driftline server v{VERSION} running on port {CFG['port']} · admin on http://127.0.0.1:{CFG['admin_port']} · {n} players")
    asyncio.ensure_future(housekeeping())
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        flush_traffic()
