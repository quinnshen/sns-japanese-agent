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
    this.dataset = {};
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
    return (!tag || this.tagName.toLowerCase() === tag) && (!className || this.classList.contains(className) || this.className?.split(" ").includes(className));
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
  click() {
    if (this.disabled) return;
    this.emit("click");
    if (this.tagName === "SUMMARY" && this.parentElement?.tagName === "DETAILS") {
      this.parentElement.open = !this.parentElement.open;
      this.parentElement.emit("toggle");
    }
  }
  getBoundingClientRect() { return { top: 0, left: 0, right: 640, bottom: 360 }; }
}

function harness({ duration = NaN, pendingPlay = false, failBegin = false, shorts = false, transcriptionEnabled = false, statusReplies = [], foreignPanel = false,
  enrichmentEnabled = false, learningReplies = [], learningLoadReply = { capture_id: null, status: "not_found", result: null },
  rearmReply = { ok: false, error: "音频未重新连接，请点击浏览器工具栏 S 重新连接音频。" } } = {}) {
  const timers = new Map();
  const messages = [];
  let time = 0;
  let nextTimer = 1;
  let listener;
  let releasePlay;
  let statusCalls = 0;
  let learningCalls = 0;
  let learningLoadCalls = 0;
  let playCalls = 0;
  let runtimeFailure;
  let runtimeFailures = 0;
  const video = new FakeElement("video");
  Object.assign(video, {
    duration, currentTime: 0, paused: true, ended: false, seeking: false, loop: false,
    playbackRate: 1, readyState: Number.isFinite(duration) ? 1 : 0, networkState: 2, error: null,
  });
  video.pause = () => { video.paused = true; };
  video.play = () => {
    playCalls++;
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
    if (message.type === "capture-rearm") return typeof rearmReply === "function" ? rearmReply(message) : rearmReply;
    if (message.type === "capture-transcription-status") {
      const reply = statusReplies[statusCalls++] ?? { status: "waiting_for_audio", windows_done: 0, windows_total: 0, sentences: 0 };
      const value = typeof reply === "function" ? await reply() : await reply;
      return { ok: true, transcription: value };
    }
    if (message.type === "capture-learning-load" || message.type === "capture-learning-status") {
      const reply = message.type === "capture-learning-load" ? (learningLoadCalls++, learningLoadReply) :
        learningReplies[learningCalls++] ?? { capture_id: sessionId, status: "waiting_for_transcription", sentences_done: 0, sentences_total: 0, result: { sentences: [] } };
      const value = typeof reply === "function" ? await reply() : await reply;
      return { ok: true, learning: value };
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
  const learning = elements.find((element) => element.className === "learning-status");
  const load = elements.find((element) => element.textContent === "读取已保存学习内容");
  const list = elements.find((element) => element.className === "list");
  const subtitle = elements.find((element) => element.className === "subtitle");
  const captureForm = elements.find((element) => element.className === "capture-options");
  const captureSummary = elements.find((element) => element.className === "capture-summary");
  const learningToolbar = elements.find((element) => element.className === "learning-toolbar");
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
    video, messages, location, begin, cancel, close, inputs, status, transcription, learning, load, list, subtitle, captureForm, captureSummary, learningToolbar, dispatch, advance, metadataListeners,
    get learningCalls() { return learningCalls; }, get learningLoadCalls() { return learningLoadCalls; }, get playCalls() { return playCalls; },
    injectionResult,
    reinject: () => vm.runInContext(source, context),
    forgetPageMarker: () => { delete context.window.__snsJapaneseAgentPage; },
    get panelCount() { return document.documentElement.children.filter((element) => element.id === rootId).length; },
    get foreignPanelConnected() { return foreignRoot?.isConnected === true && foreignRoot.parentElement === document.documentElement; },
    get panelVersion() { return elements.find((element) => element.className === "version")?.textContent; },
    get panelDisplay() { return root.style.display; },
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
      await dispatch("capture-configuring", { transcriptionEnabled, enrichmentEnabled });
      return dispatch("capture-configure", { transcriptionEnabled, enrichmentEnabled });
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
  assert.equal(h.begin.disabled, false);
  assert.equal(h.begin.textContent, "重新连接音频");
  assert.equal(h.metadataListeners(), 0);
  h.video.duration = 120;
  h.video.emit("loadedmetadata");
  await flush();
  assert.equal(h.begin.textContent, "重新连接音频", "Late metadata must not turn released audio back into a ready capture");
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
  assert.equal(h.learningCalls, 0, "Capture-only mode must not start learning polls for the new capture");
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
  assert.equal(handshake.version, "0.5.4");
  assert.equal(handshake.protocol, 1);
  assert.equal(h.panelVersion, "v0.5.4");
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
  assert.equal((await h.dispatch("page-handshake")).version, "0.5.4");
  assert.equal(h.panelCount, 1);
  assert.equal(h.foreignPanelConnected, true, "The new extension must not remove another installation's panel");
  assert.equal(h.begin.disabled, false);
  console.log("PASS a legacy panel from another extension cannot block the current extension's namespaced panel");
}
const cachedCaptureId = "00000000-0000-4000-8000-000000000099";
function studySentence(overrides = {}) {
  return { id: "s001", text: "何が必要か。", start: 1, end: 3, mapping_uncertain: false,
    translation_zh: "需要什么呢？", study: { style_version: 2, reading_text: "何(なに)が必要(ひつよう)か。",
      key_points: ["疑问小句：提出问题。"], context_note: "", asr_doubt: "" }, ...overrides };
}
function learningValue({ status = "complete", captureId = cachedCaptureId, revision = 1, items = [studySentence()], total = items.length } = {}) {
  return { capture_id: captureId, status, revision, sentences_done: items.length, sentences_total: total,
    batches_done: status === "complete" ? 1 : 0, batches_total: 1, result: { sentences: items } };
}
function allText(element) { return [element.textContent, ...element.children.map(allText)].join("\n"); }
{
  const h = harness({ duration: 120, learningLoadReply: learningValue({ items: [studySentence(),
    studySentence({ id: "s002", start: null, end: null, mapping_uncertain: true }),
    { id: "s003", text: "未注音の原文。", start: 4, end: 5 },
  ] }) });
  await h.configure(); await flush();
  assert.equal(h.learningLoadCalls, 1);
  assert.equal(h.learningCalls, 0);
  assert.equal(h.playCalls, 0, "Loading cached notes must not begin playback");
  assert.equal(h.video.currentTime, 0);
  assert.equal(h.list.children.length, 2);
  assert.match(allText(h.list), /何\(なに\)が必要\(ひつよう\)か。/);
  assert.match(allText(h.list), /需要什么呢/);
  assert.equal(allText(h.list).includes("未注音の原文"), false);
  const unmapped = h.list.children[1];
  assert.equal(unmapped.children[0].disabled, true);
  assert.match(allText(unmapped), /时间待确认/);
  const messages = h.messages.length;
  unmapped.children[1].click();
  assert.equal(unmapped.children[2].hidden, false, "An unmapped sentence must still offer its saved analysis");
  assert.equal(h.messages.length, messages, "Analysis expansion must be local DOM only");
  unmapped.children[0].click();
  assert.equal(h.video.currentTime, 0, "An unknown video time must never become a seek to zero");
  h.list.children[0].children[0].click(); await flush();
  assert.equal(h.video.currentTime, 1);
  assert.equal(h.playCalls, 1);
  assert.match(h.subtitle.textContent, /何\(なに\)が必要\(ひつよう\)か。\n需要什么呢/);
  assert.equal(h.list.children[0].classList.contains("active"), true);
  assert.equal(h.messages.some((message) => ["capture-begin", "page-started", "capture-transcription-status"].includes(message.type)), false);
  h.begin.click(); await flush();
  assert.equal(h.messages.find((message) => message.type === "capture-begin").sessionId, sessionId,
    "Loading an older cached capture must not replace the live recording session ID");
  console.log("PASS cached annotated bilingual notes load without recording, and null times disable seeking while local analysis remains usable");
}
{
  const first = learningValue({ status: "running", total: 2 });
  const second = learningValue({ revision: 2, items: [studySentence(), studySentence({ id: "s002", start: 4, end: 6 })] });
  const h = harness({ duration: 120, learningLoadReply: first, learningReplies: [second] });
  await h.configure(); await flush();
  h.list.children[0].children[1].click();
  h.list.scrollTop = 123;
  await h.advance(2500);
  assert.equal(h.list.children.length, 2);
  assert.equal(h.list.children[0].children[2].hidden, false);
  assert.equal(h.list.children[0].children[1].textContent, "收起解析");
  assert.equal(h.list.scrollTop, 123);
  assert.equal(h.playCalls, 0);
  assert.match(h.learning.textContent, /完成.*2\/2/);
  console.log("PASS learning revisions preserve expanded analyses and list position as later notes arrive");
}
{
  const h = await recording({ transcriptionEnabled: true, enrichmentEnabled: true,
    statusReplies: [{ status: "complete", windows_done: 3, windows_total: 3, sentences: 1 }],
    learningReplies: [learningValue({ status: "waiting_for_transcript", captureId: sessionId, items: [], total: 1 }),
      learningValue({ captureId: sessionId })] });
  assert.equal(h.statusCalls, 1);
  assert.equal(h.learningCalls, 1);
  await h.advance(2500);
  assert.equal(h.learningCalls, 2, "Google completion must not stop the independent learning poll");
  assert.equal(h.list.children.length, 1);
  assert.equal(h.list.children[0].children[0].disabled, true, "New notes must not let the learner seek during capture");
  const messages = h.messages.length;
  h.list.children[0].children[1].click();
  assert.equal(h.messages.length, messages);
  await h.dispatch("capture-reset", { complete: true });
  assert.equal(h.list.children[0].children[0].disabled, false);
  assert.equal(h.statusCalls, 1);
  console.log("PASS Google and learning polls finish independently and analysis stays local during protected capture playback");
}
{
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const h = await recording({ enrichmentEnabled: true, learningReplies: [pending, learningValue({ captureId: sessionId })] });
  await h.advance(5000);
  assert.equal(h.learningCalls, 1, "Only one learning request may be in flight");
  release(learningValue({ captureId: sessionId, status: "running" })); await flush();
  await h.advance(2500);
  assert.equal(h.learningCalls, 2);
  await h.dispatch("capture-reset", { complete: true });
  await h.advance(10000);
  assert.equal(h.learningCalls, 2, "Completed learning must stop status polling");
  console.log("PASS learning polling is single-flight and stops after its own completion");
}
for (const action of ["cancel", "close"]) {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const h = await recording({ enrichmentEnabled: true, learningReplies: [pending] });
  h[action].click(); await flush();
  release(learningValue({ captureId: sessionId })); await flush();
  await h.advance(5000);
  assert.equal(h.learningCalls, 1);
  assert.equal(h.list.children.length, 0, "A late learning reply must not reopen a cancelled or closed display");
  assert.equal(h.subtitle.textContent, "");
  await h.dispatch("capture-stop-playback");
}
console.log("PASS cancellation and closure ignore late learning responses and stop learning polling");
{
  let reject;
  const pending = new Promise((_, rejectPromise) => { reject = rejectPromise; });
  const h = await recording({ enrichmentEnabled: true, learningReplies: [pending] });
  reject(new Error("Extension context invalidated.")); await flush();
  assertInvalidated(h);
  assert.equal(h.learning.hidden, true);
  assert.equal(h.load.disabled, true);
  await h.advance(10000);
  assert.equal(h.learningCalls, 1);
  console.log("PASS a delayed learning context error performs the same complete stale-page cleanup");
}
{
  const h = harness({ duration: 120 });
  await h.configure(); await flush();
  await h.dispatch("result", { result: { sentences: [
    { id: "bad", text: "帰っていい。", start: 1, end: 2 },
    studySentence({ id: "rewrite", text: "帰る。", study: { style_version: 2, reading_text: "来(く)る。", key_points: [] } }),
    { id: "kana", text: "そう(かな)。", start: 3, end: 4 },
  ] } });
  assert.equal(h.list.children.length, 1);
  assert.match(allText(h.list), /そう\(かな\)。/);
  assert.equal(allText(h.list).includes("帰っていい"), false);
  console.log("PASS legacy result display cannot fall back to unannotated kanji, while kana-only originals remain readable");
}
{
  const h = harness({ duration: 120, learningLoadReply: learningValue({ status: "not_started", items: [] }) });
  await h.configure(); await flush();
  await h.advance(10000);
  assert.equal(h.learningCalls, 0);
  assert.equal(h.learningLoadCalls, 1);
  assert.match(h.learning.textContent, /自动处理未启用/);
  console.log("PASS disabled enrichment stops at an unprocessed cached capture without a paid fallback or endless polling");
}
{
  const attention = { ...learningValue({ status: "needs_attention", total: 2 }), batches_done: 0, batches_total: 2, blocked_batches: 1 };
  const h = harness({ duration: 120, enrichmentEnabled: true, learningLoadReply: attention,
    learningReplies: [learningValue({ status: "running", revision: 2, total: 2 }),
      learningValue({ revision: 3, items: [studySentence(), studySentence({ id: "s002", start: 4, end: 6 })] })] });
  await h.configure(); await flush();
  assert.match(h.learning.textContent, /正在确认进度/);
  await h.advance(2500);
  assert.equal(h.learningCalls, 1);
  await h.advance(2500);
  assert.equal(h.learningCalls, 2);
  assert.equal(h.list.children.length, 2);
  assert.match(h.learning.textContent, /完成.*2\/2/);
  const requests = h.messages.filter((message) => message.type.startsWith("capture-learning"));
  assert.ok(requests.every((message) => ["capture-learning-load", "capture-learning-status"].includes(message.type)));
  console.log("PASS a transient partial-batch warning keeps polling independent work until later notes complete");
}
{
  const attention = { ...learningValue({ status: "needs_attention", total: 2 }), batches_done: 0, batches_total: 2, blocked_batches: 1 };
  const h = harness({ duration: 120, enrichmentEnabled: true, learningLoadReply: attention, learningReplies: [attention] });
  await h.configure(); await flush();
  await h.advance(2500);
  assert.equal(h.learningCalls, 1);
  await h.advance(10000);
  assert.equal(h.learningCalls, 1, "Two identical needs-attention observations must stop polling within a bounded grace period");
  assert.match(h.learning.textContent, /需检查/);
  console.log("PASS a persistently unchanged partial-batch warning stops after two matching observations");
}
{
  const h = harness({ duration: 120 });
  await h.configure(); await flush();
  assert.equal(h.captureForm.tagName, "DETAILS");
  assert.equal(h.captureSummary.tagName, "SUMMARY");
  assert.equal(h.captureForm.open, true, "Without cached notes the capture fields must be available by default");
  assert.equal(h.captureForm.contains(h.learning), false);
  assert.equal(h.captureForm.contains(h.load), false);
  assert.equal(h.captureForm.contains(h.cancel), false);
  assert.equal(h.learningToolbar.contains(h.cancel), true, "Release must remain accessible outside the collapsed form");
  h.captureSummary.click();
  assert.equal(h.captureForm.open, false);
  h.captureSummary.click();
  h.begin.click(); await flush();
  assert.equal(h.messages.find((message) => message.type === "capture-begin").sessionId, sessionId);
  console.log("PASS capture settings use a native disclosure while learning and release controls stay outside it");
}
{
  const h = harness({ duration: 120, enrichmentEnabled: true,
    learningLoadReply: learningValue({ status: "running", total: 2 }),
    learningReplies: [learningValue({ revision: 2, items: [studySentence(), studySentence({ id: "s002", start: 4, end: 6 })] })] });
  await h.configure(); await flush();
  assert.equal(h.captureForm.open, false, "Ready notes must automatically reclaim space from the capture form");
  assert.equal(h.list.children.length, 1);
  h.captureSummary.click();
  assert.equal(h.captureForm.open, true);
  h.list.children[0].children[1].click();
  await h.advance(2500);
  assert.equal(h.captureForm.open, true, "A later revision must respect the learner's explicit expansion");
  assert.equal(h.list.children.length, 2);
  assert.equal(h.list.children[0].children[2].hidden, false);
  h.captureSummary.click();
  assert.equal(h.captureForm.open, false);
  h.cancel.click(); await flush();
  assert.equal(h.messages.filter((message) => message.type === "capture-cancel").length, 1);
  assert.equal(h.list.children.length, 2, "Collapsing or cancelling capture must not delete already displayed notes");
  console.log("PASS ready notes collapse settings once and later notes preserve manual expansion and open analysis");
}
{
  const h = await recording({ enrichmentEnabled: true, learningReplies: [learningValue({ captureId: sessionId })] });
  await flush();
  assert.equal(h.captureForm.open, false);
  assert.equal(h.video.paused, false, "Automatic form collapse must not interrupt recording playback");
  assert.equal(h.cancel.disabled, false);
  assert.equal(h.learningToolbar.contains(h.cancel), true);
  h.cancel.click(); await flush();
  assert.equal(h.messages.filter((message) => message.type === "capture-cancel").length, 1);
  await h.dispatch("capture-stop-playback");
  assert.equal(h.video.paused, true);
  console.log("PASS automatic learning layout changes preserve live recording and its always-visible release action");
}
{
  const h = harness({ duration: 120, learningLoadReply: learningValue({ items: [
    studySentence({ mapping_uncertain: true }),
    studySentence({ id: "s002", start: 4, end: 6, mapping_uncertain: true }),
  ] }) });
  await h.configure(); await flush();
  const note = "字幕时间由播放记录估算。";
  assert.equal(h.learningToolbar.children.find((element) => element.className === "alignment-note").hidden, false);
  assert.equal(allText(h.learningToolbar).split(note).length - 1, 1,
    "Estimated mapped times should produce one shared informational note");
  assert.equal(allText(h.list).includes("时间映射有误差"), false);
  assert.equal(allText(h.list).includes("对齐待核对"), false);
  assert.equal(allText(h.list).includes(note), false);
  assert.ok(h.list.children.every((entry) => entry.children[0].disabled === false),
    "A mapped estimate remains usable for sentence playback");
  console.log("PASS mapped timing uncertainty is explained once without repetitive per-sentence error warnings");
}
{
  for (const [state, pendingBatches] of [["failed", 0], ["unknown", 4], ["recording", 0], ["saving", 1]]) {
    const h = harness({ duration: 120 });
    assert.equal(h.cancel.disabled, true);
    const reply = await h.dispatch("capture-recovery", { sessionId: cachedCaptureId, recoveryState: state, pendingBatches });
    assert.equal(reply.ok, true);
    assert.equal(h.cancel.disabled, false, "A fresh page must bind the retained session before enabling release");
    assert.equal(h.begin.disabled, true);
    assert.ok(h.inputs.every((input) => input.disabled));
    assert.equal(h.playCalls, 0);
    assert.equal(h.messages.length, 0, "Recovery binding must not send capture, upload or cloud work");
    h.cancel.click(); await flush();
    assert.equal(h.messages.find((message) => message.type === "capture-cancel").sessionId, cachedCaptureId);
    await h.dispatch("capture-reset", { sessionId: cachedCaptureId });
    assert.equal((await h.configure()).ok, true);
    assert.equal(h.begin.disabled, false, "After release a new preparation can enable capture normally");
  }
  console.log("PASS fresh pages can explicitly release retained failed, unknown, recording and saving sessions before retrying");
}
{
  const h = harness({ duration: 120, learningLoadReply: learningValue() });
  await h.configure(); await flush();
  await h.dispatch("capture-recovery", { sessionId: cachedCaptureId, recoveryState: "unknown" });
  assert.equal(h.list.children.length, 1, "Recovery must preserve cached learning notes");
  assert.equal(h.list.children[0].children[0].disabled, true, "An unknown capture must not be disrupted by seeking");
  const messages = h.messages.length;
  h.list.children[0].children[1].click();
  assert.equal(h.list.children[0].children[2].hidden, false);
  assert.equal(h.messages.length, messages, "Expanding existing notes remains local during recovery");
  assert.equal((await h.dispatch("prepare", { sessionId: cachedCaptureId, rangeStart: 0, rangeEnd: 2, batchIntervalMs: 5000 })).ok, false);
  assert.equal((await h.dispatch("play", { sessionId: cachedCaptureId })).ok, false);
  assert.equal(h.playCalls, 0);
  await h.dispatch("capture-reset", { sessionId: cachedCaptureId });
  assert.equal(h.list.children[0].children[0].disabled, false);
  console.log("PASS recovery preserves learning analyses while preventing playback and capture until release");
}
{
  const h = await recording();
  const listenerCount = (h.video.listeners.get("timeupdate") || []).length;
  assert.equal((await h.dispatch("capture-recovery", { recoveryState: "recording" })).ok, true);
  assert.equal(h.video.paused, false);
  assert.equal((h.video.listeners.get("timeupdate") || []).length, listenerCount);
  assert.equal(h.cancel.disabled, false);
  assert.equal((await h.dispatch("capture-recovery", { sessionId: cachedCaptureId, recoveryState: "unknown" })).ok, false);
  h.video.currentTime = 300; h.video.emit("timeupdate"); await flush();
  assert.equal(h.messages.filter((message) => message.type === "page-ended").length, 1,
    "Recovering controls must not remove an active controller's natural completion listeners");
  console.log("PASS reopening an active capture keeps playback and completion protected without switching sessions");
}
{
  const h = harness({ duration: 120 });
  for (const payload of [{ sessionId: "invalid", recoveryState: "failed" }, { sessionId: cachedCaptureId, recoveryState: "arbitrary" }]) {
    assert.equal((await h.dispatch("capture-recovery", payload)).ok, false);
    assert.equal(h.cancel.disabled, true);
  }
  assert.equal(h.messages.length, 0);
  console.log("PASS malformed recovery messages cannot bind release controls or dispatch work");
}
{
  let resolveRearm;
  const h = harness({ duration: 1800, rearmReply: () => new Promise((resolve) => { resolveRearm = resolve; }) });
  await h.configure();
  await h.dispatch("capture-reset");
  assert.equal(h.begin.disabled, false, "Released audio must offer an actual reconnect action");
  assert.equal(h.begin.textContent, "重新连接音频");
  h.inputs[0].value = "16:00"; h.inputs[0].emit("input");
  h.inputs[1].value = "18:00"; h.inputs[1].emit("input");
  h.inputs[2].value = "7"; h.inputs[2].emit("input");
  h.begin.click(); h.begin.click(); await flush();
  assert.equal(h.messages.filter((message) => message.type === "capture-rearm").length, 1);
  assert.equal(h.begin.disabled, true);
  assert.ok(h.inputs.every((input) => input.disabled));
  assert.equal(h.playCalls, 0);
  const newSessionId = "00000000-0000-4000-8000-000000000003";
  await h.dispatch("capture-configuring", { sessionId: newSessionId });
  await h.dispatch("capture-configure", { sessionId: newSessionId });
  assert.equal(h.begin.disabled, true, "A pending reconnect must not enable duplicate starts");
  resolveRearm({ ok: true, sessionId: newSessionId }); await flush();
  assert.equal(h.begin.disabled, false);
  assert.equal(h.begin.textContent, "开始采集");
  assert.deepEqual(h.inputs.map((input) => input.value), ["16:00", "18:00", "7"]);
  assert.equal(h.messages.some((message) => message.type === "capture-begin"), false,
    "Connecting audio must not start recording, seek or initiate cloud processing");
  h.begin.click(); await flush();
  const begin = h.messages.find((message) => message.type === "capture-begin");
  assert.equal(begin.sessionId, newSessionId);
  assert.equal(begin.rangeStart, 960); assert.equal(begin.rangeEnd, 1080); assert.equal(begin.batchIntervalMs, 7000);
  console.log("PASS released audio reconnects once, preserves the selected range and waits for an explicit recording start");
}
{
  for (const rearmReply of [undefined, { ok: true, sessionId }, { ok: false, error: "请点击浏览器工具栏 S 重新连接音频。" }]) {
    const h = harness({ duration: 1800, rearmReply });
    await h.configure(); await h.dispatch("capture-reset");
    h.inputs[0].value = "16:00"; h.inputs[0].emit("input");
    h.inputs[1].value = "18:00"; h.inputs[1].emit("input");
    h.begin.click(); await flush();
    assert.match(h.status.textContent, /工具栏 S/);
    assert.equal(h.begin.disabled, false);
    assert.equal(h.begin.textContent, "重新连接音频");
    assert.ok(h.inputs.every((input) => !input.disabled));
    assert.equal(h.inputs[0].value, "16:00"); assert.equal(h.inputs[1].value, "18:00");
    assert.equal(h.messages.some((message) => message.type === "capture-begin"), false);
  }
  console.log("PASS failed or unconfirmed reconnection remains actionable and explains the toolbar fallback without starting capture");
}
{
  const h = harness({ duration: 1800 });
  await h.configure();
  h.inputs[0].value = "16:00"; h.inputs[0].emit("input");
  h.inputs[1].value = "18:00"; h.inputs[1].emit("input");
  await h.dispatch("capture-reset");
  await h.configure();
  assert.equal(h.inputs[0].value, "16:00"); assert.equal(h.inputs[1].value, "18:00");
  h.video.duration = 1000;
  await h.configure(); h.begin.click(); await flush();
  assert.match(h.status.textContent, /有效的起止时间/);
  assert.equal(h.messages.some((message) => message.type === "capture-begin"), false,
    "A preserved selection must still be checked against the current video duration");
  h.location.href = "https://www.youtube.com/watch?v=ZyXwVu987_-";
  await h.configure();
  assert.deepEqual(h.inputs.map((input) => input.value), ["0:00", "1000", "5"], "A different video must not inherit the old range");
  console.log("PASS toolbar reconnection preserves same-video selections, revalidates duration and resets selections for a different video");
}
{
  const h = harness({ duration: 1800 });
  await h.dispatch("capture-recovery", { sessionId: cachedCaptureId, recoveryState: "failed" });
  await h.dispatch("capture-reset", { sessionId: cachedCaptureId });
  h.inputs[0].value = "16:00"; h.inputs[0].emit("input");
  h.inputs[1].value = "18:00"; h.inputs[1].emit("input");
  await h.configure();
  assert.deepEqual(h.inputs.map((input) => input.value), ["16:00", "18:00", "5"]);
  assert.equal(h.begin.disabled, false);
  console.log("PASS a refreshed recovery panel retains selections entered after release before its first successful preparation");
}
{
  let resolveRearm;
  const h = harness({ duration: 1800, rearmReply: () => new Promise((resolve) => { resolveRearm = resolve; }) });
  await h.configure(); await h.dispatch("capture-reset");
  h.begin.click(); await flush();
  await h.dispatch("capture-configuring");
  h.cancel.click(); await flush();
  await h.dispatch("capture-reset");
  resolveRearm({ ok: true, sessionId }); await flush();
  assert.equal(h.begin.disabled, false);
  assert.equal(h.begin.textContent, "重新连接音频");
  assert.equal(h.messages.some((message) => message.type === "capture-begin"), false);
  assert.equal(h.playCalls, 0);
  console.log("PASS cancelling a pending reconnect ignores late success and requires a fresh connection before recording");
}
{
  const h = harness({ duration: 1800 });
  await h.dispatch("capture-recovery", { sessionId: cachedCaptureId, recoveryState: "failed" });
  await h.dispatch("capture-reset", { sessionId: cachedCaptureId });
  h.inputs[0].value = "16:00"; h.inputs[0].emit("input");
  await h.configure();
  assert.equal(h.inputs[1].value, "");
  h.begin.click(); await flush();
  const begin = h.messages.find((message) => message.type === "capture-begin");
  assert.equal(begin.rangeStart, 960); assert.equal(begin.rangeEnd, 1800);
  console.log("PASS a blank end time uses the current video ending after recovery when only the start was entered");
}
{
  let resolveRearm;
  const h = harness({ duration: 1800, rearmReply: () => new Promise((resolve) => { resolveRearm = resolve; }) });
  await h.configure(); await h.dispatch("capture-reset");
  h.begin.click(); await flush();
  const requestId = h.messages.find((message) => message.type === "capture-rearm").requestId;
  assert.ok(Number.isSafeInteger(requestId) && requestId > 0);
  h.close.click(); await flush();
  assert.equal(h.panelDisplay, "none");
  const payload = { sessionId: cachedCaptureId, rearmRequestId: requestId };
  assert.equal((await h.dispatch("capture-configuring", payload)).ok, false);
  assert.equal((await h.dispatch("capture-configure", payload)).ok, false);
  await h.dispatch("status", { ...payload, text: "Late connection status" });
  resolveRearm({ ok: true, sessionId: cachedCaptureId }); await flush();
  assert.equal(h.panelDisplay, "none", "A dismissed reconnect must not reopen or arm the page later");
  assert.equal(h.messages.some((message) => message.type === "capture-begin"), false);
  assert.equal(h.playCalls, 0);
  await h.configure();
  assert.notEqual(h.panelDisplay, "none", "A later explicit toolbar preparation must still reopen normally");
  console.log("PASS closing before a reconnect binds its new session rejects late preparation and status without blocking a later toolbar action");
}
{
  let resolveRearm;
  const h = harness({ duration: NaN, rearmReply: () => new Promise((resolve) => { resolveRearm = resolve; }) });
  await h.dispatch("capture-configuring"); await h.dispatch("capture-reset");
  h.begin.click(); await flush();
  const requestId = h.messages.find((message) => message.type === "capture-rearm").requestId;
  const payload = { sessionId: cachedCaptureId, rearmRequestId: requestId };
  await h.dispatch("capture-configuring", payload);
  const pendingConfigure = h.dispatch("capture-configure", payload); await flush();
  assert.ok(h.metadataListeners() > 0);
  h.close.click(); await flush();
  assert.equal((await pendingConfigure).ok, false);
  assert.equal(h.metadataListeners(), 0);
  h.video.duration = 1800; h.video.emit("loadedmetadata"); await flush();
  resolveRearm({ ok: false }); await flush();
  assert.equal(h.panelDisplay, "none");
  assert.equal(h.messages.find((message) => message.type === "capture-cancel").sessionId, cachedCaptureId);
  assert.equal(h.messages.some((message) => message.type === "capture-begin"), false);
  console.log("PASS closing during a reconnect metadata wait cancels preparation synchronously and late readiness cannot reopen the panel");
}
console.log("58 offline page lifecycle, layout, and learning checks passed.");
