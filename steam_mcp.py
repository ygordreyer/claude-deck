#!/usr/bin/env python3
"""Stdio MCP server exposing Steam client controls to Claude.

Each tool is a JavaScript snippet evaluated in Steam's SharedJSContext through the
CEF remote debugger on localhost:8080 (the same channel Decky Loader uses).
"""
import asyncio
import json
import os
import sys
import time

import aiohttp

import deck_tools

CDP_LIST = "http://127.0.0.1:8080/json"
MAX_OUTPUT = 60_000
SIGN_IN_TIMEOUT = 15 * 60
SECRETS_DIR = os.path.expanduser("~/.local/state/claude-deck/secrets")
BACKUPS_DIR = os.path.expanduser("~/.local/share/claude-deck/backups")
KEEP_BACKUPS = 10

PRELUDE = r"""
const __req = (() => { let r; window.webpackChunksteamui.push([[Symbol()], {}, x => { r = x; }]); return r; })();
const findModule = (src) => { const id = Object.keys(__req.m).find(i => __req.m[i].toString().includes(src)); return id ? __req(id) : null; };
const perfStore = () => {
  if (window.SystemPerfStore) return window.SystemPerfStore;
  const m = findModule("window.SystemPerfStore=");
  const c = m && Object.values(m).find(v => typeof v === "function" && v.Get);
  return c ? c.Get() : null;
};
const setSetting = (key, value) => {
  const m = findModule("SteamClient.Settings.SetSetting(");
  const f = m && Object.values(m).find(x => typeof x === "function" && x.toString().includes("SetSetting("));
  if (!f) throw new Error("Steam setting writer not found");
  return f(key, value);
};
const SHORTCUT = 1073741824;
const appOverview = (id) => {
  const o = appStore.GetAppOverviewByAppID(Number(id));
  if (!o) throw new Error(`No app with appid ${id}`);
  return o;
};
const gameId = (o) => o.m_gameid || String(o.appid);
const appDetails = (id) => new Promise((res, rej) => {
  let h;
  const t = setTimeout(() => { h?.unregister(); rej(new Error("Timed out loading app details")); }, 8000);
  h = SteamClient.Apps.RegisterForAppDetails(Number(id), d => { clearTimeout(t); h?.unregister(); res(d); });
});
const plain = (o) => Object.fromEntries(Object.entries(o || {}).filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v)));
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
"""

