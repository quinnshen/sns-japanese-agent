const API = "http://127.0.0.1:8766";
const MAX_PENDING_BYTES = 16 * 1024 * 1024;
const MAX_BATCH_BYTES = 10_000_000;
const MAX_TOTAL_BYTES = 200_000_000;
const REQUEST_TIMEOUT_MS = 15_000;
let session;

async function within(promise, milliseconds, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}

function send(current, type, data = {}) {
  return chrome.runtime.sendMessage({ target: "worker", type, sessionId: current.sessionId, ...data });
}

async function request(current, path, options = {}, cancellable = true) {
  for (let attempt = 0; attempt < 3; attempt++) {
    if (cancellable && current.cancelled) throw new Error("采集已中断");
    const controller = new AbortController();
    current.controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(`${API}${path}`, { ...options, signal: controller.signal });
      let body;
      try { body = await response.json(); } catch (_) { throw Object.assign(new Error("本机服务返回了无效 JSON"), { retryable: false }); }
      if (response.ok) return body;
      const retryable = response.status >= 500 || [408, 429].includes(response.status);
      throw Object.assign(new Error(body.error || `HTTP ${response.status}`), { retryable });
    } catch (error) {
      if ((cancellable && current.cancelled) || error.retryable === false || attempt === 2) throw error;
    } finally {
      clearTimeout(timer);
      current.controllers.delete(controller);
    }
    await new Promise((resolve) => setTimeout(resolve, 300 * 2 ** attempt));
  }
}

const jsonRequest = (current, path, body, cancellable = true) => request(current, path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
}, cancellable);

function progress(current) {
  if (session !== current) return;
  void send(current, "capture-progress", { progress: {
    id: current.sessionId, phase: current.phase, produced: current.produced || 0,
    acknowledged: current.acknowledged || 0, pending_bytes: current.pendingBytes || 0,
    peak_pending_bytes: current.peakPendingBytes || 0, total_bytes: current.totalBytes || 0,
    elapsed_sec: current.startedMono === undefined ? 0 : Math.max(0, ((current.stoppedMono ?? performance.now()) - current.startedMono) / 1000),
    waiting_events: current.playbackEvents?.filter((item) => item.type === "waiting").length || 0,
  } }).catch(() => {});
}

async function releaseMedia(current) {
  clearTimeout(current.watchdog);
  clearTimeout(current.armTimeout);
  clearInterval(current.progressTimer);
  current.stream?.getTracks().forEach((track) => { track.onended = null; track.stop(); });
  current.stream = undefined;
  if (current.context) await within(current.context.close(), 1000, "音频输出关闭超时").catch(() => {});
  current.context = undefined;
}

function failureDetails(details, notify) {
  const candidate = details.failure || {};
  const code = typeof candidate.code === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(candidate.code)
    ? candidate.code : notify ? "audio_capture_failed" : "user_cancelled";
  // Persist fixed diagnostic text only. Fetch errors can embed a URL or local path.
  const message = typeof candidate.message === "string" && candidate.message.length > 0 && candidate.message.length <= 240 &&
    !/https?:|file:|chrome-extension:|(?:^|\s)\//i.test(candidate.message)
    ? candidate.message : notify ? "浏览器音频采集中断。" : "用户中断采集。";
  const failure = { code, message, at_ms: Number.isFinite(candidate.at_ms) && candidate.at_ms >= 0 ? candidate.at_ms : Date.now() };
  if (Number.isFinite(candidate.video_sec) && candidate.video_sec >= 0) failure.video_sec = candidate.video_sec;
  for (const key of ["video_connected", "player_connected", "ad_showing", "identity_changed", "video_replaced"]) {
    if (typeof candidate[key] === "boolean") failure[key] = candidate[key];
  }
  return failure;
}

