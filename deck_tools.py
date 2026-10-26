"""Screen capture, voice transcription and Deck status, shared by main.py and steam_mcp.py.

Plain stdlib (no decky import) so the MCP server can use it too.
"""
import asyncio
import glob
import os
import shutil
import time

HOME = os.path.expanduser("~deck")
CACHE = os.path.join(HOME, ".cache/claude-deck")
SHOTS = os.path.join(CACHE, "shots")
WHISPER_DIR = os.path.join(HOME, ".local/share/claude-deck/whisper")
WHISPER_BIN = os.path.join(WHISPER_DIR, "whisper-cli")
WHISPER_MODEL = os.path.join(WHISPER_DIR, "ggml-base.en.bin")
PIPER_DIR = os.path.join(HOME, ".local/share/claude-deck/piper")
PIPER_BIN = os.path.join(PIPER_DIR, "piper")
PIPER_VOICE = os.path.join(PIPER_DIR, "en_US-lessac-medium.onnx")
KEEP_SHOTS = 10
DEVICE_NAMES = {
    "product_name": {
        "83E1": "Legion Go",
        "83N0": "Legion Go 2",
        "83N1": "Legion Go 2",
        "83L3": "Legion Go S",
        "83N6": "Legion Go S",
        "83Q2": "Legion Go S",
        "83Q3": "Legion Go S",
    },
    "board_name": {
        "RC71L": "ROG Ally",
        "RC72LA": "ROG Ally X",
        "MS-1T41": "MSI Claw",
    },
}


def device_name(dmi="/sys/class/dmi/id"):
    info = {}
    for key in ("sys_vendor", "product_family", "product_name", "board_name"):
        try:
            with open(os.path.join(dmi, key)) as f:
                info[key] = f.read().strip()
        except OSError:
            pass
    if any(info.get(key, "").lower() in ("jupiter", "galileo") for key in ("product_name", "board_name")):
        return "Steam Deck"
    for key, names in DEVICE_NAMES.items():
        if info.get(key) in names:
            return names[info[key]]
    keys = ("product_family", "product_name") if info.get("sys_vendor", "").upper() == "LENOVO" else ("product_name", "product_family")
    for key in keys:
        name = info.get(key, "")
        if name.lower() not in ("", "default string", "to be filled by o.e.m.", "system product name", "unknown", "none"):
            return name
    return "Steam Deck"


