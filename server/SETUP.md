# Driftline game server: setup (Windows)

Runs next to Tic-Tac-Toe³ on the same PC, on its own ports: **game 8766**, **admin 5051** (TTT³ keeps 8765/5050, SCODE AI keeps 11500).
Nothing secret goes on GitHub: `admin-password.txt`, `config.json`, `data\` and `backups\` are in `.gitignore`.
Driftline doesn't send any email, so it doesn't touch the Gmail limit.

## 1. Get the code (once)
```
cd %USERPROFILE%
git clone https://github.com/DriftLineGame/Driftline.git
cd Driftline\server
```
aiohttp is already installed for TTT³. If not: `python -m pip install -r requirements.txt`.
cloudflared is reused from `C:\Users\Administrator\tictactoe3\server\cloudflared.exe` automatically.

## 2. Copy everyone over from the old Firebase cloud (once, server closed)
Double-click **`migrate.bat`** (or `python migrate_from_firestore.py`). It prints how many players, battles,
listings and gift codes it copied. Safe to run again: players already here are skipped.

## 3. Start it
Double-click **`start-server.bat`**. The window shows the **admin password** (first run only, saved in
`admin-password.txt`), then a big **DRIFTLINE SERVER LINK**, then pushes `server.json` so the website finds the server.

## 4. Admin page
Double-click **`open-admin.bat`** (http://127.0.0.1:5051, this computer only). Shows players online, data used per day,
gift codes, PIN resets and backups. The same password is the in-game gift-code passcode for the `admin` account.

## 5. Start by itself
Double-click **`install-autostart.bat`** (`remove-autostart.bat` undoes it).

## Day to day
- Off 11 PM to 7 AM is fine: the site says "Game servers are offline" and people keep fishing; their catches sync when it's back.
- Each start makes a new tunnel link and pushes it automatically.
- Data: phones send only new catches, leaderboards come as totals, and bigger replies are gzipped. The admin page shows MB used per day.
- Backups: a dated copy of the database is saved in `server\backups` every day (last 14 kept).
