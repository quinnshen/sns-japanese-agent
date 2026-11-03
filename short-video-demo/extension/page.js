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
  title.textContent = "日语视频转写";
  const recapture = document.createElement("button");
  recapture.className = "close";
  recapture.type = "button";
  recapture.textContent = "↻";
  recapture.title = "重新录制并转写当前视频";
  recapture.addEventListener("click", () => {
    void chrome.runtime.sendMessage({ target: "worker", type: "recapture" });
  });
  const close = document.createElement("button");
  close.className = "close";
  close.type = "button";
  close.textContent = "×";
  close.title = "关闭面板";
  close.addEventListener("click", () => { host.style.display = "none"; subtitle.textContent = ""; });
  head.append(title, recapture, close);
  const status = document.createElement("div");
  status.className = "status";
  const list = document.createElement("div");
  list.className = "list";
  panel.append(head, status, list);
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

  function findVideo() {
    return document.querySelector("video.html5-main-video") || document.querySelector("video");
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

  async function prepare(id) {
    if (recording) throw new Error("已有录制正在进行。 ");
    video = findVideo();
    if (!video) throw new Error("页面没有找到视频播放器。请先开始播放视频。");
    if (!Number.isFinite(video.duration) || video.duration <= 0 || video.duration > 1800) {
      throw new Error("当前只支持时长不超过 30 分钟、已加载完成的视频。");
    }
    sessionId = id;
    sentences = [];
    list.replaceChildren();
    subtitle.textContent = "";
    video.pause();
    video.playbackRate = 1;
    originalLoop = video.loop;
    video.loop = false;
    if (video.currentTime > 0.05) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => { video.removeEventListener("seeked", done); reject(new Error("视频跳转到开头超时。")); }, 5000);
        function done() { clearTimeout(timer); resolve(); }
        video.addEventListener("seeked", done, { once: true });
        video.currentTime = 0;
      });
    }
    showStatus(`准备录制 ${formatTime(video.duration)} 的完整音频…`);
    return { duration: video.duration, url: location.href };
  }

  async function play(id) {
    if (id !== sessionId || !video) throw new Error("录制会话已经失效。 ");
    if (recording) throw new Error("视频已经开始录制。 ");
    recording = true;
    playbackStarted = false;
    playbackStartedAt = 0;
    let stallTimer;
    let lastPlaybackTime = 0;
    const fail = (message) => {
      if (!recording) return;
      recording = false;
      cleanupPlayback();
      video.loop = originalLoop;
      report("page-error", { error: message });
    };
    const onPlaying = () => {
      clearTimeout(stallTimer);
      if (!playbackStarted) {
        playbackStarted = true;
        playbackStartedAt = Date.now();
        report("page-started", { startedAt: playbackStartedAt });
      }
    };
    const onWaiting = () => {
      clearTimeout(stallTimer);
      stallTimer = setTimeout(() => fail("视频缓冲超过 4 秒，时间戳可能不准。请重试。"), 4000);
    };
    const onPause = () => { if (!video.ended) fail("播放被暂停。请重试并保持视频连续播放。"); };
    const complete = () => {
      if (!recording) return;
      recording = false;
      cleanupPlayback();
      video.loop = originalLoop;
      report("page-ended", { startedAt: playbackStartedAt });
    };
    const onTimeUpdate = () => { lastPlaybackTime = Math.max(lastPlaybackTime, video.currentTime); };
    const onSeeking = () => {
      if (lastPlaybackTime >= video.duration - 0.75 && video.currentTime < 1) complete();
      else fail("播放进度被拖动。请重试并保持视频连续播放。");
    };
    const onRate = () => { if (video.playbackRate !== 1) fail("播放速度已改变。请保持 1 倍速。"); };
    const onEnded = complete;
    video.addEventListener("playing", onPlaying);
    video.addEventListener("waiting", onWaiting);
    video.addEventListener("pause", onPause);
    video.addEventListener("seeking", onSeeking);
    video.addEventListener("timeupdate", onTimeUpdate);
    video.addEventListener("ratechange", onRate);
    video.addEventListener("ended", onEnded);
    cleanupPlayback = () => {
      clearTimeout(stallTimer);
      video.removeEventListener("playing", onPlaying);
      video.removeEventListener("waiting", onWaiting);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("seeking", onSeeking);
      video.removeEventListener("timeupdate", onTimeUpdate);
      video.removeEventListener("ratechange", onRate);
      video.removeEventListener("ended", onEnded);
    };
    try { await video.play(); } catch (error) { fail(`无法播放视频：${error.message}`); }
    return { ok: true };
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

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "status") {
      showStatus(message.text, message.error);
      sendResponse({ ok: true });
    } else if (message.type === "result") {
      renderResult(message.result);
      sendResponse({ ok: true });
    } else if (message.type === "prepare" || message.type === "play") {
      const action = message.type === "prepare" ? prepare(message.sessionId) : play(message.sessionId);
      action.then((value) => sendResponse({ ok: true, ...value }))
        .catch((error) => sendResponse({ ok: false, error: error.message }));
      return true;
    }
  });
})();
