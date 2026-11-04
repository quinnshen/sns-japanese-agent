/** Offline preparation lifecycle checks; fake Chrome and media, no ports or credentials. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
const workerSource = await readFile(new URL("../app/extension/service-worker.js", import.meta.url), "utf8");
const recorderSource = await readFile(new URL("../app/extension/offscreen.js", import.meta.url), "utf8");
const extensionId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const origin = `chrome-extension://${extensionId}/`;
const videoUrl = "https://www.youtube.com/watch?v=AbCdEf012_-";
const tab = { id: 7, url: videoUrl };
const pageMetadata = { ok: true, version: "0.5.2", protocol: 1 };
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
  learningReply = {}, latestLearningReply = learningReply, currentTabUrl = videoUrl } = {}) {
  const timer = clock(); let action; let listener; let stored = initial; let contexts = documentExists;
  let serial = 0; let closeCount = 0; let streamCalls = 0; let createCount = 0; let contextCalls = 0;
  const pageMessages = []; const offscreenMessages = []; const fetches = [];
  const injections = []; const badges = []; const titles = []; const lifecycle = []; const pageTargets = [];
  const fetchOptions = [];
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
        if (message.type === "capture-inspect") return inspect;
        if (message.type === "capture-abort" && stallAbort) return new Promise(() => {});
        return { ok: true };
      },
    },
    storage: { session: {
      async get() { return { activeCapture: stored && structuredClone(stored) }; },
      async set(value) { stored = structuredClone(value.activeCapture); },
      async remove() { stored = undefined; },
    } },
    scripting: { async executeScript(value) {
      lifecycle.push("inject"); injections.push(structuredClone(value));
      return typeof injectionResults === "function" ? injectionResults() : structuredClone(injectionResults);
    } },
    offscreen: { async createDocument() { lifecycle.push("create-offscreen"); contexts = true; createCount++; }, async closeDocument() { contexts = false; closeCount++; } },
    tabCapture: { async getMediaStreamId() { lifecycle.push("stream"); streamCalls++; return streamId ? streamId(streamCalls) : "synthetic-stream"; } },
    tabs: {
      async get() { return { ...tab, url: currentTabUrl }; }, async create() {}, onRemoved: { addListener() {} },
      async sendMessage(tabId, message, options) {
        pageMessages.push(structuredClone(message)); pageTargets.push({ tabId, options: structuredClone(options) });
        if (message.type === "page-handshake") {
          lifecycle.push("handshake");
          return typeof handshakeReply === "function" ? handshakeReply() : structuredClone(handshakeReply);
        }
        if (statusUnavailable && message.type === "status") throw new Error("Receiving end does not exist.");
        return { ok: true };
      },
    },
  };
  const context = vm.createContext({ chrome, URL, AbortController, ...timer, crypto: { randomUUID() { return `00000000-0000-4000-8000-${String(++serial).padStart(12, "0")}`; } },
    async fetch(url, options) {
      lifecycle.push("fetch"); fetches.push(url);
      fetchOptions.push(structuredClone({ method: options?.method || "GET", credentials: options?.credentials }));
      return { ok: true, async json() {
        const path = new URL(url).pathname;
        if (path === "/health") return { capabilities: { capture_batches: true, capture_transcription: transcriptionEnabled, capture_enrichment: enrichmentEnabled } };
        if (path === "/learning/latest") return latestLearningReply;
        if (path.endsWith("/learning")) return learningReply;
        return transcriptionReply;
      } };
    },
  });
  vm.runInContext(workerSource, context);
  return { timer, pageMessages, offscreenMessages, fetches, fetchOptions, injections, badges, titles, lifecycle, pageTargets,
    click: () => action(tab), get stored() { return stored; }, get closeCount() { return closeCount; }, get streamCalls() { return streamCalls; },
    get createCount() { return createCount; }, get contextCalls() { return contextCalls; },
    message: (payload, senderOverride = {}) => new Promise((resolve) => { listener({ target: "worker", ...payload }, { id: extensionId, tab, frameId: 0, ...senderOverride }, resolve); }),
  };
}
{
  const h = workerHarness({ streamId: (attempt) => attempt === 1 ? new Promise(() => {}) : "retry-stream", stallAbort: true });
  h.click(); await tick();
  assert.equal(h.stored.phase, "preparing");
  assert.ok(h.pageMessages.some((m) => m.type === "capture-configuring"));
  assert.equal(h.pageMessages.some((m) => m.type === "capture-configure"), false);
  await until(() => h.streamCalls === 1);
  h.timer.fire(10_000); await tick();
  h.timer.fire(3000); await tick();
  assert.equal(h.stored, undefined, "Timed-out preparation must not lock the next extension action");
  assert.equal(h.closeCount, 1);
  h.click(); await tick();
  assert.equal(h.stored.phase, "ready");
  assert.equal(h.streamCalls, 2);
  assert.ok(h.pageMessages.some((m) => m.type === "status" && /超时/.test(m.text)));
  console.log("PASS preparation and cancellation waits expire; a new action can initialize again");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "preparing" };
  const h = workerHarness({ initial: previous });
  h.click(); await tick();
  assert.equal(h.stored.phase, "ready");
  assert.notEqual(h.stored.sessionId, previous.sessionId);
  assert.equal(h.streamCalls, 1);
  console.log("PASS stale storage without a recording document reinitializes without touching disk batches");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "failed" };
  const h = workerHarness({ initial: previous, documentExists: true, inspect: { ok: true, phase: "failed", pendingBatches: 5, pendingBytes: 1000 } });
  h.click(); await tick();
  assert.equal(h.stored.sessionId, previous.sessionId);
  assert.equal(h.closeCount, 0);
  assert.equal(h.streamCalls, 0);
  assert.equal(h.offscreenMessages.some((m) => m.type === "capture-abort"), false);
  assert.ok(h.pageMessages.some((m) => m.type === "capture-failed"));
  assert.equal((await h.message({ type: "capture-cancel", sessionId: previous.sessionId })).ok, true);
  assert.equal(h.closeCount, 1);
  assert.equal(h.stored, undefined);
  console.log("PASS failed unacknowledged queue survives repeated action until explicit cancellation");
}
{
  const previous = { tabId: tab.id, sourceUrl: videoUrl, sessionId: "00000000-0000-4000-8000-000000000099", phase: "preparing" };
  const h = workerHarness({ initial: previous, documentExists: true, stallAbort: true });
  const cancelled = h.message({ type: "capture-cancel", sessionId: previous.sessionId });
  await tick(); h.timer.fire(4000); await tick();
  assert.equal((await cancelled).ok, true);
  assert.equal(h.stored, undefined);
  assert.equal(h.closeCount, 1);
  assert.ok(h.pageMessages.some((m) => m.type === "status" && /后端中断状态未确认/.test(m.text)));
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
    h.click(); await until(() => h.pageMessages.some((m) => m.type === "status" && m.text.startsWith("选择采集范围")));
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
  h.click(); await until(() => h.stored?.phase === "ready");
  assert.deepEqual(h.lifecycle.slice(0, 3), ["inject", "handshake", "fetch"]);
  assert.equal(h.injections.length, 1);
  assert.deepEqual(h.injections[0], { target: { tabId: tab.id }, files: ["page.js"] });
  assert.equal(h.pageMessages[0].type, "page-handshake");
  assert.equal(h.pageMessages[0].version, pageMetadata.version);
  assert.equal(h.pageMessages[0].protocol, pageMetadata.protocol);
  assert.equal(h.createCount, 1);
  assert.equal(h.streamCalls, 1);
  assert.equal(h.titles.at(-1)?.title.includes("刷新当前"), false,
    "Normal readiness must reset an earlier action tooltip");
  console.log("PASS verified current page code completes its handshake before normal capture preparation");
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
  h.click(); await until(() => h.stored?.phase === "ready");
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
  h.click(); await until(() => h.pageMessages.some((message) => message.type === "status" && /GPT.*预生成/.test(message.text)));
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
  h.click(); await until(() => h.pageMessages.some((message) => message.type === "status" && message.text.startsWith("选择采集范围")));
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
console.log("26 preparation, handshake, learning, diagnostic, status, and failure lifecycle checks passed.");
