import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  routerHook,
  toaster,
} from "@decky/api";
import {
  DialogButton,
  DropdownItem,
  Focusable,
  Menu,
  MenuItem,
  ModalRoot,
  Navigation,
  ToggleField,
  PanelSection,
  PanelSectionRow,
  QuickAccessTab,
  Router,
  TextField,
  afterPatch,
  appDetailsClasses,
  createReactTreePatcher,
  findInReactTree,
  findModuleExport,
  showContextMenu,
  showModal,
  staticClasses,
} from "@decky/ui";
import { CSSProperties, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Ask, AskCard, ghostBtn, primaryBtn } from "./asks";
import { Icon, IconName, toolIcon } from "./icons";
import { Markdown } from "./markdown";
import { SetupCard, SignInBanner, UpdateRow, onAuthEvent, pluginVersion } from "./setup";
import { SignInCard, installSignInBridge, pendingSignIns, qrDataUrl } from "./signin";
import { CSS, THEMES, ThemeId, installFonts, themeById, themeVars, v } from "./theme";

type Kind = "user" | "assistant" | "tool" | "tool_error" | "done" | "error" | "info" | "perm";
interface Entry {
  kind: Kind;
  text: string;
  name?: string;
  detail?: string;
  image?: boolean;
  ok?: boolean;
  ts: number;
}

interface UsageWindow {
  utilization?: number;
  resetsAt?: number;
}
interface Usage {
  status?: string;
  unifiedWindows?: { five_hour?: UsageWindow; seven_day?: UsageWindow };
}

type Mode = "ask" | "edits" | "plan" | "auto" | "trust";
type Model = "default" | "opus" | "fable" | "sonnet" | "haiku";

interface State {
  device_name: string;
  history: Entry[];
  running: boolean;
  asks: Ask[];
  usage: Usage | null;
  mode: Mode;
  model: Model;
  theme: ThemeId;
  voice: Voice;
  can_speak: boolean;
  speaking: boolean;
  remote_url: string | null;
  ptt: Ptt;
}

type Ptt = string; // "l4r4", "off", or a recorded "m:<low>:<high>" combo

type Voice = "off" | "voice" | "always";

const getState = callable<[], State>("get_state");
const send = callable<[prompt: string, context: Record<string, string>, screenshot: string | null, spoken: boolean], boolean>("send");
const setVoiceCall = callable<[voice: Voice], boolean>("set_voice");
const stopSpeaking = callable<[], boolean>("stop_speaking");
const setPttCall = callable<[chord: Ptt], boolean>("set_ptt");
const setDictateCall = callable<[chord: Ptt], boolean>("set_dictate");
const captureChord = callable<[role: string], { spec?: string; label?: string; error?: string }>("capture_chord");
const captionState = callable<[visible: boolean], boolean>("caption_state");
interface ChatItem {
  id: string;
  title?: string;
  game?: string;
  appid?: number;
  updated?: number;
  current?: boolean;
}
const listChats = callable<[], ChatItem[]>("list_chats");
const openChatCall = callable<[id: string], boolean>("open_chat");
const deleteChat = callable<[id: string], boolean>("delete_chat");
const setGameThreads = callable<[on: boolean], boolean>("set_game_threads");
const batteryStatus = callable<[], string | null>("battery_status");
interface Job {
  unit: string;
  state: string;
  sub: string;
  description: string;
}
const listJobs = callable<[], Job[]>("list_jobs");
const cancelJob = callable<[unit: string], boolean>("cancel_job");
const answerCall = callable<[id: string, decision: string, answers: Record<string, string> | null], boolean>("answer");
const setModeCall = callable<[mode: Mode], boolean>("set_mode");
const setModelCall = callable<[model: Model], boolean>("set_model");
const setThemeCall = callable<[theme: ThemeId], boolean>("set_theme");
const remoteCall = callable<[enabled: boolean], { url?: string; error?: string }>("remote");
const snap = callable<[], { path?: string; error?: string }>("snap");
const voiceAvailable = callable<[], boolean>("voice_available");
const micStart = callable<[], { ok?: boolean; error?: string }>("mic_start");
const micStop = callable<[], { text?: string; error?: string }>("mic_stop");
const stop = callable<[], boolean>("stop");
const newChat = callable<[], boolean>("new_chat");

const CHAT_ROUTE = "/claude-chat";

const PRESETS: { icon: IconName; text: string }[] = [
  { icon: "capture", text: "What's happening on my screen?" },
  { icon: "gauge", text: "Make this game run cooler and last longer" },
  { icon: "gamepad", text: "Recommend something to play tonight" },
];

// --- store ------------------------------------------------------------------------------
// Module-level so events keep arriving while no view is open.

let history: Entry[] = [];
let deviceName = "Steam Deck";
let running = false;
let streaming = ""; // the reply Claude is typing right now
let asks: Ask[] = [];
let usage: Usage | null = null;
let mode: Mode = "ask";
let model: Model = "default";
let theme: ThemeId = "claude";
let voice: Voice = "voice";
let canSpeak = false;
let speaking = false;
let pttChord: Ptt = "l4r4";
let dictateChord: Ptt = "l5r5";
let pttLabel = "L4 + R4";
let dictateLabel = "L5 + R5";
let gameThreads = true;
let chatGame: string | null = null;
let remoteUrl: string | null = null;
let notice = "";
let viewers = 0; // open views; toasts only when nobody is looking
let shot: string | null = null; // screenshot waiting to go with the next message
let mic: "off" | "rec" | "busy" = "off";
let autoSend = false; // send the transcript as soon as recording stops (Look & ask)
let micTarget: ((t: string) => void) | null = null; // where the transcript goes; null = send it
let hasVoice = false;
let chatDraft: ((t: string) => void) | null = null; // the full chat page's composer
const subscribers = new Set<() => void>();
const notify = () => subscribers.forEach((f) => f());

function applyState(s: State) {
  deviceName = s.device_name ?? "Steam Deck";
  history = s.history;
  running = s.running;
  asks = s.asks ?? [];
  usage = s.usage ?? null;
  mode = s.mode ?? "ask";
  model = s.model ?? "default";
  theme = s.theme ?? "claude";
  voice = s.voice ?? "voice";
  canSpeak = !!s.can_speak;
  speaking = !!s.speaking;
  pttChord = s.ptt ?? "l4r4";
  dictateChord = (s as any).dictate ?? "l5r5";
  pttLabel = (s as any).ptt_label ?? pttLabel;
  gameThreads = (s as any).game_threads ?? true;
  chatGame = (s as any).chat_game ?? null;
  dictateLabel = (s as any).dictate_label ?? dictateLabel;
  remoteUrl = s.remote_url ?? null;
}

function useStore() {
  const [, setTick] = useState(0);
  useEffect(() => {
    const f = () => setTick((t) => t + 1);
    subscribers.add(f);
    viewers++;
    getState().then((s) => {
      applyState(s);
      notify();
    });
    return () => {
      subscribers.delete(f);
      viewers--;
    };
  }, []);
}

const lastAssistant = () => [...history].reverse().find((e) => e.kind === "assistant");

function onEntry(entry: Entry) {
  history = [...history, entry];
  if (entry.kind !== "user") streaming = "";
  notice = "";
  if (!viewers && ptt === "idle" && (entry.kind === "done" || entry.kind === "error")) {
    toaster.toast({
      title: entry.kind === "error" ? "Claude hit a problem" : "Claude replied",
      body: (entry.kind === "error" ? entry.text : lastAssistant()?.text ?? entry.text).slice(0, 160),
      icon: <Icon name="spark" size={20} stroke={2.4} color={themeById(theme).accent} />,
      onClick: openChatFromToast,
    });
  }
  notify();
}

function onAsk(ask: Ask) {
  asks = [...asks.filter((a) => a.id !== ask.id), ask];
  if (!viewers || ptt !== "idle") {
    toaster.toast({
      title: ask.kind === "question" ? "Claude has a question" : ask.kind === "plan" ? "Claude has a plan" : "Claude needs your OK",
      body: ask.kind === "question" ? ask.questions?.[0]?.question ?? "" : ask.title || ask.description || ask.summary,
      icon: <Icon name="shield" size={20} color={themeById(theme).accent} />,
      onClick: openChatFromToast,
    });
  }
  notify();
}

const handlers: [string, (x: any) => void][] = [
  ["claude_event", onEntry],
  ["claude_delta", (t: string) => ((streaming += t), (notice = ""), notify())],
  ["claude_status", (r: boolean) => ((running = r), r || ((streaming = ""), (notice = "")), notify())],
  ["claude_ask", onAsk],
  ["claude_ask_done", (id: string) => ((asks = asks.filter((a) => a.id !== id)), notify())],
  ["claude_usage", (u: Usage) => ((usage = u), notify())],
  ["claude_notice", (t: string) => ((notice = t), notify())],
  ["claude_mode", (m: Mode) => ((mode = m), notify())],
  ["claude_model", (m: Model) => ((model = m), notify())],
  ["claude_speaking", (on: boolean) => ((speaking = on), notify())],
  ["claude_ptt", (e: any) => onPtt(e)],
  ["claude_chat", () => getState().then((st) => (applyState(st), (streaming = ""), notify()))],
  ["claude_auth", onAuthEvent],
  ["claude_setup", (e: any) => e?.done && !e.error && voiceAvailable().then((ok) => ((hasVoice = ok), notify()))],
];

