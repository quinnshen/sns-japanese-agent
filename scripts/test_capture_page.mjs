/** Offline page lifecycle regressions. No browser, credentials, media, or network. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const source = await readFile(new URL("../app/extension/page.js", import.meta.url), "utf8");
const sessionId = "00000000-0000-4000-8000-000000000001";
const extensionId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
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

function harness({ duration = NaN, pendingPlay = false, failBegin = false, shorts = false } = {}) {
  const timers = new Map();
  const messages = [];
  let time = 0;
  let nextTimer = 1;
  let listener;
  let releasePlay;
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
  const location = { href: shorts ? "https://www.youtube.com/shorts/AbCdEf012_-" : "https://www.youtube.com/watch?v=AbCdEf012_-" };
  const setTimer = (fn, delay, interval = false) => {
    const id = nextTimer++;
    timers.set(id, { fn, at: time + delay, interval: interval ? delay : 0 });
    return id;
  };
  const context = vm.createContext({
    window: {}, document, location, URL, Number, Error, Promise,
    innerHeight: 1000, innerWidth: 1200,
    Date: class extends Date { static now() { return time; } },
    performance: { now: () => time },
    HTMLMediaElement: { HAVE_NOTHING: 0, HAVE_METADATA: 1, HAVE_CURRENT_DATA: 2, HAVE_FUTURE_DATA: 3, HAVE_ENOUGH_DATA: 4 },
    setTimeout: (fn, delay) => setTimer(fn, delay), clearTimeout: (id) => timers.delete(id),
    setInterval: (fn, delay) => setTimer(fn, delay, true), clearInterval: (id) => timers.delete(id),
    chrome: { runtime: {
      id: extensionId,
      onMessage: { addListener(fn) { listener = fn; } },
      async sendMessage(message) {
        messages.push(structuredClone(message));
        if (failBegin && message.type === "capture-begin") throw new Error("Synthetic startup failure");
        return { ok: true };
      },
    } },
  });
  vm.runInContext(source, context);
  const root = document.documentElement.children.find((element) => element.id === "sns-japanese-agent-root");
  assert.ok(root?.shadowRoot, "The actual page script must render its panel");
  const elements = [];
  const visit = (element) => { elements.push(element); element.children.forEach(visit); };
  visit(root.shadowRoot);
  const begin = elements.find((element) => element.tagName === "BUTTON" && element.textContent === "开始采集");
  const cancel = elements.find((element) => element.tagName === "BUTTON" && element.textContent === "中断／释放音频");
  const inputs = elements.filter((element) => element.tagName === "INPUT");
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
    video, messages, location, begin, cancel, inputs, status, dispatch, advance, metadataListeners,
    get player() { return player; }, unrelatedPlayer, previews,
    replacePlayer(replacement) { player = replacement; },
    releasePlay: () => releasePlay?.(),
    async configure() {
      await dispatch("capture-configuring");
      return dispatch("capture-configure");
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
console.log("15 offline page lifecycle checks passed.");
