const API = "http://127.0.0.1:8766";
let session;

function send(type, data = {}) {
  if (!session) return Promise.resolve();
  return chrome.runtime.sendMessage({ target: "worker", type, sessionId: session.sessionId, ...data });
}

async function dispose() {
  if (!session) return;
  clearTimeout(session.watchdog);
  if (session.recorder?.state === "recording") session.recorder.stop();
  session.stream?.getTracks().forEach((track) => track.stop());
  await session.context?.close().catch(() => {});
  session = undefined;
}

async function fail(error) {
  await send("capture-error", { error: error.message || String(error) }).catch(() => {});
  await dispose();
}

async function start(message) {
  if (session) throw new Error("已有录制正在进行。");
  session = { ...message };
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: message.streamId } },
    video: false,
  });
  session.stream = stream;
  const context = new AudioContext();
  session.context = context;
  context.createMediaStreamSource(stream).connect(context.destination);
  await context.resume();
  const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus" : "audio/webm";
  session.mimeType = mimeType;
  session.parts = [];
  const recorder = new MediaRecorder(stream, { mimeType });
  session.recorder = recorder;
  recorder.ondataavailable = (event) => { if (event.data.size) session?.parts.push(event.data); };
  recorder.onerror = (event) => { void fail(event.error || new Error("音频录制失败")); };
  recorder.start(60_000);
  session.recordingStartedAt = Date.now();
  session.watchdog = setTimeout(() => { void fail(new Error("录制超时（30 分钟）。")); }, 31 * 60_000);
  await send("capture-ready");
}

async function finish(message) {
  if (!session || message.sessionId !== session.sessionId) return;
  session.playbackStartedAt ||= message.startedAt;
  if (!session.playbackStartedAt) throw new Error("未收到视频开始播放的时间。 ");
  const current = session;
  clearTimeout(current.watchdog);
  const stopped = new Promise((resolve, reject) => {
    current.recorder.onstop = resolve;
    current.recorder.onerror = (event) => reject(event.error || new Error("音频录制失败"));
  });
  current.recorder.stop();
  await stopped;
  current.stream.getTracks().forEach((track) => track.stop());
  await current.context.close();
  current.stream = undefined;
  current.context = undefined;
  const audio = new Blob(current.parts, { type: current.mimeType });
  current.parts = [];
  if (!audio.size) throw new Error("录音为空。 ");
  const offset = Math.max(0, (current.playbackStartedAt - current.recordingStartedAt) / 1000);
  await send("capture-progress", { text: `已录制 ${Math.ceil(audio.size / 1024)} KB，正在上传到本机…` });
  const response = await fetch(`${API}/jobs`, {
    method: "POST",
    headers: {
      "Content-Type": current.mimeType,
      "X-Japanese-Agent": "full-video-v1",
      "X-Video-Duration-Sec": String(current.duration),
      "X-Capture-Offset-Sec": String(offset),
      "X-Source-Url": current.url,
    },
    body: audio,
  });
  const submitted = await response.json();
  if (!response.ok) throw new Error(submitted.error || `本机服务返回 HTTP ${response.status}`);
  await send("capture-progress", { text: "音频已交给本机服务，正在分段请求 Google 转写…" });
  while (session === current) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const poll = await fetch(`${API}/jobs/${submitted.id}`);
    const job = await poll.json();
    if (!poll.ok) throw new Error(job.error || `查询任务失败：HTTP ${poll.status}`);
    if (job.status === "complete") {
      await send("capture-complete", { result: job.result });
      await dispose();
      return;
    }
    if (job.status === "error") throw new Error(job.error || "转写失败");
    if (job.status === "enriching") {
      const progress = job.enrichment_batches_total
        ? `${job.enrichment_batches_done}/${job.enrichment_batches_total} 批` : "准备中";
      await send("capture-progress", { text: `翻译与解析预处理中：${progress}` });
      continue;
    }
    if (job.chunks_total) {
      await send("capture-progress", { text: `Google 日语转写中：${job.chunks_done}/${job.chunks_total} 段` });
    }
  }
}

chrome.runtime.onMessage.addListener((message) => {
  if (message.target !== "offscreen") return;
  if (message.type === "capture-start") {
    void start(message).catch(fail);
  } else if (message.type === "playback-started" && session?.sessionId === message.sessionId) {
    session.playbackStartedAt = message.startedAt;
  } else if (message.type === "capture-finish") {
    void finish(message).catch(fail);
  } else if (message.type === "capture-abort" && session?.sessionId === message.sessionId) {
    void dispose();
  }
});
