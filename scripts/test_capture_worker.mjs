/** Offline preparation lifecycle checks; fake Chrome and media, no ports or credentials. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
const workerSource = await readFile(new URL("../app/extension/service-worker.js", import.meta.url), "utf8");
const recorderSource = await readFile(new URL("../app/extension/offscreen.js", import.meta.url), "utf8");
const extensionId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const origin = `chrome-extension://${extensionId}/`;
const videoUrl = "https://www.youtube.com/watch?v=AbCdEf012_-";
const tab = { id: 7, url: videoUrl, active: true };
const pageMetadata = { ok: true, version: "0.6.0", protocol: 1 };
const tick = async () => { for (let i = 0; i < 120; i++) await Promise.resolve(); };
async function until(check) { for (let i = 0; i < 20; i++) { if (check()) return; await tick(); } assert.ok(check(), "Lifecycle did not reach expected state"); }
function deferred() { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; }
function clock() {
  const timers = new Map(); let next = 0;
  return {
    setTimeout(fn, ms) { const id = ++next; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    fire(ms) { const item = [...timers].find(([, timer]) => timer.ms === ms); assert.ok(item, `Missing ${ms}ms deadline`); timers.delete(item[0]); item[1].fn(); },
  };
}
function workerHarness({ initial, documentExists = false, inspect = { ok: true, phase: "ready" }, streamId, stallAbort = false,
  transcriptionEnabled = false, transcriptionReply = {}, injectionResults = [{ frameId: 0, result: pageMetadata }],
  handshakeReply = pageMetadata, statusUnavailable = false, enrichmentEnabled = false,
  learningReply = {}, latestLearningReply = learningReply, currentTabUrl = videoUrl,
  currentTabActive = true, currentTabId = tab.id, closeDocument, configuringReply = { ok: true }, prepareReply,
  libraryReply = {}, favoriteReply = { saved: true }, reviewStatus = 200, failUpdate = false } = {}) {
  const timer = clock(); let action; let listener; let updated; let removed; let stored = initial; let contexts = documentExists;
  const pending = new Map(); const navigations = [];
  let pageSessionId;
  let serial = 0; let closeCount = 0; let streamCalls = 0; let createCount = 0; let contextCalls = 0;
  const pageMessages = []; const offscreenMessages = []; const fetches = [];
  const injections = []; const badges = []; const titles = []; const lifecycle = []; const pageTargets = [];
  const fetchOptions = [];
  const streamArguments = []; const tabGets = [];
  const chrome = {
    action: { onClicked: { addListener(fn) { action = fn; } },
      async setBadgeText(value) { badges.push(structuredClone(value)); }, async setBadgeBackgroundColor() {},
      async setTitle(value) { titles.push(structuredClone(value)); } },
    runtime: {
      id: extensionId, getURL: (path) => origin + path,
      async getContexts() { contextCalls++; return contexts ? [{}] : []; },
      onMessage: { addListener(fn) { listener = fn; } },
      async sendMessage(message) {
        offscreenMessages.push(structuredClone(message));
        if (message.type === "capture-inspect") return typeof inspect === "function" ? inspect() : inspect;
        if (message.type === "capture-abort") return stallAbort ? new Promise(() => {}) : { ok: true, abortConfirmed: true };
        return { ok: true };
      },
    },
    storage: { session: {
      async get(key) { return { [key]: structuredClone(key === "activeCapture" ? stored : pending.get(key)) }; },
      async set(value) {
        for (const [key, item] of Object.entries(value)) {
          if (key === "activeCapture") stored = structuredClone(item);
          else pending.set(key, structuredClone(item));
        }
      },
      async remove(key) { if (key === "activeCapture") stored = undefined; else pending.delete(key); },
    } },
    scripting: { async executeScript(value) {
      lifecycle.push("inject"); injections.push(structuredClone(value));
      return typeof injectionResults === "function" ? injectionResults() : structuredClone(injectionResults);
    } },
    offscreen: { async createDocument() { lifecycle.push("create-offscreen"); contexts = true; createCount++; }, async closeDocument() { closeCount++; if (closeDocument) await closeDocument(); contexts = false; } },
    tabCapture: { async getMediaStreamId(options) { lifecycle.push("stream"); streamCalls++; streamArguments.push(structuredClone(options)); return streamId ? streamId(streamCalls) : "synthetic-stream"; } },
    tabs: {
      async get(id) { tabGets.push(id); return { ...tab, id: currentTabId, url: currentTabUrl, active: currentTabActive }; },
      async create() {},
      async update(id, value) {
        if (failUpdate) throw new Error("Synthetic failed navigation");
        navigations.push({ id, ...structuredClone(value) }); currentTabUrl = value.url;
      },
      onRemoved: { addListener(fn) { removed = fn; } }, onUpdated: { addListener(fn) { updated = fn; } },
      async sendMessage(tabId, message, options) {
        pageMessages.push(structuredClone(message)); pageTargets.push({ tabId, options: structuredClone(options) });
        if (["capture-configuring", "capture-recovery"].includes(message.type)) pageSessionId = message.sessionId;
        if (message.type === "page-handshake") {
          lifecycle.push("handshake");
          return typeof handshakeReply === "function" ? handshakeReply() : structuredClone(handshakeReply);
        }
        if (message.type === "capture-configuring") {
          return typeof configuringReply === "function" ? configuringReply() : structuredClone(configuringReply);
        }
        if (message.type === "prepare") return prepareReply || { ok: true, duration: 100, url: currentTabUrl,
          rangeStart: message.rangeStart, rangeEnd: message.rangeEnd, batchIntervalMs: message.batchIntervalMs };
        if (statusUnavailable && message.type === "status") throw new Error("Receiving end does not exist.");
        return { ok: true };
      },
    },
  };
  const context = vm.createContext({ chrome, URL, AbortController, ...timer, crypto: { randomUUID() { return `00000000-0000-4000-8000-${String(++serial).padStart(12, "0")}`; } },
    async fetch(url, options) {
      lifecycle.push("fetch"); fetches.push(url);
      fetchOptions.push(structuredClone({ method: options?.method || "GET", credentials: options?.credentials,
        ...(options?.body ? { body: JSON.parse(options.body) } : {}) }));
      if (["/learning/library", "/learning/favorites"].includes(new URL(url).pathname) && reviewStatus !== 200) {
        return { ok: false, status: reviewStatus, json: async () => ({ error: "synthetic-private-error" }) };
      }
      return { ok: true, async json() {
        const path = new URL(url).pathname;
        if (path === "/health") return { capabilities: { capture_batches: true, capture_transcription: transcriptionEnabled, capture_enrichment: enrichmentEnabled } };
        if (path === "/learning/latest") return latestLearningReply;
        if (path === "/learning/library") return libraryReply;
        if (path === "/learning/favorites") return favoriteReply;
        if (path.endsWith("/learning")) return learningReply;
        return transcriptionReply;
      } };
    },
  });
  vm.runInContext(workerSource, context);
  const message = (payload, senderOverride = {}) => new Promise((resolve) => {
    listener({ target: "worker", ...payload }, { id: extensionId, tab, frameId: 0, ...senderOverride }, resolve);
  });
  return { timer, pageMessages, offscreenMessages, fetches, fetchOptions, injections, badges, titles, lifecycle, pageTargets, streamArguments, tabGets, navigations, pending,
    load(url = currentTabUrl) { updated(tab.id, { status: "complete" }, { ...tab, url }); },
    remove() { removed(tab.id); },
    arm: (requestId = 1) => message({ type: "capture-rearm", requestId }, { url: currentTabUrl }),
    click: (clickedTab = tab) => action(clickedTab), get stored() { return stored; }, get closeCount() { return closeCount; }, get streamCalls() { return streamCalls; },
    get pageSessionId() { return pageSessionId; },
    get createCount() { return createCount; }, get contextCalls() { return contextCalls; },
    message,
  };
}
{
  const h = workerHarness({ streamId: (attempt) => attempt === 1 ? new Promise(() => {}) : "retry-stream", stallAbort: true });
  const first = h.arm(); await tick();
  assert.equal(h.stored.phase, "preparing");
  assert.ok(h.pageMessages.some((m) => m.type === "capture-configuring"));
  assert.equal(h.pageMessages.some((m) => m.type === "capture-configure"), false);
  await until(() => h.streamCalls === 1);
  h.timer.fire(10_000); await tick();
  h.timer.fire(3000); assert.equal((await first).ok, false);
  assert.equal(h.stored, undefined, "Timed-out preparation must not lock the next extension action");
  assert.equal(h.closeCount, 1);
  assert.equal((await h.arm()).ok, true);
  assert.equal(h.stored.phase, "ready");
  assert.equal(h.streamCalls, 2);
  assert.ok(h.pageMessages.some((m) => m.type === "status" && /再点击生成学习内容/.test(m.text)));
  console.log("PASS preparation and cancellation waits expire; a new action can initialize again");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "preparing" };
  const h = workerHarness({ initial: previous });
  h.click(); await until(() => h.pageMessages.some((m) => m.type === "capture-idle")); await tick();
  assert.equal(h.stored, undefined);
  assert.equal(h.streamCalls, 0);
  assert.equal((await h.arm()).ok, true);
  assert.equal(h.stored.phase, "ready");
  assert.notEqual(h.stored.sessionId, previous.sessionId);
  assert.equal(h.streamCalls, 1);
  console.log("PASS stale storage without a recording document reinitializes without touching disk batches");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "failed" };
  for (const pendingBatches of [0, 5]) {
    const h = workerHarness({ initial: previous, documentExists: true, inspect: { ok: true, phase: "failed", pendingBatches, pendingBytes: pendingBatches * 200 } });
    assert.equal(h.pageSessionId, undefined, "A fresh page initially has no old task binding");
    h.click(); await tick();
    h.click(); await tick();
    assert.equal(h.stored.sessionId, previous.sessionId);
    assert.equal(h.closeCount, 0);
    assert.equal(h.streamCalls, 0);
    assert.equal(h.offscreenMessages.some((m) => m.type === "capture-abort"), false);
    const restored = h.pageMessages.filter((m) => m.type === "capture-recovery");
    assert.equal(restored.length, 2);
    assert.equal(restored[0].sessionId, previous.sessionId);
    assert.equal(restored[0].recoveryState, "failed");
    assert.equal(restored[0].pendingBatches, pendingBatches);
    assert.equal("sourceUrl" in restored[0], false);
    assert.equal(h.pageSessionId, previous.sessionId);
    assert.equal(h.pageMessages.some((m) => m.type === "capture-configure"), false);
    if (!pendingBatches) assert.match(h.titles.at(-1).title, /没有待确认批次/);
    assert.equal((await h.message({ type: "capture-cancel", sessionId: h.pageSessionId })).ok, true);
    assert.equal(h.closeCount, 1);
    assert.equal(h.stored, undefined);
    assert.equal((await h.arm()).ok, true);
    assert.notEqual(h.stored.sessionId, previous.sessionId);
    assert.equal(h.streamCalls, 1);
  }
  console.log("PASS failed queues including zero pending batches bind recovery controls until explicit cancellation, then permit a fresh preparation");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "preparing" };
  const h = workerHarness({ initial: previous, documentExists: true, stallAbort: true });
  const cancelled = h.message({ type: "capture-cancel", sessionId: previous.sessionId });
  await tick(); h.timer.fire(4000); await tick();
  const reply = await cancelled;
  assert.equal(reply.ok, true);
  assert.equal(reply.abortConfirmed, false);
  assert.equal(h.stored, undefined);
  assert.equal(h.closeCount, 1);
  assert.ok(h.pageMessages.some((m) => m.type === "status" && /本机服务未确认/.test(m.text)));
  console.log("PASS explicit cancellation releases a hung preparation with an unknown-backend-state message");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "recording" };
  const h = workerHarness({ initial: previous, documentExists: true });
  const failure = { code: "video_replaced", message: "采集的视频播放器已被替换。", at_ms: 1000, video_sec: 727, video_connected: false, player_connected: true, video_replaced: true };
  const playbackEvents = [{ type: "playing", at_ms: 100, video_sec: 480 }];
  assert.equal((await h.message({ type: "page-error", sessionId: previous.sessionId, error: failure.message,
    failure, playbackEndVideoSec: 727, playbackEvents })).ok, true);
  const forwarded = h.offscreenMessages.find((message) => message.type === "capture-fail");
  assert.deepEqual(forwarded.failure, failure);
  assert.deepEqual(forwarded.playbackEvents, playbackEvents);
  assert.equal(forwarded.playbackEndVideoSec, 727);
  console.log("PASS page failure observations reach the recorder without reading page URLs");
}
function mediaHarness({ acquisition, resume } = {}) {
  const timer = clock(); let listener; let next = 0;
  const tracks = []; const contexts = [];
  function stream() { const track = { stopped: false, stop() { this.stopped = true; } }; tracks.push(track); return { getTracks: () => [track] }; }
  const context = vm.createContext({
    chrome: { runtime: { id: extensionId, getURL: (path) => origin + path, onMessage: { addListener(fn) { listener = fn; } }, async sendMessage() { return { ok: true }; } } },
    navigator: { mediaDevices: { getUserMedia() { const attempt = ++next; return acquisition ? acquisition(attempt, stream) : Promise.resolve(stream()); } } },
    AudioContext: class { constructor() { this.closed = false; contexts.push(this); } createMediaStreamSource() { return { connect() {} }; } resume() { return resume ? resume(contexts.length) : Promise.resolve(); } async close() { this.closed = true; } },
    ...timer, clearInterval() {},
  });
  vm.runInContext(recorderSource, context);
  const dispatch = (type, sessionId) => new Promise((resolve) => { listener({ target: "offscreen", type, sessionId, streamId: "synthetic-stream" }, { id: extensionId, url: origin + "service-worker.js" }, resolve); });
  return { timer, tracks, contexts, dispatch, stream };
}
{
  const late = deferred();
  const h = mediaHarness({ acquisition: (attempt, stream) => attempt === 1 ? late.promise : Promise.resolve(stream()) });
  const first = h.dispatch("capture-arm", "first"); await tick(); h.timer.fire(10_000); await tick();
  assert.equal((await first).ok, false);
  assert.equal((await h.dispatch("capture-arm", "second")).ok, true);
  const lateStream = h.stream(); late.resolve(lateStream); await tick();
  assert.equal(lateStream.getTracks()[0].stopped, true);
  assert.equal(h.tracks[0].stopped, false, "Late cleanup must not stop the replacement stream");
  assert.equal((await h.dispatch("capture-inspect", "second")).phase, "ready");
  await h.dispatch("capture-abort", "second");
  console.log("PASS timed-out getUserMedia cannot revive a session or leak a late stream");
}
{
  const h = mediaHarness({ resume: (attempt) => attempt === 1 ? new Promise(() => {}) : Promise.resolve() });
  const first = h.dispatch("capture-arm", "first"); await tick(); h.timer.fire(3000); await tick();
  assert.equal((await first).ok, false);
  assert.equal(h.tracks[0].stopped, true);
  assert.equal(h.contexts[0].closed, true);
  assert.equal((await h.dispatch("capture-arm", "second")).ok, true);
  await h.dispatch("capture-abort", "second");
  console.log("PASS suspended audio output fails with a bounded deadline and releases capture resources");
}
{
  for (const enabled of [false, true]) {
    const h = workerHarness({ transcriptionEnabled: enabled });
    assert.equal((await h.arm()).ok, true);
    assert.equal(h.stored.transcriptionEnabled, enabled);
    assert.equal(h.pageMessages.find((m) => m.type === "capture-configuring").transcriptionEnabled, enabled);
    assert.equal(h.pageMessages.find((m) => m.type === "capture-configure").transcriptionEnabled, enabled);
    const ready = h.pageMessages.filter((m) => m.type === "status").at(-1).text;
    assert.equal(ready.includes("分批发送 Google"), enabled);
    const id = h.stored.sessionId;
    assert.equal((await h.message({ type: "capture-complete", sessionId: id, manifest: { id } },
      { tab: undefined, url: origin + "offscreen.html" })).ok, true);
    const saved = h.pageMessages.find((m) => m.type === "capture-saved");
    assert.equal(saved.transcriptionEnabled, enabled);
    const complete = h.pageMessages.filter((m) => m.type === "status").at(-1).text;
    assert.equal(complete.includes("此次未调用 Google 或 GPT"), !enabled);
    assert.equal(complete.includes("Google 转写在后台"), enabled);
    assert.deepEqual(h.fetches.map((url) => new URL(url).pathname), ["/health"]);
  }
  console.log("PASS capture-only and Google-enabled readiness/completion messages match actual capability flags");
}
{
  const captureId = "00000000-0000-4000-8000-000000000099";
  const h = workerHarness({ transcriptionReply: {
    status: "needs_attention", windows_done: 2, windows_total: 3, sentences: 8,
    blocked_windows: [{ status: "unknown", secret: "synthetic-private-marker" }],
    result: { raw_transcripts: [{ text: "synthetic-private-marker" }], sentences: [{ text: "漢字" }] },
    error: "synthetic-private-marker",
  } });
  const response = await h.message({ type: "capture-transcription-status", captureId });
  assert.deepEqual(Object.keys(response.transcription).sort(), ["status", "windows_done", "windows_total", "sentences", "unknown_windows", "failed_windows"].sort());
  assert.equal(response.transcription.unknown_windows, 1);
  assert.equal(response.transcription.sentences, 8);
  assert.equal(JSON.stringify(response).includes("synthetic-private-marker"), false);
  assert.equal(JSON.stringify(response).includes("漢字"), false);
  assert.equal(h.fetches.length, 1);
  for (const [payload, sender] of [
    [{ captureId: "../invalid" }, {}], [{ captureId }, { frameId: 1 }],
    [{ captureId }, { id: "untrusted-extension" }],
    [{ captureId }, { tab: { id: 7, url: "https://example.invalid" } }],
  ]) {
    const rejected = await h.message({ type: "capture-transcription-status", ...payload }, sender);
    assert.equal(rejected.ok, false);
  }
  assert.equal(h.fetches.length, 1, "Invalid senders or identifiers must never reach the local status API");
  console.log("PASS status requests validate sender and UUID, and forward only safe counts without ASR text");
}
{
  const h = workerHarness({ transcriptionReply: { status: "running", windows_done: 1, windows_total: 3, sentences: 4 } });
  const response = await h.message({ type: "capture-transcription-status", captureId: "00000000-0000-4000-8000-000000000099" });
  assert.equal(response.transcription.status, "running");
  assert.equal(response.transcription.windows_done, 1);
  console.log("PASS an in-flight window remains a running status instead of a terminal error");
}
function assertRefreshRequired(h) {
  assert.equal(h.badges.at(-1)?.text, "ERR");
  assert.match(h.titles.at(-1)?.title || "", /刷新当前 YouTube 页面/,
    "The action tooltip must explain recovery even when the page cannot show a status");
  assert.equal(h.fetches.length, 0, "An unverified page must not connect to the local backend");
  assert.equal(h.contextCalls, 0, "Page verification must precede offscreen setup or inspection");
  assert.equal(h.createCount, 0);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.offscreenMessages.length, 0, "An unverified page must not arm, inspect, or release a recording queue");
}
function assertDiagnostic(h, code) {
  assertRefreshRequired(h);
  assert.ok(h.titles.at(-1).title.includes(`[${code}]`), `The action tooltip must identify ${code}`);
  assert.equal(h.titles.at(-1).title.includes("synthetic-private-marker"), false,
    "Raw injection errors or unknown result values must not enter user-visible diagnostics");
}
{
  for (const [injectionResults, code] of [
    [() => undefined, "missing_main_frame"],
    [[{ frameId: 0, result: { ok: false, requiresRefresh: true } }], "injection_invalid_result"],
    [[{ frameId: 0, result: { ...pageMetadata, version: "0.4.2" } }], "injection_version_mismatch"],
    [[{ frameId: 0, result: { ...pageMetadata, protocol: 2 } }], "injection_protocol_mismatch"],
    [[{ frameId: 1, result: pageMetadata }], "missing_main_frame"],
    [[{ frameId: 0 }], "missing_injection_result"],
  ]) {
    const h = workerHarness({ injectionResults, statusUnavailable: true });
    h.click(); await tick();
    assertDiagnostic(h, code);
    assert.equal(h.injections.length, 1);
    assert.equal(h.pageMessages.some((message) => message.type === "page-handshake"), false);
    assert.equal(h.stored, undefined);
  }
  console.log("PASS missing, stale, or non-main-frame injection results require refresh before backend or audio access");
}
{
  for (const [handshakeReply, code] of [
    [undefined, "handshake_empty"], [null, "handshake_empty"],
    [{ ok: true }, "handshake_version_mismatch"],
    [{ ...pageMetadata, version: "0.4.2" }, "handshake_version_mismatch"],
    [{ ...pageMetadata, protocol: 2 }, "handshake_protocol_mismatch"],
    [{ ...pageMetadata, ok: false }, "handshake_invalid_result"],
    [() => { throw new Error("synthetic-private-marker: receiving end does not exist"); }, "handshake_throw"],
  ]) {
    // An explicit function permits testing an absent response without activating the harness default.
    const h = workerHarness({ handshakeReply: handshakeReply === undefined ? () => undefined : handshakeReply });
    h.click(); await tick();
    assertDiagnostic(h, code);
    assert.equal(h.pageMessages.filter((message) => message.type === "page-handshake").length, 1);
    assert.deepEqual(h.pageTargets[0], { tabId: tab.id, options: { frameId: 0 } });
    assert.equal(h.stored, undefined);
  }
  console.log("PASS missing or mismatched live handshakes fail closed and address only the main frame");
}
{
  const pending = deferred();
  const h = workerHarness({ handshakeReply: () => pending.promise });
  h.click(); await until(() => h.pageMessages.some((message) => message.type === "page-handshake"));
  h.timer.fire(3000); await tick();
  assertDiagnostic(h, "handshake_timeout");
  pending.resolve(pageMetadata); await tick();
  assertRefreshRequired(h);
  assert.equal(h.stored, undefined, "A late handshake must not restart timed-out initialization");
  console.log("PASS a stuck handshake expires after three seconds and a late reply cannot prepare audio");
}
{
  const h = workerHarness();
  h.click(); await until(() => h.pageMessages.some((m) => m.type === "capture-idle")); await tick();
  assert.deepEqual(h.lifecycle, ["inject", "handshake"]);
  assert.deepEqual(h.injections[0], { target: { tabId: tab.id }, files: ["review.js", "page.js"] });
  assert.equal(h.pageMessages[0].type, "page-handshake");
  assert.equal(h.pageMessages[0].version, pageMetadata.version);
  assert.equal(h.pageMessages[0].protocol, pageMetadata.protocol);
  assert.equal(h.createCount, 0);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.stored, undefined);
  assert.equal((await h.arm()).ok, true);
  assert.equal(h.streamCalls, 1);
  console.log("PASS toolbar shows idle learning without health, audio or cloud; only explicit generate arms capture");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "failed" };
  const h = workerHarness({ initial: previous, documentExists: true,
    injectionResults: [{ frameId: 0, result: { ok: false, requiresRefresh: true } }] });
  h.click(); await tick();
  assertRefreshRequired(h);
  assert.deepEqual(h.stored, previous);
  assert.equal(h.closeCount, 0, "Page verification failure must preserve an existing recording queue");
  console.log("PASS a stale page leaves existing capture storage and unacknowledged audio untouched");
}
{
  for (const reason of ["legacy_controller", "controller_version_mismatch", "dead_controller", "orphan_root", "context_invalid"]) {
    const h = workerHarness({ injectionResults: [{ frameId: 0, result: { ok: false, reason } }] });
    h.click(); await tick();
    assertDiagnostic(h, reason);
    assert.equal(h.pageMessages.some((message) => message.type === "page-handshake"), false);
  }
  for (const reason of ["synthetic-private-marker", "__proto__", "constructor"]) {
    const h = workerHarness({ injectionResults: [{ frameId: 0, result: { ok: false, reason } }] });
    h.click(); await tick();
    assertDiagnostic(h, "injection_invalid_result");
  }
  console.log("PASS controller failure reasons are preserved only through a fixed diagnostic allowlist");
}
{
  const h = workerHarness({ injectionResults: () => { throw new Error("synthetic-private-marker: cannot load private path"); } });
  h.click(); await tick();
  assertDiagnostic(h, "inject_throw");
  assert.equal(h.pageMessages.some((message) => message.type === "page-handshake"), false);
  console.log("PASS injection rejection identifies its stage without exposing the raw browser exception");
}
{
  const pending = deferred();
  const h = workerHarness({ injectionResults: () => pending.promise });
  h.click(); await until(() => h.injections.length === 1);
  h.timer.fire(3000); await tick();
  assertDiagnostic(h, "inject_timeout");
  pending.resolve([{ frameId: 0, result: pageMetadata }]); await tick();
  assertDiagnostic(h, "inject_timeout");
  assert.equal(h.pageMessages.some((message) => message.type === "page-handshake"), false);
  console.log("PASS injection timeout differs from rejection and late results cannot continue setup");
}
{
  let ready = false;
  const h = workerHarness({ injectionResults: () => [{ frameId: 0, result: ready ? pageMetadata : { ok: false, reason: "orphan_root" } }] });
  h.click(); await tick();
  assertDiagnostic(h, "orphan_root");
  ready = true;
  assert.equal((await h.arm()).ok, true);
  assert.equal(h.badges.at(-1).text, "JA");
  assert.equal(h.titles.at(-1).title.includes("[orphan_root]"), false);
  assert.equal(h.streamCalls, 1);
  console.log("PASS a later valid action clears an earlier diagnostic and prepares exactly one capture");
}
function readySentence(overrides = {}) {
  return { id: "s001", text: "何が必要か。", start: 1, end: 3, audio_start: 1, audio_end: 3,
    translation_zh: "需要什么呢？", study: { style_version: 2, reading_text: "何(なに)が必要(ひつよう)か。",
      key_points: ["疑问小句：提出问题。"], context_note: "", asr_doubt: "" }, ...overrides };
}
{
  const h = workerHarness({ learningReply: { status: "complete", sentences_done: 1, sentences_total: 3,
    batches_done: 1, batches_total: 1, revision: 1, result_available: true,
    error: "synthetic-private-marker", model: "synthetic-private-marker", source_url: "synthetic-private-marker",
    blocked_batches: [{ secret: "synthetic-private-marker" }],
    result: { raw_transcripts: [{ text: "synthetic-private-marker" }], sentences: [
      readySentence(), readySentence({ id: "s002", study: undefined }),
      readySentence({ id: "s003", study: { style_version: 2, reading_text: "何が必要か。", key_points: [] } }),
    ] } } });
  const response = await h.message({ type: "capture-learning-status", captureId: "00000000-0000-4000-8000-000000000099" });
  assert.equal(response.ok, true);
  assert.equal(response.learning.result.sentences.length, 1);
  assert.equal(response.learning.result.sentences[0].study.reading_text, "何(なに)が必要(ひつよう)か。");
  assert.equal("text" in response.learning.result.sentences[0], false, "The worker must not forward raw ASR originals");
  assert.equal(JSON.stringify(response).includes("synthetic-private-marker"), false);
  assert.equal(response.learning.blocked_batches, 1);
  assert.equal(h.fetchOptions[0].method, "GET");
  assert.equal(h.fetchOptions[0].credentials, "omit");
  assert.equal(h.offscreenMessages.length, 0);
  console.log("PASS learning status forwards validated annotated notes and safe counts without raw ASR or unknown metadata");
}
{
  for (const [original, reading, accepted] of [
    ["そう(かな)。", "そう(かな)。", true],
    ["そう(かな)。", "そう。", false],
    ["何(なに)が必要か。", "何(なに)が必要(ひつよう)か。", true],
    ["物理的に持って。", "物理的(ぶつりてき)に持(も)って。", true],
    ["彼は(たぶん)帰る。", "彼(かれ)は(たぶん)帰(かえ)る。", true],
    ["彼は帰る。", "彼(かれ)は来(く)る。", false],
    ["物理的。", "物理(ぶつり)的。", false],
  ]) {
    const h = workerHarness({ learningReply: { status: "complete", result: { sentences: [readySentence({
      text: original, study: { style_version: 2, reading_text: reading, key_points: [], asr_doubt: "需要回听确认。", context_note: "" },
    })] } } });
    const response = await h.message({ type: "capture-learning-status", captureId: "00000000-0000-4000-8000-000000000099" });
    assert.equal(response.learning.result.sentences.length, accepted ? 1 : 0, `${original} -> ${reading}`);
  }
  console.log("PASS source-aware reading validation preserves literal kana parentheses and accepts compound or split kanji readings");
}
{
  const h = workerHarness({ latestLearningReply: { capture_id: "00000000-0000-4000-8000-000000000099", status: "complete",
    result: { sentences: [readySentence()] } } });
  const response = await h.message({ type: "capture-learning-load", url: "https://example.invalid/forged-source", captureId: "../forged" });
  assert.equal(response.learning.capture_id, "00000000-0000-4000-8000-000000000099");
  assert.equal(new URL(h.fetches[0]).pathname, "/learning/latest");
  assert.equal(new URL(h.fetches[0]).searchParams.get("url"), videoUrl);
  assert.equal(h.fetchOptions[0].method, "GET");
  assert.equal(h.stored, undefined);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.offscreenMessages.length, 0);
  console.log("PASS cached learning load uses the current sender tab URL and never starts recording or processing");
}
{
  const h = workerHarness();
  for (const [payload, sender] of [
    [{ type: "capture-learning-status", captureId: "../invalid" }, {}],
    [{ type: "capture-learning-load" }, { frameId: 1 }],
    [{ type: "capture-learning-load" }, { id: "untrusted-extension" }],
    [{ type: "capture-learning-load" }, { tab: { id: 7, url: "https://example.invalid" } }],
  ]) assert.equal((await h.message(payload, sender)).ok, false);
  assert.equal(h.fetches.length, 0);
  const moved = workerHarness({ currentTabUrl: "https://example.invalid" });
  assert.equal((await moved.message({ type: "capture-learning-load" })).ok, false);
  assert.equal(moved.fetches.length, 0);
  console.log("PASS learning requests reject foreign, subframe, malformed and navigated-away senders before local access");
}
{
  const h = workerHarness({ transcriptionEnabled: true, enrichmentEnabled: true });
  assert.equal((await h.arm()).ok, true);
  assert.equal(h.stored.enrichmentEnabled, true);
  assert.equal(h.pageMessages.find((message) => message.type === "capture-configure").enrichmentEnabled, true);
  assert.match(h.titles.at(-1).title, /GPT.*预生成/);
  const id = h.stored.sessionId;
  await h.message({ type: "capture-complete", sessionId: id, manifest: { id } }, { tab: undefined, url: origin + "offscreen.html" });
  assert.equal(h.pageMessages.find((message) => message.type === "capture-saved").enrichmentEnabled, true);
  assert.match(h.titles.at(-1).title, /翻译和解析.*后台/);
  assert.equal(h.titles.at(-1).title.includes("未调用 GPT"), false);
  console.log("PASS enabled enrichment is announced honestly during capture readiness and completion");
}
{
  const h = workerHarness({ latestLearningReply: { capture_id: null, status: "not_found", result: null } });
  const response = await h.message({ type: "capture-learning-load" });
  assert.equal(response.learning.capture_id, null);
  assert.equal(response.learning.status, "not_found");
  assert.equal(response.learning.result_available, false);
  assert.equal(response.learning.result.sentences.length, 0);
  console.log("PASS missing cached learning is a read-only empty result without a paid fallback");
}
{
  const h = workerHarness({ transcriptionEnabled: false, enrichmentEnabled: true });
  assert.equal((await h.arm()).ok, true);
  assert.equal(h.stored.transcriptionEnabled, false);
  assert.equal(h.stored.enrichmentEnabled, false,
    "New-capture enrichment requires Google transcription even when saved-ASR enrichment is enabled on the backend");
  const configured = h.pageMessages.find((message) => message.type === "capture-configure");
  assert.equal(configured.enrichmentEnabled, false, "The page must not start new-capture learning polls without ASR");
  assert.match(h.titles.at(-1).title, /不调用云端服务/);
  const id = h.stored.sessionId;
  await h.message({ type: "capture-complete", sessionId: id, manifest: { id } }, { tab: undefined, url: origin + "offscreen.html" });
  const saved = h.pageMessages.find((message) => message.type === "capture-saved");
  assert.equal(saved.enrichmentEnabled, false);
  assert.match(h.titles.at(-1).title, /未调用 Google 或 GPT/);
  assert.deepEqual(h.fetches.map((url) => new URL(url).pathname), ["/health"]);
  console.log("PASS new-capture GPT mode requires Google ASR while saved learning remains independently readable");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "preparing" };
  for (const inspect of [{ ok: false }, () => undefined,
    { ok: true, phase: "unexpected", pendingBatches: 2, pendingBytes: 300 },
    () => { throw new Error("synthetic-private-marker"); },
  ]) {
    const h = workerHarness({ initial: previous, documentExists: true, inspect });
    h.click(); await tick();
    const recovery = h.pageMessages.find((message) => message.type === "capture-recovery");
    assert.equal(recovery.recoveryState, "unknown");
    assert.equal(h.pageSessionId, previous.sessionId);
    assert.equal(h.stored.sessionId, previous.sessionId);
    assert.equal(h.closeCount, 0);
    assert.equal(h.streamCalls, 0);
    assert.equal(h.fetches.length, 0);
    assert.equal(h.offscreenMessages.some((message) => ["capture-abort", "capture-arm", "capture-start"].includes(message.type)), false);
    assert.equal(h.pageMessages.some((message) => message.type === "capture-configure"), false);
    assert.equal(JSON.stringify(h.titles).includes("synthetic-private-marker"), false);
  }
  console.log("PASS unknown or failed inspection binds safe release controls without discarding data or exposing raw errors");
}
{
  const pending = deferred();
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "preparing" };
  const h = workerHarness({ initial: previous, documentExists: true, inspect: () => pending.promise });
  h.click(); await until(() => h.offscreenMessages.some((message) => message.type === "capture-inspect"));
  h.timer.fire(3000); await tick();
  assert.equal(h.pageSessionId, previous.sessionId);
  const recovery = h.pageMessages.find((message) => message.type === "capture-recovery");
  assert.equal(recovery.recoveryState, "unknown");
  assert.equal(recovery.pendingBatches, null);
  assert.equal(recovery.pendingBytes, null);
  pending.resolve({ ok: true, phase: "ready" }); await tick();
  assert.equal(h.closeCount, 0);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.pageMessages.some((message) => message.type === "capture-configure"), false);
  assert.equal(h.offscreenMessages.some((message) => message.type === "capture-abort"), false);
  console.log("PASS inspection timeout exposes recovery-only controls and ignores a late ready response");
}
{
  for (const phase of ["recording", "saving"]) {
    const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase };
    const h = workerHarness({ initial: previous, documentExists: true, inspect: { ok: true, phase, pendingBatches: 2, pendingBytes: 300 } });
    h.click(); await tick();
    const recovery = h.pageMessages.find((message) => message.type === "capture-recovery");
    assert.equal(recovery.recoveryState, phase);
    assert.equal(h.pageSessionId, previous.sessionId);
    assert.equal(h.stored.phase, phase);
    assert.equal(h.closeCount, 0);
    assert.equal(h.streamCalls, 0);
    assert.equal(h.pageMessages.some((message) => ["capture-configuring", "capture-configure", "prepare", "play"].includes(message.type)), false);
    assert.equal(h.offscreenMessages.some((message) => ["capture-abort", "capture-arm", "capture-start"].includes(message.type)), false);
    assert.equal((await h.message({ type: "capture-cancel", sessionId: h.pageSessionId })).ok, true);
    assert.equal(h.closeCount, 1);
  }
  console.log("PASS fresh-page recovery binds cancellation for recording or saving without restarting or interrupting live work");
}
{
  const changedTab = { id: tab.id, url: "https://www.youtube.com/watch?v=ZyXwVu987_-" };
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "ready" };
  const h = workerHarness({ initial: previous, documentExists: true, currentTabUrl: changedTab.url,
    inspect: { ok: true, phase: "ready", pendingBatches: 0, pendingBytes: 0 } });
  h.click(changedTab); await tick();
  assert.equal(h.pageMessages.find((message) => message.type === "capture-recovery").recoveryState, "source_changed");
  assert.equal(h.stored.sourceUrl, videoUrl);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.closeCount, 0);
  assert.equal(h.pageMessages.some((message) => message.type === "capture-configure"), false);
  await h.message({ type: "capture-cancel", sessionId: h.pageSessionId });
  assert.equal((await h.arm()).ok, true);
  assert.equal(h.stored.sourceUrl, changedTab.url);
  assert.notEqual(h.stored.sessionId, previous.sessionId);
  assert.equal(h.streamCalls, 1);
  assert.equal(h.offscreenMessages.some((message) => message.type === "capture-start"), false);
  console.log("PASS a changed video cannot reuse stale preparation and can rearm only after explicit release");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "failed" };
  const h = workerHarness({ initial: previous, documentExists: true });
  assert.equal((await h.message({ type: "capture-cancel", sessionId: previous.sessionId })).ok, true);
  const reply = await h.message({ type: "capture-rearm", requestId: 1, tabId: 999, sessionId: previous.sessionId }, { url: videoUrl });
  assert.equal(reply.ok, true);
  assert.equal(reply.sessionId, h.stored.sessionId);
  assert.notEqual(reply.sessionId, previous.sessionId);
  assert.equal(h.stored.phase, "ready");
  assert.equal(h.createCount, 1);
  assert.equal(h.streamCalls, 1);
  assert.deepEqual(h.streamArguments, [{ targetTabId: tab.id }], "A payload cannot override the verified sender tab");
  assert.equal(h.pageMessages.some((message) => ["prepare", "play"].includes(message.type)), false);
  assert.equal(h.offscreenMessages.some((message) => message.type === "capture-start"), false);
  assert.deepEqual(h.fetches.map((url) => new URL(url).pathname), ["/health"]);
  assert.equal(h.fetchOptions.every((options) => options.method === "GET"), true);
  console.log("PASS explicit release permits direct panel rearm with a fresh ID and audio preparation only");
}
{
  for (const phase of ["preparing", "ready", "recording", "saving", "failed"]) {
    const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase };
    const h = workerHarness({ initial: previous, documentExists: true });
    const reply = await h.message({ type: "capture-rearm", requestId: 1 }, { url: videoUrl });
    assert.equal(reply.ok, false);
    assert.match(reply.error, /已有采集任务/);
    assert.deepEqual(h.stored, previous);
    assert.equal(h.offscreenMessages.length, 0);
    assert.equal(h.contextCalls, 0);
    assert.equal(h.streamCalls, 0);
    assert.equal(h.closeCount, 0);
    assert.equal(h.fetches.length, 0);
  }
  console.log("PASS panel rearm never inspects, replaces, or releases an existing capture in any phase");
}
{
  const h = workerHarness();
  for (const sender of [
    { id: "untrusted-extension", url: videoUrl }, { frameId: 1, url: videoUrl },
    { tab: undefined, url: videoUrl }, { tab: { id: 0 }, url: videoUrl },
    { tab: { id: -1 }, url: videoUrl }, { tab: { id: 7.5 }, url: videoUrl },
    { tab: { id: "7" }, url: videoUrl }, {}, { url: "https://example.invalid" },
  ]) assert.equal((await h.message({ type: "capture-rearm", requestId: 1, tabId: tab.id }, sender)).ok, false);
  assert.equal(h.tabGets.length, 0);
  assert.equal(h.injections.length, 0);
  assert.equal(h.fetches.length, 0);
  assert.equal(h.offscreenMessages.length, 0);
  assert.equal(h.stored, undefined);
  console.log("PASS direct rearm rejects foreign, subframe, missing-URL, and malformed-tab senders before any setup");
}
{
  for (const options of [
    { currentTabUrl: "https://www.youtube.com/watch?v=ZyXwVu987_-" },
    { currentTabUrl: "https://example.invalid" }, { currentTabActive: false }, { currentTabId: 8 },
  ]) {
    const h = workerHarness(options);
    const reply = await h.message({ type: "capture-rearm", requestId: 1 }, { url: videoUrl });
    assert.equal(reply.ok, false);
    assert.match(reply.error, /请点击浏览器工具栏 S，再点击生成学习内容/);
    assert.deepEqual(h.tabGets, [tab.id]);
    assert.equal(h.injections.length, 0);
    assert.equal(h.fetches.length, 0);
    assert.equal(h.streamCalls, 0);
    assert.equal(h.offscreenMessages.length, 0);
    assert.equal(h.stored, undefined);
  }
  console.log("PASS rearm verifies the active current tab and exact sender URL before page or audio setup");
}
{
  for (const first of ["panel", "toolbar"]) {
    const handshake = deferred();
    const h = workerHarness({ handshakeReply: () => handshake.promise });
    const initial = first === "panel" ? h.message({ type: "capture-rearm", requestId: 1 }, { url: videoUrl }) : (h.click(), null);
    await until(() => h.pageMessages.some((message) => message.type === "page-handshake"));
    const duplicate = await h.message({ type: "capture-rearm", requestId: 1 }, { url: videoUrl });
    assert.equal(duplicate.ok, false);
    assert.equal(duplicate.error, "正在连接音频，请稍候后重试。");
    h.click(); await tick();
    assert.equal(h.injections.length, 1);
    handshake.resolve(pageMetadata);
    if (initial) assert.equal((await initial).ok, true);
    else {
      await until(() => h.pageMessages.some((message) => message.type === "capture-idle")); await tick();
      assert.equal(h.streamCalls, 0);
      assert.equal((await h.arm()).ok, true);
    }
    await until(() => h.pageMessages.some((message) => message.type === "capture-configure"));
    assert.equal(h.createCount, 1);
    assert.equal(h.streamCalls, 1);
    assert.equal(h.pageMessages.filter((message) => message.type === "capture-configure").length, 1);
  }
  console.log("PASS toolbar and panel openings share one lock and duplicate panel requests receive a fixed busy reply");
}
{
  const closing = deferred();
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "failed" };
  const h = workerHarness({ initial: previous, documentExists: true, closeDocument: () => closing.promise });
  const cancelled = h.message({ type: "capture-cancel", sessionId: previous.sessionId });
  await until(() => h.closeCount === 1);
  assert.ok(h.pageMessages.some((message) => message.type === "capture-reset"));
  const rearmed = h.message({ type: "capture-rearm", requestId: 1 }, { url: videoUrl });
  await tick();
  const duplicate = await h.message({ type: "capture-rearm", requestId: 1 }, { url: videoUrl });
  assert.equal(duplicate.ok, false);
  assert.equal(duplicate.error, "正在连接音频，请稍候后重试。");
  h.click(); await tick();
  assert.equal(h.createCount, 0);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.fetches.length, 0);
  closing.resolve();
  assert.equal((await cancelled).ok, true);
  assert.equal((await rearmed).ok, true);
  assert.equal(h.stored.phase, "ready");
  assert.notEqual(h.stored.sessionId, previous.sessionId);
  assert.equal(h.createCount, 1);
  assert.equal(h.streamCalls, 1);
  assert.match(h.titles.at(-1).title, /选择采集范围/);
  console.log("PASS rearm after reset waits for old document closure and cannot create a stream during release");
}
{
  const h = workerHarness({ streamId: (attempt) => {
    if (attempt === 1) throw new Error("synthetic-private-marker: tab capture permission denied");
    return "toolbar-retry-stream";
  } });
  const reply = await h.message({ type: "capture-rearm", requestId: 1 }, { url: videoUrl });
  assert.equal(reply.ok, false);
  assert.equal(reply.error, "音频未连接，请点击浏览器工具栏 S，再点击生成学习内容。");
  assert.equal(JSON.stringify(reply).includes("synthetic-private-marker"), false);
  assert.equal(JSON.stringify(h.titles).includes("synthetic-private-marker"), false);
  assert.equal(h.stored, undefined);
  assert.equal(h.closeCount, 1);
  assert.equal(h.offscreenMessages.filter((message) => message.type === "capture-abort").length, 1);
  assert.equal(h.offscreenMessages.some((message) => ["capture-arm", "capture-start"].includes(message.type)), false);
  assert.equal(h.pageMessages.some((message) => ["prepare", "play"].includes(message.type)), false);
  assert.equal((await h.arm()).ok, true);
  assert.equal(h.streamCalls, 2);
  assert.equal(h.stored.phase, "ready");
  console.log("PASS denied panel capture permission returns a fixed toolbar fallback and releases failed preparation");
}
{
  const closing = deferred();
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "failed" };
  const h = workerHarness({ initial: previous, documentExists: true, closeDocument: () => closing.promise });
  const cancelled = h.message({ type: "capture-cancel", sessionId: previous.sessionId });
  await until(() => h.closeCount === 1);
  h.timer.fire(3000); await tick();
  assert.equal((await cancelled).ok, true, "A stuck Chrome close must not hang the cancellation controls");
  const rearmed = h.message({ type: "capture-rearm", requestId: 1 }, { url: videoUrl });
  await tick(); h.timer.fire(4000); await tick();
  const reply = await rearmed;
  assert.equal(reply.ok, false);
  assert.match(reply.error, /请点击浏览器工具栏 S，再点击生成学习内容/);
  assert.equal(h.createCount, 0);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.fetches.length, 0);
  closing.resolve(); await tick();
  assert.equal(h.createCount, 0, "A late close cannot restart the expired rearm request");
  assert.equal((await h.message({ type: "capture-rearm", requestId: 1 }, { url: videoUrl })).ok, true);
  assert.equal(h.createCount, 1);
  assert.equal(h.streamCalls, 1);
  console.log("PASS timed-out old closure keeps rearm fail-closed until Chrome actually settles, without late automatic retry");
}
{
  const h = workerHarness();
  for (const requestId of [undefined, null, "1", 0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, 1_000_000_001]) {
    assert.equal((await h.message({ type: "capture-rearm", requestId }, { url: videoUrl })).ok, false);
  }
  assert.equal(h.tabGets.length, 0);
  assert.equal(h.injections.length, 0);
  assert.equal(h.fetches.length, 0);
  assert.equal(h.offscreenMessages.length, 0);
  console.log("PASS rearm requires a bounded positive integer request ID before any page, local, or audio work");
}
{
  const h = workerHarness();
  assert.equal((await h.message({ type: "capture-rearm", requestId: 27 }, { url: videoUrl })).ok, true);
  const tagged = h.pageMessages.filter((message) => ["status", "capture-configuring", "capture-configure"].includes(message.type));
  assert.ok(tagged.length > 5);
  assert.equal(tagged.every((message) => message.rearmRequestId === 27), true);
  assert.equal(h.offscreenMessages.some((message) => "rearmRequestId" in message), false,
    "A UI request ID must not enter the recorder session or uploaded task");
  assert.equal("rearmRequestId" in h.stored, false);
  const toolbar = workerHarness();
  toolbar.click(); await until(() => toolbar.pageMessages.some((message) => message.type === "capture-idle"));
  assert.equal(toolbar.pageMessages.some((message) => "rearmRequestId" in message), false);
  console.log("PASS all direct-rearm status and configuring messages carry the request ID while normal toolbar messages stay unchanged");
}
{
  for (const configuringReply of [{ ok: false }, { ok: 1 }, null, () => undefined]) {
    const h = workerHarness({ configuringReply });
    const reply = await h.message({ type: "capture-rearm", requestId: 28 }, { url: videoUrl });
    assert.equal(reply.ok, false);
    assert.match(reply.error, /请点击浏览器工具栏 S，再点击生成学习内容/);
    assert.equal(h.stored, undefined);
    assert.equal(h.createCount, 0);
    assert.equal(h.streamCalls, 0);
    assert.equal(h.offscreenMessages.some((message) => ["capture-arm", "capture-start"].includes(message.type)), false);
    assert.equal(h.offscreenMessages.filter((message) => message.type === "capture-abort").length, 1);
    assert.equal(h.pageMessages.filter((message) => message.type === "capture-reset").length, 1);
    assert.equal(h.pageMessages.some((message) => message.type === "capture-configure"), false);
    const statuses = h.pageMessages.filter((message) => message.type === "status");
    assert.equal(statuses.every((message) => message.rearmRequestId === 28), true);
  }
  console.log("PASS a rejected or absent configuring acknowledgement cleans the new session before any audio environment or stream is created");
}
{
  const configuring = deferred();
  const h = workerHarness({ configuringReply: () => configuring.promise });
  const pending = h.message({ type: "capture-rearm", requestId: 29 }, { url: videoUrl });
  await until(() => h.pageMessages.some((message) => message.type === "capture-configuring"));
  h.timer.fire(3000); await tick();
  assert.equal((await pending).ok, false);
  assert.equal(h.stored, undefined);
  configuring.resolve({ ok: true }); await tick();
  assert.equal(h.createCount, 0);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.offscreenMessages.some((message) => message.type === "capture-arm"), false);
  console.log("PASS a timed-out configuring acknowledgement cannot revive a closed or cancelled rearm when it arrives late");
}
const reviewId = "00000000-0000-4000-8000-000000000077";
const reviewTarget = "https://www.youtube.com/shorts/ZyXwVu987_-";
const savedClip = (sourceUrl = videoUrl) => ({ capture_id: reviewId, source_url: sourceUrl,
  range_start_sec: 10, range_end_sec: 45, updated_at: 1234.5, sentences_done: 2 });
{
  const clip = { ...savedClip(`${videoUrl}&t=25&private=discard`), private_extra: "synthetic-private-marker" };
  const favorite = { capture_id: reviewId, sentence_id: "s001", source_url: videoUrl, saved_at: 1235,
    sentence: { ...readySentence(), raw_debug: "synthetic-private-marker" } };
  const h = workerHarness({ libraryReply: { clips: [clip, savedClip("https://example.invalid")], favorites: [favorite,
    { ...favorite, sentence_id: "another_sentence" }] }, learningReply: { status: "complete", clip } });
  const library = await h.message({ type: "review-library" });
  assert.equal(library.ok, true);
  assert.equal(library.clips.length, 1);
  assert.equal(library.favorites.length, 1);
  assert.equal(library.clips[0].source_url, videoUrl);
  assert.equal(library.favorites[0].sentence.study.reading_text, "何(なに)が必要(ひつよう)か。");
  assert.equal("text" in library.favorites[0].sentence, false);
  assert.equal(JSON.stringify(library).includes("synthetic-private-marker"), false);
  const learning = await h.message({ type: "capture-learning-status", captureId: reviewId });
  assert.equal(learning.learning.clip.capture_id, reviewId);
  assert.equal(learning.learning.clip.source_url, videoUrl);
  const favoriteReply = await h.message({ type: "review-favorite", captureId: reviewId, sentenceId: "s001", saved: true });
  assert.equal(favoriteReply.saved, true);
  assert.deepEqual(h.fetchOptions.at(-1), { method: "POST", credentials: "omit", body: {
    capture_id: reviewId, sentence_id: "s001", saved: true,
  } });
  for (const [payload, sender] of [
    [{ type: "review-library" }, { frameId: 1 }],
    [{ type: "review-open", captureId: "../invalid" }, {}],
    [{ type: "review-favorite", captureId: reviewId, sentenceId: "../invalid", saved: true }, {}],
    [{ type: "review-favorite", captureId: reviewId, sentenceId: "s001", saved: 1 }, {}],
    [{ type: "review-library" }, { id: "foreign" }],
  ]) assert.equal((await h.message(payload, sender)).ok, false);
  assert.deepEqual(h.fetches.map((url) => new URL(url).pathname), ["/learning/library", `/captures/${reviewId}/learning`, "/learning/favorites"]);
  assert.equal(h.streamCalls, 0);
  console.log("PASS review library and favorites validate senders, strip private fields, and use local read/save routes only");
}
{
  const h = workerHarness({ learningReply: { clip: savedClip(`${videoUrl}&t=55`) } });
  const reply = await h.message({ type: "review-open", captureId: reviewId, url: "https://example.invalid/forged" });
  assert.equal(reply.ok, true);
  assert.equal(reply.navigated, false);
  assert.equal(h.navigations.length, 0);
  assert.deepEqual(h.pageMessages.find((message) => message.type === "review-selected"), { type: "review-selected", captureId: reviewId });
  assert.deepEqual(h.fetches.map((url) => new URL(url).pathname), [`/captures/${reviewId}/learning`]);
  assert.equal(h.streamCalls, 0);
  console.log("PASS same-video review selects exactly the requested clip without navigation or capture");
}
for (const grantLost of [false, true]) {
  let grant = !grantLost;
  const h = workerHarness({ learningReply: { clip: savedClip(`${reviewTarget}?feature=share&t=31`) },
    injectionResults: () => {
      if (!grant) throw new Error("Synthetic missing activeTab permission");
      return [{ frameId: 0, result: pageMetadata }];
    } });
  const reply = await h.message({ type: "review-open", captureId: reviewId });
  assert.equal(reply.ok, true);
  assert.equal(reply.navigated, true);
  assert.deepEqual(h.navigations, [{ id: tab.id, url: reviewTarget }]);
  assert.deepEqual(h.pending.get(`pendingReview:${tab.id}`), { captureId: reviewId, videoId: "ZyXwVu987_-" });
  h.load(); await tick();
  if (grantLost) {
    assert.equal(h.pageMessages.some((m) => m.type === "review-selected"), false);
    assert.equal(h.pending.size, 1);
    grant = true;
    h.click({ ...tab, url: reviewTarget });
  }
  await until(() => h.pageMessages.some((m) => m.type === "review-selected")); await tick();
  assert.deepEqual(h.pageMessages.filter((m) => m.type === "review-selected"), [{ type: "review-selected", captureId: reviewId }]);
  assert.equal(h.pending.size, 0);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.createCount, 0);
  assert.deepEqual(h.fetches.map((url) => new URL(url).pathname), [`/captures/${reviewId}/learning`]);
}
console.log("PASS cross-video review restores the exact pending clip on load or the next toolbar grant, without arming");
{
  const h = workerHarness({ initial: { tabId: tab.id, phase: "recording" }, learningReply: { clip: savedClip(reviewTarget) } });
  const reply = await h.message({ type: "review-open", captureId: reviewId });
  assert.equal(reply.ok, false);
  assert.match(reply.error, /中断／释放/);
  assert.equal(h.fetches.length, 0);
  assert.equal(h.navigations.length, 0);
  const invalid = workerHarness({ learningReply: { clip: { ...savedClip(reviewTarget), capture_id: "00000000-0000-4000-8000-000000000078" } } });
  assert.equal((await invalid.message({ type: "review-open", captureId: reviewId })).ok, false);
  assert.equal(invalid.navigations.length, 0);
  const failed = workerHarness({ learningReply: { clip: savedClip(reviewTarget) }, failUpdate: true });
  assert.equal((await failed.message({ type: "review-open", captureId: reviewId })).ok, false);
  assert.equal(failed.pending.size, 0);
  console.log("PASS review cannot navigate an active capture, use mismatched clip metadata, or retain a failed navigation");
}
for (const preRoll of [false, true]) {
  const code = "advertisement_started";
  const h = workerHarness({ prepareReply: preRoll ? { ok: false, code, error: "请等待当前播放器的广告结束后再开始采集。" } : undefined,
    inspect: { ok: true, phase: "failed", pendingBatches: 1, pendingBytes: 10 } });
  assert.equal((await h.arm()).ok, true);
  const sessionId = h.stored.sessionId;
  const reply = await h.message({ type: "capture-begin", sessionId, rangeStart: 10, rangeEnd: 45, batchIntervalMs: 5000 });
  assert.equal(reply.ok, !preRoll);
  assert.equal(h.offscreenMessages.some((message) => message.type === "capture-start"), !preRoll);
  if (!preRoll) {
    await h.message({ type: "capture-error", sessionId, code, error: "当前播放器开始播放广告，采集已停止。" },
      { tab: undefined, url: origin + "offscreen.html" });
  } else assert.equal(reply.code, code);
  assert.equal(h.stored.failureCode, code);
  assert.equal(h.stored.rangeStart, 10);
  assert.equal(h.stored.rangeEnd, 45);
  assert.equal(h.pageMessages.find((m) => m.type === "capture-failed").code, code);
  h.click(); await until(() => h.pageMessages.some((m) => m.type === "capture-recovery"));
  const recovery = h.pageMessages.find((m) => m.type === "capture-recovery");
  assert.equal(recovery.code, code);
  assert.equal(recovery.rangeStart, 10);
  assert.equal(recovery.rangeEnd, 45);
}
console.log("PASS pre-roll and mid-capture ad interruption retain a bounded code and original interval through recovery");
{
  const h = workerHarness({ reviewStatus: 404 });
  for (const payload of [{ type: "review-library" }, { type: "review-favorite", captureId: reviewId, sentenceId: "s001", saved: true }]) {
    const reply = await h.message(payload);
    assert.equal(reply.ok, false);
    assert.equal(reply.code, "backend_upgrade_required");
    assert.match(reply.error, /重启本机后端/);
    assert.equal(JSON.stringify(reply).includes("synthetic-private-error"), false);
  }
  const current = workerHarness();
  const empty = await current.message({ type: "review-library" });
  assert.equal(empty.ok, true);
  assert.equal(empty.clips.length, 0);
  assert.equal(empty.favorites.length, 0);
  assert.equal(h.streamCalls, 0);
  console.log("PASS missing review APIs explain backend restart while a real empty library remains a successful empty result");
}
console.log("48 preparation, recovery, review, learning, status, and failure lifecycle checks passed.");