TOOLS = {
    "steam_js": {
        "description": "Run an async JavaScript function body in Steam's UI context (SharedJSContext) and return its JSON result. "
                       "Globals: SteamClient, appStore, collectionStore, appDetailsStore, settingsStore, SteamUIStore, etc. "
                       "Helpers: findModule(srcSubstring), perfStore(), setSetting(key,value), appOverview(appid), appDetails(appid), plain(obj), sleep(ms). "
                       "Use `return` to return a value. Use this for anything the other tools don't cover.",
        "schema": {"code": {"type": "string", "description": "Function body, e.g. `return Object.keys(SteamClient.Apps)`"}},
        "required": ["code"],
        "js": None,
    },
    "sign_in": {
        "description": "Ask the user to sign in to something without leaving the chat. Shows a card in the Claude chat "
                       "(and the Quick Access Menu) with the URL as a QR code, an 'Open here' button that opens Steam's "
                       "built-in browser, an optional device/user code with a Copy button, and optionally a box for the "
                       "user to paste a value back. Blocks until the user finishes or cancels (up to 15 minutes). "
                       "Use it for device-code logins (Microsoft, GitHub, etc.), OAuth URLs whose resulting code must be "
                       "pasted back (e.g. legendary/Epic authorizationCode), or when you need a password or API token. "
                       "With ask_for='secret' the value is NOT returned; it is saved to a 0600 file and you get the path, "
                       "so pipe it into commands (e.g. `--password-stdin < path`) and delete the file when done. "
                       "Start background login commands (device-code flows) BEFORE calling this so their code is ready.",
        "schema": {
            "service": {"type": "string", "description": "What the user signs in to, e.g. 'Microsoft (Minecraft)'"},
            "url": {"type": "string", "description": "Page the user must visit"},
            "code": {"type": "string", "description": "Device/user code to enter on that page, if any"},
            "instructions": {"type": "string", "description": "One or two short sentences on what to do"},
            "ask_for": {"type": "string", "enum": ["none", "text", "secret"], "default": "none",
                        "description": "'text' to have the user paste a code back, 'secret' for passwords/tokens"},
            "input_label": {"type": "string", "description": "Label for the input box"},
        },
        "required": ["service"],
        "js": None,
    },
    "screenshot": {
        "description": "Capture what's on the device's screen right now (the game, after closing the Quick Access Menu) "
                       "and return the PNG path. Then Read the file to see it.",
        "schema": {},
        "js": None,
    },
    "backup_saves": {
        "description": "Snapshot a game's save files before anything risky (changing Proton version, mods, "
                       "deleting or moving game files). Pass the absolute save paths (find them from the game's "
                       "PCGamingWiki 'Save game data location', its compatdata prefix, or the launcher). "
                       "Keeps the last 10 per game. Returns the backup file.",
        "schema": {"game": {"type": "string", "description": "Game name, used to group backups"},
                   "paths": {"type": "array", "items": {"type": "string"}, "description": "Absolute files/folders"}},
        "required": ["game", "paths"],
        "js": None,
    },
    "list_backups": {
        "description": "List save backups made with backup_saves, newest first (optionally for one game).",
        "schema": {"game": {"type": "string"}},
        "js": None,
    },
    "restore_backup": {
        "description": "Restore a save backup over the current saves (they're backed up first, as 'before-restore').",
        "schema": {"file": {"type": "string", "description": "A path from list_backups"}},
        "required": ["file"],
        "js": None,
    },
    "list_games": {
        "description": "List games in the Steam library (including non-Steam shortcuts), most recently played first.",
        "schema": {
            "query": {"type": "string", "description": "Case-insensitive name filter"},
            "installed_only": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "default": 50},
        },
        "js": r"""
const col = ARGS.installed_only === false ? collectionStore.allGamesCollection : collectionStore.localGamesCollection;
const q = (ARGS.query || "").toLowerCase();
let apps = [...col.allApps].filter(a => !q || a.display_name.toLowerCase().includes(q));
apps.sort((a, b) => (b.rt_last_time_played || 0) - (a.rt_last_time_played || 0));
return { total: apps.length, games: apps.slice(0, ARGS.limit || 50).map(a => ({
  appid: a.appid, name: a.display_name, installed: a.installed, non_steam: a.app_type === SHORTCUT,
  size_gb: +(Number(a.size_on_disk || 0) / 1e9).toFixed(1),
  hours: +((a.minutes_playtime_forever || 0) / 60).toFixed(1),
  last_played: a.rt_last_time_played ? new Date(a.rt_last_time_played * 1000).toISOString().slice(0, 10) : null,
})) };
""",
    },
    "game_info": {
        "description": "Details for one game: launch options, compatibility tool (Proton), install info, cloud status, etc.",
        "schema": {"appid": {"type": "integer"}},
        "required": ["appid"],
        "js": r"""
const o = appOverview(ARGS.appid);
const d = await appDetails(o.appid);
const running = (SteamUIStore.RunningApps || []).some(a => a.appid === o.appid);
return { appid: o.appid, name: o.display_name, non_steam: o.app_type === SHORTCUT, installed: o.installed, running,
         compat_data_path: `~/.steam/steam/steamapps/compatdata/${o.appid}/pfx`, details: plain(d) };
""",
    },
    "set_launch_options": {
        "description": "Set a game's launch options (e.g. `PROTON_LOG=1 %command%`). Pass an empty string to clear.",
        "schema": {"appid": {"type": "integer"}, "options": {"type": "string"}},
        "required": ["appid", "options"],
        "js": r"""
const o = appOverview(ARGS.appid);
if (o.app_type === SHORTCUT) SteamClient.Apps.SetShortcutLaunchOptions(o.appid, ARGS.options);
else SteamClient.Apps.SetAppLaunchOptions(o.appid, ARGS.options);
await sleep(300);
const d = await appDetails(o.appid);
return { name: o.display_name, launch_options: d.strLaunchOptions };
""",
    },
    "list_compat_tools": {
        "description": "List the compatibility tools (Proton versions) available for a game.",
        "schema": {"appid": {"type": "integer"}},
        "required": ["appid"],
        "js": "return await SteamClient.Apps.GetAvailableCompatTools(Number(ARGS.appid));",
    },
    "set_compat_tool": {
        "description": "Force a compatibility tool for a game by its internal name (strToolName from list_compat_tools). Empty string resets to default.",
        "schema": {"appid": {"type": "integer"}, "tool": {"type": "string"}},
        "required": ["appid", "tool"],
        "js": r"""
SteamClient.Apps.SpecifyCompatTool(Number(ARGS.appid), ARGS.tool);
await sleep(300);
const d = await appDetails(ARGS.appid);
return { compat_tool: d.strCompatToolName, display: d.strCompatToolDisplayName };
""",
    },
    "launch_game": {
        "description": "Launch a game.",
        "schema": {"appid": {"type": "integer"}},
        "required": ["appid"],
        "js": r"""
const o = appOverview(ARGS.appid);
SteamClient.Apps.RunGame(gameId(o), "", -1, 100);
return { launching: o.display_name };
""",
    },
    "stop_game": {
        "description": "Force-quit a running game (unsaved progress is lost).",
        "schema": {"appid": {"type": "integer"}},
        "required": ["appid"],
        "js": r"""
const o = appOverview(ARGS.appid);
SteamClient.Apps.TerminateApp(gameId(o), false);
return { stopping: o.display_name };
""",
    },
    "running_games": {
        "description": "List currently running games.",
        "schema": {},
        "js": "return (SteamUIStore.RunningApps || []).map(a => ({ appid: a.appid, name: a.display_name }));",
    },
    "get_performance": {
        "description": "Read Game Mode performance settings: FPS limit, refresh rate, TDP, per-game profile, overlay level, limits.",
        "schema": {},
        "js": r"""
const p = perfStore();
const s = settingsStore.settings || {};
return { perf_api_available: !!SteamClient.System.Perf, current_game_id: p?.nCurrentGameID,
         per_app: p?.msgSettingsPerApp, global: p?.msgSettingsGlobal, limits: p?.msgLimits, state: p?.msgState,
         tdp_limit_enabled: s.steamos_tdp_limit_enabled, tdp_limit_watts: s.steamos_tdp_limit };
""",
    },
    "set_performance": {
        "description": "Change Game Mode performance settings. Omit a field to leave it unchanged. "
                       "fps_limit/refresh_hz apply to the current game's profile (or global profile).",
        "schema": {
            "fps_limit": {"type": "integer", "description": "FPS cap; 0 disables the cap"},
            "refresh_hz": {"type": "integer", "description": "Display refresh rate (40-60 LCD, 45-90 OLED)"},
            "tdp_watts": {"type": "integer", "description": "TDP limit in watts; 0 disables the limit"},
            "per_game_profile": {"type": "boolean", "description": "Use a game-specific profile for the current game"},
            "perf_overlay_level": {"type": "integer", "description": "Performance overlay level 0-4"},
        },
        "js": r"""
const p = perfStore();
const done = [];
const needsPerf = ["fps_limit", "refresh_hz", "per_game_profile", "perf_overlay_level"].some(k => ARGS[k] != null);
if (needsPerf && (!SteamClient.System.Perf || !p)) throw new Error("Steam performance API unavailable (only works in Game Mode)");
if (ARGS.per_game_profile != null) { p.SetGameSpecificProfileEnabled(ARGS.per_game_profile); done.push("per_game_profile"); await sleep(300); }
if (ARGS.fps_limit != null) {
  if (ARGS.fps_limit > 0) { p.SetFPSLimit(false, ARGS.fps_limit); p.SetFPSLimitEnabled(true); }
  else p.SetFPSLimitEnabled(false);
  done.push("fps_limit");
}
if (ARGS.refresh_hz != null) { p.SetDisplayRefreshRateManualHz(false, ARGS.refresh_hz); done.push("refresh_hz"); }
if (ARGS.perf_overlay_level != null) { p.SetPerfOverlayLevel(ARGS.perf_overlay_level); done.push("perf_overlay_level"); }
if (ARGS.tdp_watts != null) {
  if (ARGS.tdp_watts > 0) { await setSetting("steamos_tdp_limit_enabled", true); await setSetting("steamos_tdp_limit", ARGS.tdp_watts); }
  else await setSetting("steamos_tdp_limit_enabled", false);
  done.push("tdp");
}
await sleep(500);
const s = settingsStore.settings || {};
return { applied: done, per_app: p?.msgSettingsPerApp, tdp_limit_enabled: s.steamos_tdp_limit_enabled, tdp_limit_watts: s.steamos_tdp_limit };
""",
    },
}


