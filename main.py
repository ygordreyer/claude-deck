import asyncio
import base64
import json
import math
import os
import re
import sys
import time
import uuid
import urllib.request
from datetime import datetime

import decky

sys.path.insert(0, decky.DECKY_PLUGIN_DIR)
import deck_tools  # noqa: E402

HOME = decky.DECKY_USER_HOME
DEVICE_NAME = deck_tools.device_name()
CLAUDE = os.path.join(HOME, ".local/bin/claude")
STATE_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "state.json")
CHATS_DIR = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "chats")  # history of past chats, one file each
MCP_CONFIG = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "mcp.json")
STEAM_MCP = os.path.join(decky.DECKY_PLUGIN_DIR, "steam_mcp.py")
MAX_HISTORY = 400

# Permission presets shown in the UI, mapped to Claude Code permission modes.
MODES = {"ask": "default", "edits": "acceptEdits", "plan": "plan", "auto": "auto", "trust": "bypassPermissions"}
MODELS = ("default", "opus", "fable", "sonnet", "haiku")
EFFORTS = ("default", "low", "medium", "high", "xhigh", "max")
THEMES = ("claude", "midnight", "tokyo", "minecraft", "steamos")
VOICES = ("off", "voice", "always")
# Terminal controls, app-owned session actions and internal workflow commands.
HIDDEN_COMMANDS = frozenset(("login", "logout", "exit", "clear", "config", "terminal-setup", "vim", "ide",
                             "color", "heapdump", "__remote-workflow", "workflow-launch-exec", "plugin-types"))
# Hold-to-talk and dictation chords: presets like "l4r4", "off", or recorded "m:<low>:<high>" masks.
GAMES_DIR = os.path.join(HOME, ".local/share/claude-deck/games")  # per-game notes Claude keeps
REPO = "RyanWReid/claude-deck"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
VOICE_FILES = {  # what setup_voice downloads into ~/.local/share/claude-deck
    "piper": "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz",
    "voice": "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
    "whisper_model": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
}
TAP_SECONDS = 0.3  # a press shorter than this isn't a question  # spoken replies: never, when you talked, every reply
VOICE_HINT = ("voice. Your reply will be read aloud, so answer in one to three short spoken "
              "sentences with no lists, headings or code unless the user asks for detail.")

# Runs without a card. Everything else that changes something asks first (in "ask" mode).
SAFE_TOOLS = [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite", "Task", "Agent",
    "mcp__steam__list_games", "mcp__steam__game_info", "mcp__steam__running_games",
    "mcp__steam__get_performance", "mcp__steam__set_performance", "mcp__steam__list_compat_tools",
    "mcp__steam__set_compat_tool", "mcp__steam__set_launch_options", "mcp__steam__launch_game",
    "mcp__steam__screenshot", "mcp__steam__sign_in", "mcp__steam__backup_saves", "mcp__steam__list_backups",
    "Bash(flatpak list*)", "Bash(flatpak info*)", "Bash(flatpak search*)", "Bash(df *)", "Bash(free*)",
    "Bash(ps *)", "Bash(uptime*)", "Bash(ip addr*)", "Bash(nmcli*)", "Bash(cat /sys/*)", "Bash(sensors*)",
    "Edit(~/.local/share/claude-deck/games/**)", "Write(~/.local/share/claude-deck/games/**)",
    "Bash(systemctl --user list-*)", "Bash(systemctl --user status *)", "Bash(journalctl --user *)",
]
DENY_TOOLS = ["Read(~/.ssh/**)", "Read(~/.claude/.credentials.json)", "Read(~/.local/state/claude-deck/secrets/**)"]

# Hard policy, enforced by a PreToolUse hook callback so no mode can bypass it.
DENY_BASH = [
    (re.compile(r"(^|[\s;&|(`])(sudo|pkexec|doas|su)\s"), "root access is off-limits from the plugin"),
    (re.compile(r"steamos-readonly\s+disable"), "unlocking the read-only OS partition"),
    (re.compile(r"(^|[\s;&|])passwd\b"), "changing passwords"),
    (re.compile(r"\b(mkfs|wipefs|fdisk|parted)\b|\bdd\b[^\n]*\bof=/dev/"), "disk formatting/partitioning"),
    (re.compile(r"rm\s+(-\w*\s+)*(/|~|\$HOME|/home/deck)/?(\s|$)"), "deleting the home or root directory"),
]
ASK_BASH = re.compile(r"(^|[\s;&|(])(rm|rmdir|shred|systemctl|reboot|poweroff|shutdown|kill|pkill|killall)\b"
                      r"|flatpak\s+(uninstall|remove)|steamos-update|>\s*~?/?\S*\.(vdf|acf)\b")

SYSTEM_PROMPT = f"""You are running inside the "Claude" Decky Loader plugin on the user's {DEVICE_NAME}, \
usually in Game Mode. It is a chat app: the user talks to you like a chatbot, in a chat window or \
the narrow Quick Access Menu, typing with a controller and on-screen keyboard. So:
- Be conversational and friendly. Happily chat about anything (games, questions, ideas), not \
only device tasks. Ask a short follow-up question when it helps.
- Keep replies short and plain: short paragraphs, simple bullet lists and **bold** are fine; \
no tables, no wide code blocks.
- When you need the user to choose, use the AskUserQuestion tool with 2-4 short options: \
picking with the d-pad is much easier than typing.
- Act directly instead of asking the user to run commands; there is no terminal in Game Mode.
- Actions that change things may show the user an Approve/Deny card. If one is denied, accept it \
and offer an alternative; don't retry the same thing. Some actions (sudo, unlocking the OS \
partition, formatting disks) are blocked outright by Deck policy.
- Use the `steam` MCP tools to manage games: list/launch/stop games, launch options, Proton \
versions, performance (FPS limit, TDP, refresh rate). `steam_js` runs arbitrary JavaScript in \
Steam's UI context (SteamClient, appStore, collectionStore, settingsStore...) for anything else.
- Performance tools only work while Steam is in Game Mode.
- Each user message may start with a <deck_context> block (current game, battery, performance \
settings) gathered automatically; the user didn't type it, so use it silently. "This game" means \
the running game in it. A message may include a screenshot of the screen; `steam.screenshot` \
takes a fresh one. If the user asks about what's on screen ("what's happening", "where do I \
go", "what does this say") and no screenshot is attached, take one with `steam.screenshot` and \
Read it before answering. For game help, give the next concrete step first, keep it spoiler-light \
unless asked, and say which part of the screen you mean. Messages may come from voice input, so \
forgive odd words.
- When anything needs the user to sign in or give a password/token, use `steam.sign_in` so it \
happens inside the chat; never send them to Desktop Mode or a terminal. Prefer device-code or \
paste-back flows: start the login command in the background first, read its URL/code, call \
sign_in, then wait for the command to finish. Use ask_for='secret' for passwords and tokens.
- You run as user `deck` without root. Install software as Flatpaks (`flatpak install --user`) \
or inside the `dev` distrobox, never with pacman on the host.
- Never modify the files, prefixes or launch options of games with anti-cheat (most online \
multiplayer games); explain the risk and stop.
- Before anything that could damage a game's saves (changing its Proton version, installing \
mods, deleting or moving its files, wiping its prefix), back up its saves with \
`steam.backup_saves` and say so in one short line. `steam.restore_backup` puts them back.
- Game notes: <deck_context> may name a notes file for the running game. Read it when helping \
with that game, and add short durable facts worth remembering next time (where the user is \
stuck or headed, mods and settings that worked, preferences). Keep it under ~40 lines.
- Work that should happen later or keep going after this chat (overnight save backups, waiting \
for a download, a reminder) goes in a transient systemd user unit named `claude-<short-name>`: \
`systemd-run --user --unit=claude-x --on-calendar=... ` or `--on-active=...`, wrap long work in \
`systemd-inhibit --what=sleep --why="Claude: ..."` so the device stays awake only while it runs, \
and finish with `notify-send "Claude" "<result>"` so the user sees it. The plugin lists these \
under Jobs. Timers only fire while the device is awake."""