async function abort(current, error, notify = false, details = {}) {
  if (!current.aborting) {
    current.aborting = (async () => {
      current.failure ||= failureDetails(details, notify);
      current.cancelled = true;
      current.phase = "failed";
      if (current.startedMono !== undefined) {
        current.stoppedMono ??= performance.now();
        current.recordingStoppedAtMs ??= Date.now();
      }
      if (Array.isArray(details.playbackEvents)) {
        current.playbackEvents = details.playbackEvents.slice(0, 1000).filter((event) =>
          ["playing", "waiting"].includes(event?.type) && Number.isFinite(event.at_ms) && event.at_ms >= 0 &&
          Number.isFinite(event.video_sec) && event.video_sec >= 0).map(({ type, at_ms, video_sec }) => ({ type, at_ms, video_sec }));
      }
      current.controllers.forEach((controller) => controller.abort());
      if (current.recorder?.state !== "inactive" && current.recorder) current.recorder.stop();
      await releaseMedia(current);
      current.abortConfirmed = !current.created;
      if (current.created) {
        const payload = { failure: current.failure };
        if (current.recordingStoppedAtMs !== undefined) payload.recording_stopped_at_ms = current.recordingStoppedAtMs;
        if (Number.isFinite(details.playbackEndVideoSec) && details.playbackEndVideoSec >= 0) payload.playback_end_video_sec = details.playbackEndVideoSec;
        if (current.playbackEvents?.length) payload.playback_events = current.playbackEvents;
        try {
          const receipt = await jsonRequest(current, `/captures/${current.sessionId}/abort`, payload, false);
          if (receipt?.id !== current.sessionId || receipt.status !== "aborted") throw new Error("中断确认不匹配");
          current.abortConfirmed = true;
        }
        catch (_) { error = `${error}；无法确认后端中断状态，请检查本机任务清单`; }
      }
      // Keep every unacknowledged Blob in the failed session until explicit user cancellation.
      progress(current);
      if (notify) await send(current, "capture-error", {
        error, pendingBatches: current.queue?.length || 0, pendingBytes: current.pendingBytes || 0,
        ...(current.failure.code === "advertisement_started" ? { code: current.failure.code } : {}),
      }).catch(() => {});
    })();
  }
  await current.aborting;
  if (!notify) {
    // The user explicitly chose to discard the failed/unfinished browser queue.
    current.queue = [];
    current.pendingBytes = 0;
    if (session === current) session = undefined;
  }
  return current.abortConfirmed === true;
}

async function arm(message) {
  if (session) throw new Error("已有采集会话。");
  const current = { ...message, controllers: new Set(), phase: "ready", playbackEvents: [] };
  session = current;
  try {
    const acquisition = navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: message.streamId } }, video: false,
    }).then((stream) => {
      // getUserMedia cannot be cancelled. Stop a late stream after timeout, cancellation, or reinitialization.
      if (current.cancelled || session !== current) {
        stream.getTracks().forEach((track) => track.stop());
        throw new Error("音频准备已取消。");
      }
      return stream;
    });
    const stream = await within(acquisition, 10_000, "浏览器音频访问超时。请先正常播放视频，再重新点击扩展图标。");
    current.stream = stream;
    const context = new AudioContext();
    current.context = context;
    context.createMediaStreamSource(stream).connect(context.destination);
    await within(context.resume(), 3000, "浏览器未启动音频输出。请先点击视频正常播放，再重新点击扩展图标。");
    if (current.cancelled || session !== current) throw new Error("音频准备已取消。");
    for (const track of stream.getTracks()) track.onended = () => { void abort(current, "标签页音频流已停止", true); };
    current.armTimeout = setTimeout(() => { void abort(current, "准备超过 5 分钟，请重新点击扩展图标", true); }, 5 * 60_000);
    return { ok: true };
  } catch (error) {
    current.cancelled = true;
    await releaseMedia(current);
    if (session === current) session = undefined;
    throw error;
  }
}

