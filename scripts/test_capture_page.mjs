/** Offline page lifecycle regressions. No browser, credentials, media, or network. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const source = await readFile(new URL("../app/extension/page.js", import.meta.url), "utf8");
const sessionId = "00000000-0000-4000-8000-000000000001";
const extensionId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const rootId = `sns-japanese-agent-root-${extensionId}`;
const flush = async () => { for (let count = 0; count < 12; count++) await Promise.resolve(); };

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.style = {};
    this.listeners = new Map();
    this.attributes = new Map();
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.value = "";
    this.isConnected = true;
    const classes = new Set();
    this.classList = {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const enabled = force === undefined ? !classes.has(name) : force;
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      },
    };
  }
  append(...children) {
    for (const child of children) { this.children.push(child); child.parentElement = this; }
  }
  replaceChildren(...children) {
    for (const child of this.children) { child.parentElement = null; child.isConnected = false; }
    this.children = [];
    this.append(...children);
  }
  contains(element) { return element === this || this.children.some((child) => child.contains(element)); }
  matches(selector) {
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    const [tag, className] = selector.split(".");
    return (!tag || this.tagName.toLowerCase() === tag) && (!className || this.classList.contains(className));
  }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
  attachShadow() { this.shadowRoot = new FakeElement("shadow-root"); return this.shadowRoot; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  addEventListener(type, listener, options = {}) {
    const listeners = this.listeners.get(type) || [];
    listeners.push({ listener, once: Boolean(options.once) });
    this.listeners.set(type, listeners);
  }
  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter((entry) => entry.listener !== listener));
  }
  emit(type) {
    for (const entry of [...(this.listeners.get(type) || [])]) {
      if (entry.once) this.removeEventListener(type, entry.listener);
      entry.listener({ type, target: this, preventDefault() {} });
    }
  }
  click() { if (!this.disabled) this.emit("click"); }
  getBoundingClientRect() { return { top: 0, left: 0, right: 640, bottom: 360 }; }
}

function harness({ duration = NaN, pendingPlay = false, failBegin = false, shorts = false, transcriptionEnabled = false, statusReplies = [], foreignPanel = false } = {}) {
  const timers = new Map();
  const messages = [];
  let time = 0;
  let nextTimer = 1;
  let listener;
  let releasePlay;
  let statusCalls = 0;
  let runtimeFailure;
  let runtimeFailures = 0;
  const video = new FakeElement("video");
  Object.assign(video, {
    duration, currentTime: 0, paused: true, ended: false, seeking: false, loop: false,
    playbackRate: 1, readyState: Number.isFinite(duration) ? 1 : 0, networkState: 2, error: null,
  });
  video.pause = () => { video.paused = true; };
  video.play = () => {
    video.paused = false;
    if (pendingPlay) return new Promise((resolve) => { releasePlay = resolve; });
    video.emit("playing");
    return Promise.resolve();
  };
  video.classList.add("html5-main-video");
  let player = new FakeElement("div");
  player.id = "movie_player";
  player.classList.add("html5-video-player");
  player.append(video);
  const activeShorts = new FakeElement("ytd-reel-video-renderer");
  if (shorts) activeShorts.append(player);
  const unrelatedPlayer = new FakeElement("div");
  unrelatedPlayer.classList.add("html5-video-player");
  const previews = [];
  const document = {
    documentElement: new FakeElement("html"),
    createElement: (name) => new FakeElement(name),
    getElementById(id) { return this.documentElement.querySelector(`#${id}`); },
    querySelector(selector) {
      if (selector === "ytd-reel-video-renderer[is-active]") return shorts ? activeShorts : null;
      if (selector === "#movie_player") return player;
      if (selector === ".html5-video-player.ad-showing") return unrelatedPlayer.classList.contains("ad-showing") ? unrelatedPlayer : null;
      if (selector === "video.html5-main-video") return player.querySelector(selector);
      if (selector === "video") return previews[0] || player.querySelector(selector);
      return null;
    },
    querySelectorAll: (selector) => selector === "video" ? [...previews, ...player.children] : [],
  };
  document.documentElement.append(shorts ? activeShorts : player, unrelatedPlayer);
  let foreignRoot;
  if (foreignPanel) {
    foreignRoot = new FakeElement("div");
    foreignRoot.id = "sns-japanese-agent-root";
    document.documentElement.append(foreignRoot);
  }
  const location = { href: shorts ? "https://www.youtube.com/shorts/AbCdEf012_-" : "https://www.youtube.com/watch?v=AbCdEf012_-" };
  const setTimer = (fn, delay, interval = false) => {
    const id = nextTimer++;
    timers.set(id, { fn, at: time + delay, interval: interval ? delay : 0 });
    return id;
  };
  const runtime = {
    id: extensionId,
    onMessage: { addListener(fn) { listener = fn; } },
    // Keep this entry point synchronous so a throw before a Promise exists is tested.
    sendMessage(message) {
      messages.push(structuredClone(message));
      if (runtimeFailure && (!runtimeFailure.type || runtimeFailure.type === message.type)) {
        runtimeFailures++;
        const error = new Error("Extension context invalidated.");
        if (runtimeFailure.mode === "async") return Promise.reject(error);
        throw error;
      }
      return replyToMessage(message);
    },
  };
  async function replyToMessage(message) {
    if (failBegin && message.type === "capture-begin") throw new Error("Synthetic startup failure");
    if (message.type === "capture-transcription-status") {
      const reply = statusReplies[statusCalls++] ?? { status: "waiting_for_audio", windows_done: 0, windows_total: 0, sentences: 0 };
      const value = typeof reply === "function" ? await reply() : await reply;
      return { ok: true, transcription: value };
    }
    return { ok: true };
  }
  const context = vm.createContext({
    window: {}, document, location, URL, Number, Error, Promise,
    innerHeight: 1000, innerWidth: 1200,
    Date: class extends Date { static now() { return time; } },
    performance: { now: () => time },
    HTMLMediaElement: { HAVE_NOTHING: 0, HAVE_METADATA: 1, HAVE_CURRENT_DATA: 2, HAVE_FUTURE_DATA: 3, HAVE_ENOUGH_DATA: 4 },
    setTimeout: (fn, delay) => setTimer(fn, delay), clearTimeout: (id) => timers.delete(id),
    setInterval: (fn, delay) => setTimer(fn, delay, true), clearInterval: (id) => timers.delete(id),
    chrome: { runtime },
  });
  const injectionResult = vm.runInContext(source, context);
  const root = document.documentElement.children.find((element) => element.id === rootId);
  assert.ok(root?.shadowRoot, "The actual page script must render its panel");
  const elements = [];
  const visit = (element) => { elements.push(element); element.children.forEach(visit); };
  visit(root.shadowRoot);
  const begin = elements.find((element) => element.tagName === "BUTTON" && element.textContent === "开始采集");
  const cancel = elements.find((element) => element.tagName === "BUTTON" && element.textContent === "中断／释放音频");
  const inputs = elements.filter((element) => element.tagName === "INPUT");
  const close = elements.find((element) => element.tagName === "BUTTON" && element.textContent === "×");
  const transcription = elements.find((element) => element.className === "transcription-status");
  const status = elements.find((element) => element.className === "status");
  assert.ok(begin && cancel && status && inputs.length === 3);
  const dispatch = (type, payload = {}) => new Promise((resolve) => {
    let responded = false;
    const keepOpen = listener({ type, sessionId, ...payload }, { id: extensionId }, (reply) => {
      responded = true;
      resolve(reply);
    });
    if (keepOpen !== true && !responded) resolve(undefined);
  });
  const advance = async (milliseconds) => {
    const target = time + milliseconds;
    while (true) {
      const candidate = [...timers].sort((a, b) => a[1].at - b[1].at).find(([, timer]) => timer.at <= target);
      if (!candidate) break;
      const [id, timer] = candidate;
      time = timer.at;
      if (timer.interval) timer.at += timer.interval; else timers.delete(id);
      timer.fn();
      await flush();
    }
    time = target;
    await flush();
  };
  const metadataListeners = () => ["loadedmetadata", "durationchange", "error", "emptied"]
    .reduce((count, type) => count + (video.listeners.get(type) || []).length, 0);
  return {
    video, messages, location, begin, cancel, close, inputs, status, transcription, dispatch, advance, metadataListeners,
    injectionResult,
    reinject: () => vm.runInContext(source, context),
    forgetPageMarker: () => { delete context.window.__snsJapaneseAgentPage; },
    get panelCount() { return document.documentElement.children.filter((element) => element.id === rootId).length; },
    get foreignPanelConnected() { return foreignRoot?.isConnected === true && foreignRoot.parentElement === document.documentElement; },
    get panelVersion() { return elements.find((element) => element.className === "version")?.textContent; },
    get statusCalls() { return statusCalls; },
    get timerCount() { return timers.size; },
    get runtimeFailures() { return runtimeFailures; },
    invalidateContext({ clearId = true, mode = "sync", type } = {}) {
      if (clearId) runtime.id = undefined;
      runtimeFailure = { mode, type };
    },
    get player() { return player; }, unrelatedPlayer, previews,
    replacePlayer(replacement) { player = replacement; },
    releasePlay: () => releasePlay?.(),
    async configure() {
      await dispatch("capture-configuring", { transcriptionEnabled });
      return dispatch("capture-configure", { transcriptionEnabled });
    },
  };
}

{
  const h = harness();
  await h.dispatch("capture-configuring");
  const configured = h.dispatch("capture-configure");
  await flush();
  assert.equal(h.begin.disabled, true);
  assert.equal(h.cancel.disabled, false, "Preparation must remain cancellable while metadata is loading");
  assert.ok(h.inputs.every((input) => input.disabled));
  h.video.duration = 120;
  h.video.readyState = 1;
  h.video.emit("loadedmetadata");
  const reply = await configured;
  assert.equal(reply.ok, true);
  assert.equal(h.begin.disabled, false);
  assert.ok(h.inputs.every((input) => !input.disabled));
  assert.equal(Number(h.inputs[1].value), 120);
  assert.equal(h.metadataListeners(), 0, "Successful preparation must release metadata listeners");
  console.log("PASS delayed video metadata enables capture without reinjecting the extension");
}
{
  const h = harness({ duration: Infinity });
  const reply = await h.configure();
  assert.equal(reply.ok, false);
  assert.match(reply.error, /直播/);
  assert.equal(h.begin.disabled, true);
  assert.equal(h.metadataListeners(), 0);
  console.log("PASS live or unbounded video is rejected without enabling capture");
}
{
  const h = harness();
  const configured = h.configure();
  await flush();
  await h.advance(8001);
  const reply = await configured;
  assert.equal(reply.ok, false);
  assert.match(reply.error, /超时|加载|等待/);
  assert.equal(h.begin.disabled, true);
  assert.equal(h.metadataListeners(), 0);
  console.log("PASS missing metadata times out and releases temporary listeners");
}
{
  const h = harness();
  const configured = h.configure();
  await flush();
  await h.dispatch("capture-reset");
  await h.advance(250);
  const reply = await configured;
  assert.equal(reply.ok, false);
  assert.equal(h.begin.disabled, true);
  assert.equal(h.metadataListeners(), 0);
  h.video.duration = 120;
  h.video.emit("loadedmetadata");
  await flush();
  assert.equal(h.begin.disabled, true, "Late metadata from a cancelled session must not re-enable capture");
  console.log("PASS reset cancels metadata preparation and ignores late readiness events");
}
{
  const h = harness();
  const configured = h.configure();
  await flush();
  h.location.href = "https://www.youtube.com/watch?v=ZyXwVu987_-";
  await h.advance(500);
  const reply = await configured;
  assert.equal(reply.ok, false);
  assert.match(reply.error, /切换|失效|取消/);
  assert.equal(h.begin.disabled, true);
  assert.equal(h.metadataListeners(), 0);
  console.log("PASS changing video invalidates pending metadata preparation");
}
{
  const h = harness({ duration: 120, failBegin: true });
  assert.equal((await h.configure()).ok, true);
  h.begin.click();
  await flush();
  assert.ok(h.messages.some((message) => message.type === "capture-begin"));
  assert.equal(h.cancel.disabled, false, "A failed start must keep the release action usable");
  assert.ok(h.inputs.every((input) => !input.disabled));
  assert.match(h.status.textContent, /释放/);
  console.log("PASS failed startup offers release and restores editable inputs");
}
{
  const h = harness({ duration: 120 });
  assert.equal((await h.configure()).ok, true);
  assert.equal((await h.dispatch("prepare", { rangeStart: 0, rangeEnd: 60, batchIntervalMs: 5000 })).ok, true);
  assert.equal((await h.dispatch("play")).ok, true);
  assert.equal(h.messages.filter((message) => message.type === "page-started").length, 1);
  await h.advance(8001);
  assert.equal(h.messages.some((message) => message.type === "page-error"), false,
    "Successful playback must clear the startup deadline");
  await h.dispatch("capture-stop-playback");
  assert.equal((h.video.listeners.get("playing") || []).length, 0);
  console.log("PASS normal playback starts once and clears its startup deadline");
}
{
  const h = harness({ duration: 120, pendingPlay: true });
  assert.equal((await h.configure()).ok, true);
  assert.equal((await h.dispatch("prepare", { rangeStart: 0, rangeEnd: 60, batchIntervalMs: 5000 })).ok, true);
  const played = h.dispatch("play");
  await flush();
  await h.advance(8001);
  const reply = await played;
  assert.equal(reply.ok, false);
  assert.ok(h.messages.some((message) => message.type === "page-error"));
  assert.equal(h.messages.some((message) => message.type === "page-started"), false);
  h.releasePlay();
  h.video.emit("playing");
  await flush();
  assert.equal(h.messages.some((message) => message.type === "page-started"), false,
    "A play promise resolving after the startup deadline must not revive the failed capture");
  assert.equal((h.video.listeners.get("playing") || []).length, 0);
  console.log("PASS permanently pending playback times out and late playback cannot revive capture");
}

async function recording(options = {}) {
  const h = harness({ duration: 600, ...options });
  assert.equal((await h.configure()).ok, true);
  assert.equal((await h.dispatch("prepare", { rangeStart: 0, rangeEnd: 300, batchIntervalMs: 5000 })).ok, true);
  assert.equal((await h.dispatch("play")).ok, true);
  return h;
}
{
  const h = await recording();
  const preview = new FakeElement("video");
  preview.getBoundingClientRect = () => ({ top: 0, left: 0, right: 1200, bottom: 1000 });
  h.video.getBoundingClientRect = () => ({ top: -600, left: 0, right: 10, bottom: -500 });
  h.previews.unshift(preview);
  h.video.currentTime = 249.9;
  await h.advance(500);
  assert.equal(h.messages.some((m) => m.type === "page-error"), false);
  assert.equal(h.video.paused, false);
  await h.dispatch("capture-stop-playback");
  console.log("PASS hover previews and scrolling cannot replace the captured main video");
}
{
  const h = await recording({ shorts: true });
  h.unrelatedPlayer.classList.add("ad-showing");
  h.video.currentTime = 249.9;
  await h.advance(500);
  assert.equal(h.messages.some((m) => m.type === "page-error"), false);
  await h.dispatch("capture-stop-playback");
  console.log("PASS active Shorts player ignores another player's advertisement state");
}
const diagnosticKeys = ["code", "message", "at_ms", "video_sec", "video_connected", "player_connected", "ad_showing", "identity_changed", "video_replaced"].sort();
for (const kind of ["identity", "ad", "detached", "replacement"]) {
  const h = await recording();
  h.video.currentTime = 249.9;
  if (kind === "identity") h.location.href = "https://www.youtube.com/watch?v=ZyXwVu987_-";
  if (kind === "ad") h.player.classList.add("ad-showing");
  if (kind === "detached") h.video.isConnected = false;
  if (kind === "replacement") {
    const replacement = new FakeElement("video");
    replacement.classList.add("html5-main-video");
    h.player.replaceChildren(replacement);
  }
  await h.advance(100);
  const errors = h.messages.filter((m) => m.type === "page-error");
  assert.equal(errors.length, 1);
  const error = errors[0];
  assert.equal(error.failure.code, kind === "identity" ? "video_identity_changed" : kind === "ad" ? "advertisement_started" : "video_replaced");
  assert.deepEqual(Object.keys(error.failure).sort(), diagnosticKeys);
  assert.equal(error.failure.video_sec, 249.9);
  assert.equal(error.playbackEndVideoSec, 249.9);
  assert.ok(error.playbackEvents.length > 0 && error.playbackEvents.length <= 1000);
  assert.equal(JSON.stringify(error.failure).includes("https://"), false);
  assert.equal(h.video.paused, true);
  assert.equal((h.video.listeners.get("playing") || []).length, 0);
  await h.advance(1000);
  assert.equal(h.messages.filter((m) => m.type === "page-error").length, 1);
  console.log(`PASS real ${kind} stops once with specific safe diagnostics and retained playback events`);
}
{
  const h = await recording();
  h.player.classList.add("ad-showing");
  h.video.currentTime = 301;
  h.video.emit("timeupdate");
  await flush();
  assert.equal(h.messages.some((m) => m.type === "page-ended"), false);
  assert.equal(h.messages.find((m) => m.type === "page-error").failure.code, "advertisement_started");
  console.log("PASS an advertisement time jump cannot falsely complete the selected capture range");
}
{
  const h = await recording();
  await h.advance(5000);
  assert.equal(h.statusCalls, 0);
  assert.equal(h.transcription.hidden, true);
  await h.dispatch("capture-stop-playback");
  console.log("PASS capture-only mode never polls or advertises Google transcription");
}
{
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const h = await recording({ transcriptionEnabled: true, statusReplies: [pending,
    { status: "complete", windows_done: 3, windows_total: 3, sentences: 12 }] });
  assert.equal(h.statusCalls, 1);
  await h.advance(5000);
  assert.equal(h.statusCalls, 1, "Only one status request may be in flight");
  release({ status: "waiting_for_audio", windows_done: 1, windows_total: 2, sentences: 4 });
  await flush();
  await h.advance(2500);
  assert.equal(h.statusCalls, 2);
  assert.match(h.transcription.textContent, /完成.*12/);
  await h.advance(10000);
  assert.equal(h.statusCalls, 2, "Completion must stop status polling");
  await h.dispatch("capture-stop-playback");
  console.log("PASS enabled status polling is single-flight and stops after count-only completion");
}
{
  const h = await recording({ transcriptionEnabled: true, statusReplies: [
    { status: "waiting_for_audio", windows_done: 1, windows_total: 2, sentences: 4 },
    { status: "complete", windows_done: 3, windows_total: 3, sentences: 12, raw_transcript: "unannotated-private-marker" },
  ] });
  await h.dispatch("capture-saved", { transcriptionEnabled: true, manifest: {
    id: sessionId, audio_url: `/captures/${sessionId}/audio`, expected_batches: 60, audio_size_bytes: 2000000,
  } });
  await h.dispatch("capture-reset", { complete: true });
  await h.advance(2500);
  assert.equal(h.statusCalls, 2);
  assert.match(h.transcription.textContent, /完成.*12/);
  assert.equal(h.transcription.textContent.includes("unannotated-private-marker"), false);
  console.log("PASS saving audio releases capture while Google progress continues without displaying raw Japanese");
}
{
  const h = await recording({ transcriptionEnabled: true, statusReplies: [
    { status: "needs_attention", windows_done: 1, windows_total: 2, sentences: 4, unknown_windows: 1 },
  ] });
  await h.advance(5000);
  assert.equal(h.statusCalls, 1);
  assert.match(h.transcription.textContent, /结果未知.*不会自动重试/);
  await h.dispatch("capture-stop-playback");
  console.log("PASS unknown request outcomes stop polling and require local inspection without paid retry");
}
for (const action of ["cancel", "close"]) {
  const h = await recording({ transcriptionEnabled: true });
  h[action].click();
  await flush();
  await h.advance(5000);
  assert.equal(h.statusCalls, 1);
  await h.dispatch("capture-stop-playback");
}
console.log("PASS cancellation and panel closure stop background status polling");

function assertInvalidated(h) {
  assert.match(h.status.textContent, /刷新当前 YouTube 页面/);
  assert.equal(h.begin.disabled, true);
  assert.equal(h.cancel.disabled, true);
  assert.ok(h.inputs.every((input) => input.disabled));
  assert.equal(h.transcription.hidden, true);
  assert.equal(h.timerCount, 0, "An invalidated page must release all preparation, playback, and polling timers");
  return h.status.textContent;
}
for (const mode of ["sync", "async"]) {
  const h = harness({ duration: 120 });
  assert.equal((await h.configure()).ok, true);
  h.invalidateContext({ clearId: false, mode, type: "capture-begin" });
  assert.doesNotThrow(() => h.begin.click());
  await flush();
  assert.equal(h.runtimeFailures, 1, "The sendMessage failure must occur while runtime.id still exists");
  const notice = assertInvalidated(h);
  h.begin.click();
  await h.advance(9000);
  assert.equal(h.runtimeFailures, 1);
  assert.equal(h.status.textContent, notice, "The ordinary startup failure handler must not overwrite the refresh notice");
  console.log(`PASS ${mode} context invalidation during startup disables stale controls without an uncaught error`);
}
{
  const h = harness({ duration: 120 });
  assert.equal((await h.configure()).ok, true);
  h.invalidateContext({ clearId: false, type: "capture-cancel" });
  assert.doesNotThrow(() => h.cancel.click());
  await flush();
  assert.equal(h.runtimeFailures, 1);
  assertInvalidated(h);
  console.log("PASS cancellation reports safely handle a synchronous invalidated runtime");
}
{
  const h = await recording();
  h.invalidateContext({ clearId: false, type: "playback-event" });
  assert.doesNotThrow(() => h.video.emit("waiting"));
  await flush();
  assert.equal(h.runtimeFailures, 1);
  const notice = assertInvalidated(h);
  assert.equal(h.video.paused, true, "Stale capture playback must stop when the runtime is invalidated");
  for (const event of ["playing", "waiting", "pause", "seeking", "timeupdate", "ratechange", "ended"]) {
    assert.equal((h.video.listeners.get(event) || []).length, 0, `The ${event} capture listener must be removed`);
  }
  const sent = h.messages.length;
  await h.advance(9000);
  assert.equal(h.messages.length, sent, "Waiting callbacks must not recreate a stall timer or send later errors");
  assert.equal(h.status.textContent, notice);
  assert.equal(h.timerCount, 0);
  console.log("PASS synchronous playback-report invalidation removes listeners and cannot recreate delayed work");
}
{
  let rejectStatus;
  const pending = new Promise((_, reject) => { rejectStatus = reject; });
  const h = await recording({ transcriptionEnabled: true, statusReplies: [pending] });
  assert.equal(h.statusCalls, 1);
  rejectStatus(new Error("Extension context invalidated."));
  await flush();
  const notice = assertInvalidated(h);
  assert.equal(h.video.paused, true);
  const sent = h.messages.length;
  await h.advance(10000);
  assert.equal(h.statusCalls, 1);
  assert.equal(h.messages.length, sent);
  assert.equal(h.status.textContent, notice, "Late polling failure handling must preserve the refresh notice");
  console.log("PASS delayed status rejection invalidates the page and stops future polling without overwriting the notice");
}
{
  const h = harness();
  let responded = false;
  void h.configure().then(() => { responded = true; });
  await flush();
  assert.ok(h.metadataListeners() > 0);
  h.invalidateContext();
  await h.advance(200);
  const notice = assertInvalidated(h);
  assert.equal(h.metadataListeners(), 0);
  h.video.duration = 120;
  h.video.emit("loadedmetadata");
  await h.advance(9000);
  assert.equal(h.status.textContent, notice);
  assert.equal(h.begin.disabled, true);
  assert.equal(responded, false, "The old context must not respond to its abandoned configuration request");
  console.log("PASS runtime removal cancels metadata preparation and late readiness cannot revive the page");
}
{
  const h = harness({ duration: 120 });
  assert.equal((await h.configure()).ok, true);
  let responded = false;
  void h.dispatch("prepare", { rangeStart: 30, rangeEnd: 90, batchIntervalMs: 5000 }).then(() => { responded = true; });
  await flush();
  assert.equal((h.video.listeners.get("seeked") || []).length, 1);
  h.invalidateContext();
  assert.doesNotThrow(() => h.cancel.click());
  await flush();
  const notice = assertInvalidated(h);
  assert.equal((h.video.listeners.get("seeked") || []).length, 0);
  h.video.emit("seeked");
  await h.advance(9000);
  assert.equal(h.status.textContent, notice);
  assert.equal(responded, false, "The old context must not respond to its abandoned seek request");
  assert.equal(h.messages.some((message) => message.type === "capture-cancel"), false,
    "Cancellation after runtime removal must not attempt to contact the dead worker");
  console.log("PASS invalidation during a pending seek releases its listener and timeout without stale responses");
}
{
  const h = await recording();
  const handshake = await h.dispatch("page-handshake");
  assert.equal(handshake.ok, true);
  assert.equal(handshake.version, "0.4.3");
  assert.equal(handshake.protocol, 1);
  assert.equal(h.panelVersion, "v0.4.3");
  assert.equal(h.injectionResult.version, handshake.version);
  assert.equal(h.reinject().version, handshake.version);
  assert.equal(h.panelCount, 1);
  assert.equal(h.video.paused, false, "Repeated injection must preserve a healthy active recording");
  await h.dispatch("capture-stop-playback");
  console.log("PASS page injection and handshake identify the visible build without replacing a healthy capture");
}
{
  const h = harness({ duration: 120 });
  assert.equal((await h.configure()).ok, true);
  h.invalidateContext();
  const reply = h.reinject();
  assert.equal(reply.ok, false);
  assert.equal(reply.reason, "dead_controller");
  assert.equal(h.panelCount, 1);
  assertInvalidated(h);
  console.log("PASS reinjection refuses a dead page controller instead of silently treating it as initialized");
}
{
  const h = harness({ duration: 120 });
  h.forgetPageMarker();
  const reply = h.reinject();
  assert.equal(reply.ok, false);
  assert.equal(reply.reason, "orphan_root");
  assert.equal(h.panelCount, 1, "An orphaned panel from an older isolated world must not be duplicated");
  console.log("PASS orphaned DOM from an older extension context requires a page reload before capture");
}
{
  const result = vm.runInNewContext(source, { window: { __snsJapaneseAgentPage: true } });
  assert.equal(result.ok, false);
  assert.equal(result.reason, "legacy_controller");
  console.log("PASS a legacy boolean page marker is recognized as stale instead of falsely reporting readiness");
}
{
  const h = harness({ duration: 120, foreignPanel: true });
  assert.equal(h.injectionResult.ok, true);
  assert.equal((await h.configure()).ok, true);
  assert.equal((await h.dispatch("page-handshake")).version, "0.4.3");
  assert.equal(h.panelCount, 1);
  assert.equal(h.foreignPanelConnected, true, "The new extension must not remove another installation's panel");
  assert.equal(h.begin.disabled, false);
  console.log("PASS a legacy panel from another extension cannot block the current extension's namespaced panel");
}
console.log("32 offline page lifecycle checks passed.");