// --- actions -----------------------------------------------------------------------------

function answer(id: string, decision: "allow" | "always" | "deny", answers?: Record<string, string>) {
  asks = asks.filter((a) => a.id !== id);
  notify();
  answerCall(id, decision, answers ?? null);
}

/** Deck status Claude gets with every message, so "this game" just works. */
function deckContext(): Record<string, string> {
  const ctx: Record<string, string> = {};
  try {
    const app = Router.MainRunningApp;
    ctx["Running game"] = app ? `${app.display_name} (appid ${app.appid})` : "none";
    const p = (window as any).SystemPerfStore?.msgSettingsPerApp;
    if (p) {
      ctx["Performance"] = [
        p.is_fps_limit_enabled ? `FPS cap ${p.fps_limit}` : "no FPS cap",
        `refresh ${p.display_refresh_manual_hz}Hz`,
        p.is_tdp_limit_enabled ? `TDP ${p.tdp_limit}W` : "no TDP limit",
      ].join(", ");
    }
  } catch (e) {
    console.warn("Claude: deck context failed", e);
  }
  return ctx;
}

async function submit(text: string, spoken = false) {
  if (!text.trim() && !shot) return;
  const s = shot;
  shot = null;
  notify();
  await send(text, deckContext(), s, spoken);
}

/** Reopen the Quick Access menu on Decky's tab, where this plugin stays the active panel. */
function reopenPanel() {
  Navigation.OpenQuickAccessMenu(QuickAccessTab.Decky);
  try {
    (window as any).DeckyPluginLoader?.deckyState?.setActivePlugin("Claude");
  } catch {
    // older Decky: the tab still opens, one tap away
  }
}

/** Close the menu so the game is visible, capture it, then come back. */
async function captureScreen(): Promise<boolean> {
  Navigation.CloseSideMenus();
  await new Promise((r) => setTimeout(r, 700));
  const r = await snap();
  reopenPanel();
  if (r.path) shot = r.path;
  else toaster.toast({ title: "Screenshot failed", body: r.error ?? "unknown error" });
  notify();
  return !!r.path;
}

async function startMic() {
  const r = await micStart().catch((e) => ({ error: String(e), ok: false }));
  if (r.error) {
    toaster.toast({ title: "Microphone unavailable", body: r.error });
    autoSend = false;
  } else mic = "rec";
  notify();
}

/** Stop recording; returns the transcript. */
async function finishMic(): Promise<string | null> {
  mic = "busy";
  notify();
  const r = await micStop().catch((e) => ({ error: String(e), text: undefined }));
  mic = "off";
  notify();
  if (r.error) toaster.toast({ title: "Voice input failed", body: r.error });
  else if (!r.text) toaster.toast({ title: "Didn't catch that", body: "Try again a little closer to the mic." });
  return r.text || null;
}

/** Start or finish talking. The transcript goes to `onText` (a composer) or is sent straight away. */
async function toggleMic(onText?: (t: string) => void) {
  if (mic === "busy") return;
  if (mic === "off") {
    micTarget = onText ?? null;
    return startMic();
  }
  const text = await finishMic();
  const go = autoSend;
  const target = micTarget;
  autoSend = false;
  micTarget = null;
  if (!text) {
    if (go) shot = null;
    notify();
    return;
  }
  if (go || !target) submit(text, true);
  else target(text);
}

/** Look & ask: grab the game screen, then listen. The question sends itself when you stop talking. */
async function lookAndAsk() {
  if (!(await captureScreen())) return;
  if (hasVoice) {
    autoSend = true;
    micTarget = null;
    await startMic();
  }
}

async function startOver() {
  await newChat();
  history = [];
  streaming = "";
  asks = [];
  remoteUrl = null;
  notify();
}

function openChat() {
  Navigation.Navigate(CHAT_ROUTE);
  Navigation.CloseSideMenus();
}

/** From a toast: Steam runs its own click action first, so navigate just after it. */
function openChatFromToast() {
  setTimeout(openChat, 250);
}

async function continueOnPhone() {
  if (remoteUrl) return showPhoneQr(remoteUrl);
  toaster.toast({ title: "Claude", body: "Linking this chat to the Claude app…" });
  const r = await remoteCall(true);
  if (!r.url) {
    toaster.toast({ title: "Couldn't link your phone", body: r.error ?? "Remote Control isn't available." });
    return;
  }
  remoteUrl = r.url;
  notify();
  showPhoneQr(r.url);
}

function showPhoneQr(url: string) {
  const qr = qrDataUrl(url);
  const modal = showModal(
    <ModalRoot onCancel={() => modal.Close()} onEscKeypress={() => modal.Close()}>
      <div className="cd-root" style={{ ...(themeVars(themeById(theme)) as CSSProperties), display: "flex", gap: "24px", alignItems: "center" }}>
        {qr && <img src={qr} style={{ width: "200px", height: "200px", imageRendering: "pixelated", background: "#fff", borderRadius: "12px", padding: "6px" }} />}
        <div>
          <div style={{ fontFamily: v("display"), fontSize: "24px", fontWeight: 500, marginBottom: "8px" }}>Continue on your phone</div>
          <div style={{ fontSize: "14px", color: v("dim"), marginBottom: "14px", lineHeight: 1.5 }}>
            Scan with your phone's camera. The chat opens in the Claude app and stays live here; approvals work from either.
          </div>
          <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px" }}>
            <DialogButton className="cd-btn" style={primaryBtn} onClick={() => modal.Close()}>
              <Icon name="check" size={16} />
              Done
            </DialogButton>
            <DialogButton
              className="cd-btn"
              style={ghostBtn}
              onClick={async () => {
                await remoteCall(false);
                remoteUrl = null;
                notify();
                modal.Close();
              }}
            >
              <Icon name="x" size={16} />
              Unlink
            </DialogButton>
          </Focusable>
        </div>
      </div>
    </ModalRoot>,
  );
}

function ago(ts?: number) {
  if (!ts) return "";
  const m = (Date.now() / 1000 - ts) / 60;
  if (m < 1) return "just now";
  if (m < 60) return `${Math.round(m)} min ago`;
  if (m < 1440) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} d ago`;
}

function ChatsList({ close }: { close: () => void }) {
  const [chats, setChats] = useState<ChatItem[] | null>(null);
  useEffect(() => {
    listChats().then(setChats);
  }, []);
  return (
    <div className="cd-root" style={{ ...(themeVars(themeById(theme)) as CSSProperties), display: "flex", flexDirection: "column", gap: "10px", minWidth: "560px" }}>
      <div style={{ fontFamily: v("display"), fontSize: "22px" }}>Chats</div>
      {chats === null ? (
        <div style={{ color: v("dim") }}>Loading…</div>
      ) : chats.length === 0 ? (
        <div style={{ color: v("dim") }}>No past chats yet.</div>
      ) : (
        <Focusable style={{ display: "flex", flexDirection: "column", gap: "6px", maxHeight: "60vh", overflowY: "auto" }}>
          {chats.map((c) => (
            <Focusable key={c.id} flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <DialogButton
                className="cd-btn"
                style={{ ...ghostBtn, flex: 1, height: "auto", minHeight: "48px", padding: "8px 14px", textAlign: "left", background: c.current ? v("surface2") : "transparent" }}
                onClick={async () => {
                  await openChatCall(c.id);
                  close();
                  openChat();
                }}
              >
                <span style={{ color: c.game ? v("accent") : v("dim") }}>
                  <Icon name={c.game ? "gamepad" : "spark"} size={16} />
                </span>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.title || "Chat"}</div>
                  <div style={{ fontSize: "12px", color: v("dim") }}>
                    {[c.game, c.current ? "current" : ago(c.updated)].filter(Boolean).join(" · ")}
                  </div>
                </div>
              </DialogButton>
              {!c.current && (
                <DialogButton
                  className="cd-btn"
                  style={{ ...iconBtn, width: "40px", height: "40px" }}
                  onClick={() => deleteChat(c.id).then(() => setChats((cur) => (cur ?? []).filter((x) => x.id !== c.id)))}
                >
                  <Icon name="x" size={16} />
                </DialogButton>
              )}
            </Focusable>
          ))}
        </Focusable>
      )}
    </div>
  );
}

function showChats() {
  const modal = showModal(
    <ModalRoot onCancel={() => modal.Close()} onEscKeypress={() => modal.Close()}>
      <ChatsList close={() => modal.Close()} />
    </ModalRoot>,
  );
}

// --- options -------------------------------------------------------------------------------

const MODES: { id: Mode; label: string; icon: IconName; desc: string }[] = [
  { id: "ask", label: "Ask first", icon: "shield", desc: "Approve anything risky with a card." },
  { id: "edits", label: "Accept edits", icon: "pencil", desc: "File edits run without asking; commands still ask." },
  { id: "plan", label: "Plan first", icon: "plan", desc: "Claude proposes a plan before changing anything." },
  { id: "auto", label: "Auto", icon: "auto", desc: "Claude's safety check approves routine actions." },
  { id: "trust", label: "Full trust", icon: "bolt", desc: "Nothing asks. Hard blocks (sudo, disk wipes) still apply." },
];
const MODELS: { id: Model; label: string; desc: string }[] = [
  { id: "default", label: "Default", desc: "Claude Code's recommended model." },
  { id: "fable", label: "Fable", desc: "Most capable; slowest, uses the most of your limit." },
  { id: "opus", label: "Opus", desc: "Very capable and quicker." },
  { id: "sonnet", label: "Sonnet", desc: "Fast and easy on your usage limit." },
  { id: "haiku", label: "Haiku", desc: "Quickest, for simple questions." },
];
const modeInfo = (m: Mode) => MODES.find((x) => x.id === m) ?? MODES[0];
const modelInfo = (m: Model) => MODELS.find((x) => x.id === m) ?? MODELS[0];

function chooseMode(m: Mode) {
  mode = m;
  notify();
  setModeCall(m);
}

function chooseModel(m: Model) {
  if (m === model) return;
  const prev = model;
  model = m;
  notify();
  setModelCall(m).then((ok) => {
    if (!ok) {
      model = prev;
      notify();
    }
    toaster.toast({ title: "Claude", body: ok ? `Now using ${modelInfo(m).label}` : `Couldn't switch to ${modelInfo(m).label}` });
  });
}

