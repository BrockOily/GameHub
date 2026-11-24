"""GameHub - controller-first library for ALL games on a Windows PC.
Install: pip install pygame-ce      Run: python gamehub.py
Config:  %APPDATA%\\GameHub\\config.json
         {"roots": ["D:/MyGames"], "custom": [{"title": "X", "exe": "C:/x.exe"}], "sgdb_key": ""}
"""
import os, re, json, time, queue, hashlib, difflib, sqlite3, threading, subprocess
import urllib.request, urllib.parse
from pathlib import Path
import pygame
from pygame._sdl2 import controller as sdlc
try: import winreg
except ImportError: winreg = None

APP = Path(os.environ.get("APPDATA", Path.home())) / "GameHub"
ART = APP / "art"; ART.mkdir(parents=True, exist_ok=True)
W, H = 1920, 1080
BG, WHITE, DIM, ACCENT = (12, 14, 20), (240, 242, 247), (150, 158, 175), (90, 170, 255)
CFG = {"roots": [], "custom": [], "sgdb_key": os.environ.get("STEAMGRIDDB_KEY", "")}
cf = APP / "config.json"
if cf.exists():
    try: CFG.update(json.loads(cf.read_text("utf-8")))
    except Exception as e: print("bad config:", e)
else: cf.write_text(json.dumps(CFG, indent=2), "utf-8")

