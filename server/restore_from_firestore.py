"""
Driftline: put back anything the first copy from the old Firebase cloud missed.

    python restore_from_firestore.py           (shows what it would change)
    python restore_from_firestore.py --fix     (changes it)

Only ever ADDS: fish missing from a bag are put back, rod tokens and skins
are raised to what the old cloud had. Nothing anyone did on the new server
is removed. A backup of the database is made first.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DRIFTLINE_NO_BACKUP", "1")
import migrate_from_firestore as M  # noqa: E402
S = M.S
FIX = "--fix" in sys.argv[1:]


def old_players():
    src = {}
    exp = S.P("backups", "firestore-export.json")
    if os.path.exists(exp):
        for d in json.load(open(exp, encoding="utf-8")).get("players", []):
            src[d["id"]] = d
    try:
        print("Downloading a fresh copy from the old cloud…")
        for d in M.fetch("players"):
            old = src.get(d["id"])
            # keep whichever copy has more fish
            if not old or len(d.get("bag") or []) >= len(old.get("bag") or []):
                src[d["id"]] = d
    except Exception as e:
        print(f"  (couldn't download, using the saved copy only: {e})")
    return src


def main():
    src = old_players()
    if FIX:
        target = S.P("backups", "before-restore.db")
        if os.path.exists(target):
            os.remove(target)
        S.db.execute("VACUUM INTO ?", (target,))
        print("Backed up the database first: " + target)
    changed = 0
    print()
    for uid, d in sorted(src.items()):
        p = S.load_player(uid)
        if not p:
            print(f"  {uid}: not on this server, skipped")
            continue
        bag = p["data"].get("bag") or []
        have = {c.get("id") for c in bag}
        have_legacy = {(c.get("name"), c.get("tier"), float(c.get("ts") or 0)) for c in bag}
        sold = S.sold_catch_ids(uid)
        add = []
        old_bag = d.get("bag") or []
        for c in old_bag:
            if not isinstance(c, dict) or not c.get("name") or not c.get("tier"):
                continue
            c = dict(c, ts=float(c.get("ts") or 0), bonus=float(c.get("bonus") or 1))
            c["id"] = c.get("id") or M.legacy_id(c)
            if c["id"] in have or c["id"] in sold or (c["name"], c["tier"], c["ts"]) in have_legacy:
                continue
            add.append(c)
            have.add(c["id"])
        shop = p["data"].get("shop") or {}
        oshop = d.get("shop") or {}
        rod_old, rod_now = int(oshop.get("bonusRodLevels") or 0), int(shop.get("bonusRodLevels") or 0)
        skins = 0
        owned = dict(shop.get("ownedCosmetics") or {})
        for k, items in (oshop.get("ownedCosmetics") or {}).items():
            cur = list(owned.get(k) or [])
            for it in items or []:
                if it not in cur:
                    cur.append(it); skins += 1
            owned[k] = cur
        ach = list(shop.get("unlockedAchievements") or [])
        new_ach = [a for a in oshop.get("unlockedAchievements") or [] if a not in ach]
        print(f"  {p['displayName']}: old cloud {len(old_bag)} fish, here {len(bag)} | "
              f"rod tokens old {rod_old}, here {rod_now} | to put back: {len(add)} fish, "
              f"{max(0, rod_old - rod_now)} rod tokens, {skins} skins, {len(new_ach)} achievements")
        if not (add or rod_old > rod_now or skins or new_ach):
            continue
        changed += 1
        if FIX:
            with S.tx():
                p = S.load_player(uid)
                p["data"]["bag"] = sorted(add + (p["data"].get("bag") or []), key=lambda c: -(c.get("ts") or 0))
                sh = p["data"].get("shop") or {}
                sh["bonusRodLevels"] = max(rod_old, int(sh.get("bonusRodLevels") or 0))
                sh["ownedCosmetics"] = owned
                sh["unlockedAchievements"] = ach + new_ach
                p["data"]["shop"] = sh
                S.save_player(p)
    print()
    if not changed:
        print("Nothing missing. Everything from the old cloud is already here.")
    elif FIX:
        print(f"Done: {changed} player(s) fixed. Sign out and back in on the phone to see it.")
    else:
        print(f"{changed} player(s) are missing something. Run again with --fix to put it back.")


if __name__ == "__main__":
    main()
