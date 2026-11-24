# GameHub

A controller-first, Big Picture-style library for **every game on your Windows PC**, not just Steam.

## Features
- **Multi-source discovery:** Steam, Epic, GOG, known standalone games (e.g. Minecraft), a heuristic folder scanner for games with no launcher, and manual entries.
- **Artwork pipeline:** Steam's local cache, then Steam CDN, then a Steam store title match (no key needed), then SteamGridDB (optional key). Generated placeholders fill any gaps.
- **Sorting and filtering:** recently played, A-Z, most played, recently added, launcher, favorites first. Filter by launcher or favorites.
- **Controller, mouse and keyboard:** fully navigable with any pad (SDL standard mapping), plus clickable UI.
- Play counts and favorites are stored in SQLite and survive rescans.

## Install and run
```
pip install -r requirements.txt
python gamehub.py
```
Windows only. Data lives in `%APPDATA%\GameHub` (`config.json`, `hub.db`, `art/`).

## Controls
| Action | Controller | Keyboard | Mouse |
|---|---|---|---|
| Move | D-pad / left stick | Arrows | Wheel / click tile |
| Play | A | Enter, Space | Click selected tile or Play |
| Cycle sort | X | Tab | Click sort label |
| Favorite | Y | F | Right-click tile |
| Filter | LB / RB | Q / E | Click tab |
| Jump to start | B | Home | Top button |
| Rescan | Start | F5 | Rescan button |
| Fullscreen toggle | - | F11 | - |
| Exit | Back | Esc | Exit button |

## Configuration (`config.json`)
```json
{
  "roots": ["D:/MyGames"],
  "custom": [{"title": "My Game", "exe": "C:/Games/MyGame/game.exe"}],
  "sgdb_key": ""
}
```
- `roots`: extra folders to scan (each subfolder is checked for game files).
- `custom`: games added by hand.
- `sgdb_key`: free [SteamGridDB](https://www.steamgriddb.com) API key (or set `STEAMGRIDDB_KEY`) for better art on non-Steam games.

## Extending
Add a scanner by writing a function decorated with `@scanner` that yields dicts with `id`, `title`, `source`, `launch` (`uri:`, `exe:` or `aumid:`) and optionally `dir` and `appid`. Add standalone games with no manifest to the `KNOWN` table.

## Roadmap
- Hide/remove games from the UI
- Xbox / Game Pass scanner and exe-icon extraction
- Playtime tracking and restoring the hub when a game exits
- Delete `art/*.miss` files to force artwork retries

## License
MIT