function chooseVoice(x: Voice) {
  voice = x;
  if (x === "off") speaking = false;
  notify();
  setVoiceCall(x);
}

function chooseTheme(t: ThemeId) {
  theme = t;
  notify();
  setThemeCall(t);
}

function menu<T extends string>(label: string, items: { id: T; label: string }[], current: T, pick: (id: T) => void) {
  return (e: any) =>
    showContextMenu(
      <Menu label={label}>
        {items.map((i) => (
          <MenuItem key={i.id} onSelected={() => pick(i.id)}>
            {i.id === current ? "●  " : "    "}
            {i.label}
          </MenuItem>
        ))}
      </Menu>,
      e?.currentTarget ?? window,
    );
}

// --- building blocks ---------------------------------------------------------------------------

function Root({ children, style }: { children: any; style?: CSSProperties }) {
  return (
    <div className="cd-root" style={{ ...(themeVars(themeById(theme)) as CSSProperties), ...style }}>
      <style>{CSS}</style>
      {children}
    </div>
  );
}

function Spark({ size = 18, busy }: { size?: number; busy?: boolean }) {
  return (
    <span className={busy ? "cd-think" : undefined} style={{ display: "inline-flex", color: v("accent") }}>
      <Icon name="spark" size={size} stroke={2.4} />
    </span>
  );
}

function Avatar() {
  return (
    <div
      style={{
        width: "28px",
        height: "28px",
        flexShrink: 0,
        borderRadius: v("r-sm"),
        background: v("surface"),
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        marginTop: "2px",
      }}
    >
      <Spark size={16} />
    </div>
  );
}

const iconBtn: CSSProperties = {
  boxSizing: "border-box",
  width: "40px",
  height: "40px",
  minWidth: 0,
  padding: 0,
  borderRadius: v("r-sm"),
  background: "transparent",
  color: v("dim"),
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  border: "none",
};

const pill: CSSProperties = {
  boxSizing: "border-box",
  height: "36px",
  minWidth: 0,
  width: "auto",
  padding: "0 12px",
  borderRadius: v("r-pill"),
  border: `1px solid ${v("line")}`,
  background: v("surface"),
  color: v("text"),
  fontSize: "13px",
  fontFamily: v("font"),
  display: "flex",
  alignItems: "center",
  gap: "8px",
};

function UsageRing({ w }: { w?: UsageWindow }) {
  if (w?.utilization == null) return null;
  const pct = Math.round(w.utilization * 100);
  const c = 2 * Math.PI * 14;
  const color = pct >= 90 ? v("err") : pct >= 75 ? v("warn") : v("accent");
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "8px" }} aria-label={`Session usage ${pct} percent`}>
      <svg width="28" height="28" viewBox="0 0 36 36">
        <circle cx="18" cy="18" r="14" fill="none" stroke={v("surface2")} strokeWidth="4" />
        <circle
          cx="18"
          cy="18"
          r="14"
          fill="none"
          stroke={color}
          strokeWidth="4"
          strokeLinecap="round"
          strokeDasharray={`${(c * Math.min(pct, 100)) / 100} ${c}`}
          transform="rotate(-90 18 18)"
          style={{ transition: "stroke-dasharray .5s" }}
        />
      </svg>
      <span style={{ fontSize: "12px", color: v("dim"), lineHeight: 1.2 }}>
        {pct}%<br />
        session
      </span>
    </div>
  );
}