db = sqlite3.connect(APP / "hub.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY, title, source, launch, dir, appid,
  added REAL, last_played REAL DEFAULT 0, plays INT DEFAULT 0, fav INT DEFAULT 0, present INT DEFAULT 1)""")

norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
hid = lambda s: hashlib.md5(s.encode()).hexdigest()[:12]
pretty = lambda n: re.sub(r"\s+", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"[_.\-]+", " ", n))).strip()

# ───────────────────────── 1. SCANNERS (each yields game dicts; launch = "uri:" | "exe:" | "aumid:") ─────────────
SCANNERS = []
def scanner(f): SCANNERS.append(f); return f

def reg_subkeys(hive, path):
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                    yield {winreg.EnumValue(sk, j)[0]: winreg.EnumValue(sk, j)[1] for j in range(winreg.QueryInfoKey(sk)[1])}
    except OSError: return

_steam = None
def steam_root():
    global _steam
    if _steam is None:
        _steam = Path(r"C:\Program Files (x86)\Steam")
        if winreg:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                    _steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
            except OSError: pass
    return _steam

@scanner
def steam():
    root = steam_root(); vdf = root / "steamapps" / "libraryfolders.vdf"
    libs = [root] + ([Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8", "ignore"))] if vdf.exists() else [])
    seen = set()
    for lib in libs:
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                t = acf.read_text("utf-8", "ignore")
                f = lambda k: (re.search(rf'"{k}"\s+"([^"]*)"', t) or [None, ""])[1]
                aid, name = f("appid"), f("name")
                if aid in seen or not name or not int(f("StateFlags") or 0) & 4: continue
                if re.match(r"(Steamworks|Proton|Steam Linux Runtime|SteamVR)", name): continue
                seen.add(aid)
                yield dict(id=f"steam:{aid}", title=name, source="Steam", launch=f"uri:steam://rungameid/{aid}",
                           dir=str(lib / "steamapps" / "common" / f("installdir")), appid=aid)
            except Exception: continue

@scanner
def epic():
    man = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Epic/EpicGamesLauncher/Data/Manifests")
    for p in man.glob("*.item"):
        try:
            m = json.loads(p.read_text("utf-8", "ignore"))
            if "games" not in m.get("AppCategories", ["games"]) or m.get("bIsIncompleteInstall"): continue
            uri = f"com.epicgames.launcher://apps/{m['CatalogNamespace']}%3A{m['CatalogItemId']}%3A{m['AppName']}?action=launch&silent=true"
            yield dict(id=f"epic:{m['AppName']}", title=m["DisplayName"], source="Epic", launch="uri:" + uri, dir=m.get("InstallLocation", ""))
        except Exception: continue

@scanner
def gog():
    if not winreg: return
    for v in reg_subkeys(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games"):
        if v.get("gameName") and v.get("exe"):
            yield dict(id=f"gog:{v.get('gameID')}", title=v["gameName"], source="GOG", launch="exe:" + v["exe"], dir=v.get("path", ""))

KNOWN = [("Minecraft", [r"%APPDATA%\.minecraft", r"%ProgramFiles(x86)%\Minecraft Launcher"],
          [r"%ProgramFiles(x86)%\Minecraft Launcher\MinecraftLauncher.exe", "aumid:Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft"])]
@scanner
def known():  # standalone games with no launcher/manifest: extend KNOWN with (title, detect paths, launch candidates)
    for title, detect, launch in KNOWN:
        if any(os.path.exists(os.path.expandvars(p)) for p in detect):
            for l in launch:
                if l.startswith("aumid:") or os.path.exists(os.path.expandvars(l)):
                    yield dict(id=f"known:{norm(title)}", title=title, source="Standalone",
                               launch=l if l.startswith("aumid:") else "exe:" + os.path.expandvars(l), dir="")
                    break

@scanner
def manual():
    for c in CFG["custom"]:
        yield dict(id="manual:" + hid(c["exe"].lower()), title=c["title"], source="Manual", launch="exe:" + c["exe"], dir=os.path.dirname(c["exe"]))

SIG = {"unityplayer.dll", "gameassembly.dll", "steam_api.dll", "steam_api64.dll", "unitycrashhandler64.exe", "galaxy64.dll",
       "eossdk-win64-shipping.dll", "xinput1_3.dll", "xinput9_1_0.dll", "fmod.dll", "fmodex.dll", "physxcore.dll", "d3dx9_43.dll"}
SIG_EXT = (".pak", ".pck", ".vpk", ".rpa", ".bsa", ".xp3")
SKIP = {"epic games", "steam", "gog galaxy", "windowsapps", "common files", "windows nt", "windows defender", "microsoft", "nvidia corporation",
        "modifiableWindowsApps".lower(), "internet explorer", "dotnet", "msbuild", "reference assemblies", "uninstall information"}
BAD = re.compile(r"unins|setup|install|redist|dxsetup|crash|report|helper|update|config|cef|eac|battleye|dotnet|notification", re.I)

def sig_hit(d, depth=2):  # does this folder look like game files? (engine DLLs, *_Data, .pak archives...)
    try:
        for e in os.scandir(d):
            n = e.name.lower()
            if n in SIG or n.endswith(SIG_EXT) or (e.is_dir() and (n.endswith("_data") or n == "engine")): return True
            if depth > 1 and e.is_dir() and sig_hit(e.path, depth - 1): return True
    except OSError: pass
    return False

def best_exe(d, title):
    base, cands, key = len(Path(d).parts), [], norm(title)
    for r, ds, fs in os.walk(d):
        depth = len(Path(r).parts) - base
        if depth >= 3: ds[:] = []
        cands += [(Path(r, f), depth) for f in fs if f.lower().endswith(".exe") and not BAD.search(f)]
    if not cands: return None
    return max(cands, key=lambda c: (norm(c[0].stem) in key or key in norm(c[0].stem), -c[1], c[0].stat().st_size))[0]

@scanner
def folders():  # heuristic scan of Games folders + Program Files on every drive, plus user "roots"
    roots = [Path(r) for r in CFG["roots"]]
    for L in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        roots += [Path(f"{L}:/{s}") for s in ("Games", "Program Files", "Program Files (x86)", "Program Files/Games")]
    for root in roots:
        try:
            if not root.is_dir(): continue
            for d in sorted(root.iterdir()):
                if d.is_dir() and d.name.lower() not in SKIP and sig_hit(d):
                    exe = best_exe(d, d.name)
                    if exe: yield dict(id="local:" + hid(str(d).lower()), title=pretty(d.name), source="Local", launch=f"exe:{exe}", dir=str(d))
        except OSError: continue

def dedupe(games):  # launcher-sourced entries win over heuristic "Local" ones
    keep = [g for g in games if g["source"] != "Local"]
    dirs = [Path(g["dir"]).as_posix().lower() for g in keep if g.get("dir")]
    titles = {norm(g["title"]) for g in keep}
    for g in games:
        if g["source"] != "Local": continue
        d = Path(g["dir"]).as_posix().lower()
        if norm(g["title"]) in titles or any(d == x or d.startswith(x + "/") or x.startswith(d + "/") for x in dirs): continue
        keep.append(g)
    return keep

def run_scan():
    out = []
    for s in SCANNERS:
        try: out += list(s())
        except Exception as e: print("scanner", s.__name__, "failed:", e)
    return dedupe(out)

def upsert(found):
    now = time.time(); db.execute("UPDATE games SET present=0")
    for g in found:
        db.execute("""INSERT INTO games(id,title,source,launch,dir,appid,added) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          title=excluded.title, source=excluded.source, launch=excluded.launch, dir=excluded.dir, appid=excluded.appid, present=1""",
                   (g["id"], g["title"], g["source"], g["launch"], g.get("dir", ""), g.get("appid"), now))
    db.commit()

def launch(g):
    kind, _, target = g["launch"].partition(":")
    try:
        if kind == "uri": os.startfile(target)
        elif kind == "aumid": subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + target])
        else: subprocess.Popen([target], cwd=os.path.dirname(target))
        db.execute("UPDATE games SET last_played=?, plays=plays+1 WHERE id=?", (time.time(), g["id"])); db.commit()
        pygame.display.iconify()
    except Exception as e: print("launch failed:", e)

# ───────────────────────── 2. ART PIPELINE: Steam local cache → Steam CDN → Steam store match → SteamGridDB ──────
KINDS = (("cover", "library_600x900", "600x900"), ("hero", "library_hero", None))

def get_json(url, headers=None):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GameHub/0.1", **(headers or {})})
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception: return None

def save(url, dest):
    if not url: return False
    try:
        data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "GameHub/0.1"}), timeout=20).read()
        if len(data) < 2000: return False
        tmp = dest.with_suffix(".tmp"); tmp.write_bytes(data); tmp.replace(dest); return True
    except Exception: return False

def steam_local(aid, name, dest):  # art the Steam client already cached (old flat layout + new hashed-subfolder layout)
    lc = steam_root() / "appcache" / "librarycache"
    for p in [*lc.glob(f"{aid}_{name}.jpg"), *(lc / aid).rglob(f"{name}.jpg")]:
        try: dest.write_bytes(p.read_bytes()); return True
        except OSError: pass
    return False

def steam_search(title):  # key-free: match a non-Steam title to a Steam appid so we can reuse Steam's art
    j = get_json("https://store.steampowered.com/api/storesearch/?cc=us&l=en&term=" + urllib.parse.quote(title))
    best = max(((difflib.SequenceMatcher(None, norm(i["name"]), norm(title)).ratio(), i) for i in (j or {}).get("items", [])), key=lambda t: t[0], default=None)
    return str(best[1]["id"]) if best and best[0] >= 0.9 else None

def sgdb_url(title, kind, dims):
    h = {"Authorization": "Bearer " + CFG["sgdb_key"]}
    j = get_json("https://www.steamgriddb.com/api/v2/search/autocomplete/" + urllib.parse.quote(title), h)
    if not j or not j.get("data"): return None
    ep = "grids" if kind == "cover" else "heroes"
    j = get_json(f"https://www.steamgriddb.com/api/v2/{ep}/game/{j['data'][0]['id']}" + (f"?dimensions={dims}" if dims else ""), h)
    return j["data"][0]["url"] if j and j.get("data") else None

def miss_fresh(p): return p.exists() and time.time() - p.stat().st_mtime < 7 * 86400

def resolve(g):
    key = hid(g["id"])
    todo = [k for k in KINDS if not (ART / f"{key}_{k[0]}.jpg").exists() and not miss_fresh(ART / f"{key}_{k[0]}.miss")]
    if not todo: return
    aid = g.get("appid") or steam_search(g["title"])
    for kind, name, dims in todo:
        dest = ART / f"{key}_{kind}.jpg"
        ok = bool(aid) and (steam_local(aid, name, dest) or save(f"https://cdn.cloudflare.steamstatic.com/steam/apps/{aid}/{name}.jpg", dest))
        if not ok and CFG["sgdb_key"]: ok = save(sgdb_url(g["title"], kind, dims), dest)
        if not ok: dest.with_suffix(".miss").touch()
        time.sleep(0.2)

def art_worker(q):
    while True:
        try: resolve(q.get())
        except Exception as e: print("art:", e)

# ───────────────────────── 3. SORTING / FILTERING ─────────────────────────
SORTS = [("Recently played", "last_played DESC, title COLLATE NOCASE"), ("A-Z", "title COLLATE NOCASE"),
         ("Most played", "plays DESC, title COLLATE NOCASE"), ("Recently added", "added DESC, title COLLATE NOCASE"),
         ("Launcher", "source, title COLLATE NOCASE"), ("Favorites first", "fav DESC, title COLLATE NOCASE")]

# ───────────────────────── 4. UI ─────────────────────────
FONT, cache, tiles = {}, {}, {}

def fit(img, w, h):  # scale-to-fill + center crop
    iw, ih = img.get_size(); k = max(w / iw, h / ih)
    s = pygame.transform.smoothscale(img, (max(w, int(iw * k)), max(h, int(ih * k))))
    return s.subsurface(((s.get_width() - w) // 2, (s.get_height() - h) // 2, w, h)).copy()

def art(g, kind, w, h):
    p = ART / f"{hid(g['id'])}_{kind}.jpg"; k = (p.name, w, h)
    if k not in cache:
        if not p.exists(): return None
        try: cache[k] = fit(pygame.image.load(str(p)).convert(), w, h)
        except Exception: cache[k] = None
        if len(cache) > 400: cache.pop(next(iter(cache)))
    return cache[k]

def placeholder(title, w, h, text=True):  # generated tile so the UI is never empty
    base = pygame.Color(0, 0, 0); base.hsva = (int(hid(title), 16) % 360, 60, 62, 100)
    s = pygame.Surface((w, h))
    for y in range(h):
        k = 1 - 0.55 * y / h
        pygame.draw.line(s, (int(base.r * k), int(base.g * k), int(base.b * k)), (0, y), (w, y))
    if text:
        lines, cur = [], ""
        for wd in title.split():
            t = (cur + " " + wd).strip()
            if FONT["tile"].size(t)[0] > w - 24 and cur: lines.append(cur); cur = wd
            else: cur = t
        lines.append(cur); y = h / 2 - len(lines) * 17
        for l in lines:
            im = FONT["tile"].render(l, True, WHITE); s.blit(im, (w / 2 - im.get_width() / 2, y)); y += 34
    return s

def tile(g, w, h):
    a = art(g, "cover", w, h)
    if a: return a
    k = (g["id"], w, h)
    if k not in tiles: tiles[k] = placeholder(g["title"], w, h)
    return tiles[k]

def hero(g):
    a = art(g, "hero", W, 640)
    if a: return a
    k = (g["id"], "h")
    if k not in tiles: tiles[k] = placeholder(g["title"], W, 640, False)
    return tiles[k]

def txt(font, s, color, maxw=None):
    im = font.render(s, True, color)
    return pygame.transform.smoothscale(im, (maxw, int(im.get_height() * maxw / im.get_width()))) if maxw and im.get_width() > maxw else im

BTN = {pygame.CONTROLLER_BUTTON_A: "play", pygame.CONTROLLER_BUTTON_X: "sort", pygame.CONTROLLER_BUTTON_Y: "fav",
       pygame.CONTROLLER_BUTTON_LEFTSHOULDER: "prev", pygame.CONTROLLER_BUTTON_RIGHTSHOULDER: "next",
       pygame.CONTROLLER_BUTTON_B: "top", pygame.CONTROLLER_BUTTON_START: "scan", pygame.CONTROLLER_BUTTON_BACK: "quit"}
KEYS = {pygame.K_RETURN: "play", pygame.K_TAB: "sort", pygame.K_f: "fav", pygame.K_q: "prev", pygame.K_e: "next",
        pygame.K_HOME: "top", pygame.K_F5: "scan", pygame.K_ESCAPE: "quit", pygame.K_SPACE: "play", pygame.K_F11: "full"}
DPAD = {pygame.CONTROLLER_BUTTON_DPAD_LEFT: "L", pygame.CONTROLLER_BUTTON_DPAD_RIGHT: "R",
        pygame.CONTROLLER_BUTTON_DPAD_UP: "U", pygame.CONTROLLER_BUTTON_DPAD_DOWN: "D"}
ARROWS = {pygame.K_LEFT: "L", pygame.K_RIGHT: "R", pygame.K_UP: "U", pygame.K_DOWN: "D"}
STEP = {"L": -1, "R": 1, "U": -7, "D": 7}

def main():
    pygame.init(); sdlc.init()
    if hasattr(sdlc, "set_eventstate"): sdlc.set_eventstate(True)  # not present in pygame-ce; events are on once a pad is opened
    scr = pygame.display.set_mode((W, H), pygame.SCALED | pygame.FULLSCREEN)
    pygame.display.set_caption("GameHub"); pygame.mouse.set_visible(False)
    mk = lambda s, b=False: pygame.font.SysFont("segoeui,arial", s, bold=b)
    FONT.update(title=mk(76, True), meta=mk(30), ui=mk(30, True), tile=mk(26, True), hint=mk(24))
    shade = pygame.Surface((W, 640), pygame.SRCALPHA)
    for y in range(640):
        a = 0 if y < 260 else int(255 * ((y - 260) / 380) ** 1.3)
        if y < 130: a = max(a, int(170 * (1 - y / 130)))
        pygame.draw.line(shade, (*BG, min(a, 255)), (0, y), (W, y))
    dimmer = pygame.Surface((220, 330), pygame.SRCALPHA); dimmer.fill((0, 0, 0, 90))

    pads = {}
    def add_pad(i):
        try:
            if sdlc.is_controller(i): c = sdlc.Controller(i); pads[c.as_joystick().get_instance_id()] = c
        except Exception as e: print("pad:", e)
    for i in range(sdlc.get_count()): add_pad(i)

    S = dict(games=[], sel=0, pos=0.0, sort=0, filt=0, filters=["All"], fade=255, cur=None, scanning=False, run=True)
    art_q, scan_out = queue.Queue(), queue.Queue()
    threading.Thread(target=art_worker, args=(art_q,), daemon=True).start()

    def reload(keep=None):
        srcs = [r[0] for r in db.execute("SELECT DISTINCT source FROM games WHERE present=1 ORDER BY 1")]
        S["filters"] = ["All", "Favorites"] + srcs; f = S["filters"][S["filt"] % len(S["filters"])]
        where = "present=1" + {"All": "", "Favorites": " AND fav=1"}.get(f, " AND source=?")
        cur = db.execute(f"SELECT * FROM games WHERE {where} ORDER BY {SORTS[S['sort']][1]}", [] if f in ("All", "Favorites") else [f])
        cols = [c[0] for c in cur.description]; S["games"] = [dict(zip(cols, r)) for r in cur]
        ids = [g["id"] for g in S["games"]]
        S["sel"] = ids.index(keep) if keep in ids else min(S["sel"], max(len(ids) - 1, 0))
    def queue_art():
        cur = db.execute("SELECT id,title,appid FROM games WHERE present=1")
        for r in cur.fetchall(): art_q.put(dict(id=r[0], title=r[1], appid=r[2]))
    def start_scan():
        if S["scanning"]: return
        S["scanning"] = True
        threading.Thread(target=lambda: scan_out.put(run_scan()), daemon=True).start()
    def cur_id(): return S["games"][S["sel"]]["id"] if S["games"] else None

    def act(a):
        gs = S["games"]
        if a == "quit": S["run"] = False
        elif a == "full": pygame.display.toggle_fullscreen()
        elif a == "scan": start_scan()
        elif a == "top": S["sel"] = 0
        elif a == "sort": S["sort"] = (S["sort"] + 1) % len(SORTS); reload(cur_id())
        elif a in ("prev", "next"): S["filt"] += 1 if a == "next" else -1; S["filt"] %= len(S["filters"]); S["sel"] = 0; reload()
        elif a == "fav" and gs:
            db.execute("UPDATE games SET fav=1-fav WHERE id=?", (cur_id(),)); db.commit(); reload(cur_id())
        elif a == "play" and gs: launch(gs[S["sel"]])

    hits = []   # (rect, left-click fn, right-click fn) rebuilt every frame while drawing
    def pick(i, fav=False):
        if not fav and i == S["sel"]: return act("play")
        S["sel"] = i
        if fav: act("fav")
    def click(pos, btn):
        for r, left, right in hits:
            if r.collidepoint(pos):
                fn = left if btn == 1 else right
                if fn: fn()
                return
    def button(r, label, fn, accent=False):
        col = (60, 120, 200) if accent else (28, 32, 44)
        if fn and pygame.mouse.get_visible() and r.collidepoint(pygame.mouse.get_pos()): col = tuple(min(255, c + 30) for c in col)
        pygame.draw.rect(scr, col, r, border_radius=r.height // 2)
        im = FONT["hint"].render(label, True, WHITE if fn else DIM); scr.blit(im, im.get_rect(center=r.center))
        if fn: hits.append((r, fn, None))

    reload(); queue_art(); start_scan()
    dpad, stick, prev, nxt, clock = set(), set(), set(), 0.0, pygame.time.Clock()
    while S["run"]:
        dt = clock.tick(60) / 1000; now = time.time()
        for e in pygame.event.get():
            if e.type in (pygame.CONTROLLERBUTTONDOWN, pygame.KEYDOWN): pygame.mouse.set_visible(False)
            if e.type == pygame.QUIT: S["run"] = False
            elif e.type == pygame.MOUSEMOTION: pygame.mouse.set_visible(True)
            elif e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3): pygame.mouse.set_visible(True); click(e.pos, e.button)
            elif e.type == pygame.MOUSEWHEEL and S["games"]: S["sel"] = max(0, min(len(S["games"]) - 1, S["sel"] + e.x - e.y))
            elif e.type == pygame.CONTROLLERDEVICEADDED: add_pad(e.device_index)
            elif e.type == pygame.CONTROLLERDEVICEREMOVED: pads.pop(e.instance_id, None)
            elif e.type == pygame.CONTROLLERAXISMOTION and e.axis in (pygame.CONTROLLER_AXIS_LEFTX, pygame.CONTROLLER_AXIS_LEFTY):
                pair = ("L", "R") if e.axis == pygame.CONTROLLER_AXIS_LEFTX else ("U", "D")
                stick.difference_update(pair); v = e.value / 32768
                if v < -0.5: stick.add(pair[0])
                elif v > 0.5: stick.add(pair[1])
            elif e.type == pygame.CONTROLLERBUTTONDOWN:
                if e.button in DPAD: dpad.add(DPAD[e.button])
                elif e.button in BTN: act(BTN[e.button])
            elif e.type == pygame.CONTROLLERBUTTONUP: dpad.discard(DPAD.get(e.button))
            elif e.type == pygame.KEYDOWN and e.key in KEYS: act(KEYS[e.key])
            elif e.type == pygame.WINDOWFOCUSGAINED: reload(cur_id())  # refresh "recently played" after returning from a game
        keys = pygame.key.get_pressed()
        held = dpad | stick | {n for k, n in ARROWS.items() if keys[k]}
        fire = False
        if held != prev: fire, nxt = bool(held), now + 0.4
        elif held and now >= nxt: fire, nxt = True, now + 0.08
        if fire and S["games"]:
            d = next(x for x in "LRUD" if x in held)
            S["sel"] = max(0, min(len(S["games"]) - 1, S["sel"] + STEP[d]))
        prev = set(held)
        try: upsert(scan_out.get_nowait()); S["scanning"] = False; reload(cur_id()); queue_art()
        except queue.Empty: pass

        # ── draw ──
        scr.fill(BG); hits.clear(); gs, sel = S["games"], S["sel"]
        if gs:
            g = gs[sel]
            if g["id"] != S["cur"]: S["cur"], S["fade"] = g["id"], 40
            S["fade"] = min(255, S["fade"] + 900 * dt)
            hs = hero(g); hs.set_alpha(int(S["fade"])); scr.blit(hs, (0, 0)); scr.blit(shade, (0, 0))
            scr.blit(txt(FONT["title"], g["title"], WHITE, W - 180), (90, 430))
            played = "Never played" if not g["last_played"] else "Last played " + time.strftime("%b %d, %Y", time.localtime(g["last_played"]))
            meta = f"{g['source']}    |    {played}    |    {g['plays']} launches" + ("    |    FAVORITE" if g["fav"] else "")
            scr.blit(FONT["meta"].render(meta, True, DIM), (92, 530))
            button(pygame.Rect(90, 570, 150, 46), "Play", lambda: act("play"), True)
            button(pygame.Rect(256, 570, 190, 46), "Unfavorite" if g["fav"] else "Favorite", lambda: act("fav"))
            TW, TH, BW, BH, GAP, cy = 220, 330, 264, 396, 26, 820
            S["pos"] += (sel - S["pos"]) * min(1, dt * 14); pos = S["pos"]; extra = (BW - TW) / 2
            cxf = lambda i: W / 2 + (i - pos) * (TW + GAP) + max(-1, min(1, i - pos)) * extra
            for i in range(max(0, int(pos) - 6), min(len(gs), int(pos) + 8)):
                if i == sel: continue
                x, y = cxf(i) - TW / 2, cy - TH / 2; scr.blit(tile(gs[i], TW, TH), (x, y)); scr.blit(dimmer, (x, y))
                hits.append((pygame.Rect(int(x), int(y), TW, TH), lambda i=i: pick(i), lambda i=i: pick(i, True)))
            s = tile(g, BW, BH); r = s.get_rect(center=(cxf(sel), cy))
            pygame.draw.rect(scr, ACCENT, r.inflate(12, 12), 4, border_radius=8); scr.blit(s, r)
            hits.insert(0, (r, lambda: pick(sel), lambda: pick(sel, True)))
        else:
            scr.blit(FONT["meta"].render("No games found. Press Start / F5 to scan, or add folders to config.json", True, DIM), (90, 500))
        x = 90   # top bar: launcher filter tabs (LB/RB) + sort mode (X)
        cur_f = S["filt"] % len(S["filters"])
        for i, name in enumerate(S["filters"]):
            im = FONT["ui"].render(name, True, WHITE if i == cur_f else DIM)
            hits.append((pygame.Rect(x - 16, 28, im.get_width() + 32, 46), lambda i=i: (S.update(filt=i, sel=0), reload()), None))
            if i == cur_f: pygame.draw.rect(scr, (40, 70, 110), (x - 16, 28, im.get_width() + 32, 46), border_radius=23)
            scr.blit(im, (x, 33)); x += im.get_width() + 48
        right = FONT["ui"].render(f"Sort: {SORTS[S['sort']][0]}    {sel + 1 if gs else 0} / {len(gs)}", True, WHITE)
        scr.blit(right, (W - 90 - right.get_width(), 33))
        hits.append((right.get_rect(topleft=(W - 90 - right.get_width(), 33)).inflate(24, 16), lambda: act("sort"), None))
        x = 90   # bottom row: controller hints that double as clickable buttons
        for key, label, a in (("A", "Play", "play"), ("X", "Sort", "sort"), ("Y", "Favorite", "fav"), ("LB/RB", "Filter", None),
                              ("B", "Top", "top"), ("Start", "Rescan", "scan"), ("Back", "Exit", "quit")):
            lab = f"{key}  {label}"; r = pygame.Rect(x, H - 54, FONT["hint"].size(lab)[0] + 36, 40)
            button(r, lab, (lambda a=a: act(a)) if a else None); x += r.width + 14
        if S["scanning"]: scr.blit(FONT["hint"].render("Scanning...", True, ACCENT), (x + 10, H - 46))
        pygame.display.flip()
    pygame.quit()

if __name__ == "__main__":
    main()