def clean_env():
    uid = os.getuid()
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")}
    env.update({
        "HOME": HOME,
        "PATH": f"{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    })
    return env


async def _run(*args, env=None, timeout=30):
    p = await asyncio.create_subprocess_exec(
        *args, env=env or clean_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise RuntimeError(f"{os.path.basename(args[0])} timed out")
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def game_mode():
    """True when gamescope (Game Mode) is the compositor."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if open(f"/proc/{pid}/comm").read().startswith("gamescope"):
                return True
        except OSError:
            pass
    return False


# --- screenshots -------------------------------------------------------------------

async def screenshot():
    """Capture the screen to a PNG and return its path."""
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, time.strftime("shot-%Y%m%d-%H%M%S.png"))
    if game_mode():
        # gamescope writes /tmp/gamescope.png (older builds: /tmp/gamescope_<time>.png) when
        # this root-window atom is set.
        t0 = time.time()
        env = clean_env() | {"DISPLAY": ":0"}
        await _run("xprop", "-root", "-f", "GAMESCOPECTRL_REQUEST_SCREENSHOT", "32c",
                   "-set", "GAMESCOPECTRL_REQUEST_SCREENSHOT", "1", env=env)
        for _ in range(40):
            await asyncio.sleep(0.15)
            new = [f for f in glob.glob("/tmp/gamescope*.png") if os.path.getmtime(f) >= t0 - 0.05]
            if new:
                src = max(new, key=os.path.getmtime)
                size = -1
                while size != os.path.getsize(src):  # wait until gamescope finishes writing
                    size = os.path.getsize(src)
                    await asyncio.sleep(0.25)
                shutil.move(src, path)
                break
        else:
            raise RuntimeError("gamescope did not produce a screenshot")
    else:
        env = clean_env() | {"WAYLAND_DISPLAY": "wayland-0", "QT_QPA_PLATFORM": "wayland"}
        code, _, err = await _run("spectacle", "-b", "-n", "-f", "-o", path, env=env)
        if not os.path.exists(path):
            raise RuntimeError(f"spectacle failed ({code}): {err.strip()[-300:]}")
    for old in sorted(glob.glob(os.path.join(SHOTS, "shot-*.png")))[:-KEEP_SHOTS]:
        os.remove(old)
    return path


# --- voice input ---------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.proc = None
        self.path = os.path.join(CACHE, "voice.wav")

    @property
    def active(self):
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        if self.active:
            return
        os.makedirs(CACHE, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            "pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", self.path,
            env=clean_env(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    async def stop(self):
        """Stop recording and return the transcript."""
        if not await self.cancel():
            raise RuntimeError("Not recording")
        return await transcribe(self.path)

    async def cancel(self):
        """Stop recording; returns whether it was recording."""
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return False
        proc.terminate()
        await proc.wait()
        return True


async def transcribe(wav):
    if not (os.path.exists(WHISPER_BIN) and os.path.exists(WHISPER_MODEL)):
        raise RuntimeError("Voice input isn't installed (whisper-cli or model missing)")
    # Size the audio context to the clip: a 5 s question decodes ~2.5x faster than with the
    # default 30 s window. 4 threads beats 8 on the Deck (SMT siblings, and a game is running).
    try:
        import wave
        with wave.open(wav) as w:
            secs = w.getnframes() / w.getframerate()
        ctx = min(1500, max(256, -(-int(secs * 50 + 128) // 64) * 64))
    except Exception:
        ctx = 1500
    code, out, err = await _run(WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav, "-l", "en",
                                "-nt", "-np", "-t", "4", "-ac", str(ctx), timeout=120)
    if code != 0:
        raise RuntimeError(f"whisper failed: {err.strip()[-300:]}")
    text = " ".join(line.strip() for line in out.splitlines() if line.strip())
    # whisper marks silence/noise as [BLANK_AUDIO], (wind), etc.
    if text.strip("[]() .").upper() in ("", "BLANK_AUDIO", "SILENCE", "MUSIC"):
        return ""
    # Whisper's classic hallucinations on near-silence.
    if text.strip(" .!?").lower() in ("you", "thank you", "thanks for watching", "bye"):
        return ""
    # Game audio comes through as sound captions like "(footsteps)" or "[music]"; drop those.
    import re
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]|\*[^*]*\*|♪+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not re.search(r"[A-Za-z]{2}", text):
        return ""
    return text


# --- spoken replies -------------------------------------------------------------------------

def speech_text(md):
    """Markdown reply -> something pleasant to hear: no code, symbols or link targets."""
    import re
    t = re.sub(r"```.*?```", " ", md, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"^\s*#+\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"[*_>|#]|~~", "", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\.\s*\.", ".", t)
    return re.sub(r"\s{2,}", " ", t).strip()


class Speaker:
    """Streams Piper speech to the speakers sentence by sentence (espeak-ng as a fallback).

    Either say(text) for a whole reply, or begin() / feed(sentence)... / end() to start talking
    while the reply is still being written."""

    def __init__(self):
        self.proc = None

    @staticmethod
    def available():
        return (os.path.exists(PIPER_BIN) and os.path.exists(PIPER_VOICE)) or shutil.which("espeak-ng") is not None

    @property
    def active(self):
        return self.proc is not None and self.proc.returncode is None

    async def begin(self):
        await self.stop()
        if os.path.exists(PIPER_BIN) and os.path.exists(PIPER_VOICE):
            # Piper speaks each stdin line as soon as it arrives.
            cmd = (f"'{PIPER_BIN}' --model '{PIPER_VOICE}' --output-raw --sentence_silence 0.15 2>/dev/null | "
                   "pw-play --rate 22050 --channels 1 --format s16 -")
        else:
            cmd = "espeak-ng -v en-us -s 165 --stdout | pw-play -"
        self.proc = await asyncio.create_subprocess_shell(
            cmd, env=clean_env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

    def feed(self, text):
        text = speech_text(text)
        if text and self.active and not self.proc.stdin.is_closing():
            self.proc.stdin.write((text + "\n").encode())

    async def end(self):
        """No more text: let it finish speaking."""
        proc = self.proc
        if proc is None:
            return
        if not proc.stdin.is_closing():
            proc.stdin.close()
        await proc.wait()

    async def say(self, text):
        await self.begin()
        self.feed(text)
        await self.end()

    async def stop(self):
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return False
        import signal
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        await proc.wait()
        return True


# --- push-to-talk chord -------------------------------------------------------------------

# Steam Deck controller input report (hidraw, vendor interface): byte 2 == 0x09, then two
# little-endian button words at offsets 8 ("low") and 12 ("high"). Bits as in SDL's driver.
LOW_BITS = {
    0x1: "R2", 0x2: "L2", 0x4: "R1", 0x8: "L1", 0x10: "Y", 0x20: "B", 0x40: "X", 0x80: "A",
    0x100: "Up", 0x200: "Right", 0x400: "Left", 0x800: "Down", 0x1000: "View", 0x2000: "Steam",
    0x4000: "Menu", 0x8000: "L5", 0x10000: "R5", 0x20000: "Left pad", 0x40000: "Right pad",
    0x400000: "L3", 0x4000000: "R3",
}
HIGH_BITS = {0x200: "L4", 0x400: "R4", 0x40000: "QAM"}
NOISY_HIGH = 0x4000 | 0x8000  # stick touch sensors
NOISY_LOW = 0x80000 | 0x100000  # trackpad touch (not click)
BACK_BUTTONS = (0x8000 | 0x10000, 0x200 | 0x400)  # chords made only of these never reach games
PRESETS = {
    "l4r4": (0, 0x600), "l5r5": (0x18000, 0), "l4l5": (0x8000, 0x200), "r4r5": (0x10000, 0x400),
    "l4": (0, 0x200), "r4": (0, 0x400), "l5": (0x8000, 0), "r5": (0x10000, 0),
    "viewmenu": (0x1000 | 0x4000, 0), "l3r3": (0x400000 | 0x4000000, 0), "pads": (0x20000 | 0x40000, 0),
}


def chord_masks(spec):
    """'l4r4' or 'm:<low hex>:<high hex>' -> (low mask, high mask); (0, 0) means off."""
    if spec in PRESETS:
        return PRESETS[spec]
    if isinstance(spec, str) and spec.startswith("m:"):
        try:
            lo, hi = (int(x, 16) for x in spec[2:].split(":"))
            return lo, hi
        except ValueError:
            pass
    return 0, 0


def valid_chord(spec):
    return spec == "off" or chord_masks(spec) != (0, 0)


def chord_label(spec):
    lo, hi = chord_masks(spec)
    if not (lo or hi):
        return "Off"
    names = [n for b, n in HIGH_BITS.items() if hi & b] + [n for b, n in LOW_BITS.items() if lo & b]
    unknown = bin(lo & ~sum(LOW_BITS)).count("1") + bin(hi & ~sum(HIGH_BITS)).count("1")
    names += ["button"] * unknown
    return " + ".join(names)


def find_controller(sysfs="/sys/class/hidraw"):
    """Prefer the built-in Deck input interface, then InputPlumber's Deck emulation."""
    virtual = None
    # InputPlumber: src/drivers/steam_deck/mod.rs ProductId (0.78.0).
    emulated = {0x12F0, 0x12FA, 0x12FB, 0x12FC, 0x12FD, 0x12FE, 0x12FF}
    for dev in sorted(glob.glob(os.path.join(sysfs, "hidraw*"))):
        try:
            with open(os.path.join(dev, "device/uevent")) as f:
                ev = f.read()
            fields = dict(line.split("=", 1) for line in ev.splitlines() if "=" in line)
            _, vendor, product = (int(x, 16) for x in fields.get("HID_ID", "").split(":"))
        except (OSError, ValueError):
            continue
        if vendor != 0x28DE:
            continue
        node = "/dev/" + os.path.basename(dev)
        uhid = "uhid" in os.path.realpath(os.path.join(dev, "device")).split(os.sep)
        if product == 0x1205 and "input2" in ev and not uhid:
            return node
        if virtual is None and (uhid or product in emulated):
            virtual = node
    return virtual


class ChordWatcher:
    """Reads the controller directly (alongside Steam, not instead of it) and reports when a
    button chord is pressed and released, even while a game has focus.

    `bindings` maps a role ("ask", "dictate") to a chord name; callbacks get the role."""

    def __init__(self, on_down, on_up, log=None):
        self.on_down, self.on_up, self.log = on_down, on_up, log
        self.bindings = {}
        self.held = set()
        self.task = None
        self.words = (0, 0)  # latest (low, high) button words
        self.capturing = False

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            self.task = None

    async def _run(self):
        loop = asyncio.get_running_loop()
        while True:
            path = find_controller()
            fd = None
            try:
                if not path:
                    raise OSError("controller not found")
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                q = asyncio.Queue(maxsize=8)

                def readable():
                    try:
                        data = os.read(fd, 64)
                    except BlockingIOError:
                        return
                    except OSError as e:
                        q.put_nowait(e)
                        return
                    if q.full():
                        q.get_nowait()  # only the latest state matters
                    q.put_nowait(data)

                loop.add_reader(fd, readable)
                try:
                    while True:
                        data = await q.get()
                        if isinstance(data, Exception):
                            raise data
                        await self._report(data)
                finally:
                    loop.remove_reader(fd)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # unplugged, suspended, permissions: try again shortly
                if self.log:
                    self.log(f"chord watcher: {e}")
                for role in list(self.held):
                    self.held.discard(role)
                    await self.on_up(role)
                await asyncio.sleep(3)
            finally:
                if fd is not None:
                    os.close(fd)

    async def capture(self, timeout=10.0, hold=1.0):
        """Wait for the user to hold a new button combination; returns its 'm:lo:hi' spec or None."""
        self.capturing = True
        for role in list(self.held):  # release anything bound so it doesn't fire meanwhile
            self.held.discard(role)
            await self.on_up(role)
        try:
            base_lo, base_hi = self.words
            deadline = time.monotonic() + timeout
            cand, since = None, 0.0
            while time.monotonic() < deadline:
                lo, hi = self.words
                cur = (lo & ~base_lo & ~NOISY_LOW, hi & ~base_hi & ~NOISY_HIGH)
                if cur != (0, 0) and cur == cand:
                    if time.monotonic() - since >= hold:
                        # wait for release so the combo doesn't trigger right after binding
                        while any(self.words[i] & cand[i] for i in range(2)) and time.monotonic() < deadline + 5:
                            await asyncio.sleep(0.05)
                        return f"m:{cand[0]:x}:{cand[1]:x}"
                elif cur != cand:
                    cand, since = cur, time.monotonic()
                await asyncio.sleep(0.03)
            return None
        finally:
            self.capturing = False

    async def _report(self, d):
        if len(d) < 16 or d[0] != 0x01 or d[2] != 0x09:
            return
        lo, hi = int.from_bytes(d[8:12], "little"), int.from_bytes(d[12:16], "little")
        self.words = (lo, hi)
        if self.capturing:
            return
        for role, chord in self.bindings.items():
            mlo, mhi = chord_masks(chord)
            down = bool(mlo or mhi) and (lo & mlo) == mlo and (hi & mhi) == mhi
            if down != (role in self.held):
                if down:
                    self.held.add(role)
                    await self.on_down(role)
                else:
                    self.held.discard(role)
                    await self.on_up(role)


# --- status ---------------------------------------------------------------------------

def battery():
    for bat in glob.glob("/sys/class/power_supply/BAT*"):
        try:
            cap = open(f"{bat}/capacity").read().strip()
            status = open(f"{bat}/status").read().strip().lower()
            return f"{cap}% ({status})"
        except OSError:
            continue
    return None