function resetLabel(ts?: number) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const hrs = (d.getTime() - Date.now()) / 3.6e6;
  if (hrs < 0) return "";
  return hrs < 24
    ? `resets ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : `resets ${d.toLocaleDateString([], { weekday: "short" })}`;
}

function Meter({ label, w }: { label: string; w?: UsageWindow }) {
  if (w?.utilization == null) return null;
  const pct = Math.round(w.utilization * 100);
  const color = pct >= 90 ? v("err") : pct >= 75 ? v("warn") : v("accent");
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", color: v("dim") }}>
        <span>
          {label} · {pct}%
        </span>
        <span>{resetLabel(w.resetsAt)}</span>
      </div>
      <div style={{ height: "5px", borderRadius: "3px", background: v("surface2"), marginTop: "5px" }}>
        <div style={{ width: `${Math.min(pct, 100)}%`, height: "100%", borderRadius: "3px", background: color, transition: "width .5s" }} />
      </div>
    </div>
  );
}

// --- conversation model -----------------------------------------------------------------------

type Block = { t: "say"; e: Entry } | { t: "steps"; items: Entry[] } | { t: "note"; e: Entry };
interface Turn {
  user?: Entry;
  blocks: Block[];
}

/** Split history into turns: a user message, then Claude's text and grouped tool steps. */
function toTurns(entries: Entry[]): Turn[] {
  const turns: Turn[] = [];
  let cur: Turn = { blocks: [] };
  const flush = () => {
    if (cur.user || cur.blocks.length) turns.push(cur);
  };
  for (const e of entries) {
    if (e.kind === "user") {
      flush();
      cur = { user: e, blocks: [] };
    } else if (e.kind === "tool" || e.kind === "perm" || e.kind === "tool_error") {
      const last = cur.blocks[cur.blocks.length - 1];
      if (last?.t === "steps") last.items.push(e);
      else cur.blocks.push({ t: "steps", items: [e] });
    } else if (e.kind === "assistant") cur.blocks.push({ t: "say", e });
    else if (e.kind === "error" || e.kind === "info") cur.blocks.push({ t: "note", e });
  }
  flush();
  return turns;
}

function StepsRow({ items, live }: { items: Entry[]; live: boolean }) {
  const [open, setOpen] = useState(false);
  const steps = items.filter((i) => i.kind === "tool");
  const failed = items.some((i) => i.kind === "tool_error" || (i.kind === "perm" && !i.ok));
  const label = steps[steps.length - 1]?.text || "Working";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
      <DialogButton
        className="cd-btn"
        style={{ ...pill, alignSelf: "flex-start", maxWidth: "100%", background: v("sunken"), color: v("body"), height: "34px", borderRadius: v("r-sm") }}
        onClick={() => setOpen(!open)}
      >
        {live ? <Spark size={15} busy /> : <Icon name={failed ? "alert" : "check"} size={15} color={failed ? v("warn") : v("ok")} />}
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}</span>
        {steps.length > 1 && <span style={{ color: v("faint"), whiteSpace: "nowrap" }}>· {steps.length} steps</span>}
        <Icon name={open ? "chevronDown" : "chevronRight"} size={14} color={v("faint")} />
      </DialogButton>
      {open && (
        <div className="cd-fade" style={{ display: "flex", flexDirection: "column", gap: "2px", paddingLeft: "6px" }}>
          {items.map((i, n) => (
            <Focusable key={n} onActivate={() => {}} className="cd-focus-soft" style={{ display: "flex", gap: "10px", alignItems: "flex-start", padding: "5px 6px" }}>
              <span style={{ color: i.kind === "tool" ? v("dim") : i.ok ? v("ok") : v("warn"), marginTop: "2px" }}>
                <Icon name={i.kind === "tool" ? toolIcon(i.name) : i.ok ? "check" : "alert"} size={14} />
              </span>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ fontSize: "13px", color: i.kind === "tool" ? v("body") : i.ok ? v("dim") : v("warn") }}>{i.text}</div>
                {i.detail && i.detail !== i.text && (
                  <div style={{ fontFamily: v("mono"), fontSize: "11.5px", color: v("faint"), overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {i.detail}
                  </div>
                )}
              </div>
            </Focusable>
          ))}
        </div>
      )}
    </div>
  );
}

function UserBubble({ e }: { e: Entry }) {
  return (
    <Focusable onActivate={() => {}} className="cd-msg" style={{ display: "flex", justifyContent: "flex-end" }}>
      <div
        style={{
          maxWidth: "70%",
          background: v("surface2"),
          color: v("text"),
          borderRadius: `${v("r")} ${v("r")} 6px ${v("r")}`,
          padding: "10px 16px",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          display: "flex",
          gap: "8px",
          alignItems: "flex-start",
        }}
      >
        {e.image && (
          <span style={{ color: v("accent"), marginTop: "3px" }}>
            <Icon name="capture" size={15} />
          </span>
        )}
        {e.text}
      </div>
    </Focusable>
  );
}

function Note({ e }: { e: Entry }) {
  if (e.kind === "info") return <div style={{ textAlign: "center", fontSize: "12px", color: v("faint"), padding: "2px 0" }}>{e.text}</div>;
  return (
    <div
      style={{ display: "flex", gap: "10px", alignItems: "flex-start", color: v("err"), fontSize: "13.5px", background: v("sunken"), borderRadius: v("r-sm"), padding: "10px 12px" }}
    >
      <Icon name="alert" size={16} />
      <span style={{ color: v("body"), wordBreak: "break-word" }}>{e.text}</span>
    </div>
  );
}

function Thinking() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "10px", color: v("dim"), fontSize: "13.5px", height: "28px" }}>
      <Spark size={15} busy />
      {notice || "Thinking"}
    </div>
  );
}

function TurnView({ turn, live }: { turn: Turn; live: boolean }) {
  const tail = live && running;
  const lastBlock = turn.blocks[turn.blocks.length - 1];
  return (
    <>
      {turn.user && <UserBubble e={turn.user} />}
      {(turn.blocks.length > 0 || tail) && (
        <div className="cd-msg" style={{ display: "flex", gap: "14px" }}>
          <Avatar />
          <div style={{ display: "flex", flexDirection: "column", gap: "12px", minWidth: 0, flex: 1 }}>
            {turn.blocks.map((b, i) =>
              b.t === "say" ? (
                <Focusable key={i} style={{ color: v("body"), lineHeight: 1.55 }}>
                  <Markdown text={b.e.text} accent={v("accent")} />
                </Focusable>
              ) : b.t === "steps" ? (
                <StepsRow key={i} items={b.items} live={tail && b === lastBlock && !streaming} />
              ) : (
                <Note key={i} e={b.e} />
              ),
            )}
            {tail && streaming && (
              <div style={{ color: v("body"), lineHeight: 1.55 }}>
                <Markdown text={streaming} accent={v("accent")} focusable={false} />
                <span className="cd-caret" />
              </div>
            )}
            {tail && !streaming && lastBlock?.t !== "steps" && !asks.length && <Thinking />}
          </div>
        </div>
      )}
    </>
  );
}

// --- composer -------------------------------------------------------------------------------------

function Waveform({ label }: { label: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "3px", flex: 1, minWidth: 0 }}>
      {[0, 1, 2, 3, 4].map((i) => (
        <span key={i} className="cd-bar" style={{ animationDelay: `${i * 120}ms` }} />
      ))}
      <span style={{ marginLeft: "10px", color: v("dim"), fontSize: "13.5px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
    </div>
  );
}

function Composer({ compact }: { compact?: boolean }) {
  const [input, setInput] = useState("");
  const spokenDraft = useRef(false);
  const addSpoken = (t: string) => {
    spokenDraft.current = true;
    setInput((cur) => (cur.trim() ? `${cur.trim()} ${t}` : t));
  };
  useEffect(() => {
    if (compact) return;
    chatDraft = addSpoken;
    return () => {
      if (chatDraft === addSpoken) chatDraft = null;
    };
  }, []);
  const go = () => {
    const t = input;
    setInput("");
    submit(t, spokenDraft.current);
    spokenDraft.current = false;
  };
  const canSend = !!(input.trim() || shot);
  const listening = mic !== "off";
  return (
    <Focusable
      flow-children="horizontal"
      style={{
        display: "flex",
        boxSizing: "border-box",
        alignItems: "center",
        gap: "4px",
        height: compact ? "48px" : "56px",
        padding: compact ? "0 4px 0 12px" : "0 8px 0 16px",
        borderRadius: v("r"),
        background: v("surface"),
        border: `1px solid ${listening ? `color-mix(in srgb, ${v("accent")} 55%, transparent)` : v("line")}`,
        transition: "border-color .2s",
        width: "100%",
      }}
    >
      {listening ? (
        <Waveform label={mic === "busy" ? "Transcribing…" : autoSend ? "Listening… I'll send it with the screenshot" : "Listening…"} />
      ) : (
        <div className="cd-field" style={{ flex: 1, minWidth: 0 }}>
          <TextField
            value={input}
            focusOnMount={!compact}
            {...({ placeholder: running ? "Reply (sent when Claude finishes)" : compact ? "Ask Claude" : "Message Claude" } as object)}
            onChange={(e) => {
              setInput(e.target.value);
              if (!e.target.value) spokenDraft.current = false;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") go();
            }}
          />
        </div>
      )}
      {!listening && !compact && (
        <DialogButton className="cd-btn" style={iconBtn} onClick={() => captureScreen()} aria-label="Attach screenshot">
          <Icon name="capture" size={19} />
        </DialogButton>
      )}
      {hasVoice && (
        <DialogButton
          className="cd-btn"
          style={{ ...iconBtn, color: listening ? v("on-accent") : v("dim"), background: listening ? v("accent") : "transparent" }}
          onClick={() => toggleMic(compact ? undefined : addSpoken)}
          aria-label={listening ? "Done talking" : "Voice input"}
        >
          <Icon name={listening ? "check" : "mic"} size={19} />
        </DialogButton>
      )}
      {!listening && (
        <DialogButton
          className="cd-btn"
          style={{ ...iconBtn, background: canSend || running ? v("text") : v("surface2"), color: canSend || running ? v("bg") : v("faint") }}
          onClick={() => (canSend ? go() : running && stop())}
          aria-label={canSend || !running ? "Send" : "Stop"}
        >
          {canSend || !running ? (
            <Icon name="send" size={18} stroke={2.2} />
          ) : (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
              <rect x="4" y="4" width="16" height="16" rx="3" />
            </svg>
          )}
        </DialogButton>
      )}
    </Focusable>
  );
}

function ShotChip() {
  if (!shot) return null;
  return (
    <Focusable flow-children="horizontal" className="cd-fade" style={{ display: "flex", alignItems: "center", gap: "10px", fontSize: "13px", color: v("dim"), margin: "0 0 8px" }}>
      <span style={{ color: v("accent") }}>
        <Icon name="capture" size={16} />
      </span>
      <span style={{ flex: 1 }}>Screenshot attached. Ask about it, or just send.</span>
      <DialogButton
        className="cd-btn"
        style={{ ...iconBtn, width: "32px", height: "32px" }}
        onClick={() => {
          shot = null;
          notify();
        }}
        aria-label="Remove screenshot"
      >
        <Icon name="x" size={15} />
      </DialogButton>
    </Focusable>
  );
}

// --- full-screen chat page ---------------------------------------------------------------------

function ChatPage() {
  useStore();
  const scroller = useRef<HTMLDivElement>(null);
  const turns = toTurns(history);
  const toBottom = () => scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [history.length, streaming, running, asks.length, pendingSignIns().length]);

  const app = Router.MainRunningApp;
  const status = asks.length ? "Waiting for you" : running ? "Working" : speaking ? "Speaking" : mic === "rec" ? "Listening" : "Ready";

  return (
    <Root style={{ marginTop: "40px", height: "calc(100% - 40px)", background: v("bg") }}>
      <Focusable
        style={{ height: "100%", display: "flex", flexDirection: "column" }}
        onSecondaryButton={hasVoice ? () => toggleMic(chatDraft ?? undefined) : undefined}
        onSecondaryActionDescription={hasVoice ? (mic === "off" ? "Talk" : "Done talking") : undefined}
        onOptionsButton={toBottom}
        onOptionsActionDescription="Latest"
      >
        <Focusable
          flow-children="horizontal"
          style={{ height: "64px", flexShrink: 0, padding: "0 28px", display: "flex", alignItems: "center", gap: "10px", borderBottom: `1px solid ${v("line")}` }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "12px", flex: 1, minWidth: 0 }}>
            <div style={{ width: "34px", height: "34px", borderRadius: v("r-sm"), background: v("surface"), display: "flex", alignItems: "center", justifyContent: "center" }}>
              <Spark size={20} busy={running} />
            </div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontFamily: v("display"), fontSize: "20px", fontWeight: 500, lineHeight: 1.1 }}>Claude</div>
              <div style={{ fontSize: "12px", color: asks.length ? v("accent") : v("dim"), whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {status}
                {chatGame ? ` · ${chatGame} chat` : app ? ` · ${app.display_name}` : ""}
              </div>
            </div>
          </div>
          {speaking && (
            <DialogButton className="cd-btn cd-fade" style={{ ...pill, color: v("accent") }} onClick={() => stopSpeaking()}>
              <Icon name="volume" size={16} />
              Stop voice
            </DialogButton>
          )}
          <DialogButton className="cd-btn" style={pill} onClick={menu("Model", MODELS, model, chooseModel)}>
            <Icon name="chip" size={16} />
            {modelInfo(model).label}
            <Icon name="chevronDown" size={14} color={v("faint")} />
          </DialogButton>
          <DialogButton className="cd-btn" style={pill} onClick={menu("Mode", MODES, mode, chooseMode)}>
            <Icon name={modeInfo(mode).icon} size={16} />
            {modeInfo(mode).label}
            <Icon name="chevronDown" size={14} color={v("faint")} />
          </DialogButton>
          <div style={{ padding: "0 6px" }}>
            <UsageRing w={usage?.unifiedWindows?.five_hour} />
          </div>
          <DialogButton className="cd-btn" style={{ ...iconBtn, color: remoteUrl ? v("accent") : v("dim") }} onClick={continueOnPhone} aria-label="Continue on phone">
            <Icon name="phone" size={20} />
          </DialogButton>
          <DialogButton className="cd-btn" style={iconBtn} onClick={showChats} aria-label="Chats">
            <Icon name="plan" size={20} />
          </DialogButton>
          <DialogButton className="cd-btn" style={iconBtn} onClick={startOver} aria-label="New chat">
            <Icon name="pencil" size={20} />
          </DialogButton>
        </Focusable>

        <div ref={scroller} style={{ flex: 1, overflowY: "auto", display: "flex", justifyContent: "center" }}>
          <div style={{ width: "780px", maxWidth: "calc(100% - 48px)", padding: "28px 0 16px", display: "flex", flexDirection: "column", gap: "18px" }}>
            <SignInBanner />
            {turns.length === 0 && !running && (
              <div className="cd-fade" style={{ textAlign: "center", marginTop: "64px", display: "flex", flexDirection: "column", alignItems: "center", gap: "10px" }}>
                <Spark size={40} />
                <div style={{ fontFamily: v("display"), fontSize: "30px", fontWeight: 400, margin: "6px 0 2px" }}>What are we playing today?</div>
                <div style={{ color: v("dim"), fontSize: "14px", marginBottom: "18px" }}>Ask anything, or let Claude look at your screen.</div>
                <Focusable flow-children="horizontal" style={{ display: "flex", gap: "10px", justifyContent: "center", flexWrap: "wrap" }}>
                  {PRESETS.map((p) => (
                    <DialogButton key={p.text} className="cd-btn" style={{ ...pill, height: "42px", padding: "0 16px" }} onClick={() => submit(p.text)}>
                      <span style={{ color: v("accent") }}>
                        <Icon name={p.icon} size={16} />
                      </span>
                      {p.text}
                    </DialogButton>
                  ))}
                </Focusable>
              </div>
            )}
            {turns.map((t, i) => (
              <TurnView key={t.user?.ts ?? `t${i}`} turn={t} live={i === turns.length - 1} />
            ))}
            {pendingSignIns().map((r) => (
              <SignInCard key={r.id} req={r} notify={notify} />
            ))}
            {asks.map((a) => (
              <div key={a.id} style={{ paddingLeft: "42px" }}>
                <AskCard ask={a} answer={answer} />
              </div>
            ))}
          </div>
        </div>

        <div style={{ flexShrink: 0, display: "flex", justifyContent: "center", padding: "8px 0 56px" }}>
          <div style={{ width: "780px", maxWidth: "calc(100% - 48px)" }}>
            <ShotChip />
            <Composer />
          </div>
        </div>
      </Focusable>
    </Root>
  );
}

// --- hold-to-talk caption over games ------------------------------------------------------------

type PttPhase = "idle" | "listening" | "dictating" | "typed" | "transcribing" | "thinking" | "answer" | "error";
let ptt: PttPhase = "idle";
let pttQuestion = "";
let pttError = "";
let pttFrom = 0; // history length when the question was sent; the answer is what comes after
let pttHide: ReturnType<typeof setTimeout> | null = null;

function pttSet(phase: PttPhase, hideAfterMs?: number) {
  if ((phase === "idle") !== (ptt === "idle")) captionState(phase !== "idle").catch(() => {});
  ptt = phase;
  if (pttHide) clearTimeout(pttHide);
  pttHide = hideAfterMs ? setTimeout(() => pttSet("idle"), hideAfterMs) : null;
  notify();
}

function onPtt(ev: { phase: string; text?: string; shot?: string | null }) {
  if (ev.phase === "listening") {
    pttQuestion = "";
    pttSet("listening");
  } else if (ev.phase === "dictating") {
    pttQuestion = "";
    pttSet("dictating");
  } else if (ev.phase === "dictated" && ev.text) {
    pttQuestion = ev.text;
    const ok = typeText(ev.text);
    if (!ok) pttError = "Couldn't type here. Open a text box first.";
    pttSet(ok ? "typed" : "error", ok ? 1800 : 3500);
  } else if (ev.phase === "transcribing") pttSet("transcribing");
  else if (ev.phase === "idle" || ev.phase === "dismiss") pttSet("idle");
  else if (ev.phase === "error") {
    pttError = ev.text || "Something went wrong";
    pttSet("error", 4000);
  } else if (ev.phase === "heard" && ev.text) {
    pttQuestion = ev.text;
    pttFrom = history.length;
    pttSet("thinking");
    const inGame = !!Router.MainRunningApp;
    send(ev.text, deckContext(), inGame ? ev.shot ?? null : null, true);
  }
}

/** The reply to the hold-to-talk question so far. */
function pttAnswer(): string {
  const said = history
    .slice(pttFrom)
    .filter((e) => e.kind === "assistant")
    .map((e) => e.text)
    .join("\n\n");
  return running && streaming ? `${said ? said + "\n\n" : ""}${streaming}` : said;
}

// Keep the caption in step with the conversation: show the answer, then fade once it's been
// read (or spoken).
function pttTick() {
  if (ptt === "thinking" && (streaming || pttAnswer())) pttSet("answer");
  if ((ptt === "thinking" || ptt === "answer") && !running && history.length > pttFrom + 1) {
    if (ptt === "thinking") ptt = "answer";
    if (speaking) {
      if (pttHide) clearTimeout(pttHide);
      pttHide = null;
    } else if (!pttHide) {
      const words = pttAnswer().split(/\s+/).length;
      pttSet("answer", Math.min(20000, Math.max(6000, words * 330)));
    }
  }
}

let useComposition: ((state: number, name: string) => void) | null | undefined;
function compositionHook() {
  if (useComposition === undefined) {
    try {
      useComposition =
        findModuleExport(
          (e: any) => typeof e === "function" && /AddMinimumCompositionStateRequest/.test(e.toString()) && !e.toString().startsWith("class"),
        ) ?? null;
    } catch {
      useComposition = null;
    }
  }
  return useComposition;
}

/** Asks Steam to draw its window over the running game (like a notification), without taking input. */
function OverGame() {
  const hook = compositionHook();
  hook?.(1 /* Notification */, "ClaudeCaption");
  return null;
}

function Caption() {
  useStore();
  useEffect(pttTick);
  if (ptt === "idle") return null;
  const answer = ptt === "answer" ? pttAnswer() : "";
  const short = answer.length > 360 ? answer.slice(0, 360).replace(/\s+\S*$/, "") + "…" : answer;
  const needsYou = asks.length > 0 && (ptt === "thinking" || ptt === "answer");
  return (
    <Root
      style={{
        position: "fixed",
        top: "18px",
        left: "50%",
        transform: "translateX(-50%)",
        width: "min(720px, calc(100vw - 48px))",
        zIndex: 99999,
        pointerEvents: "none",
      }}
    >
      <OverGame />
      <div
        className="cd-card"
        style={{
          boxSizing: "border-box",
          background: `color-mix(in srgb, ${v("bg")} 90%, transparent)`,
          backdropFilter: "blur(12px)",
          border: `1px solid ${ptt === "listening" || ptt === "dictating" ? v("accent") : v("line")}`,
          borderRadius: v("r"),
          padding: "14px 18px",
          boxShadow: "0 12px 40px rgba(0,0,0,.45)",
          display: "flex",
          gap: "14px",
          alignItems: "flex-start",
        }}
      >
        <div style={{ marginTop: "2px" }}>
          <Spark size={20} busy={ptt === "transcribing" || ptt === "thinking" || (ptt === "answer" && running)} />
        </div>
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: "6px" }}>
          {ptt === "listening" && <Waveform label="Listening… let go to send" />}
          {ptt === "dictating" && <Waveform label="Dictating… let go to type it" />}
          {ptt === "typed" && (
            <div style={{ color: v("body"), fontSize: "15px", display: "flex", gap: "8px", alignItems: "center" }}>
              <Icon name="check" size={16} color={v("ok")} />
              Typed “{pttQuestion}”
            </div>
          )}
          {ptt === "transcribing" && <div style={{ color: v("dim"), fontSize: "15px" }}>Got it…</div>}
          {ptt === "error" && <div style={{ color: v("warn"), fontSize: "15px" }}>{pttError}</div>}
          {pttQuestion && (ptt === "transcribing" || ptt === "thinking" || ptt === "answer") && (
            <div style={{ color: v("dim"), fontSize: "13px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>“{pttQuestion}”</div>
          )}
          {ptt === "thinking" && !needsYou && <div style={{ color: v("body"), fontSize: "15px" }}>{notice || "Thinking"}</div>}
          {needsYou && (
            <div style={{ color: v("accent"), fontSize: "15px", display: "flex", alignItems: "center", gap: "8px" }}>
              <Icon name="shield" size={16} />
              Claude needs your OK. Open the Claude menu to answer.
            </div>
          )}
          {short && (
            <div style={{ color: v("text"), fontSize: "16px", lineHeight: 1.45 }}>
              <Markdown text={short} accent={v("accent")} focusable={false} />
            </div>
          )}
        </div>
        {speaking && ptt === "answer" && (
          <span style={{ color: v("accent"), marginTop: "2px" }}>
            <Icon name="volume" size={18} />
          </span>
        )}
      </div>
      {(ptt === "answer" || ptt === "thinking" || ptt === "error") && (
        <div className="cd-fade" style={{ textAlign: "right", fontSize: "11px", color: v("faint"), marginTop: "6px", paddingRight: "6px", textShadow: "0 1px 3px rgba(0,0,0,.8)" }}>
          Press {pttLabel} to dismiss
        </div>
      )}
    </Root>
  );
}

// --- Steam integration: game pages, game sessions, screenshots ---------------------------------

const GAME_PROMPTS: { icon: IconName; label: string; prompt: (name: string) => string }[] = [
  { icon: "gauge", label: "Make it run better", prompt: (n) => `Make ${n} run as well as it can on this ${deviceName}: performance settings, Proton version and launch options. Check what's known about it first.` },
  { icon: "alert", label: "Fix a problem", prompt: (n) => `${n} isn't working right. Ask me what's happening, then find out why and fix it.` },
  { icon: "layers", label: "Mods and tweaks", prompt: (n) => `What are the best mods or tweaks for ${n} on the ${deviceName}? Offer to set them up.` },
  { icon: "question", label: "Is it worth playing?", prompt: (n) => `Should I play ${n}? Tell me what it's like, how long it is and how it runs on the ${deviceName}, in a few sentences.` },
];