async def cdp_eval(body, args):
    expr = (
        "(async () => {" + PRELUDE + "\nconst ARGS = " + json.dumps(args) + ";\n"
        + "const __v = await (async () => {\n" + body + "\n})();\n"
        + "return JSON.stringify(__v === undefined ? null : __v, (k, x) => typeof x === 'bigint' ? String(x) : x);\n})()"
    )
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        try:
            tabs = await (await s.get(CDP_LIST)).json()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"Steam's debugger at :8080 is not reachable ({e}). Is Steam running?")
        url = next((t["webSocketDebuggerUrl"] for t in tabs if t.get("title") == "SharedJSContext"), None)
        if not url:
            raise RuntimeError("Steam's SharedJSContext was not found")
        async with s.ws_connect(url, max_msg_size=0) as ws:
            await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
                "expression": expr, "awaitPromise": True, "returnByValue": True}})
            async for msg in ws:
                d = json.loads(msg.data)
                if d.get("id") != 1:
                    continue
                if "error" in d:
                    raise RuntimeError(d["error"].get("message"))
                r = d["result"]
                if "exceptionDetails" in r:
                    ex = r["exceptionDetails"]
                    raise RuntimeError(ex.get("exception", {}).get("description") or ex.get("text"))
                return r["result"].get("value")
    raise RuntimeError("No response from Steam")


