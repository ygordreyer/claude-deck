import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

// Exercise the implementation and Decky's installed patcher without Steam or a browser.
const source = readFileSync(new URL("../src/index.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("index.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(["installClaudeQam", "reopenPanel", "usePanelVisible", "useStore", "Jobs"]);
const picked = ast.statements.filter(s =>
  (ts.isFunctionDeclaration(s) && names.has(s.name?.text)) ||
  (ts.isVariableStatement(s) && s.declarationList.declarations.some(d =>
    ["CLAUDE_QAM_TAB", "claudeQamReady"].includes(d.name.getText(ast))))
).map(s => s.getText(ast)).join("\n");
const compile = text => ts.transpileModule(text, {
  fileName: "subject.tsx",
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const patcher = { exports: {}, console: { debug() {} } };
vm.runInNewContext(compile(readFileSync(new URL("../node_modules/@decky/ui/src/utils/patcher.ts", import.meta.url), "utf8")), patcher);

const MUSIC = 7;
const FRIENDS = 3;
const HELP = 6;
const DECKY = 999;
const CLAUDE = 0x434c4155;
const music = () => ({ key: MUSIC, title: "Localized Soundtracks" });
const decky = { key: DECKY, decky: true };
const hook = () => ({ tabs: [{ id: DECKY }], render() { return "original result"; } });
function fixture(tabsHook = hook()) {
  const selected = new Set();
  const warnings = [];
  const navigation = [];
  const context = {
    exports: {},
    require: () => ({ jsx: (type, props) => ({ type, props }) }),
    window: { __TABS_HOOK_INSTANCE: tabsHook },
    document: { getElementById: id => ({ getAttribute: () => selected.has(id) ? "true" : "false" }) },
    console: { warn: (...args) => warnings.push(args) },
    afterPatch: patcher.exports.afterPatch,
    QuickAccessTab: { Music: MUSIC, Friends: FRIENDS, Help: HELP, Decky: DECKY },
    staticClasses: { Title: "title" },
    ErrorBoundary() {}, Panel() {}, TabIcon() {},
    Navigation: { OpenQuickAccessMenu: tab => navigation.push(tab) },
    WeakRef,
  };
  vm.runInNewContext(compile(picked + "\nglobalThis.subject = {installClaudeQam, reopenPanel, usePanelVisible, useStore, Jobs, ready: () => claudeQamReady};"), context);
  return { context, selected, warnings, navigation, subject: context.subject, hook: tabsHook };
}
function test(name, run) {
  run();
  console.log(`PASS: ${name}`);
}

test("repeated renders replace Soundtracks in place once and preserve Decky", () => {
  const f = fixture();
  const original = music();
  const tabs = [{ key: 1 }, original, decky];
  const remove = f.subject.installClaudeQam();
  for (let i = 0; i < 3; i++) assert.equal(f.hook.render(tabs, false), "original result");
  assert.equal(tabs.length, 3);
  assert.equal(tabs[1].key, CLAUDE);
  assert.equal(tabs[1].decky, undefined);
  assert.equal(tabs[2], decky);
  assert.equal(tabs[1].panel.props.children.props.topLevel, true);
  f.subject.reopenPanel();
  assert.deepEqual(f.navigation, [CLAUDE]);
  const secondMusic = { key: String(MUSIC) };
  const second = [decky, secondMusic];
  f.hook.render(second, false);
  const patchedAgain = patcher.exports.afterPatch(f.hook, "render", (_args, ret) => ret);
  remove();
  remove();
  assert.equal(tabs[1], original);
  assert.equal(second[1], secondMusic);
  assert.equal(f.subject.ready(), false);
  f.subject.reopenPanel();
  assert.deepEqual(f.navigation, [CLAUDE, DECKY]);
  patchedAgain.unpatch();
  assert.equal(f.hook.render(tabs, false), "original result");
});

test("no native Soundtracks appends Claude, preserves Decky entries keyed 7, and removes only Claude", () => {
  const f = fixture();
  const pluginMusic = { key: MUSIC, decky: true };
  const tabs = [pluginMusic, decky];
  const remove = f.subject.installClaudeQam();
  f.hook.render(tabs, false);
  assert.deepEqual(tabs.map(t => t.key), [MUSIC, DECKY, CLAUDE]);
  remove();
  assert.deepEqual(tabs, [pluginMusic, decky]);
});

test("Friends and Help hide by native key while Claude takes Soundtracks' place across arrays and reloads", () => {
  const f = fixture();
  const pluginFriends = { key: FRIENDS, decky: true };
  const pluginHelp = { key: String(HELP), decky: true };
  const lookalike = { key: 42, title: "Friends" };
  const originals = [
    [{ key: 0 }, { key: FRIENDS }, { key: 4 }, music(), { key: HELP }, decky],
    [{ key: String(HELP) }, music(), { key: String(FRIENDS) }, decky],
    [music(), { key: HELP }, decky, { key: FRIENDS }],
    [pluginFriends, { key: FRIENDS }, pluginHelp, { key: HELP }, lookalike, decky],
  ];
  const arrays = originals.map(tabs => tabs.slice());
  const expected = [[0, 4, CLAUDE, DECKY], [CLAUDE, DECKY], [CLAUDE, DECKY], [FRIENDS, String(HELP), 42, DECKY, CLAUDE]];
  for (let reload = 0; reload < 2; reload++) {
    const remove = f.subject.installClaudeQam();
    for (const [i, tabs] of arrays.entries()) {
      for (let render = 0; render < 3; render++) f.hook.render(tabs, false);
      assert.deepEqual(tabs.map(t => t.key), expected[i]);
      assert.equal(tabs.filter(t => t.key === CLAUDE).length, 1);
    }
    remove();
    remove();
    for (const [i, tabs] of arrays.entries()) {
      assert.deepEqual(tabs, originals[i]);
      originals[i].forEach((tab, index) => assert.equal(tabs[index], tab));
    }
  }
});

test("unload restores hidden objects relative to surviving tabs after Claude is displaced", () => {
  const f = fixture();
  const original = [{ key: FRIENDS }, { key: 4 }, music(), { key: HELP }, decky];
  const tabs = original.slice();
  const remove = f.subject.installClaudeQam();
  f.hook.render(tabs, false);
  tabs.splice(tabs.findIndex(t => t.key === CLAUDE), 1);
  const added = { key: 42 };
  tabs.unshift(added);
  remove();
  assert.deepEqual(tabs, [added, ...original]);
  original.forEach((tab, index) => assert.equal(tabs[index + 1], tab));
});

test("missing hook or Decky registration is a warned no-op", () => {
  for (const missing of [null, { render() {}, tabs: [] }]) {
    const f = fixture(missing);
    f.subject.installClaudeQam()();
    assert.equal(f.subject.ready(), false);
    assert.equal(f.warnings.length, 1);
  }
});

test("collision, immutable, read-only, and accessor arrays retain every original tab", () => {
  for (const kind of ["collision", "frozen", "sealed", "readonly", "late-readonly", "length-readonly", "accessor", "nonextensible"]) {
    const f = fixture();
    const original = music();
    const tabs = [{ key: FRIENDS }, original, { key: HELP }, decky];
    if (kind === "collision") tabs.push({ key: String(CLAUDE) });
    if (kind === "frozen") Object.freeze(tabs);
    if (kind === "sealed") Object.seal(tabs);
    if (kind === "readonly") Object.defineProperty(tabs, 0, { writable: false });
    if (kind === "late-readonly") Object.defineProperty(tabs, 3, { writable: false });
    if (kind === "length-readonly") Object.defineProperty(tabs, "length", { writable: false });
    if (kind === "accessor") Object.defineProperty(tabs, 3, { get: () => decky });
    if (kind === "nonextensible") Object.preventExtensions(tabs);
    const before = tabs.slice();
    const remove = f.subject.installClaudeQam();
    f.hook.render(tabs, false);
    assert.deepEqual(tabs, before);
    assert.equal(f.subject.ready(), false);
    assert.equal(f.warnings.length, 1);
    remove();
    assert.deepEqual(tabs, before);
  }
});

test("selected Friends and Help defer all changes until selection moves", () => {
  for (const key of [FRIENDS, HELP]) {
    const f = fixture();
    const original = [{ key: FRIENDS }, music(), { key: HELP }, decky];
    const tabs = original.slice();
    const remove = f.subject.installClaudeQam();
    f.selected.add(`quickaccess_tab_${key}`);
    f.hook.render(tabs, true);
    assert.deepEqual(tabs, original);
    assert.equal(f.subject.ready(), false);
    f.selected.clear();
    f.hook.render(tabs, true);
    assert.deepEqual(tabs.map(t => t.key), [CLAUDE, DECKY]);
    remove();
    original.forEach((tab, index) => assert.equal(tabs[index], tab));
  }
});

test("active Soundtracks defers until the next render after selection changes", () => {
  const f = fixture();
  const original = music();
  const tabs = [original, decky];
  const remove = f.subject.installClaudeQam();
  f.selected.add("quickaccess_tab_7");
  f.hook.render(tabs, true);
  assert.equal(tabs[0], original);
  assert.equal(f.subject.ready(), false);
  f.selected.clear();
  f.hook.render(tabs, true);
  assert.equal(tabs[0].key, CLAUDE);
  remove();
  assert.equal(tabs[0], original);
});

test("render exception retains Soundtracks and warns without breaking Decky", () => {
  const f = fixture();
  const original = music();
  const tabs = [{ key: FRIENDS }, original, { key: HELP }, decky];
  const before = tabs.slice();
  f.context.document.getElementById = () => { throw new Error("document unavailable"); };
  const remove = f.subject.installClaudeQam();
  assert.equal(f.hook.render(tabs, true), "original result");
  assert.deepEqual(tabs, before);
  assert.equal(f.warnings.length, 1);
  remove();
  assert.deepEqual(tabs, before);
});

test("reload does not duplicate Claude and unload restores a displaced original", () => {
  const f = fixture();
  const original = music();
  const tabs = [original, decky];
  let remove = f.subject.installClaudeQam();
  f.hook.render(tabs, false);
  tabs.splice(0, 1);
  remove();
  assert.equal(tabs[0], original);
  remove = f.subject.installClaudeQam();
  f.hook.render(tabs, false);
  assert.equal(tabs.filter(t => t.key === CLAUDE).length, 1);
  remove();
  assert.deepEqual(tabs, [original, decky]);
});

test("tracking exception leaves Friends, Help, and Soundtracks untouched", () => {
  const f = fixture();
  const original = [{ key: FRIENDS }, music(), { key: HELP }, decky];
  const tabs = original.slice();
  f.context.WeakRef = class { constructor() { throw new Error("tracking unavailable"); } };
  const remove = f.subject.installClaudeQam();
  assert.equal(f.hook.render(tabs, false), "original result");
  assert.deepEqual(tabs, original);
  assert.equal(f.subject.ready(), false);
  assert.equal(f.warnings.length, 1);
  remove();
  assert.deepEqual(tabs, original);
});

test("panel visibility requires the selected tab, intersection, and an open menu", () => {
  const f = fixture();
  let effect;
  let panelVisible = false;
  let menuVisible = true;
  let intersection;
  let selection;
  let disconnected = 0;
  Object.assign(f.context, {
    useQuickAccessVisible: () => menuVisible,
    useState: () => [panelVisible, value => { panelVisible = value; }],
    useEffect: fn => { effect = fn; },
    IntersectionObserver: class {
      constructor(fn) { intersection = fn; }
      observe() {}
      disconnect() { disconnected++; }
    },
    MutationObserver: class {
      constructor(fn) { selection = fn; }
      observe() {}
      disconnect() { disconnected++; }
    },
  });
  f.context.document.defaultView = {
    IntersectionObserver: f.context.IntersectionObserver,
    MutationObserver: f.context.MutationObserver,
  };
  f.context.IntersectionObserver = class { constructor() { assert.fail("must observe the panel's own window"); } };
  f.context.MutationObserver = class { constructor() { assert.fail("must observe the panel's own document"); } };
  const root = { current: { ownerDocument: f.context.document } };
  const visible = () => f.subject.usePanelVisible(root, CLAUDE);
  assert.equal(visible(), false);
  const remove = effect();
  intersection([{ isIntersecting: true }]);
  assert.equal(visible(), false);
  f.selected.add(`quickaccess_tab_${CLAUDE}`);
  selection();
  assert.equal(visible(), true);
  menuVisible = false;
  assert.equal(visible(), false);
  menuVisible = true;
  intersection([{ isIntersecting: false }]);
  assert.equal(visible(), false);
  remove();
  assert.equal(disconnected, 2);
});

test("hidden mounted panels do not count as viewers or poll Jobs", () => {
  const f = fixture();
  const effects = [];
  let polls = 0;
  let intervalCleared = false;
  Object.assign(f.context, {
    useState: value => [value, () => {}],
    useEffect: fn => effects.push(fn),
    viewers: 0,
    subscribers: new Set(),
    getState: () => new Promise(() => {}),
    listJobs: () => { polls++; return Promise.resolve([]); },
    setInterval: () => 42,
    clearInterval: id => { assert.equal(id, 42); intervalCleared = true; },
  });
  f.subject.useStore(false);
  const hiddenSubscription = effects.shift()();
  assert.equal(effects.shift()(), undefined);
  assert.equal(f.context.viewers, 0);
  f.subject.useStore(true);
  const visibleSubscription = effects.shift()();
  const unview = effects.shift()();
  assert.equal(f.context.viewers, 1);
  unview();
  hiddenSubscription();
  visibleSubscription();
  assert.equal(f.context.viewers, 0);
  assert.equal(f.context.subscribers.size, 0);
  f.subject.Jobs({ visible: false });
  assert.equal(effects.shift()(), undefined);
  assert.equal(polls, 0);
  f.subject.Jobs({ visible: true });
  const stopPolling = effects.shift()();
  assert.equal(polls, 1);
  stopPolling();
  assert.equal(intervalCleared, true);
});