function askAboutGame(appid: number, name: string, prompt: string) {
  openChat();
  setTimeout(() => submit(prompt.replace(name, `${name} (appid ${appid})`)), 300);
}

function AskOnGamePage({ overview }: { overview: any }) {
  useStore();
  const name = overview?.display_name ?? "this game";
  const appid = overview?.appid;
  const open = (e: any) =>
    showContextMenu(
      <Menu label={`Ask Claude about ${name}`}>
        {GAME_PROMPTS.map((p) => (
          <MenuItem key={p.label} onSelected={() => askAboutGame(appid, name, p.prompt(name))}>
            {p.label}
          </MenuItem>
        ))}
        <MenuItem onSelected={() => askAboutGame(appid, name, `About ${name}: `.trim())}>Something else…</MenuItem>
      </Menu>,
      e?.currentTarget ?? window,
    );
  return (
    <Root style={{ position: "absolute", top: "56px", right: "24px", zIndex: 7 }}>
      <DialogButton
        className="cd-btn"
        style={{ ...pill, height: "38px", padding: "0 14px", background: `color-mix(in srgb, ${v("bg")} 80%, transparent)`, backdropFilter: "blur(8px)" }}
        onClick={open}
      >
        <Spark size={16} />
        Ask Claude
      </DialogButton>
    </Root>
  );
}

