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
function workerHarness({ initial, documentExists = false, inspect = { ok: true, phase: "ready" }, streamId, stallAbort = false } = {}) {
  const timer = clock(); let action; let listener; let stored = initial; let contexts = documentExists;
  let serial = 0; let closeCount = 0; let streamCalls = 0;
  const pageMessages = []; const offscreenMessages = [];
  const chrome = {
    action: { onClicked: { addListener(fn) { action = fn; } }, async setBadgeText() {}, async setBadgeBackgroundColor() {} },
    runtime: {
      id: extensionId, getURL: (path) => origin + path,
      async getContexts() { return contexts ? [{}] : []; },
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
    scripting: { async executeScript() {} },
    offscreen: { async createDocument() { contexts = true; }, async closeDocument() { contexts = false; closeCount++; } },
    tabCapture: { async getMediaStreamId() { streamCalls++; return streamId ? streamId(streamCalls) : "synthetic-stream"; } },
    tabs: {
      async get() { return tab; }, async create() {}, onRemoved: { addListener() {} },
      async sendMessage(_id, message) { pageMessages.push(structuredClone(message)); return { ok: true }; },
    },
  };
  const context = vm.createContext({ chrome, URL, AbortController, ...timer, crypto: { randomUUID() { return `00000000-0000-4000-8000-${String(++serial).padStart(12, "0")}`; } },
    async fetch() { return { ok: true, async json() { return { capabilities: { capture_batches: true } }; } }; },
  });
  vm.runInContext(workerSource, context);
  return { timer, pageMessages, offscreenMessages, click: () => action(tab), get stored() { return stored; }, get closeCount() { return closeCount; }, get streamCalls() { return streamCalls; },
    message: (payload) => new Promise((resolve) => { listener({ target: "worker", ...payload }, { id: extensionId, tab, frameId: 0 }, resolve); }),
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
console.log("7 preparation and failure lifecycle checks passed.");