def tool_list():
    out = []
    for name, t in TOOLS.items():
        out.append({"name": name, "description": t["description"], "inputSchema": {
            "type": "object", "properties": t["schema"], "required": t.get("required", [])}})
    return out


async def sign_in(args):
    req = {k: args[k] for k in ("service", "url", "code", "instructions", "ask_for", "input_label") if args.get(k)}
    sid = await cdp_eval("if (!window.__claudeDeck) throw new Error('The Claude plugin UI is not loaded'); "
                         "return window.__claudeDeck.signIn(ARGS);", req)
    sid = json.loads(sid)
    deadline = time.monotonic() + SIGN_IN_TIMEOUT
    result = None
    while time.monotonic() < deadline:
        r = await cdp_eval("return window.__claudeDeck ? await window.__claudeDeck.wait(ARGS.id, 25000) "
                           ": {status: 'cancelled', value: 'Plugin unloaded'};", {"id": sid})
        result = json.loads(r or "null")
        if result:
            break
    if not result:
        await cdp_eval("window.__claudeDeck?.cancel(ARGS.id); return null;", {"id": sid})
        return {"status": "timeout", "note": "The user did not respond within 15 minutes; the card was removed."}
    if result.get("status") == "submitted" and req.get("ask_for") == "secret":
        os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(SECRETS_DIR, sid)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(result.pop("value", ""))
        result["secret_file"] = path
        result["note"] = "Value saved without showing it to you. Use it from the file, then delete the file."
    return result