function patchGamePage() {
  return routerHook.addPatch("/library/app/:appid", (tree: any) => {
    const routeProps = findInReactTree(tree, (x: any) => x?.renderFunc);
    if (routeProps) {
      let overview: any;
      const handler = createReactTreePatcher(
        [
          (t: any) => {
            const node = findInReactTree(t, (x: any) => x?.props?.children?.props?.overview);
            overview = node?.props?.children?.props?.overview;
            return node?.props?.children;
          },
        ],
        (_: any, ret: any) => {
          const container = findInReactTree(
            ret,
            (x: any) => Array.isArray(x?.props?.children) && x?.props?.className?.includes(appDetailsClasses.InnerContainer),
          );
          if (container && !container.props.children.some((c: any) => c?.key === "claude-ask")) {
            container.props.children.splice(1, 0, <AskOnGamePage key="claude-ask" overview={overview} />);
          }
          return ret;
        },
        "ClaudeGamePage",
      );
      afterPatch(routeProps, "renderFunc", handler);
    }
    return tree;
  });
}

const started = new Map<number, number>(); // appid -> launch time

function appName(appid: number) {
  try {
    return (window as any).appStore?.GetAppOverviewByAppID(appid)?.display_name ?? `app ${appid}`;
  } catch {
    return `app ${appid}`;
  }
}

/** Offer help when a game quits right after launching (usually a crash). Nothing is sent unless tapped. */
function onLifetime(n: { unAppID: number; bRunning: boolean }) {
  if (!n?.unAppID) return;
  if (n.bRunning) {
    started.set(n.unAppID, Date.now());
    return;
  }
  const t0 = started.get(n.unAppID);
  started.delete(n.unAppID);
  if (!t0) return;
  const secs = Math.round((Date.now() - t0) / 1000);
  if (secs > 90) return;
  const name = appName(n.unAppID);
  toaster.toast({
    title: `${name} closed after ${secs}s`,
    body: "Tap and Claude will find out why.",
    icon: <Icon name="alert" size={20} color={themeById(theme).accent} />,
    duration: 8000,
    onClick: () => {
      openChatFromToast();
      setTimeout(
        () => submit(`${name} (appid ${n.unAppID}) closed ${secs} seconds after I launched it. Find out why and fix it if you can.`),
        600,
      );
    },
  });
}

/** After a Steam screenshot (Steam + R1), offer to ask about it. */
async function onScreenshot() {
  try {
    const s = await SteamClient.Screenshots.GetLastScreenshotTaken();
    if (!s?.hHandle) return;
    // Steam expects the app id as a string here, whatever its type declarations say.
    const path: string = await (SteamClient.Screenshots as any).GetLocalScreenshotPath(String(s.nAppID), s.hHandle);
    if (!path) return;
    toaster.toast({
      title: "Screenshot saved",
      body: "Tap to ask Claude about it.",
      icon: <Icon name="capture" size={20} color={themeById(theme).accent} />,
      duration: 6000,
      onClick: () => {
        shot = path;
        notify();
        openChatFromToast();
      },
    });
  } catch (e) {
    console.warn("Claude: screenshot hook failed", e);
  }
}

const batteryWarned = new Set<number>(); // games we already offered help for this session

/** Low battery mid-game: offer to stretch it (once per game per session, only when discharging). */
async function onBattery(b: { flLevel?: number; nSecondsRemaining?: number }) {
  const app = Router.MainRunningApp;
  if (!app || (b.flLevel ?? 1) > 0.2 || batteryWarned.has(Number(app.appid))) return;
  const state = (await batteryStatus().catch(() => null)) ?? "";
  if (!/discharging/i.test(state)) return;
  batteryWarned.add(Number(app.appid));
  const pct = Math.round((b.flLevel ?? 0) * 100);
  const mins = b.nSecondsRemaining && b.nSecondsRemaining > 0 ? Math.round(b.nSecondsRemaining / 60) : null;
  toaster.toast({
    title: `Battery ${pct}%${mins ? ` · about ${mins} min left` : ""}`,
    body: "Tap and Claude will stretch it for this game.",
    icon: <Icon name="gauge" size={20} color={themeById(theme).accent} />,
    duration: 8000,
    onClick: () => {
      openChatFromToast();
      setTimeout(
        () =>
          submit(
            `My battery is at ${pct}%${mins ? ` with about ${mins} minutes left` : ""} while playing ${app.display_name} (appid ${app.appid}). Stretch it: lower the FPS cap, refresh rate or TDP sensibly for this game, then tell me in one line what you changed and roughly how much longer it should last.`,
          ),
        600,
      );
    },
  });
}

function registerSteamHooks(): () => void {
  const subs: { unregister?: () => void }[] = [];
  try {
    subs.push(SteamClient.System.RegisterForBatteryStateChanges(onBattery));
    subs.push(SteamClient.GameSessions.RegisterForAppLifetimeNotifications(onLifetime));
    subs.push(SteamClient.GameSessions.RegisterForScreenshotNotification(() => setTimeout(onScreenshot, 800)));
  } catch (e) {
    console.warn("Claude: Steam hooks unavailable", e);
  }
  const page = patchGamePage();
  return () => {
    subs.forEach((s) => s?.unregister?.());
    routerHook.removePatch("/library/app/:appid", page);
  };
}