async function sha256(blob) {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

async function pump(current) {
  if (current.pumping) return;
  current.pumping = true;
  try {
    while (current.queue.length && !current.cancelled) {
      const item = current.queue[0];
      item.hash ||= await sha256(item.blob);
      const headers = {
        "Content-Type": current.mimeType, "X-Batch-SHA256": item.hash,
        "X-Capture-Start-Ms": String(item.startMs), "X-Capture-End-Ms": String(item.endMs),
      };
      if (Number.isFinite(item.timecode)) headers["X-Blob-Timecode-Ms"] = String(item.timecode);
      const ack = await request(current, `/captures/${current.sessionId}/batches/${item.sequence}`, { method: "PUT", headers, body: item.blob });
      if (ack.id !== current.sessionId || ack.sequence !== item.sequence || ack.stored !== true ||
          ack.sha256 !== item.hash || ack.size_bytes !== item.blob.size) {
        throw new Error(`第 ${item.sequence} 批确认信息不匹配`);
      }
      // Drop the only queued Blob reference strictly after a matching durable-save acknowledgement.
      current.pendingBytes -= item.blob.size;
      current.queue.shift();
      current.acknowledged++;
      progress(current);
    }
  } catch (error) {
    current.uploadError = error;
    void abort(current, error.message, true);
  } finally { current.pumping = false; }
}

function enqueue(current, event) {
  if (current.cancelled || !event.data?.size) return;
  const endMs = Math.max(current.lastEmissionMs, performance.now() - current.startedMono);
  const item = { sequence: current.produced++, blob: event.data, startMs: current.lastEmissionMs, endMs, timecode: event.timecode };
  current.lastEmissionMs = endMs;
  current.pendingBytes += item.blob.size;
  current.totalBytes += item.blob.size;
  current.peakPendingBytes = Math.max(current.peakPendingBytes, current.pendingBytes);
  current.queue.push(item);
  progress(current);
  if (item.blob.size > MAX_BATCH_BYTES || current.pendingBytes > MAX_PENDING_BYTES || current.totalBytes > MAX_TOTAL_BYTES) {
    void abort(current, "音频批次、待确认队列或总量超过限制，已停止采集", true);
    return;
  }
  void pump(current);
}

async function start(message) {
  const current = session;
  if (!current || current.sessionId !== message.sessionId || current.phase !== "ready") throw new Error("采集准备已失效。");
  clearTimeout(current.armTimeout);
  if (!Number.isInteger(message.batchIntervalMs) || message.batchIntervalMs < 1000 || message.batchIntervalMs > 30000) throw new Error("上传间隔应为 1–30 秒。");
  current.mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm";
  const metadata = {
    id: current.sessionId, source_url: message.url, video_duration_sec: message.duration,
    range_start_sec: message.rangeStart, range_end_sec: message.rangeEnd,
    batch_interval_ms: message.batchIntervalMs, mime_type: current.mimeType,
  };
  current.created = true; // Even an unknown create response may have left a persisted task.
  const created = await jsonRequest(current, "/captures", metadata);
  if (created.id !== current.sessionId || created.status !== "recording") throw new Error("创建采集任务的确认无效。");
  Object.assign(current, { queue: [], produced: 0, acknowledged: 0, pendingBytes: 0, peakPendingBytes: 0, totalBytes: 0, lastEmissionMs: 0 });
  const recorder = new MediaRecorder(current.stream, { mimeType: current.mimeType, audioBitsPerSecond: 64_000 });
  current.recorder = recorder;
  current.stopped = new Promise((resolve) => { recorder.onstop = resolve; });
  recorder.ondataavailable = (event) => enqueue(current, event);
  recorder.onerror = () => { void abort(current, "浏览器音频录制失败", true); };
  current.recordingStartedAtMs = Date.now();
  current.startedMono = performance.now();
  recorder.start(message.batchIntervalMs);
  current.phase = "recording";
  current.watchdog = setTimeout(() => { void abort(current, "录音超过所选范围的允许等待时间", true); }, (message.rangeEnd - message.rangeStart + 15) * 1000);
  current.progressTimer = setInterval(() => progress(current), 1000);
  progress(current);
  return { ok: true };
}

async function timing(message) {
  const current = session;
  if (!current || current.sessionId !== message.sessionId || current.cancelled) throw new Error("采集已失效。");
  if (!current.timing) {
    current.timing = {
      recording_started_at_ms: current.recordingStartedAtMs,
      playback_started_at_ms: message.playbackStartedAtMs,
      playback_start_video_sec: message.playbackStartVideoSec,
    };
    current.playbackEvents.push({ type: "playing", at_ms: message.playbackStartedAtMs, video_sec: message.playbackStartVideoSec });
    current.timingSaved = jsonRequest(current, `/captures/${current.sessionId}/timing`, current.timing);
  }
  await current.timingSaved;
  return { ok: true };
}

function playbackEvent(message) {
  const current = session;
  if (!current?.timing || current.sessionId !== message.sessionId) return { ok: true };
  if (!["playing", "waiting"].includes(message.event?.type) || !Number.isFinite(message.event.at_ms) || !Number.isFinite(message.event.video_sec)) throw new Error("播放观测无效。");
  if (current.playbackEvents.length >= 1000) throw new Error("播放等待过于频繁，请重试。");
  current.playbackEvents.push(message.event);
  progress(current);
  return { ok: true };
}

async function finish(message) {
  const current = session;
  if (!current || current.sessionId !== message.sessionId || current.finishing || current.cancelled) return;
  current.finishing = true;
  try {
    current.phase = "saving";
    current.recorder.stop();
    await current.stopped; // The final dataavailable handler has run before this stop event.
    current.recordingStoppedAtMs = Date.now();
    current.stoppedMono = performance.now();
    await releaseMedia(current);
    if (!current.timingSaved) throw new Error("缺少实际开始播放的时间观测。");
    await current.timingSaved;
    while ((current.pumping || current.queue.length) && !current.cancelled) await new Promise((resolve) => setTimeout(resolve, 50));
    if (current.cancelled) return;
    if (current.uploadError) throw current.uploadError;
    if (!current.produced || current.acknowledged !== current.produced) throw new Error("录音为空或批次尚未全部确认。");
    if (!Array.isArray(message.playbackEvents) || !message.playbackEvents.length || message.playbackEvents.length > 1000 ||
        message.playbackEvents.some((item) => !["playing", "waiting"].includes(item.type) || !Number.isFinite(item.at_ms) || !Number.isFinite(item.video_sec))) {
      throw new Error("最终播放时间观测无效。");
    }
    current.playbackEvents = message.playbackEvents;
    const manifest = await jsonRequest(current, `/captures/${current.sessionId}/complete`, {
      expected_batches: current.produced, recording_stopped_at_ms: current.recordingStoppedAtMs,
      playback_end_video_sec: message.playbackEndVideoSec, playback_events: current.playbackEvents,
    });
    if (manifest.id !== current.sessionId || manifest.status !== "complete" || manifest.expected_batches !== current.produced || manifest.audio_size_bytes !== current.totalBytes) throw new Error("最终完整性确认无效。");
    current.phase = "complete";
    progress(current);
    // Release session before notifying worker, which closes this offscreen document.
    session = undefined;
    await send(current, "capture-complete", { manifest }).catch(() => {});
  } catch (error) { await abort(current, error.message, true); }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.target !== "offscreen" || sender.id !== chrome.runtime.id || sender.tab || (sender.url && sender.url !== chrome.runtime.getURL("service-worker.js"))) return;
  let action;
  if (message.type === "capture-arm") action = arm(message);
  else if (!session || message.sessionId !== session.sessionId) action = Promise.reject(new Error("采集会话已失效。"));
  else if (message.type === "capture-inspect") action = Promise.resolve({
    ok: true, phase: session.phase, pendingBatches: session.queue?.length || 0, pendingBytes: session.pendingBytes || 0,
  });
  else if (message.type === "capture-start") action = start(message);
  else if (message.type === "playback-started") action = timing(message);
  else if (message.type === "playback-event") action = Promise.resolve().then(() => playbackEvent(message));
  else if (message.type === "capture-finish") { void finish(message); sendResponse({ ok: true }); return; }
  else if (message.type === "capture-fail") action = abort(session, message.error || "采集失败", true, message).then(() => ({ ok: true }));
  else if (message.type === "capture-abort") action = abort(session, "用户中断采集").then((abortConfirmed) => ({ ok: true, abortConfirmed }));
  else return;
  Promise.resolve(action).then(sendResponse).catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
});