# Claude Code's ways of saying the login is gone (expired OAuth, logged out, bad key).
_AUTH_RE = re.compile(r"Failed to authenticate|OAuth (session|token)[^\n]*(expired|revoked|invalid)|"
                      r"authentication_failed|Not logged in|Please run /login|Invalid API key|invalid x-api-key", re.I)
_ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
SIGNED_OUT_MSG = "You're signed out of Claude. Tap Sign in at the top, then send your message again."


def _clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


STEP_LABELS = {
    "mcp__steam__list_games": "Looked through your library", "mcp__steam__game_info": "Checked game details",
    "mcp__steam__running_games": "Checked what's running", "mcp__steam__get_performance": "Read performance settings",
    "mcp__steam__set_performance": "Changed performance settings", "mcp__steam__list_compat_tools": "Listed Proton versions",
    "mcp__steam__set_compat_tool": "Changed the Proton version", "mcp__steam__set_launch_options": "Set launch options",
    "mcp__steam__launch_game": "Launched a game", "mcp__steam__stop_game": "Stopped a game",
    "mcp__steam__screenshot": "Looked at your screen", "mcp__steam__sign_in": "Asked you to sign in",
    "mcp__steam__steam_js": "Worked inside Steam", "WebSearch": "Searched the web", "WebFetch": "Read a web page",
    "Read": "Read a file", "Write": "Wrote a file", "Edit": "Edited a file", "Glob": "Looked for files",
    "Grep": "Searched files", "TodoWrite": "Updated its to-do list", "Task": "Started a helper", "Agent": "Started a helper",
}


def _step_label(name, inp):
    """Plain-English one-liner for a tool step; the raw command goes in the details."""
    inp = inp if isinstance(inp, dict) else {}
    desc = (inp.get("description") or "").strip()
    if desc and name not in ("Task", "Agent"):
        return _clip(desc[0].upper() + desc[1:], 90)
    if name in ("Read", "Write", "Edit") and inp.get("file_path"):
        return f"{STEP_LABELS[name]}: {os.path.basename(inp['file_path'])}"
    if name == "WebSearch" and inp.get("query"):
        return _clip(f"Searched the web for “{inp['query']}”", 90)
    if name == "mcp__steam__launch_game" or name == "mcp__steam__game_info":
        return STEP_LABELS[name]
    return STEP_LABELS.get(name) or ("Ran a command" if name == "Bash" else _pretty_tool(name))


def _tool_summary(name, inp):
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "pattern", "url", "query", "description", "code"):
        if key in inp:
            return _clip(inp[key], 160)
    return _clip(json.dumps(inp), 160) if inp else ""


def _pretty_tool(name):
    return name.replace("mcp__steam__", "steam.")


def _env():
    env = dict(os.environ)
    # Decky's bundled Python leaks its own library paths; they break normal binaries.
    for k in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)
    uid = os.getuid()
    env.update({
        "HOME": HOME,
        "USER": "deck",
        "PATH": f"{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
        "DISPLAY": env.get("DISPLAY", ":0"),
        "TERM": "dumb",
        # Lets user hooks tell plugin turns apart (e.g. skip desktop notifications; we toast ourselves).
        "CLAUDE_DECK_PLUGIN": "1",
        # sign_in blocks until the user finishes logging in (up to 15 min).
        "MCP_TOOL_TIMEOUT": "1000000",
    })
    return env


def _ssl():
    """Decky's bundled Python has no CA bundle; use the system's."""
    import ssl
    for ca in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ca-certificates/extracted/tls-ca-bundle.pem"):
        if os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


def _current_usage(usage, now=None):
    if not isinstance(usage, dict):
        return None
    now = time.time() if now is None else now
    windows = usage.get("unifiedWindows") or {}
    current = {k: w for k, w in windows.items() if isinstance(w, dict)
               and isinstance(w.get("resetsAt"), (int, float))
               and math.isfinite(w["resetsAt"]) and w["resetsAt"] > now}
    result = {k: v for k, v in usage.items() if k != "unifiedWindows"}
    if current:
        result["unifiedWindows"] = current
    if current != windows or not current:
        result.pop("status", None)
    return result


def _merge_usage(usage, data):
    result = dict(usage or {})
    windows = dict(result.get("unifiedWindows") or {})
    for key in ("five_hour", "seven_day"):
        if key not in data:
            continue
        bucket = data[key]
        if bucket is None:
            windows.pop(key, None)
            continue
        if not isinstance(bucket, dict):
            continue
        percent, reset = bucket.get("utilization"), bucket.get("resets_at")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent):
            continue
        try:
            reset = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if reset.tzinfo is None:
                continue
            windows[key] = {"utilization": max(0, min(100, percent)) / 100, "resetsAt": reset.timestamp()}
        except (AttributeError, ValueError, OverflowError, OSError):
            continue
        result.pop("status", None)
    result["unifiedWindows"] = windows
    return _current_usage(result)


class _NoUsageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Keep the bearer header on the usage endpoint.


def _fetch_usage():
    try:
        with open(os.path.join(HOME, ".claude/.credentials.json")) as f:
            oauth = json.load(f).get("claudeAiOauth") or {}
        token, expires = oauth.get("accessToken"), oauth.get("expiresAt")
        if (not isinstance(token, str) or not token or isinstance(expires, bool)
                or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= time.time() * 1000):
            return None
        req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1"})
        opener = urllib.request.build_opener(_NoUsageRedirect, urllib.request.HTTPSHandler(context=_ssl()))
        with opener.open(req, timeout=10) as response:
            data = json.load(response)
        return data if isinstance(data, dict) else None
    except urllib.error.HTTPError as error:
        error.close()
        return None
    except Exception:
        return None  # Request errors can contain credentials; keep them out of logs and events.


def deck_policy(inp):
    """PreToolUse hook: hard denies and forced approval cards. {} means no opinion."""
    tool = inp.get("tool_name", "")
    ti = inp.get("tool_input") or {}

    def out(decision, reason):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": decision, "permissionDecisionReason": reason}}

    if tool == "Bash":
        cmd = ti.get("command", "")
        if "distrobox enter" not in cmd:
            for rx, why in DENY_BASH:
                if rx.search(cmd):
                    return out("deny", f"Blocked by Deck policy: {why}.")
        if ASK_BASH.search(cmd) and inp.get("permission_mode") != "bypassPermissions":
            return out("ask", "Deletes, stops or changes system state.")
    elif tool == "mcp__steam__steam_js" and inp.get("permission_mode") != "bypassPermissions":
        if re.search(r"Uninstall|Delete|Remove|SetSetting|Terminate|Shutdown|Restart|Factory", ti.get("code", "")):
            return out("ask", "Changes Steam settings or removes something.")
    return {}