/** Dictation: type the transcript into whatever text box has focus, through Steam's keyboard. */
function typeText(text: string) {
  try {
    SteamClient.Input.ControllerKeyboardSendText(text);
    return true;
  } catch (e) {
    console.warn("Claude: typing failed", e);
    return false;
  }
}

// --- Quick Access panel ---------------------------------------------------------------------------

const VOICES: { id: Voice; label: string; desc: string }[] = [
  { id: "voice", label: "When I talk", desc: "Claude answers out loud when you asked by voice." },
  { id: "always", label: "Always", desc: "Every reply is read aloud." },
  { id: "off", label: "Off", desc: "Replies stay on screen only." },
];

/** A padded block inside the Quick Access panel. Decky's rows add their own odd margins, so the
 *  custom parts of the panel use one container with a consistent rhythm instead. */
function Stack({ gap = 12, children, style }: { gap?: number; children: any; style?: CSSProperties }) {
  return <div style={{ display: "flex", flexDirection: "column", gap: `${gap}px`, width: "100%", boxSizing: "border-box", ...style }}>{children}</div>;
}

const CHORD_PRESETS: { id: string; label: string }[] = [
  { id: "l4r4", label: "L4 + R4" },
  { id: "l5r5", label: "L5 + R5" },
  { id: "l4l5", label: "L4 + L5 (left side)" },
  { id: "r4r5", label: "R4 + R5 (right side)" },
  { id: "l4", label: "L4" },
  { id: "r4", label: "R4" },
  { id: "l5", label: "L5" },
  { id: "r5", label: "R5" },
  { id: "viewmenu", label: "View + Menu" },
  { id: "l3r3", label: "Both stick clicks" },
  { id: "pads", label: "Both trackpad clicks" },
  { id: "off", label: "Off" },
];
/** Presets that use buttons games also see (the back buttons are unbound in most games). */
const SHARED_WITH_GAMES = new Set(["viewmenu", "l3r3", "pads"]);

/** Ask the user to hold the buttons they want, and bind them. */
async function recordChord(role: "ask" | "dictate") {
  const modal = showModal(
    <ModalRoot bAllowFullSize={false} onCancel={() => modal.Close()}>
      <div className="cd-root" style={{ ...(themeVars(themeById(theme)) as CSSProperties), display: "flex", gap: "18px", alignItems: "center" }}>
        <span style={{ color: v("accent") }}>
          <Icon name="gamepad" size={40} />
        </span>
        <div>
          <div style={{ fontFamily: v("display"), fontSize: "22px", marginBottom: "6px" }}>Hold your new shortcut</div>
          <div style={{ color: v("dim"), fontSize: "14px", lineHeight: 1.5 }}>
            Press and hold the buttons you want for {role === "ask" ? "asking Claude" : "dictation"} for a second, then let go. Back
            buttons work best: they don't do anything in most games.
          </div>
        </div>
      </div>
    </ModalRoot>,
  );
  const r = await captureChord(role).catch((e) => ({ error: String(e) }) as { error: string; spec?: string; label?: string });
  modal.Close();
  if (r.spec) {
    if (role === "ask") {
      pttChord = r.spec;
      pttLabel = r.label ?? r.spec;
    } else {
      dictateChord = r.spec;
      dictateLabel = r.label ?? r.spec;
    }
    toaster.toast({ title: "Shortcut saved", body: `${role === "ask" ? "Ask Claude" : "Dictate"}: hold ${r.label}` });
  } else toaster.toast({ title: "Shortcut not changed", body: r.error ?? "Try again." });
  notify();
}

function ChordSetting({ role }: { role: "ask" | "dictate" }) {
  const cur = role === "ask" ? pttChord : dictateChord;
  const other = role === "ask" ? dictateChord : pttChord;
  const curLabel = role === "ask" ? pttLabel : dictateLabel;
  const options = CHORD_PRESETS.filter((p) => p.id === "off" || p.id !== other).map((p) => ({ data: p.id, label: p.label }));
  if (!CHORD_PRESETS.some((p) => p.id === cur)) options.unshift({ data: cur, label: `${curLabel} (custom)` });
  options.push({ data: "__record", label: "Record new combo…" });
  return (
    <DropdownItem
      label={role === "ask" ? "Hold to ask" : "Hold to dictate"}
      description={
        cur === "off"
          ? "Off"
          : (role === "ask"
              ? "Hold anywhere, even in a game, and ask. Press again to dismiss the answer."
              : "Types what you say into the text box you're in.") +
            (SHARED_WITH_GAMES.has(cur) || cur.startsWith("m:") ? " The game sees these presses too." : "")
      }
      rgOptions={options}
      selectedOption={cur}
      onChange={(o) => {
        if (o.data === "__record") return void recordChord(role);
        const label = CHORD_PRESETS.find((p) => p.id === o.data)?.label.replace(/ \(.*\)$/, "") ?? o.data;
        if (role === "ask") {
          pttChord = o.data;
          pttLabel = label;
          setPttCall(o.data);
        } else {
          dictateChord = o.data;
          dictateLabel = label;
          setDictateCall(o.data);
        }
        notify();
      }}
    />
  );
}