async def take_screenshot():
    try:
        await cdp_eval("window.DFL?.Navigation?.CloseSideMenus(); return null;", {})
        await asyncio.sleep(0.6)
    except Exception:
        pass  # capture anyway
    return await deck_tools.screenshot()


def _slug(name):
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")[:60] or "game"


def backup_saves(game, paths):
    import tarfile
    paths = [os.path.expanduser(p) for p in paths]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise RuntimeError(f"Not found: {', '.join(missing)}")
    d = os.path.join(BACKUPS_DIR, _slug(game))
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + ".tar.gz")
    with tarfile.open(out, "w:gz") as t:
        for p in paths:
            t.add(p, arcname=p.lstrip("/"))
    for old in sorted(f for f in os.listdir(d) if f.endswith(".tar.gz"))[:-KEEP_BACKUPS]:
        os.remove(os.path.join(d, old))
    return {"file": out, "size_mb": round(os.path.getsize(out) / 1e6, 1), "paths": paths}


def list_backups(game=None):
    out = []
    if not os.path.isdir(BACKUPS_DIR):
        return out
    for g in sorted(os.listdir(BACKUPS_DIR)):
        if game and g != _slug(game):
            continue
        for f in sorted(os.listdir(os.path.join(BACKUPS_DIR, g)), reverse=True):
            p = os.path.join(BACKUPS_DIR, g, f)
            out.append({"game": g, "file": p, "size_mb": round(os.path.getsize(p) / 1e6, 1)})
    return out


def restore_backup(file):
    import tarfile
    file = os.path.realpath(os.path.expanduser(file))
    if not file.startswith(os.path.realpath(BACKUPS_DIR) + os.sep):
        raise RuntimeError("Only backups made by backup_saves can be restored")
    with tarfile.open(file) as t:
        members = t.getmembers()
        existing = [p for p in {"/" + m.name for m in members if not m.isdir()} if os.path.exists(p)]
        safety = None
        if existing:
            safety = backup_saves(os.path.basename(os.path.dirname(file)) + "-before-restore", existing)
        for m in members:
            if m.name.startswith(("/", "..")) or "/../" in m.name:
                raise RuntimeError("Unsafe path in backup")
        t.extractall("/", members=members, filter="tar")
    return {"restored": file, "files": len(members), "previous_saved_to": safety and safety["file"]}


def call_tool(name, args):
    t = TOOLS.get(name)
    if not t:
        return f"Unknown tool {name}", True
    if name in ("backup_saves", "list_backups", "restore_backup"):
        try:
            fn = {"backup_saves": lambda: backup_saves(args["game"], args["paths"]),
                  "list_backups": lambda: list_backups(args.get("game")),
                  "restore_backup": lambda: restore_backup(args["file"])}[name]
            return json.dumps(fn()), False
        except Exception as e:
            return f"{type(e).__name__}: {e}", True
    if name == "screenshot":
        try:
            return json.dumps({"path": asyncio.run(take_screenshot())}), False
        except Exception as e:
            return f"{type(e).__name__}: {e}", True
    if name == "sign_in":
        try:
            return json.dumps(asyncio.run(sign_in(args))), False
        except Exception as e:
            return f"{type(e).__name__}: {e}", True
    body = args.get("code", "") if name == "steam_js" else t["js"]
    try:
        text = asyncio.run(cdp_eval(body, args))
        return (text or "null")[:MAX_OUTPUT], False
    except Exception as e:
        return f"{type(e).__name__}: {e}", True


def handle(msg):
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "steam", "version": "0.1.0"}}
    if method == "tools/list":
        return {"tools": tool_list()}
    if method == "tools/call":
        text, is_error = call_tool(params.get("name"), params.get("arguments") or {})
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    if method == "ping":
        return {}
    raise KeyError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue  # notification
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except KeyError as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"Method not found: {e}"}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
