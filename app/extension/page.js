(() => {
  if (window.__snsJapaneseAgentPage) return;
  window.__snsJapaneseAgentPage = true;

  let video;
  let sessionId;
  let recording = false;
  let playbackStarted = false;
  let playbackStartedAt = 0;
  let cleanupPlayback = () => {};
  let sentences = [];
  let rows = [];
  let activeId = "";
  let originalLoop = false;
  let rangeStart = 0;
  let rangeEnd = 0;
  let sourceIdentity = "";
  let playbackEvents = [];
  let configured = false;
  let busy = false;
  let preparationVersion = 0;
  let cleanupPreparation = () => {};

  const host = document.createElement("div");
  host.id = "sns-japanese-agent-root";
  const shadow = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = `
    :host { all: initial; }
    .panel { position: fixed; top: 16px; right: 16px; z-index: 2147483647;
      width: min(390px, calc(100vw - 32px)); max-height: min(78vh, 800px);
      display: flex; flex-direction: column; background: #f8faf8; color: #21382d;
      border: 1px solid #d8e3da; border-radius: 14px; box-shadow: 0 12px 36px #0005;
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, sans-serif; }
    .head { padding: 13px 15px; border-bottom: 1px solid #dbe7df; display: flex; gap: 10px; align-items: start; }
    .title { font-size: 15px; font-weight: 700; flex: 1; }
    .close { cursor: pointer; border: 0; background: transparent; font-size: 18px; color: #647a6d; }
    button:disabled { opacity: .5; cursor: default !important; }
    .status { padding: 10px 15px; color: #4a6554; white-space: pre-wrap; }
    .status.error { color: #b42318; }
    .list { overflow: auto; padding: 0 8px 8px; }
    .row { display: block; width: 100%; text-align: left; padding: 10px 9px; margin: 3px 0;
      border: 0; border-radius: 8px; background: transparent; color: #21382d;
      cursor: pointer; font: inherit; }
    .row:hover, .row.active { background: #e6f1e8; }
    .time { display: block; color: #798f80; font-size: 12px; margin-bottom: 4px; }
    .translation { display: block; color: #60786a; font-size: 13px; margin-top: 4px; }
    .entry { border-radius: 8px; margin: 3px 0; }
    .entry.active { background: #e6f1e8; }
    .entry .row { margin: 0; }
    .analyze { margin: 0 9px 9px; padding: 3px 9px; border: 1px solid #bdcec1; border-radius: 6px;
      background: #fff; color: #355944; cursor: pointer; font: inherit; font-size: 12px; }
    .study { padding: 10px 12px 13px; border-top: 1px solid #d9e5da; font-size: 13px; white-space: pre-wrap; }
    .study[hidden] { display: none; }
    .study p { margin: 4px 0; }
    .study ul { margin: 5px 0 9px; padding-left: 19px; }
    .study li { margin: 3px 0; }
    .subtitle { position: fixed; left: 50%; bottom: 7vh; transform: translateX(-50%);
      z-index: 2147483646; max-width: min(80vw, 900px); padding: 8px 14px;
      border-radius: 8px; background: #111c19d9; color: white; text-align: center;
      font: 20px/1.45 -apple-system, BlinkMacSystemFont, sans-serif;
      white-space: pre-line; pointer-events: none; }
    .subtitle:empty { display: none; }
  `;
  shadow.append(style);
  const panel = document.createElement("section");
  panel.className = "panel";
  const head = document.createElement("div");
  head.className = "head";
  const title = document.createElement("div");
  title.className = "title";
  title.textContent = "日语视频 · 音频采集";
  const close = document.createElement("button");
  close.className = "close";
  close.type = "button";
  close.textContent = "×";
  close.title = "关闭面板";
  close.addEventListener("click", () => {
    if (configured || busy || recording) report("capture-cancel");
    host.style.display = "none";
    subtitle.textContent = "";
  });
  head.append(title, close);
  const status = document.createElement("div");
  status.className = "status";
  const list = document.createElement("div");
  list.className = "list";
  const controls = document.createElement("div");
  controls.style.cssText = "padding:0 15px 12px;display:grid;gap:8px";
  function field(labelText, value) {
    const label = document.createElement("label");
    label.textContent = labelText;
    label.style.cssText = "display:grid;gap:3px";
    const input = document.createElement("input");
    input.type = "text";
    input.value = value;
    input.style.cssText = "padding:6px;border:1px solid #bdcec1;border-radius:5px;font:inherit;color:#21382d;background:white";
    label.append(input);
    controls.append(label);
    return input;
  }
  const startInput = field("起点（秒、分:秒或时:分:秒）", "0:00");
  const endInput = field("终点（默认视频结尾）", "");
  const intervalInput = field("上传间隔（1–30 秒）", "5");
  intervalInput.inputMode = "decimal";
  const buttons = document.createElement("div");
  buttons.style.cssText = "display:flex;gap:8px";
  const begin = document.createElement("button");
  begin.type = "button";
  begin.textContent = "开始采集";
  begin.disabled = true;
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "中断／释放音频";
  cancel.disabled = true;
  for (const button of [begin, cancel]) button.style.cssText = "padding:6px 10px;border:1px solid #bdcec1;border-radius:6px;background:white;color:#355944;cursor:pointer;font:inherit";
  buttons.append(begin, cancel);
  controls.append(buttons);
  const metrics = document.createElement("div");
  metrics.style.cssText = "font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere";
  const replay = document.createElement("div");
  replay.style.cssText = "display:grid;gap:8px";
  controls.append(metrics, replay);
  panel.append(head, status, controls, list);
  begin.addEventListener("click", () => {
    if (busy || !configured) return;
    try {
      const rangeStart = parseTime(startInput.value);
      const rangeEnd = parseTime(endInput.value);
      const batchIntervalMs = Number(intervalInput.value) * 1000;
      if (!Number.isInteger(batchIntervalMs) || batchIntervalMs < 1000 || batchIntervalMs > 30000) throw new Error("上传间隔应为 1–30 秒。");
      validateRange(rangeStart, rangeEnd, video?.duration);
      busy = true;
      begin.disabled = true;
      setInputsDisabled(true);
      showStatus("正在跳转到所选起点，准备采集…");
      void chrome.runtime.sendMessage({ target: "worker", type: "capture-begin", sessionId, rangeStart, rangeEnd, batchIntervalMs })
        .then((reply) => { if (!reply?.ok) throw new Error(reply?.error || "启动失败"); })
        .catch((error) => {
          busy = false;
          configured = false;
          begin.disabled = true;
          cancel.disabled = false;
          setInputsDisabled(false);
          showStatus(`启动失败：${error.message}。请先点“中断／释放音频”，再点击扩展图标重试。`, true);
        });
    } catch (error) { showStatus(error.message, true); }
  });
  cancel.addEventListener("click", () => {
    cancelPreparation();
    cancel.disabled = true;
    report("capture-cancel");
  });
  const subtitle = document.createElement("div");
  subtitle.className = "subtitle";
  shadow.append(panel, subtitle);
  document.documentElement.append(host);

  function showStatus(text, error = false) {
    host.style.display = "";
    status.textContent = text;
    status.classList.toggle("error", error);
  }

  function report(type, data = {}) {
    void chrome.runtime.sendMessage({ target: "worker", type, sessionId, ...data }).catch(() => {});
  }

  function mainVideoWithin(scope) {
    return scope?.querySelector("video.html5-main-video") || scope?.querySelector("video") || null;
  }

  function findVideo() {
    // Choose the actual player, never whichever preview currently occupies the largest area.
    if (location.pathname?.startsWith("/shorts/") || new URL(location.href).pathname.startsWith("/shorts/")) {
      const active = document.querySelector("ytd-reel-video-renderer[is-active]");
      if (active) return mainVideoWithin(active);
    }
    const main = document.querySelector("#movie_player") || document.querySelector("ytd-watch-flexy #player");
    return mainVideoWithin(main) || document.querySelector("video.html5-main-video") || document.querySelector("video");
  }

  function playerFor(currentVideo) {
    return currentVideo?.closest(".html5-video-player") || currentVideo?.closest("#movie_player") || null;
  }

  function playerShowsAd(currentVideo, currentPlayer = playerFor(currentVideo)) {
    // An unrelated hover preview or advertising player must not affect this capture.
    return Boolean(currentPlayer?.classList.contains("ad-showing"));
  }

  function videoWasReplaced(currentVideo, currentPlayer) {
    if (!currentVideo.isConnected) return true;
    if (currentPlayer) {
      if (!currentPlayer.isConnected || !currentPlayer.contains(currentVideo)) return true;
      const selected = mainVideoWithin(currentPlayer);
      if (selected && selected !== currentVideo) return true;
      // Detect replacement of the watch-page player without selecting unrelated video elements.
      if (currentPlayer.id === "movie_player" && document.querySelector("#movie_player") !== currentPlayer) return true;
    }
    return false;
  }

  function identity() {
    const url = new URL(location.href);
    return url.pathname === "/watch" ? url.searchParams.get("v") : url.pathname.split("/")[2];
  }

  function parseTime(value) {
    const text = value.trim();
    if (!/^(?:\d+:){0,2}\d+(?:\.\d+)?$/.test(text)) throw new Error("时间格式应为秒、分:秒或时:分:秒。");
    const pieces = text.split(":").map(Number);
    if (pieces.slice(1).some((part) => part >= 60)) throw new Error("冒号后的分、秒应小于 60。");
    const seconds = pieces.reduce((total, part) => total * 60 + part, 0);
    if (!Number.isFinite(seconds)) throw new Error("时间数值无效。");
    return seconds;
  }

  function validateRange(start, end, duration) {
    if (!Number.isFinite(duration) || duration <= 0) throw new Error("请先加载一个非直播视频。");
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end - start < 1 || end > duration) throw new Error("请选择视频时长内有效的起止时间。");
    if (end - start > 1800) throw new Error("本阶段每次采集范围不超过 30 分钟；长视频可以选择其中一段。");
  }

  function setInputsDisabled(disabled) {
    for (const input of [startInput, endInput, intervalInput]) input.disabled = disabled;
  }

  function cancelPreparation() {
    preparationVersion++;
    cleanupPreparation();
    cleanupPreparation = () => {};
  }

  function configuring(message) {
    cancelPreparation();
    cleanupPlayback();
    sessionId = message.sessionId;
    sourceIdentity = identity();
    configured = false;
    busy = false;
    recording = false;
    begin.disabled = true;
    cancel.disabled = false;
    setInputsDisabled(true);
    metrics.textContent = "";
    replay.replaceChildren();
    host.style.display = "";
    return { ok: true };
  }

  function waitForMetadata(currentVideo, expectedSession, version) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        clearInterval(poll);
        for (const event of events) currentVideo.removeEventListener(event, check);
        cleanupPreparation = () => {};
        if (error) reject(error); else resolve();
      };
      const check = () => {
        if (sessionId !== expectedSession || preparationVersion !== version) return finish(new Error("视频准备已取消。"));
        if (identity() !== sourceIdentity || findVideo() !== currentVideo) return finish(new Error("当前视频已切换，请重新点击扩展图标。"));
        if (currentVideo.error) return finish(new Error("视频加载失败。请确认 YouTube 视频能正常播放后重试。"));
        if (currentVideo.duration === Infinity) return finish(new Error("暂不支持直播，请选择已结束的视频。"));
        if (Number.isFinite(currentVideo.duration) && currentVideo.duration > 0) finish();
      };
      const events = ["loadedmetadata", "durationchange", "error", "emptied"];
      const timer = setTimeout(() => finish(new Error("视频信息仍未加载。请先确认视频能正常播放，再点击扩展图标重试。")), 8000);
      const poll = setInterval(check, 200);
      for (const event of events) currentVideo.addEventListener(event, check);
      cleanupPreparation = () => finish(new Error("视频准备已取消。"));
      check();
    });
  }

  async function configure(message) {
    if (message.sessionId !== sessionId) configuring(message);
    cancelPreparation();
    const version = preparationVersion;
    video = findVideo();
    if (!video) throw new Error("未找到视频播放器。请先打开并正常播放视频，再重试。");
    showStatus("正在读取视频信息；播放器转圈时请等待加载…");
    await waitForMetadata(video, message.sessionId, version);
    if (message.sessionId !== sessionId || preparationVersion !== version) throw new Error("视频准备已取消。");
    originalLoop = video.loop;
    configured = true;
    busy = false;
    recording = false;
    startInput.value = "0:00";
    endInput.value = String(video.duration);
    intervalInput.value = "5";
    begin.disabled = false;
    cancel.disabled = false;
    setInputsDisabled(false);
    metrics.textContent = "";
    replay.replaceChildren();
    sentences = [];
    list.replaceChildren();
    subtitle.textContent = "";
    host.style.display = "";
    return { ok: true };
  }

  function formatTime(seconds) {
    const whole = Math.floor(seconds);
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
  }

  function formatPreciseTime(seconds) {
    const tenths = Math.round(seconds * 10);
    const whole = Math.floor(tenths / 10);
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}.${tenths % 10}`;
  }

  async function prepare(message) {
    if (recording || message.sessionId !== sessionId) throw new Error("录制会话无效。");
    if (identity() !== sourceIdentity || findVideo() !== video) throw new Error("当前视频已切换，请重新点击扩展图标。");
    if (playerShowsAd(video)) throw new Error("请等待当前播放器的广告结束后再开始采集。");
    validateRange(message.rangeStart, message.rangeEnd, video.duration);
    rangeStart = message.rangeStart;
    rangeEnd = message.rangeEnd;
    video.pause();
    video.playbackRate = 1;
    originalLoop = video.loop;
    video.loop = false;
    if (Math.abs(video.currentTime - rangeStart) > 0.025) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => { video.removeEventListener("seeked", done); reject(new Error("跳转到采集起点超时。")); }, 8000);
        function done() { clearTimeout(timer); resolve(); }
        video.addEventListener("seeked", done, { once: true });
        video.currentTime = rangeStart;
      });
    }
    showStatus(`正在采集 ${formatPreciseTime(rangeStart)}–${formatPreciseTime(rangeEnd)}；请保持正常播放。`);
    return { duration: video.duration, url: location.href, rangeStart, rangeEnd, batchIntervalMs: message.batchIntervalMs };
  }

  async function play(id) {
    if (id !== sessionId || !video || recording) throw new Error("录制会话已失效。");
    const currentVideo = video;
    const currentPlayer = playerFor(currentVideo);
    const observations = () => ({
      video_sec: Number.isFinite(currentVideo.currentTime) && currentVideo.currentTime >= 0 ? currentVideo.currentTime : 0,
      video_connected: Boolean(currentVideo.isConnected),
      player_connected: Boolean(currentPlayer?.isConnected),
      ad_showing: playerShowsAd(currentVideo, currentPlayer),
      identity_changed: identity() !== sourceIdentity,
      video_replaced: videoWasReplaced(currentVideo, currentPlayer),
    });
    recording = true;
    playbackStarted = false;
    playbackStartedAt = 0;
    playbackEvents = [];
    let stallTimer;
    let startTimer;
    let poll;
    let resolvePlaying;
    let rejectPlaying;
    const firstPlaying = new Promise((resolve, reject) => { resolvePlaying = resolve; rejectPlaying = reject; });
    let lastPlaybackTime = rangeStart;
    const observe = (type) => {
      const event = { type, at_ms: Date.now(), video_sec: currentVideo.currentTime };
      playbackEvents.push(event);
      if (playbackEvents.length > 1000) { fail("播放等待过于频繁。请重试。", "playback_events_limit"); return; }
      report("playback-event", { event });
    };
    const fail = (message, code = "playback_error", observed = observations()) => {
      if (!recording) return;
      if (observed.identity_changed) {
        code = "video_identity_changed";
        message = "视频页面已切换，采集已停止。";
      } else if (observed.ad_showing) {
        code = "advertisement_started";
        message = "当前播放器开始播放广告，采集已停止。";
      } else if (observed.video_replaced) {
        code = "video_replaced";
        message = "采集的视频元素已被移除或主播放器已替换，采集已停止。";
      }
      const failure = { code, message, at_ms: Date.now(), ...observed };
      recording = false;
      rejectPlaying(new Error(message));
      cleanupPlayback();
      currentVideo.pause();
      currentVideo.loop = originalLoop;
      report("page-error", {
        error: message, failure, playbackEndVideoSec: observed.video_sec,
        playbackEvents: playbackEvents.slice(0, 1000),
      });
    };
    const checkPlayer = () => {
      const observed = observations();
      if (observed.identity_changed || observed.ad_showing || observed.video_replaced) {
        fail("当前视频状态已改变。", "playback_error", observed);
        return false;
      }
      return true;
    };
    const complete = (observedEnd = currentVideo.currentTime) => {
      if (!recording || !checkPlayer()) return;
      recording = false;
      const playbackEndVideoSec = observedEnd;
      cleanupPlayback();
      currentVideo.pause();
      currentVideo.loop = originalLoop;
      showStatus("采集范围已结束，正在等待最后批次保存…");
      report("page-ended", { playbackEndVideoSec, playbackEvents });
    };
    const onPlaying = () => {
      if (!checkPlayer()) return;
      clearTimeout(startTimer);
      resolvePlaying();
      clearTimeout(stallTimer);
      if (!playbackStarted) {
        playbackStarted = true;
        playbackStartedAt = Date.now();
        playbackEvents.push({ type: "playing", at_ms: playbackStartedAt, video_sec: currentVideo.currentTime });
        report("page-started", { playbackStartedAtMs: playbackStartedAt, playbackStartVideoSec: currentVideo.currentTime });
      } else observe("playing");
    };
    const onWaiting = () => {
      clearTimeout(stallTimer);
      if (playbackStarted) observe("waiting");
      stallTimer = setTimeout(() => fail("视频缓冲超过 4 秒，采集已中断。", "buffer_timeout"), 4000);
    };
    const onPause = () => {
      if (currentVideo.ended || currentVideo.currentTime >= rangeEnd - 0.025) complete();
      else fail("播放被暂停。请重新采集并保持连续播放。", "user_paused");
    };
    const onTimeUpdate = () => {
      if (!checkPlayer()) return;
      lastPlaybackTime = Math.max(lastPlaybackTime, currentVideo.currentTime);
      if (currentVideo.currentTime >= rangeEnd) complete();
    };
    const onSeeking = () => {
      if (!checkPlayer()) return;
      if (lastPlaybackTime >= rangeEnd - 0.5 && rangeEnd >= currentVideo.duration - 0.1 && currentVideo.currentTime < 1) complete(lastPlaybackTime);
      else fail("播放进度被拖动。请重新采集并保持连续播放。", "user_seeked");
    };
    const onRate = () => { if (currentVideo.playbackRate !== 1) fail("播放速度已改变。请保持 1 倍速。", "playback_rate_changed"); };
    const onEnded = () => {
      if (currentVideo.currentTime >= rangeEnd - 0.5) complete();
      else fail("视频在所选终点之前结束。", "video_ended_early");
    };
    const handlers = { playing: onPlaying, waiting: onWaiting, pause: onPause, seeking: onSeeking, timeupdate: onTimeUpdate, ratechange: onRate, ended: onEnded };
    for (const [event, handler] of Object.entries(handlers)) currentVideo.addEventListener(event, handler);
    poll = setInterval(() => {
      const observed = observations();
      if (observed.identity_changed) {
        fail("视频页面已切换，采集已停止。", "video_identity_changed", observed);
      } else if (observed.ad_showing) {
        fail("当前播放器开始播放广告，采集已停止。", "advertisement_started", observed);
      } else if (observed.video_replaced) {
        fail("采集的视频元素已被移除或主播放器已替换，采集已停止。", "video_replaced", observed);
      } else onTimeUpdate();
    }, 50);
    cleanupPlayback = () => {
      rejectPlaying(new Error("播放准备已取消。"));
      clearTimeout(startTimer);
      clearTimeout(stallTimer);
      clearInterval(poll);
      for (const [event, handler] of Object.entries(handlers)) currentVideo.removeEventListener(event, handler);
    };
    startTimer = setTimeout(() => fail("视频在 8 秒内未开始播放。请确认视频能正常播放后重试。", "playback_start_timeout"), 8000);
    try {
      const requested = Promise.resolve().then(() => currentVideo.play());
      // A delayed play resolution must not resume a cancelled capture.
      void requested.then(() => { if (sessionId === id && !recording) currentVideo.pause(); }, () => {});
      await Promise.all([requested, firstPlaying]);
      if (id !== sessionId || !recording) throw new Error("播放准备已取消或超时。");
    } catch (error) { if (id === sessionId) fail("无法启动当前视频播放。请确认视频能正常播放后重试。", "playback_start_failed"); throw error; }
    return { ok: true };
  }

  function stopPlayback() {
    cleanupPlayback();
    if (recording || busy) video?.pause();
    if (video) video.loop = originalLoop;
    recording = false;
  }

  function showProgress(value) {
    const kib = (bytes) => `${(bytes / 1024).toFixed(1)} KB`;
    metrics.textContent = `任务 ${value.id}\n已产生 ${value.produced} 批 · 已确认 ${value.acknowledged} 批\n待确认 ${kib(value.pending_bytes)} · 峰值 ${kib(value.peak_pending_bytes)}\n已采集 ${formatPreciseTime(value.elapsed_sec)} · 累计 ${kib(value.total_bytes)}` +
      (value.waiting_events ? `\n记录到 ${value.waiting_events} 次缓冲等待，后续对齐须参考播放观测。` : "");
  }

  function showSaved(manifest) {
    replay.replaceChildren();
    const audioUrl = new URL(manifest.audio_url, "http://127.0.0.1:8766");
    if (audioUrl.origin !== "http://127.0.0.1:8766" || !/^\/captures\/[0-9a-f-]+\/audio$/.test(audioUrl.pathname)) throw new Error("本机回放地址无效。");
    const label = document.createElement("div");
    label.textContent = `本机回放 · ${manifest.expected_batches} 批 · ${(manifest.audio_size_bytes / 1024).toFixed(1)} KB`;
    const open = document.createElement("button");
    open.type = "button";
    open.textContent = "打开私有录音回放";
    open.addEventListener("click", () => {
      void chrome.runtime.sendMessage({ target: "worker", type: "capture-replay", captureId: manifest.id })
        .then((reply) => { if (!reply?.ok) throw new Error(reply?.error || "无法打开回放"); })
        .catch((error) => showStatus(error.message, true));
    });
    const note = document.createElement("div");
    note.textContent = "可反复回放。要采集另一段，请重新点击扩展图标。";
    replay.append(label, open, note);
  }

  function updateSubtitle() {
    if (!video || !sentences.length) return;
    const time = video.currentTime;
    const active = sentences.find((item) => item.start <= time && time <= item.end + 0.25);
    if (active?.id === activeId) return;
    activeId = active?.id || "";
    subtitle.textContent = active ? [active.study?.reading_text || active.text, active.translation_zh].filter(Boolean).join("\n") : "";
    rows.forEach((row) => row.classList.toggle("active", row.dataset.id === activeId));
  }

  function renderResult(result) {
    video?.removeEventListener("timeupdate", updateSubtitle);
    video = findVideo();
    sentences = result.sentences || [];
    rows = [];
    list.replaceChildren();
    for (const sentence of sentences) {
      const entry = document.createElement("div");
      entry.className = "entry";
      entry.dataset.id = sentence.id;
      const row = document.createElement("button");
      row.type = "button";
      row.className = "row";
      row.dataset.id = sentence.id;
      const time = document.createElement("span");
      time.className = "time";
      time.textContent = `${formatPreciseTime(sentence.start)}–${formatPreciseTime(sentence.end)}`;
      const text = document.createElement("span");
      text.textContent = sentence.study?.reading_text || sentence.text;
      row.append(time, text);
      if (sentence.translation_zh) {
        const translation = document.createElement("span");
        translation.className = "translation";
        translation.textContent = sentence.translation_zh;
        row.append(translation);
      }
      row.addEventListener("click", () => {
        if (!video) return;
        video.currentTime = sentence.start;
        void video.play();
        updateSubtitle();
      });
      entry.append(row);
      if (sentence.study) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "analyze";
        toggle.textContent = "解析";
        toggle.setAttribute("aria-expanded", "false");
        const detail = document.createElement("div");
        detail.className = "study";
        detail.hidden = true;
        const original = document.createElement("p");
        original.textContent = `原句：${sentence.study.reading_text || sentence.text}`;
        const meaning = document.createElement("p");
        meaning.textContent = `意思：${sentence.translation_zh || "未生成"}`;
        detail.append(original, meaning);
        if (sentence.study.key_points?.length) {
          const label = document.createElement("p");
          label.textContent = "关键拆解：";
          const points = document.createElement("ul");
          for (const point of sentence.study.key_points) {
            const item = document.createElement("li");
            item.textContent = point;
            points.append(item);
          }
          detail.append(label, points);
        }
        if (sentence.study.context_note) {
          const note = document.createElement("p");
          note.textContent = `语境待确认：${sentence.study.context_note}`;
          detail.append(note);
        }
        if (sentence.study.asr_doubt) {
          const doubt = document.createElement("p");
          doubt.textContent = `ASR 存疑：${sentence.study.asr_doubt}`;
          detail.append(doubt);
        }
        toggle.addEventListener("click", () => {
          detail.hidden = !detail.hidden;
          toggle.setAttribute("aria-expanded", String(!detail.hidden));
          toggle.textContent = detail.hidden ? "解析" : "收起解析";
        });
        entry.append(toggle, detail);
      }
      list.append(entry);
      rows.push(entry);
    }
    video?.addEventListener("timeupdate", updateSubtitle);
    activeId = "";
    updateSubtitle();
    const translated = sentences.filter((item) => item.translation_zh).length;
    const analyzed = sentences.filter((item) => item.study).length;
    showStatus(sentences.length
      ? `共 ${sentences.length} 句${translated ? `，已翻译 ${translated} 句` : ""}${analyzed ? `，已解析 ${analyzed} 句` : ""}；点击句子跳转，点击解析展开。`
      : "转写完成，但没有识别出日语语音。");
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id) return;
    if (message.type === "status") {
      showStatus(message.text, message.error);
      sendResponse({ ok: true });
    } else if (message.type === "capture-show") {
      host.style.display = "";
      sendResponse({ ok: true });
    } else if (message.type === "capture-configuring") {
      sendResponse(configuring(message));
    } else if (message.type === "capture-configure") {
      configure(message).then(sendResponse).catch((error) => {
        if (sessionId === message.sessionId) showStatus(error.message, true);
        sendResponse({ ok: false, error: error.message });
      });
      return true;
    } else if (message.type === "result") {
      renderResult(message.result);
      sendResponse({ ok: true });
    } else if (message.sessionId !== sessionId) return;
    else if (message.type === "capture-progress") {
      showProgress(message.progress);
      sendResponse({ ok: true });
    } else if (message.type === "capture-saved") {
      try { showSaved(message.manifest); sendResponse({ ok: true }); }
      catch (error) { showStatus(error.message, true); sendResponse({ ok: false, error: error.message }); }
    } else if (message.type === "capture-stop-playback") {
      stopPlayback();
      sendResponse({ ok: true });
    } else if (message.type === "capture-failed") {
      cancelPreparation();
      configured = false;
      busy = false;
      begin.disabled = true;
      cancel.disabled = false;
      setInputsDisabled(false);
      sendResponse({ ok: true });
    } else if (message.type === "capture-reset") {
      cancelPreparation();
      stopPlayback();
      configured = false;
      busy = false;
      cancel.disabled = true;
      begin.disabled = true;
      setInputsDisabled(false);
      sendResponse({ ok: true });
    } else if (message.type === "prepare" || message.type === "play") {
      const action = message.type === "prepare" ? prepare(message) : play(message.sessionId);
      action.then((value) => sendResponse({ ok: true, ...value }))
        .catch((error) => sendResponse({ ok: false, error: error.message }));
      return true;
    }
  });
})();