/** Background jobs Claude scheduled (transient systemd user units named claude-*). */
function Jobs() {
  const [jobs, setJobs] = useState<Job[]>([]);
  useEffect(() => {
    let alive = true;
    const load = () => listJobs().then((j) => alive && setJobs(j)).catch(() => {});
    load();
    const t = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  const shown = jobs.filter((j) => j.unit.endsWith(".timer") || j.state === "active");
  if (!shown.length) return null;
  return (
    <PanelSection title="Jobs">
      <Stack gap={8} style={{ paddingBottom: "6px" }}>
        {shown.map((j) => (
          <Focusable key={j.unit} flow-children="horizontal" style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span style={{ color: j.state === "active" ? v("accent") : v("dim") }}>
              <Icon name={j.unit.endsWith(".timer") ? "auto" : "terminal"} size={16} />
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: "13px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {j.description && !j.description.startsWith("/") ? j.description : j.unit.replace(/^claude-|\.(service|timer)$/g, "")}
              </div>
              <div style={{ fontSize: "11px", color: v("faint") }}>
                {j.unit.endsWith(".timer") ? "Scheduled" : j.sub === "running" ? "Running" : j.sub}
              </div>
            </div>
            <DialogButton
              className="cd-btn"
              style={{ ...iconBtn, width: "32px", height: "32px" }}
              onClick={() => cancelJob(j.unit).then(() => setJobs((cur) => cur.filter((x) => x.unit !== j.unit)))}
            >
              <Icon name="x" size={15} />
            </DialogButton>
          </Focusable>
        ))}
      </Stack>
    </PanelSection>
  );
}

function Panel() {
  useStore();
  const app = Router.MainRunningApp;
  const turns = toTurns(history);
  const lastTurn = turns[turns.length - 1];
  const said = lastTurn?.blocks.filter((b): b is { t: "say"; e: Entry } => b.t === "say").pop();
  const stepCount = lastTurn?.blocks.reduce((n, b) => n + (b.t === "steps" ? b.items.filter((i) => i.kind === "tool").length : 0), 0) ?? 0;
  const status = asks.length ? "Needs you" : running ? "Working" : speaking ? "Speaking" : mic === "rec" ? "Listening" : "Ready";
  const dot = asks.length || speaking || mic === "rec" ? v("accent") : running ? v("warn") : v("ok");
  const label = { fontSize: "11px", letterSpacing: ".06em", textTransform: "uppercase" as const, color: v("faint"), fontWeight: 600 };
  const wide = { width: "100%", boxSizing: "border-box" as const };

  return (
    <Root>
      <PanelSection>
        <Stack gap={14} style={{ padding: "4px 0 8px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <Spark size={20} busy={running} />
            <span style={{ fontFamily: v("display"), fontSize: "20px", fontWeight: 500, flex: 1 }}>Claude</span>
            <span style={{ fontSize: "12px", color: v("dim"), display: "flex", alignItems: "center", gap: "6px" }}>
              <span style={{ width: "7px", height: "7px", borderRadius: "4px", background: dot }} />
              {status}
            </span>
          </div>

          <SetupCard />

          {app && (
            <div style={{ ...wide, display: "flex", gap: "12px", alignItems: "center", padding: "10px 12px", borderRadius: v("r"), background: v("surface") }}>
              <div style={{ width: "36px", height: "36px", flexShrink: 0, borderRadius: v("r-sm"), background: v("surface2"), display: "flex", alignItems: "center", justifyContent: "center", color: v("dim") }}>
                <Icon name="gamepad" size={19} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: "11.5px", color: v("dim") }}>Now playing</div>
                <div style={{ fontWeight: 600, fontSize: "14px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{app.display_name}</div>
              </div>
            </div>
          )}

          <Focusable style={{ display: "flex", flexDirection: "column", gap: "10px", ...wide }}>
            <DialogButton className="cd-btn" style={{ ...primaryBtn, ...wide, justifyContent: "center", height: "44px", padding: "0 16px" }} onClick={lookAndAsk}>
              <Icon name="capture" size={18} />
              {hasVoice ? "Look & ask" : "Look at my screen"}
            </DialogButton>
            <ShotChip />
            <Composer compact />
            {speaking && (
              <DialogButton className="cd-btn cd-fade" style={{ ...ghostBtn, ...wide, padding: "0 14px", color: v("accent") }} onClick={() => stopSpeaking()}>
                <Icon name="volume" size={16} />
                <span style={{ flex: 1, textAlign: "left" }}>Speaking</span>
                <Icon name="x" size={15} />
              </DialogButton>
            )}
          </Focusable>

          {asks.map((a) => (
            <AskCard key={a.id} ask={a} answer={answer} compact />
          ))}
          {pendingSignIns().map((r) => (
            <SignInCard key={r.id} req={r} notify={notify} compact />
          ))}

          <Stack gap={8} style={{ marginTop: "2px" }}>
            {(running || said) && <div style={label}>{running ? "Working" : "Last reply"}</div>}
            {running && streaming ? (
              <div style={{ fontSize: "13.5px", color: v("body"), lineHeight: 1.5, maxHeight: "170px", overflow: "hidden" }}>
                <Markdown text={streaming.slice(-500)} accent={v("accent")} focusable={false} />
                <span className="cd-caret" />
              </div>
            ) : running ? (
              <Thinking />
            ) : said ? (
              <Focusable
                onActivate={openChat}
                className="cd-focus-soft"
                style={{ fontSize: "13.5px", color: v("body"), lineHeight: 1.5, maxHeight: "170px", overflow: "hidden", WebkitMaskImage: "linear-gradient(#000 75%, transparent)" }}
              >
                <Markdown text={said.e.text} accent={v("accent")} focusable={false} />
              </Focusable>
            ) : (
              <Focusable style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                {PRESETS.map((p) => (
                  <DialogButton
                    key={p.text}
                    className="cd-btn"
                    style={{ ...ghostBtn, ...wide, height: "auto", minHeight: "40px", padding: "8px 12px", fontSize: "13px", textAlign: "left" }}
                    onClick={() => submit(p.text)}
                  >
                    <span style={{ color: v("accent") }}>
                      <Icon name={p.icon} size={15} />
                    </span>
                    {p.text}
                  </DialogButton>
                ))}
              </Focusable>
            )}
            {!running && stepCount > 0 && (
              <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "12px", color: v("dim") }}>
                <Icon name="check" size={14} color={v("ok")} />
                {stepCount} {stepCount === 1 ? "step" : "steps"} taken
              </div>
            )}
            <DialogButton className="cd-btn" style={{ ...ghostBtn, ...wide, justifyContent: "center", padding: "0 16px" }} onClick={openChat}>
              Open full chat
              <Icon name="chevronRight" size={15} />
            </DialogButton>
          </Stack>
        </Stack>
      </PanelSection>

      <Jobs />

      <PanelSection title="Settings">
        <PanelSectionRow>
          <DropdownItem
            label="Model"
            description={modelInfo(model).desc}
            rgOptions={MODELS.map((m) => ({ data: m.id, label: m.label }))}
            selectedOption={model}
            onChange={(o) => chooseModel(o.data)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Mode"
            description={modeInfo(mode).desc}
            rgOptions={MODES.map((m) => ({ data: m.id, label: m.label }))}
            selectedOption={mode}
            onChange={(o) => chooseMode(o.data)}
          />
        </PanelSectionRow>
        {canSpeak && (
          <PanelSectionRow>
            <DropdownItem
              label="Spoken replies"
              description={(VOICES.find((x) => x.id === voice) ?? VOICES[0]).desc}
              rgOptions={VOICES.map((x) => ({ data: x.id, label: x.label }))}
              selectedOption={voice}
              onChange={(o) => chooseVoice(o.data)}
            />
          </PanelSectionRow>
        )}
        {hasVoice && (
          <PanelSectionRow>
            <ChordSetting role="ask" />
          </PanelSectionRow>
        )}
        {hasVoice && (
          <PanelSectionRow>
            <ChordSetting role="dictate" />
          </PanelSectionRow>
        )}
        {hasVoice && deviceName !== "Steam Deck" && (
          <div style={{ fontSize: "12px", color: v("dim"), paddingBottom: "10px" }}>
            Back buttons map to Deck button names through InputPlumber.
          </div>
        )}
        <PanelSectionRow>
          <ToggleField
            label="A chat per game"
            description="Questions asked in a game go to that game's own chat."
            checked={gameThreads}
            onChange={(on) => {
              gameThreads = on;
              notify();
              setGameThreads(on);
            }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Theme"
            rgOptions={THEMES.map((t) => ({ data: t.id, label: `${t.name} · ${t.tag}` }))}
            selectedOption={theme}
            onChange={(o) => chooseTheme(o.data)}
          />
        </PanelSectionRow>
        <Stack gap={8} style={{ padding: "10px 0 4px" }}>
          <UpdateRow />
          <Focusable style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            <DialogButton className="cd-btn" style={{ ...ghostBtn, ...wide, padding: "0 14px" }} onClick={continueOnPhone}>
              <Icon name="phone" size={16} />
              <span style={{ flex: 1, textAlign: "left" }}>{remoteUrl ? "Linked to phone" : "Continue on phone"}</span>
              <Icon name="chevronRight" size={15} color={v("faint")} />
            </DialogButton>
            <DialogButton className="cd-btn" style={{ ...ghostBtn, ...wide, padding: "0 14px" }} onClick={showChats}>
              <Icon name="plan" size={16} />
              <span style={{ flex: 1, textAlign: "left" }}>Chats</span>
              <Icon name="chevronRight" size={15} color={v("faint")} />
            </DialogButton>
            {history.length > 0 && (
              <DialogButton className="cd-btn" style={{ ...ghostBtn, ...wide, padding: "0 14px" }} onClick={startOver}>
                <Icon name="pencil" size={16} />
                <span style={{ flex: 1, textAlign: "left" }}>New conversation</span>
              </DialogButton>
            )}
          </Focusable>
          {usage?.unifiedWindows && (
            <div style={{ ...wide, display: "flex", flexDirection: "column", gap: "10px", padding: "12px", borderRadius: v("r"), background: v("sunken") }}>
              <Meter label="Session" w={usage.unifiedWindows.five_hour} />
              <Meter label="Week" w={usage.unifiedWindows.seven_day} />
              {usage.status === "rejected" && <div style={{ fontSize: "12px", color: v("err") }}>Limit reached. Claude is back after the reset.</div>}
            </div>
          )}
          {pluginVersion() && <div style={{ fontSize: "11px", color: v("faint"), textAlign: "center" }}>Claude for {deviceName} v{pluginVersion()} · unofficial</div>}
        </Stack>
      </PanelSection>
    </Root>
  );
}

function TabIcon() {
  return (
    <svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" strokeWidth={2.6} strokeLinecap="round">
      <path d="M12 3v18M3 12h18M5.6 5.6l12.8 12.8M18.4 5.6L5.6 18.4" />
    </svg>
  );
}

export default definePlugin(() => {
  const listeners = handlers.map(([n, f]) => [n, addEventListener(n, f)] as const);
  routerHook.addRoute(CHAT_ROUTE, ChatPage, { exact: true });
  routerHook.addGlobalComponent("ClaudeCaption", Caption);
  const removeFonts = installFonts();
  const removeSignIn = installSignInBridge(notify, () => viewers > 0, openChatFromToast);
  const removeSteamHooks = registerSteamHooks();
  // Automation hooks for scripts/deckctl.py (README captures and demos); same actions as the buttons.
  (window as any).__claudeDeck.ui = {
    talk: () => toggleMic(),
    talkChat: () => toggleMic(chatDraft ?? undefined),
    lookAndAsk,
    openChat,
    theme: chooseTheme,
    model: chooseModel,
    mode: chooseMode,
    voice: chooseVoice,
    submit,
    state: () => ({ mic, running, speaking, theme, model, mode, voice, ptt }),
  };
  getState().then((s) => {
    applyState(s);
    notify();
  });
  // Retry: right after a plugin reload the backend may not answer yet, which used to hide the mic.
  const checkVoice = (tries: number) =>
    voiceAvailable()
      .then((ok) => {
        hasVoice = ok;
        notify();
      })
      .catch(() => tries > 0 && setTimeout(() => checkVoice(tries - 1), 1500));
  checkVoice(5);

  return {
    name: "Claude",
    titleView: <div className={staticClasses.Title}>Claude</div>,
    content: <Panel />,
    icon: <TabIcon />,
    alwaysRender: true,
    onDismount() {
      listeners.forEach(([n, l]) => removeEventListener(n, l as any));
      routerHook.removeRoute(CHAT_ROUTE);
      routerHook.removeGlobalComponent("ClaudeCaption");
      removeFonts();
      removeSignIn();
      removeSteamHooks();
      if (mic !== "off") micStop();
    },
  };
});