async def _image_block(path):
    """Screenshot as an inline image block, re-encoded to JPEG to keep it small."""
    data, media = None, "image/png"
    jpg = path.rsplit(".", 1)[0] + ".jpg"
    try:
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", path, "-vf", "scale='min(1280,iw)':-2", "-q:v", "4", jpg,
            env=deck_tools.clean_env(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await asyncio.wait_for(p.wait(), 20) == 0 and os.path.exists(jpg):
            path, media = jpg, "image/jpeg"
    except Exception:
        pass
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def _commands_from_init(ev):
    commands = {}
    terminal = ev.get("terminal_slash_commands")
    hidden = HIDDEN_COMMANDS.union(n.lstrip("/") for n in terminal if isinstance(n, str)) \
        if isinstance(terminal, list) else HIDDEN_COMMANDS
    for field in ("slash_commands", "skills"):
        items = ev.get(field)
        for item in items if isinstance(items, list) else []:
            name = item.get("name", "") if isinstance(item, dict) else item
            if not isinstance(name, str):
                continue
            name = name.lstrip("/")
            if not name or re.search(r"\s", name) or name in hidden:
                continue
            description = item.get("description", "") if isinstance(item, dict) else ""
            if name not in commands or isinstance(description, str) and description:
                commands[name] = {"name": name, "description": description if isinstance(description, str) else ""}
    return [commands[name] for name in sorted(commands)]


class Plugin:
    async def _main(self):
        self.proc = None
        self.reader = None
        self.ready = None  # future resolved once `initialize` answers
        self.ctrl = {}  # control request id -> future
        self.asks = {}  # pending approval cards: request id -> card
        # Busy tracking. The CLI may fold several queued messages into one turn (one result, one
        # echo), so track what it has taken (echoes) and the turn itself, not result counts.
        self.inflight = 0  # user messages written and not yet taken into a turn
        self.turn_active = False
        self.turn_spoken = False
        self.stopping = False
        self.running = False
        self.session_id = None
        self.history = []
        self.usage = None
        self.usage_checked_at = None
        self.usage_lock = asyncio.Lock()
        self.usage_expiry = None
        self.info = {}
        self.commands = []
        self.mode = "ask"
        self.model = "default"
        self.effort = "default"
        self.theme = "claude"
        self.voice = "voice"
        self.spoken = []  # per queued user message: was it spoken?
        self.turn_text = []  # Claude's text so far in the current turn
        self.tts_buf = ""  # streamed reply text not yet spoken
        self.tts_on = False  # speaking this turn as it streams
        self.tts_code = False  # inside a code block (not spoken)
        self.speaker = deck_tools.Speaker()
        self.ptt = "l4r4"
        self.dictate = "l5r5"
        self.chats = {}  # session id -> {title, appid, game, updated}
        self.chat_meta = {}  # meta for the current chat until it has a session id
        self.game_threads = True  # questions asked in a game go to that game's own chat
        self.login_proc = None
        self.auth_lost = False  # this turn failed because the login is gone
        self.ptt_role = None
        self.caption_up = False  # the over-game caption is showing (frontend tells us)
        self.ptt_t0 = 0
        self.ptt_shot = None
        self.remote_url = None
        self.recorder = deck_tools.Recorder()
        # TEMP speech bench: transient state, retained when the panel is reopened.
        self.bench_task = None
        self.bench_state = {"phase": "idle", "results": []}
        self.bench_mic_starting = False
        self._load()
        self.chords = deck_tools.ChordWatcher(self._ptt_down, self._ptt_up, decky.logger.info)
        self._bind_chords()
        self.chords.start()
        with open(MCP_CONFIG, "w") as f:
            json.dump({"mcpServers": {"steam": {
                "command": "/usr/bin/python3", "args": [STEAM_MCP]}}}, f)
        decky.logger.info("Claude plugin loaded (session %s)", self.session_id)
        await self._set_usage(self.usage)
        await self._refresh_usage()

    async def _unload(self):
        # TEMP speech bench: cancellation also kills the current model subprocess.
        if self.bench_task and not self.bench_task.done():
            self.bench_task.cancel()
            try:
                await self.bench_task
            except asyncio.CancelledError:
                pass
        if self.usage_expiry:
            self.usage_expiry.cancel()
        await self.chords.stop()
        await self.recorder.cancel()
        await self.speaker.stop()
        await self._kill()

    # --- persistence ---------------------------------------------------------
    def _load(self):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            self.session_id = data.get("session_id")
            self.history = data.get("history", [])
            self.usage = data.get("usage")
            self.mode = data.get("mode", "ask") if data.get("mode") in MODES else "ask"
            self.model = data.get("model", "default") if data.get("model") in MODELS else "default"
            self.effort = data.get("effort") if data.get("effort") in EFFORTS else "default"
            self.theme = data.get("theme") if data.get("theme") in THEMES else "claude"
            self.voice = data.get("voice") if data.get("voice") in VOICES else "voice"
            self.ptt = data.get("ptt") if deck_tools.valid_chord(data.get("ptt")) else "l4r4"
            self.dictate = data.get("dictate") if deck_tools.valid_chord(data.get("dictate")) else "l5r5"
            self.chats = data.get("chats", {})
            self.chat_meta = data.get("chat_meta", {})
            self.game_threads = data.get("game_threads", True)
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"session_id": self.session_id, "history": self.history, "usage": self.usage,
                       "mode": self.mode, "model": self.model, "effort": self.effort, "theme": self.theme,
                       "voice": self.voice, "ptt": self.ptt,
                       "dictate": self.dictate, "chats": self.chats, "chat_meta": self.chat_meta,
                       "game_threads": self.game_threads}, f)
        os.replace(tmp, STATE_FILE)

    async def _push(self, kind, text, **extra):
        entry = {"kind": kind, "text": text, "ts": time.time(), **extra}
        self.history.append(entry)
        del self.history[:-MAX_HISTORY]
        self._save()
        await decky.emit("claude_event", entry)

    async def _set_running(self, value):
        if value != self.running:
            self.running = value
            await decky.emit("claude_status", value)

    # --- frontend API ----------------------------------------------------------
    async def get_state(self):
        await self._refresh_usage()
        await self._prune_usage()
        return {"history": self.history, "running": self.running, "asks": list(self.asks.values()),
                "usage": self.usage, "mode": self.mode, "model": self.model, "effort": self.effort, "theme": self.theme,
                "voice": self.voice, "can_speak": self.speaker.available(), "speaking": self.speaker.active,
                "ptt": self.ptt, "dictate": self.dictate, "game_threads": self.game_threads,
                "chat_title": self.chat_meta.get("title"), "chat_game": self.chat_meta.get("game"),
                "ptt_label": deck_tools.chord_label(self.ptt), "dictate_label": deck_tools.chord_label(self.dictate),
                "remote_url": self.remote_url, "account": self.info.get("account"), "device_name": DEVICE_NAME,
                "commands": self.commands}

    async def _prune_usage(self):
        usage = _current_usage(self.usage)
        if usage != self.usage:
            await self._set_usage(usage)

    async def _set_usage(self, usage):
        self.usage = _current_usage(usage)
        if self.usage_expiry and self.usage_expiry is not asyncio.current_task():
            self.usage_expiry.cancel()
        windows = (self.usage or {}).get("unifiedWindows", {})
        reset = min((w["resetsAt"] for w in windows.values()), default=None)
        self.usage_expiry = asyncio.create_task(self._expire_usage(reset)) if reset is not None else None
        self._save()
        await decky.emit("claude_usage", self.usage)

    async def _expire_usage(self, reset):
        while (wait := reset - time.time()) > 0:
            await asyncio.sleep(wait)
        await self._set_usage(self.usage)

    async def _refresh_usage(self):
        async with self.usage_lock:
            await self._prune_usage()
            now = time.monotonic()
            if self.usage_checked_at is not None and now - self.usage_checked_at < 60:
                return
            self.usage_checked_at = now
            previous = self.usage
            data = await asyncio.to_thread(_fetch_usage)
            if data is not None and self.usage is previous:
                await self._set_usage(_merge_usage(self.usage, data))

    async def send(self, prompt, context=None, screenshot=None, spoken=False):
        """Post a message. If Claude is busy it is answered next, like a chat.

        `context` is Deck status gathered by the frontend; `screenshot` a path from snap().
        Both reach Claude but only the typed text shows in the chat."""
        prompt = (prompt or "").strip()
        if not prompt and not screenshot:
            return False
        await self._game_thread(context)
        shown = prompt or "What's on my screen?"
        self._note_chat(shown, context)
        await self._push("user", shown, **({"image": True} if screenshot else {}))
        content = []
        if screenshot:
            try:
                content.append(await _image_block(screenshot))
            except OSError as e:
                await self._push("tool_error", f"Couldn't attach the screenshot: {e}")
        if spoken and self.voice != "off":
            context = dict(context or {}, Input=VOICE_HINT)
        content.append({"type": "text", "text": self._with_context(shown, context)})
        try:
            await self._ensure()
            self._write({"type": "user", "message": {"role": "user", "content": content},
                         "parent_tool_use_id": None, "session_id": ""})
            self.inflight += 1
            self.spoken.append(bool(spoken))
            await self._set_running(True)
        except Exception as e:
            decky.logger.exception("send failed")
            await self._push("error", f"Couldn't reach Claude: {_clip(e, 400)}")
        return True

    def _with_context(self, prompt, context):
        if prompt.startswith("/"):
            return prompt  # The CLI needs the command first and its arguments unchanged.
        context = dict(context or {})
        m = re.search(r"appid (\d+)", str(context.get("Running game", "")))
        if m:
            notes = os.path.join(GAMES_DIR, f"{m.group(1)}.md")
            os.makedirs(GAMES_DIR, exist_ok=True)
            context["Game notes"] = notes + ("" if os.path.exists(notes) else " (none yet)")
        lines = [f"{k}: {v}" for k, v in context.items() if v not in (None, "")]
        if bat := deck_tools.battery():
            lines.append(f"Battery: {bat}")
        head = "<deck_context>\n" + "\n".join(lines) + "\n</deck_context>\n\n" if lines else ""
        return head + prompt

    async def answer(self, rid, decision, answers=None):
        """Resolve an approval card. decision: allow | always | deny.

        For a plan card, "allow" leaves plan mode for Ask first and "always" for Accept edits."""
        card = self.asks.pop(rid, None)
        if not card:
            return False
        req = card["_req"]
        if decision in ("allow", "always"):
            inp = dict(req.get("input") or {})
            if answers is not None:
                inp["answers"] = answers
            resp = {"behavior": "allow", "updatedInput": inp}
            if decision == "always":
                rules = [s for s in req.get("permission_suggestions") or [] if s.get("type") == "addRules"]
                if rules:
                    resp["updatedPermissions"] = [dict(rules[0], destination="session")]
        else:
            resp = {"behavior": "deny", "message": "The user tapped Deny on the device.", "interrupt": False}
        self._respond(rid, resp)
        if card["kind"] == "plan" and decision != "deny":
            self.mode = "edits" if decision == "always" else "ask"
            self._save()
            await decky.emit("claude_mode", self.mode)
            asyncio.create_task(self._apply_mode())
        if card["kind"] == "tool":
            label = {"allow": "Approved", "always": "Approved for this chat", "deny": "Denied"}[decision]
            await self._push("perm", f"{label}: {card['title']}", ok=decision != "deny")
        await decky.emit("claude_ask_done", rid)
        return True

    async def set_mode(self, mode):
        if mode not in MODES:
            return False
        self.mode = mode
        self._save()
        await self._apply_mode()
        return True

    async def _apply_mode(self):
        if self._alive():
            try:
                await self._control({"subtype": "set_permission_mode", "mode": MODES[self.mode]})
            except Exception as e:
                decky.logger.warning("set_permission_mode failed: %s", e)

    async def set_model(self, model):
        if model not in MODELS:
            return False
        if self._alive():  # otherwise it applies when Claude next starts (--model)
            try:
                await self._control({"subtype": "set_model", "model": model})
            except Exception as e:
                decky.logger.warning("set_model failed: %s", e)
                await decky.emit("claude_model", self.model)
                return False
        self.model = model
        self._save()
        return True

    async def set_effort(self, effort):
        if effort not in EFFORTS:
            return False
        if self._alive():
            try:
                await self._control({"subtype": "apply_flag_settings",
                                     "settings": {"effortLevel": None if effort == "default" else effort}})
            except Exception as e:
                decky.logger.warning("set_effort failed: %s", e)
                await decky.emit("claude_effort", self.effort)
                return False
        self.effort = effort
        self._save()
        return True

    async def set_theme(self, theme):
        if theme not in THEMES:
            return False
        self.theme = theme
        self._save()
        return True

    async def set_voice(self, voice):
        if voice not in VOICES:
            return False
        self.voice = voice
        self._save()
        if voice == "off":
            await self.stop_speaking()
        return True

    def _bind_chords(self):
        b = {"ask": self.ptt}
        if self.dictate != self.ptt:
            b["dictate"] = self.dictate
        self.chords.bindings = {k: c for k, c in b.items() if c != "off"}

    async def set_ptt(self, chord):
        if not deck_tools.valid_chord(chord):
            return False
        self.ptt = chord
        self._bind_chords()
        self._save()
        return True

    async def set_dictate(self, chord):
        if not deck_tools.valid_chord(chord):
            return False
        self.dictate = chord
        self._bind_chords()
        self._save()
        return True

    async def capture_chord(self, role):
        """Wait for the user to hold a new button combo, bind it to `role` ("ask"/"dictate")."""
        if role not in ("ask", "dictate"):
            return {"error": "unknown role"}
        spec = await self.chords.capture()
        if not spec:
            return {"error": "No buttons held. Try again and hold them for a second."}
        other = self.dictate if role == "ask" else self.ptt
        if deck_tools.chord_masks(spec) == deck_tools.chord_masks(other):
            return {"error": "That combo is already used for the other shortcut."}
        if role == "ask":
            self.ptt = spec
        else:
            self.dictate = spec
        self._bind_chords()
        self._save()
        return {"spec": spec, "label": deck_tools.chord_label(spec)}

    async def chord_label(self, spec):
        return deck_tools.chord_label(spec)

    async def caption_state(self, visible):
        self.caption_up = bool(visible)
        return True

    # --- first-run setup and updates -------------------------------------------------
    async def _cmd(self, *args, timeout=60, stdin=None):
        p = await asyncio.create_subprocess_exec(
            *args, env=_env(), stdin=asyncio.subprocess.PIPE if stdin else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(stdin.encode() if stdin else None), timeout)
        return p.returncode, out.decode(errors="replace")

    async def setup_status(self):
        """What's ready: Claude Code, sign-in, voice, controller shortcuts."""
        st = {"claude": os.path.exists(CLAUDE), "version": None, "signed_in": False, "plan": None,
              "voice": await self.voice_available(), "speech": self.speaker.available(),
              "controller": deck_tools.find_controller() is not None,
              "plugin_version": self._plugin_version()}
        if st["claude"]:
            try:
                _, out = await self._cmd(CLAUDE, "--version", timeout=20)
                st["version"] = out.split()[0] if out.strip() else None
                code, out = await self._cmd(CLAUDE, "auth", "status", "--json", timeout=20)
                info = json.loads(out[out.find("{"):]) if "{" in out else {}
                st["signed_in"] = bool(info.get("loggedIn"))
                st["plan"] = info.get("subscriptionType") or info.get("authMethod")
                if st["signed_in"]:
                    self.auth_lost = False
            except Exception:
                decky.logger.exception("setup_status")
        return st

    def _plugin_version(self):
        try:
            with open(os.path.join(decky.DECKY_PLUGIN_DIR, "package.json")) as f:
                return json.load(f).get("version")
        except (OSError, ValueError):
            return None

    async def install_claude(self):
        """Install Claude Code for the deck user (official installer, into ~/.local)."""
        try:
            code, out = await self._cmd("bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash", timeout=600)
            return {"ok": code == 0 and os.path.exists(CLAUDE), "log": out[-600:]}
        except Exception as e:
            return {"ok": False, "log": str(e)}

    async def login_start(self):
        """Begin Claude sign-in; returns the URL to open (the code comes back via login_finish).

        `claude auth login --claudeai` prints "Opening browser to sign in…", then
        "If the browser didn't open, visit: <url>", then "Paste code here if prompted >" and reads
        the code from stdin. Return as soon as a whole URL has arrived (whitespace after it, or
        nothing more for a moment), without waiting for the prompt line."""
        await self._login_cancel()
        env = _env() | {"BROWSER": "true"}  # no browser on the Deck: just print the link
        try:
            self.login_proc = await asyncio.create_subprocess_exec(
                CLAUDE, "auth", "login", "--claudeai", env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as e:
            return {"error": f"Couldn't run Claude Code: {e}", "log": ""}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        buf, url, ended = "", None, False
        while (wait := deadline - loop.time()) > 0:
            try:
                chunk = await asyncio.wait_for(self.login_proc.stdout.read(4096), wait)
            except asyncio.TimeoutError:
                break
            if not chunk:
                ended = True
                break
            buf += chunk.decode(errors="replace")
            text = _ANSI_RE.sub("", buf)
            m = re.search(r"https://\S+", text)
            if m:
                url = m.group(0)
                if m.end() < len(text):  # something follows, so the URL is complete
                    break
                deadline = min(deadline, loop.time() + 1.5)  # it may continue in the next chunk
        if url and not ended:
            return {"url": url}
        log = _ANSI_RE.sub("", buf).strip()[-400:]
        decky.logger.warning("login_start: no sign-in link (%s)", log)
        await self._login_cancel()
        return {"error": "Claude Code didn't show a sign-in link." if not ended else "Claude Code stopped before showing a sign-in link.",
                "log": log}

    async def login_finish(self, code):
        p = getattr(self, "login_proc", None)
        if not p or p.returncode is not None:
            return {"error": "Sign-in expired. Start again."}
        try:
            p.stdin.write((code.strip() + "\n").encode())
            await p.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            await self._login_cancel()
            return {"error": "Sign-in expired. Start again."}
        try:
            out = await asyncio.wait_for(p.stdout.read(), 60)  # the rest of its output, until it exits
            await asyncio.wait_for(p.wait(), 10)
        except asyncio.TimeoutError:
            await self._login_cancel()
            return {"error": "Sign-in didn't finish. Check the code and try again."}
        self.login_proc = None
        st = await self.setup_status()
        if st["signed_in"]:
            await self._kill()  # the next message starts Claude with the new login
            await decky.emit("claude_auth", {"signed_in": True})
            return {"ok": True, "plan": st["plan"]}
        log = _ANSI_RE.sub("", out.decode(errors="replace")).strip()[-400:]
        decky.logger.warning("login_finish failed: %s", log)
        return {"error": "That code didn't work. Start again and paste the new code.", "log": log}

    async def _login_cancel(self):
        p = getattr(self, "login_proc", None)
        if p and p.returncode is None:
            p.kill()
            await p.wait()
        self.login_proc = None

    async def setup_voice(self):
        """Download Piper, a voice and the Whisper model, and a prebuilt whisper-cli from this
        project's latest release. Progress arrives as claude_setup events."""
        import tarfile
        import urllib.request
        base = os.path.join(HOME, ".local/share/claude-deck")
        piper_dir, whisper_dir = os.path.join(base, "piper"), os.path.join(base, "whisper")
        os.makedirs(piper_dir, exist_ok=True)
        os.makedirs(whisper_dir, exist_ok=True)

        def fetch(url, dest, step):
            tmp, done, last = dest + ".part", 0, -1
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "claude-deck"}),
                                        timeout=60, context=_ssl()) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                while chunk := r.read(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else None
                    if pct is not None and pct // 5 != last:
                        last = pct // 5
                        asyncio.run_coroutine_threadsafe(decky.emit("claude_setup", {"step": step, "pct": pct}), loop)
            os.replace(tmp, dest)

        loop = asyncio.get_running_loop()
        try:
            if not os.path.exists(deck_tools.PIPER_BIN):
                tgz = os.path.join(base, "piper.tgz")
                await asyncio.to_thread(fetch, VOICE_FILES["piper"], tgz, "Downloading the voice engine")
                with tarfile.open(tgz) as t:
                    for m in t.getmembers():
                        m.name = m.name.split("/", 1)[1] if "/" in m.name else m.name
                        if m.name and not m.name.startswith(("/", "..")):
                            t.extract(m, piper_dir)
                os.remove(tgz)
            if not os.path.exists(deck_tools.PIPER_VOICE):
                await asyncio.to_thread(fetch, VOICE_FILES["voice"], deck_tools.PIPER_VOICE, "Downloading Claude's voice")
                await asyncio.to_thread(fetch, VOICE_FILES["voice"] + ".json", deck_tools.PIPER_VOICE + ".json", "Downloading Claude's voice")
            if not os.path.exists(deck_tools.WHISPER_MODEL):
                await asyncio.to_thread(fetch, VOICE_FILES["whisper_model"], deck_tools.WHISPER_MODEL, "Downloading speech recognition")
            if not os.path.exists(deck_tools.WHISPER_BIN):
                rel = await asyncio.to_thread(self._latest_release)
                asset = next((a for a in rel.get("assets", []) if a.get("name") == "whisper-cli-linux-x86_64"), None)
                if not asset:
                    raise RuntimeError("whisper-cli isn't in the latest release; run scripts/setup-voice.sh to build it")
                await asyncio.to_thread(fetch, asset["browser_download_url"], deck_tools.WHISPER_BIN, "Downloading the transcriber")
                os.chmod(deck_tools.WHISPER_BIN, 0o755)
            await decky.emit("claude_setup", {"step": "Voice is ready", "pct": 100, "done": True})
            return {"ok": True}
        except Exception as e:
            decky.logger.exception("setup_voice failed")
            await decky.emit("claude_setup", {"step": f"Voice setup failed: {e}", "error": True, "done": True})
            return {"ok": False, "error": str(e)}

    def _latest_release(self):
        import urllib.request
        req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                            "User-Agent": "claude-deck"})
        with urllib.request.urlopen(req, timeout=15, context=_ssl()) as r:
            return json.load(r)

    async def check_update(self):
        """The newest release if it's newer than what's installed."""
        try:
            rel = await asyncio.to_thread(self._latest_release)
        except Exception as e:
            return {"error": str(e)}
        latest = (rel.get("tag_name") or "").lstrip("v")
        current = self._plugin_version() or "0"
        asset = next((a for a in rel.get("assets", []) if a.get("name") == "claude-deck.zip"), None)
        newer = asset and tuple(int(x) for x in re.findall(r"\d+", latest)[:3]) > tuple(
            int(x) for x in re.findall(r"\d+", current)[:3])
        return {"current": current, "latest": latest, "update": bool(newer),
                "url": asset["browser_download_url"] if asset else None, "notes": (rel.get("body") or "")[:600]}

    # --- background jobs (transient systemd user units named claude-*) ------------
    async def list_jobs(self):
        out = []
        try:
            p = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "list-units", "claude-*", "--all", "--no-legend", "--plain", "-o", "json",
                env=deck_tools.clean_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            raw, _ = await p.communicate()
            for u in json.loads(raw or b"[]"):
                name = u.get("unit", "")
                if name.endswith((".service", ".timer")):
                    out.append({"unit": name, "state": u.get("active"), "sub": u.get("sub"),
                                "description": u.get("description", "")})
        except Exception:
            decky.logger.exception("list_jobs failed")
        return out

    async def cancel_job(self, unit):
        if not re.fullmatch(r"claude-[\w@.-]+\.(service|timer)", unit or ""):
            return False
        p = await asyncio.create_subprocess_exec("systemctl", "--user", "stop", unit, env=deck_tools.clean_env())
        return await p.wait() == 0

    # --- hold-to-talk from anywhere ------------------------------------------------
    async def _ptt_down(self, role="ask"):
        # TEMP speech bench: reserve the shared recorder and CPU during comparison.
        if self._bench_busy():
            return
        if role == "ask" and (self.caption_up or self.speaker.active) and not self.ptt_t0:
            # Pressing again while an answer is up dismisses it; press once more to ask again.
            await self.stop_speaking()
            await decky.emit("claude_ptt", {"phase": "dismiss"})
            self.caption_up = False
            return
        if self.ptt_t0 or self.recorder.active or not await self.voice_available():
            return  # already listening, the on-screen mic is in use, or voice isn't installed
        self.ptt_role = role
        self.ptt_t0 = time.monotonic()
        self.ptt_shot = None
        try:
            await self.recorder.start()  # start at once so the first word isn't lost
        except Exception as e:
            self.ptt_t0 = 0
            await decky.emit("claude_ptt", {"phase": "error", "text": f"Mic unavailable: {e}"})
            return
        await asyncio.sleep(TAP_SECONDS)
        if not self.ptt_t0:
            return  # it was a tap: _ptt_up dismissed instead
        await self.stop_speaking()
        if role == "dictate":
            await decky.emit("claude_ptt", {"phase": "dictating"})
            return
        await decky.emit("claude_ptt", {"phase": "listening"})
        try:  # what's on screen right now (the game, not our caption)
            self.ptt_shot = await deck_tools.screenshot()
        except Exception:
            decky.logger.exception("ptt screenshot failed")

    async def _ptt_up(self, role="ask"):
        if not self.ptt_t0 or role != self.ptt_role:
            return
        held, self.ptt_t0 = time.monotonic() - self.ptt_t0, 0
        if held < TAP_SECONDS:
            # Too short to be a question: treat it like a dismiss.
            await self.recorder.cancel()
            await self.stop_speaking()
            await decky.emit("claude_ptt", {"phase": "dismiss"})
            return
        await decky.emit("claude_ptt", {"phase": "transcribing"})
        try:
            text = await self.recorder.stop()
        except Exception as e:
            await decky.emit("claude_ptt", {"phase": "error", "text": str(e)})
            return
        if not text:
            await decky.emit("claude_ptt", {"phase": "error", "text": "Didn't catch that. Hold the buttons while you talk."})
            return
        if role == "dictate":  # the frontend types it into whatever has focus
            await decky.emit("claude_ptt", {"phase": "dictated", "text": text})
            return
        # The frontend adds Deck context and sends it, so it lands in the chat like any message.
        await decky.emit("claude_ptt", {"phase": "heard", "text": text, "shot": self.ptt_shot})

    async def ptt_press(self, down, role="ask"):
        """Same as holding/releasing a chord; for demos and scripts/deckctl.py."""
        await (self._ptt_down(role) if down else self._ptt_up(role))
        return True

    def _tts_stream(self, text, flush=False):
        """Speak complete sentences as the reply streams in; skip code blocks."""
        if not self.speaker.active:
            self.tts_on = False  # stopped by the user (or the mic): stay quiet this turn
            return
        self.tts_buf += text
        while True:
            fence = self.tts_buf.find("```")
            m = re.search(r"[.!?…](?=\s)|\n", self.tts_buf)
            if fence != -1 and (not m or fence < m.start()):
                if not self.tts_code:
                    self.speaker.feed(self.tts_buf[:fence])
                self.tts_code = not self.tts_code
                self.tts_buf = self.tts_buf[fence + 3:]
                continue
            if not m:
                break
            chunk, self.tts_buf = self.tts_buf[:m.end()], self.tts_buf[m.end():]
            if not self.tts_code:
                self.speaker.feed(chunk)
        if flush and self.tts_buf and not self.tts_code:
            self.speaker.feed(self.tts_buf)
            self.tts_buf = ""

    async def _tts_finish(self):
        try:
            await self.speaker.end()
        finally:
            if not self.speaker.active:
                await decky.emit("claude_speaking", False)

    async def stop_speaking(self):
        stopped = await self.speaker.stop()
        await decky.emit("claude_speaking", False)
        return stopped

    async def remote(self, enabled):
        """Mirror this chat to claude.ai/code so it continues on a phone. Returns {url} or {error}."""
        try:
            await self._ensure()
            r = await self._control({"subtype": "remote_control", "enabled": bool(enabled)}, timeout=45)
            self.remote_url = (r or {}).get("session_url") if enabled else None
            return {"url": self.remote_url}
        except Exception as e:
            decky.logger.exception("remote control failed")
            return {"error": _clip(e, 300)}

    async def snap(self):
        """Take a screenshot for the next message. Returns {path} or {error}."""
        try:
            return {"path": await deck_tools.screenshot()}
        except Exception as e:
            decky.logger.exception("screenshot failed")
            return {"error": str(e)}

    async def battery_status(self):
        return deck_tools.battery()

    async def voice_available(self):
        return os.path.exists(deck_tools.WHISPER_BIN) and os.path.exists(deck_tools.WHISPER_MODEL)

    async def mic_start(self):
        # TEMP speech bench
        if self._bench_busy():
            return {"error": "Speech benchmark is using the microphone"}
        self.bench_mic_starting = True
        try:
            await self.stop_speaking()  # don't talk over the user
            await self.recorder.start()
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}
        finally:
            self.bench_mic_starting = False  # TEMP speech bench

    async def mic_stop(self):
        """Stop recording and return {text} or {error}."""
        # TEMP speech bench
        if self._bench_busy():
            return {"error": "Use Stop and compare to finish the speech benchmark"}
        try:
            return {"text": await self.recorder.stop()}
        except Exception as e:
            decky.logger.exception("voice input failed")
            return {"error": str(e)}

    async def mic_cancel(self):
        # TEMP speech bench
        if self._bench_busy():
            return False
        await self.recorder.cancel()
        return True

    # TEMP speech bench: remove this block and the guarded recorder hooks after choosing a model.
    def _bench_busy(self):
        return getattr(self, "bench_state", {}).get("phase") in ("recording", "running")

    async def _bench_emit(self, **event):
        self.bench_state = {**self.bench_state, **event}
        await decky.emit("claude_bench", self.bench_state)

    async def bench_last(self):
        return self.bench_state

    async def bench_start(self):
        if (self._bench_busy() or self.recorder.active or self.ptt_t0
                or getattr(self, "bench_mic_starting", False)):
            return {"error": "Recorder or speech benchmark is busy"}
        # Reserve before the first await, including recorder startup.
        self.bench_state = {**self.bench_state, "phase": "recording", "message": ""}
        try:
            await self.stop_speaking()
            await self.recorder.start()
            await self._bench_emit(phase="recording")
            return {"ok": True}
        except Exception as e:
            await self._bench_emit(phase="error", message=str(e))
            return {"error": str(e)}

    async def bench_stop(self):
        if self.bench_state["phase"] != "recording":
            return {"error": "Speech benchmark is not recording"}
        self.bench_state = {**self.bench_state, "phase": "running", "index": 0,
                            "total": 0, "model": "Preparing"}
        try:
            if not await self.recorder.cancel():
                raise RuntimeError("The microphone stopped before recording finished")
            await self._bench_emit(phase="running")
            self.bench_task = asyncio.create_task(self._run_speech_bench())
            return {"ok": True}
        except Exception as e:
            await self._bench_emit(phase="error", message=str(e))
            return {"error": str(e)}

    async def _run_speech_bench(self):
        async def progress(index, total, model):
            await self._bench_emit(phase="running", index=index, total=total, model=model)

        try:
            import wave
            with wave.open(self.recorder.path, "rb") as w:
                secs = w.getnframes() / w.getframerate()
            results = await deck_tools.speech_bench(self.recorder.path, progress)
            await self._bench_emit(phase="done", results=results, clip_seconds=round(secs, 1))
        except Exception as e:
            await self._bench_emit(phase="error", message=str(e))
    # TEMP speech bench end.

    async def stop(self):
        """Interrupt the current reply. Claude keeps what it said so far."""
        if not self._alive() or not self.running:
            return False
        for rid in list(self.asks):
            await self.answer(rid, "deny")
        try:
            await self._control({"subtype": "interrupt"}, timeout=5)
        except Exception:
            await self._kill()
        # Queued messages the interrupt dropped will never be echoed; don't wait for them.
        self.inflight = 0
        self.spoken.clear()
        if not self.turn_active:
            await self._set_running(False)
        return True

    async def new_chat(self):
        await self._kill()
        self._archive()
        self.session_id = None
        self.history = []
        self.chat_meta = {}
        self._save()
        return True

    # --- chat history and per-game threads ---------------------------------------
    def _archive(self):
        """Keep the current chat's messages so it can be reopened."""
        if not (self.session_id and self.history):
            return
        os.makedirs(CHATS_DIR, exist_ok=True)
        with open(os.path.join(CHATS_DIR, f"{self.session_id}.json"), "w") as f:
            json.dump(self.history, f)
        meta = self.chats.setdefault(self.session_id, {})
        meta.update({k: v for k, v in self.chat_meta.items() if v}, updated=time.time())
        meta.setdefault("title", next((e["text"][:70] for e in self.history if e["kind"] == "user"), "Chat"))

    def _note_chat(self, prompt, context):
        if not self.chat_meta.get("title"):
            self.chat_meta["title"] = prompt[:70]
        m = re.search(r"^(.*) \(appid (\d+)\)$", str((context or {}).get("Running game", "")))
        if m and not self.chat_meta.get("appid"):
            self.chat_meta.update(game=m.group(1), appid=int(m.group(2)))

    async def list_chats(self):
        cur = self.session_id
        items = [dict(meta, id=sid, current=sid == cur) for sid, meta in self.chats.items()]
        if cur and cur not in self.chats and self.history:
            items.append(dict(self.chat_meta, id=cur, current=True, updated=time.time()))
        return sorted(items, key=lambda c: -c.get("updated", 0))[:40]

    async def open_chat(self, sid):
        if sid == self.session_id:
            return True
        path = os.path.join(CHATS_DIR, f"{sid}.json")
        if not os.path.exists(path):
            return False
        await self._kill()
        self._archive()
        with open(path) as f:
            self.history = json.load(f)
        self.session_id = sid
        self.chat_meta = dict(self.chats.get(sid, {}))
        self._save()
        await decky.emit("claude_chat", {"title": self.chat_meta.get("title")})
        return True

    async def delete_chat(self, sid):
        if sid == self.session_id:
            return False
        self.chats.pop(sid, None)
        try:
            os.remove(os.path.join(CHATS_DIR, f"{sid}.json"))
        except OSError:
            pass
        self._save()
        return True

    async def set_game_threads(self, on):
        self.game_threads = bool(on)
        self._save()
        return True

    async def _game_thread(self, context):
        """Switch to the running game's own chat (or a fresh one) before a message about it."""
        m = re.search(r"\(appid (\d+)\)$", str((context or {}).get("Running game", "")))
        if not (self.game_threads and m) or self.running:
            return
        appid = int(m.group(1))
        cur_app = self.chat_meta.get("appid")
        if cur_app is None or cur_app == appid:
            return  # an untagged chat becomes this game's chat (_note_chat tags it)
        mine = [sid for sid, meta in self.chats.items() if meta.get("appid") == appid and sid != self.session_id]
        if mine:
            await self.open_chat(max(mine, key=lambda sid: self.chats[sid].get("updated", 0)))
        else:
            await self.new_chat()
            await decky.emit("claude_chat", {"title": None})

    # --- claude process --------------------------------------------------------
    def _alive(self):
        return self.proc is not None and self.proc.returncode is None

    def _write(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())

    def _respond(self, rid, response):
        if self._alive():
            self._write({"type": "control_response",
                         "response": {"subtype": "success", "request_id": rid, "response": response}})

    async def _control(self, request, timeout=30):
        rid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.ctrl[rid] = fut
        self._write({"type": "control_request", "request_id": rid, "request": request})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.ctrl.pop(rid, None)

    async def _ensure(self):
        """Start the long-lived Claude process if it isn't running."""
        if self._alive():
            await self.ready
            return
        args = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--replay-user-messages", "--permission-prompt-tool", "stdio",
                "--permission-mode", MODES[self.mode], "--allow-dangerously-skip-permissions",
                "--settings", json.dumps({"permissions": {"allow": SAFE_TOOLS, "deny": DENY_TOOLS}}),
                "--mcp-config", MCP_CONFIG, "--strict-mcp-config",
                "--system-prompt-snapshot", "off", "--append-system-prompt", SYSTEM_PROMPT,
                "--name", DEVICE_NAME]
        if self.model != "default":
            args += ["--model", self.model]
        if self.effort != "default":
            args += ["--effort", self.effort]
        if self.session_id:
            args += ["--resume", self.session_id]
        self.stopping = False
        self.inflight = 0
        self.turn_active = False
        self.spoken.clear()
        self.remote_url = None
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=HOME, env=_env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024)
        self.ready = asyncio.get_running_loop().create_future()
        self.reader = asyncio.create_task(self._read(self.proc))
        try:
            self.info = await self._control({"subtype": "initialize", "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hookCallbackIds": ["deck_policy"]},
                {"matcher": "mcp__steam__steam_js", "hookCallbackIds": ["deck_policy"]}]}}, timeout=60) or {}
            self.ready.set_result(True)
        except Exception as e:
            self.ready.set_exception(e)
            self.ready.exception()  # mark retrieved
            await self._kill()
            raise RuntimeError(f"Claude didn't start: {e}")

    async def _kill(self):
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        self.stopping = True
        try:
            proc.stdin.close()  # graceful: the CLI exits on EOF
            await asyncio.wait_for(proc.wait(), 1.5)
        except Exception:
            proc.terminate()  # Decky kills the whole plugin 5 s after asking it to stop
            try:
                await asyncio.wait_for(proc.wait(), 1.5)
            except asyncio.TimeoutError:
                proc.kill()

    async def _read(self, proc):
        stderr_tail = b""

        async def drain_stderr():
            nonlocal stderr_tail
            while chunk := await proc.stderr.read(4096):
                stderr_tail = (stderr_tail + chunk)[-2000:]

        err_task = asyncio.create_task(drain_stderr())
        try:
            async for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                try:
                    await self._dispatch(ev)
                except Exception:
                    decky.logger.exception("event handling failed")
        finally:
            await proc.wait()
            await err_task
            if self.proc is proc:
                self.proc = None
            for fut in self.ctrl.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("Claude exited"))
            for rid in list(self.asks):
                self.asks.pop(rid)
                await decky.emit("claude_ask_done", rid)
            if (self.inflight or self.turn_active) and not self.stopping:
                msg = stderr_tail.decode(errors="replace").strip() or f"Claude exited with code {proc.returncode}"
                if _AUTH_RE.search(msg):
                    await self._signed_out()
                    msg = SIGNED_OUT_MSG
                await self._push("error", _clip(msg, 600))
            self.inflight = 0
            self.turn_active = False
            self.spoken.clear()
            self.turn_text.clear()
            self.remote_url = None
            await self._set_running(False)

    async def _signed_out(self):
        """Claude Code lost its login mid-session: tell the UI so it shows Sign in again."""
        if not self.auth_lost:
            decky.logger.warning("Claude Code is signed out")
        self.auth_lost = True
        await decky.emit("claude_auth", {"signed_in": False})

    async def _dispatch(self, ev):
        t = ev.get("type")
        if t == "control_response":
            r = ev.get("response", {})
            fut = self.ctrl.get(r.get("request_id"))
            if fut and not fut.done():
                if r.get("subtype") == "error":
                    fut.set_exception(RuntimeError(r.get("error") or "control request failed"))
                else:
                    fut.set_result(r.get("response"))
        elif t == "control_request":
            await self._on_request(ev.get("request_id"), ev.get("request", {}))
        elif t == "control_cancel_request":
            rid = ev.get("request_id")
            if self.asks.pop(rid, None):  # answered elsewhere (e.g. on the phone)
                await decky.emit("claude_ask_done", rid)
        elif t == "rate_limit_event":
            await self._set_usage(ev.get("rate_limit_info"))
        else:
            await self._handle(ev)

    async def _on_request(self, rid, req):
        sub = req.get("subtype")
        if sub == "hook_callback":
            try:
                self._respond(rid, deck_policy(req.get("input") or {}))
            except Exception:
                decky.logger.exception("deck policy failed")
                self._respond(rid, {})
        elif sub == "can_use_tool":
            name = req.get("tool_name", "")
            inp = req.get("input") or {}
            kind = {"AskUserQuestion": "question", "ExitPlanMode": "plan"}.get(name, "tool")
            card = {"id": rid, "kind": kind, "tool": _pretty_tool(name), "tool_name": name,
                    "title": _step_label(name, inp),
                    "summary": _tool_summary(name, inp), "reason": req.get("decision_reason") or "",
                    "description": req.get("description") or inp.get("description") or "",
                    "can_always": any(s.get("type") == "addRules" for s in req.get("permission_suggestions") or []),
                    "questions": inp.get("questions") if kind == "question" else None,
                    "plan": inp.get("plan") if kind == "plan" else None,
                    "ts": time.time(), "_req": req}
            self.asks[rid] = card
            await decky.emit("claude_ask", {k: v for k, v in card.items() if k != "_req"})
        else:
            self._respond(rid, {})

    async def _handle(self, ev):
        """Translate one stream-json event into UI entries."""
        t = ev.get("type")
        if ev.get("session_id"):
            self.session_id = ev["session_id"]
        if t == "stream_event":
            e = ev.get("event", {})
            if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                await decky.emit("claude_delta", e["delta"].get("text", ""))
                if self.tts_on:
                    self._tts_stream(e["delta"].get("text", ""))
            elif e.get("type") == "content_block_stop" and self.tts_on:
                self._tts_stream("\n", flush=True)
        elif t == "assistant":
            if ev.get("error") == "authentication_failed" or (ev.get("is_api_error_message") and _AUTH_RE.search(
                    " ".join(b.get("text", "") for b in ev.get("message", {}).get("content", []) if isinstance(b, dict)))):
                await self._signed_out()  # the result event right after carries the message
                return
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.turn_text.append(block["text"].strip())
                    await self._push("assistant", block["text"].strip())
                elif block.get("type") == "tool_use" and block.get("name") not in ("AskUserQuestion",):
                    name = block.get("name", "")
                    await self._push("tool", _step_label(name, block.get("input")), name=name,
                                     detail=_tool_summary(name, block.get("input")))
        elif t == "user" and ev.get("isReplay"):
            # The CLI took everything queued so far into this turn (it merges queued messages
            # into one echo), so nothing we've written is still waiting.
            self.inflight = 0
            self.turn_active = True
            self.turn_spoken |= any(self.spoken)
            self.spoken.clear()
            await self._set_running(True)
            if self.voice == "always" or (self.voice == "voice" and self.turn_spoken):
                if not self.tts_on:
                    self.tts_on, self.tts_buf, self.tts_code = True, "", False
                    await self.speaker.begin()
                    await decky.emit("claude_speaking", True)
        elif t == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    c = block.get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    if c and "user tapped Deny" not in c:
                        await self._push("tool_error", _clip(c, 300))
        elif t == "system":
            sub = ev.get("subtype")
            if sub == "init":
                self.commands = _commands_from_init(ev)
                await decky.emit("claude_commands", self.commands)
            elif sub == "compact_boundary":
                await self._push("info", "Conversation compacted to save space")
            elif sub == "api_retry":
                await decky.emit("claude_notice", f"Retrying… ({ev.get('error') or 'busy'})")
        elif t == "result":
            if ev.get("local_command"):
                self.inflight = max(0, self.inflight - 1)  # Local commands finish without a user echo.
                if self.spoken:
                    self.spoken.pop(0)
            self.turn_active = False
            self.turn_spoken = False
            self.turn_text = []
            if self.tts_on:
                self._tts_stream("", flush=True)
                self.tts_on = False
                asyncio.create_task(self._tts_finish())
            secs = (ev.get("duration_ms") or 0) / 1000
            if ev.get("is_error") or ev.get("subtype") not in (None, "success"):
                err = ev.get("result") or ev.get("subtype") or "error"
                if ev.get("subtype") == "error_during_execution" and not ev.get("result"):
                    await self._push("info", "Stopped.")
                else:
                    if "No conversation found" in str(err):
                        self.session_id = None
                    if _AUTH_RE.search(str(err)):
                        await self._signed_out()
                        err = SIGNED_OUT_MSG
                    await self._push("error", _clip(err, 600))
            else:
                await self._push("done", f"{secs:.0f}s" if secs >= 1 else "")
            if self.inflight == 0:
                await self._set_running(False)
            else:  # more messages are waiting; a new turn starts right away
                self.turn_active = True
