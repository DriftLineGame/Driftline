"""
Starts the Driftline game server AND a Cloudflare quick tunnel, then publishes the new tunnel link.

What it does:
  1. starts driftline_server.py (restarts it if it ever crashes)
  2. starts `cloudflared tunnel --url http://localhost:8766` (restarts it if it dies)
  3. when the tunnel prints its https://….trycloudflare.com link it:
       - writes server.json in the repo folder: {"server": "https://…"}
       - copies the link to the clipboard
       - commits and pushes server.json so the website finds the server
Stop everything with Ctrl+C or by closing the window.
(Same pattern as Tic-Tac-Toe³, on its own ports: game 8766, admin 5051.)
"""
import datetime, json, os, re, shutil, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
cfg_path = os.path.join(HERE, "config.json")
CFG = {"port": 8766, "repo_dir": "..", "auto_push_server_json": True, "cloudflared_path": ""}
if os.path.exists(cfg_path):
    CFG.update(json.load(open(cfg_path, encoding="utf-8")))
PORT = CFG["port"]
REPO = os.path.abspath(os.path.join(HERE, CFG.get("repo_dir") or ".."))
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
stop = threading.Event()


def say(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def find_cloudflared():
    home = os.environ.get("USERPROFILE", "")
    cands = [CFG.get("cloudflared_path") or "",
             # the copy Tic-Tac-Toe³ already uses on this computer
             os.path.join(home, "tictactoe3", "server", "cloudflared.exe") if home else "",
             os.path.abspath(os.path.join(HERE, "..", "..", "tictactoe3", "server", "cloudflared.exe")),
             os.path.join(HERE, "cloudflared.exe"), shutil.which("cloudflared") or "",
             r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe"]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, timeout=120)


def publish(url):
    data = {"server": url, "updated": datetime.datetime.now().isoformat(timespec="seconds")}
    path = os.path.join(REPO, "server.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    bar = "=" * 64
    print(f"\n{bar}\n  DRIFTLINE SERVER LINK:  {url}\n  Saved to {path}\n{bar}\n", flush=True)
    if os.name == "nt":
        try:
            subprocess.run("clip", input=url.encode(), shell=True, check=False)
            say("Link copied to the clipboard.")
        except Exception:
            pass
    if CFG.get("auto_push_server_json") and os.path.isdir(os.path.join(REPO, ".git")) and shutil.which("git"):
        try:
            git("pull", "--rebase", "--autostash", "-q")
            git("add", "server.json")
            git("commit", "-q", "-m", "Update Driftline server link")
            r = git("push", "-q")
            if r.returncode == 0:
                branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
                say(f"server.json pushed to GitHub ({branch}). The website will find the server in about a minute.")
            else:
                say("Couldn't push server.json: " + (r.stderr.strip()[:300] or "unknown error"))
                say("Upload server.json to the repo yourself (GitHub website → server.json → edit → paste the link).")
        except Exception as e:
            say(f"git push failed: {e}")
    else:
        say("Not a git folder, so update server.json on GitHub yourself with the link above.")


def run_server():
    while not stop.is_set():
        say("Starting Driftline game server…")
        p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "driftline_server.py")], cwd=HERE)
        while p.poll() is None and not stop.is_set():
            time.sleep(1)
        if stop.is_set():
            p.terminate()
            return
        say(f"Game server stopped (code {p.returncode}). Restarting in 5 s…")
        time.sleep(5)


def run_tunnel():
    exe = find_cloudflared()
    if not exe:
        say("!! Couldn't find cloudflared.exe. Put its full path in config.json as \"cloudflared_path\", then start again.")
        say("   The server still runs locally at http://localhost:%d" % PORT)
        return
    say(f"Using {exe}")
    while not stop.is_set():
        say("Starting Cloudflare tunnel…")
        p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        published = False
        for line in p.stdout:
            if stop.is_set():
                break
            m = URL_RE.search(line)
            if m and not published:
                published = True
                publish(m.group(0))
            elif any(k in line for k in ("ERR", "error")) and "trycloudflare" not in line:
                print("   tunnel: " + line.strip()[:200], flush=True)
        if stop.is_set():
            p.terminate()
            return
        say("Tunnel stopped. Restarting in 10 s (a new link will be published)…")
        time.sleep(10)


if __name__ == "__main__":
    print("Driftline server launcher · Powered by SCODE\n", flush=True)
    threads = [threading.Thread(target=run_server, daemon=True), threading.Thread(target=run_tunnel, daemon=True)]
    for t in threads:
        t.start()
        time.sleep(2)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        say("Shutting down…")
        stop.set()
        time.sleep(2)
