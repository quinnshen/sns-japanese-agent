/** Offline extension contract checks. Synthetic bytes only; no Chrome, cloud, credentials, or network. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { webcrypto } from "node:crypto";

const source = await readFile(new URL("../app/extension/offscreen.js", import.meta.url), "utf8");
const workerSource = await readFile(new URL("../app/extension/service-worker.js", import.meta.url), "utf8");
const pageSource = await readFile(new URL("../app/extension/page.js", import.meta.url), "utf8");
const id = "00000000-0000-4000-8000-000000000001";
const workerUrl = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/service-worker.js";
const sender = { id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", url: workerUrl };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function eventually(check) {
  for (let n = 0; n < 150; n++) { if (check()) return; await sleep(20); }
  assert.fail("Expected extension event did not arrive");
}
function harness({ uploadStatus = [], badAck = false, holdFirst = false, finalBytes = "end" } = {}) {
  let listener;
  let recorder;
  let attempts = 0;
  let release;
  const messages = [];
  const requests = [];
  const stored = new Map();
  let manifest = {};
  class Recorder {
    static isTypeSupported() { return true; }
    constructor(_stream, options) { this.state = "inactive"; this.options = options; recorder = this; }
    start(interval) { this.state = "recording"; this.interval = interval; }
    emit(bytes, timecode = 0) { this.ondataavailable?.({ data: new Blob([bytes]), timecode }); }
    stop() {
      assert.equal(this.state, "recording");
      this.state = "inactive";
      setTimeout(() => { this.emit(finalBytes, 9000); this.onstop?.(); }, 0);
    }
  }
  const track = { stop() { this.stopped = true; } };
  const context = vm.createContext({
    Blob, AbortController, URL, Date, performance, crypto: webcrypto,
    setTimeout: (fn, ms) => { const timer = setTimeout(fn, ms); if (ms > 2000) timer.unref(); return timer; },
    clearTimeout, setInterval: (fn, ms) => { const timer = setInterval(fn, ms); timer.unref(); return timer; }, clearInterval,
    navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [track] }) } },
    AudioContext: class { createMediaStreamSource() { return { connect() {} }; } async resume() {} async close() {} },
    MediaRecorder: Recorder,
    chrome: { runtime: {
      id: sender.id, getURL: (path) => `chrome-extension://${sender.id}/${path}`,
      onMessage: { addListener(fn) { listener = fn; } },
      async sendMessage(message) { messages.push(structuredClone(message)); return { ok: true }; },
    } },
    async fetch(url, options) {
      const path = new URL(url).pathname;
      requests.push({ path, method: options.method, body: options.body, headers: options.headers });
      if (path === "/captures") {
        manifest = { ...JSON.parse(options.body), status: "recording" };
        return { ok: true, status: 201, json: async () => ({ ...manifest }) };
      }
      if (path.includes("/batches/")) {
        attempts++;
        if (holdFirst && attempts === 1) {
          await new Promise((resolve, reject) => {
            release = resolve;
            options.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
          });
        }
        const status = uploadStatus.shift() || 200;
        if (status === "network") throw new TypeError("offline");
        if (status !== 200) return { ok: false, status, json: async () => ({ error: "synthetic failure" }) };
        const sequence = Number(path.split("/").at(-1));
        const hash = options.headers["X-Batch-SHA256"];
        const blob = options.body;
        assert.equal(hash, Buffer.from(await webcrypto.subtle.digest("SHA-256", await blob.arrayBuffer())).toString("hex"));
        stored.set(sequence, blob);
        return { ok: true, status: 200, json: async () => ({ id, sequence, stored: true, sha256: badAck ? "incorrect" : hash, size_bytes: blob.size }) };
      }
      if (path.endsWith("/complete")) {
        const body = JSON.parse(options.body);
        assert.equal(body.expected_batches, stored.size);
        assert.equal(body.playback_events[0].type, "playing");
        manifest = { ...manifest, ...body, status: "complete", audio_size_bytes: [...stored.values()].reduce((sum, blob) => sum + blob.size, 0) };
        return { ok: true, status: 200, json: async () => ({ ...manifest }) };
      }
      return { ok: true, status: 200, json: async () => ({ id, status: path.endsWith("/abort") ? "aborted" : "recording" }) };
    },
  });
  vm.runInContext(source, context);
  const dispatch = (type, payload = {}, origin = sender) => new Promise((resolve) => {
    const keepOpen = listener({ target: "offscreen", type, sessionId: id, ...payload }, origin, resolve);
    if (keepOpen !== true) resolve(undefined);
  });
  return { dispatch, messages, requests, stored, get recorder() { return recorder; }, get attempts() { return attempts; }, release: () => release?.() };
}
async function begin(h) {
  assert.equal((await h.dispatch("capture-arm", { streamId: "synthetic-stream" })).ok, true);
  assert.equal(h.recorder, undefined, "Arming must not create a recorder");
  assert.equal((await h.dispatch("capture-start", { url: "https://www.youtube.com/watch?v=AbCdEf012_-", duration: 600, rangeStart: 30, rangeEnd: 150, batchIntervalMs: 5000 })).ok, true);
  assert.equal(h.recorder.options.audioBitsPerSecond, 64000);
  assert.equal(h.recorder.interval, 5000);
  assert.equal((await h.dispatch("playback-started", { playbackStartedAtMs: Date.now(), playbackStartVideoSec: 30.01 })).ok, true);
}
async function finish(h) {
  await h.dispatch("capture-finish", { playbackEndVideoSec: 150.01, playbackEvents: [{ type: "playing", at_ms: Date.now(), video_sec: 30.01 }] });
}

{
  const h = harness({ holdFirst: true });
  await begin(h);
  h.recorder.emit("one", 0);
  h.recorder.emit("two", 5000);
  await eventually(() => h.attempts === 1);
  await finish(h);
  await eventually(() => h.messages.some((m) => m.progress?.produced === 3));
  const pending = h.messages.filter((m) => m.type === "capture-progress").at(-1).progress;
  assert.equal(pending.acknowledged, 0);
  assert.equal(pending.pending_bytes, 9, "Unacknowledged blobs must remain queued, including final dataavailable");
  assert.equal(h.requests.some((r) => r.path.endsWith("/complete")), false);
  h.release();
  await eventually(() => h.messages.some((m) => m.type === "capture-complete"));
  assert.deepEqual([...h.stored.keys()], [0, 1, 2]);
  assert.equal(h.messages.find((m) => m.type === "capture-complete").manifest.audio_size_bytes, 9);
  assert.equal(h.messages.filter((m) => m.progress).at(-1).progress.pending_bytes, 0);
  console.log("PASS stop flushes final batch; data retained until matching ack; ordered completion");
}
{
  const h = harness({ uploadStatus: [500, "network", 200] });
  await begin(h); h.recorder.emit("one");
  await eventually(() => h.stored.size === 1);
  assert.equal(h.attempts, 3);
  const retried = h.requests.filter((r) => r.path.includes("/batches/"));
  assert.equal(new Set(retried.map((r) => r.headers["X-Batch-SHA256"])).size, 1);
  await finish(h); await eventually(() => h.messages.some((m) => m.type === "capture-complete"));
  console.log("PASS transient retries preserve the same sequence, bytes, hash, and timing");
}
for (const [name, options, expected] of [
  ["non-transient rejection", { uploadStatus: [409] }, 1],
  ["bounded transient failure", { uploadStatus: [500, 500, 500, 200] }, 3],
  ["mismatched durable-save acknowledgement", { badAck: true }, 1],
]) {
  const h = harness(options);
  await begin(h); h.recorder.emit("one");
  await eventually(() => h.messages.some((m) => m.type === "capture-error"));
  assert.equal(h.attempts, expected);
  assert.equal(h.messages.some((m) => m.type === "capture-complete"), false);
  assert.equal(h.requests.some((r) => r.path.endsWith("/abort")), true);
  const failed = h.messages.find((m) => m.type === "capture-error");
  assert.equal(failed.pendingBatches, 1);
  assert.equal(failed.pendingBytes, 3, "Failed unacknowledged bytes remain until explicit user discard");
  await h.dispatch("capture-abort");
  console.log(`PASS ${name} stops explicitly without reporting complete`);
}
{
  const h = harness({ holdFirst: true });
  await begin(h);
  h.recorder.emit(new Uint8Array(9 * 1024 * 1024));
  await eventually(() => h.attempts === 1);
  h.recorder.emit(new Uint8Array(9 * 1024 * 1024));
  await eventually(() => h.messages.some((m) => m.type === "capture-error"));
  assert.equal(h.recorder.state, "inactive");
  assert.match(h.messages.find((m) => m.type === "capture-error").error, /超过限制/);
  console.log("PASS pending upload queue is bounded and stops the recorder on overflow");
}
{
  const h = harness();
  assert.equal(await h.dispatch("capture-arm", { streamId: "synthetic" }, { id: sender.id, tab: { id: 1 } }), undefined);
  assert.equal(h.recorder, undefined);
  await begin(h);
  await h.dispatch("capture-abort");
  assert.equal(h.requests.some((r) => r.path.endsWith("/complete")), false);
  assert.equal(h.requests.some((r) => r.path.endsWith("/abort")), true);
  console.log("PASS page-origin commands cannot control the recorder; user cancellation marks abort");
}
{
  const h = harness();
  await begin(h);
  h.recorder.emit("one");
  await eventually(() => h.stored.size === 1);
  const failure = { code: "video_replaced", message: "采集的视频播放器已被替换。", at_ms: Date.now(),
    video_sec: 727.2, video_connected: false, player_connected: true, video_replaced: true };
  const playbackEvents = [{ type: "playing", at_ms: Date.now(), video_sec: 30.01 }];
  assert.equal((await h.dispatch("capture-fail", { error: failure.message, failure, playbackEndVideoSec: 727.2, playbackEvents })).ok, true);
  const body = JSON.parse(h.requests.find((request) => request.path.endsWith("/abort")).body);
  assert.deepEqual(body.failure, failure);
  assert.deepEqual(body.playback_events, playbackEvents);
  assert.equal(body.playback_end_video_sec, 727.2);
  assert.ok(Number.isFinite(body.recording_stopped_at_ms));
  assert.equal(h.requests.some((request) => request.path.endsWith("/complete")), false);
  await h.dispatch("capture-abort");
  assert.equal(h.requests.filter((request) => request.path.endsWith("/abort")).length, 1,
    "Releasing a failed session must preserve its original abort diagnostic");
  console.log("PASS interrupted capture saves its failure and playback observations without claiming completion");
}
{
  const h = harness();
  await begin(h);
  const privateError = "Synthetic transfer failure https://example.invalid/private-location";
  await h.dispatch("capture-fail", { error: privateError, failure: { code: "audio_capture_failed", message: privateError } });
  const body = JSON.parse(h.requests.find((request) => request.path.endsWith("/abort")).body);
  assert.equal(body.failure.code, "audio_capture_failed");
  assert.equal(body.failure.message, "浏览器音频采集中断。");
  assert.equal(JSON.stringify(body).includes("private-location"), false);
  await h.dispatch("capture-abort");
  console.log("PASS persisted failure text excludes URLs from runtime errors");
}
assert.equal(source.includes("/jobs"), false);
assert.equal(workerSource.includes("/jobs"), false);
assert.equal(source.includes("recognize"), false);
assert.equal(pageSource.includes("innerHTML"), false);
assert.match(workerSource, /capture-replay/);
assert.match(pageSource, /playbackEvents/);
console.log("PASS stage 1 has no paid processing endpoint and uses safe text rendering");
{
  const parseSource = pageSource.slice(pageSource.indexOf("  function parseTime("), pageSource.indexOf("  function setInputsDisabled("));
  const validation = vm.createContext({});
  vm.runInContext(`${parseSource}; this.parse = parseTime; this.validate = validateRange;`, validation);
  assert.equal(validation.parse("1:02:03.5"), 3723.5);
  assert.equal(validation.parse("90"), 90);
  assert.throws(() => validation.parse("0:60"));
  assert.throws(() => validation.parse("-1"));
  assert.doesNotThrow(() => validation.validate(1200, 1500, 4000));
  assert.throws(() => validation.validate(0, 0.9, 100));
  assert.throws(() => validation.validate(0, 1801, 4000));
  assert.throws(() => validation.validate(0, 100.001, 100));
  console.log("PASS range selection accepts long-source excerpts and rejects invalid boundaries");
}
console.log("11 offline extension checks passed.");
