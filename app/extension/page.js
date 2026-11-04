(() => {
  const PAGE_VERSION = "0.5.4";
  const PAGE_PROTOCOL = 1;
  const previous = window.__snsJapaneseAgentPage;
  if (previous) {
    if (previous === true || typeof previous !== "object") return { ok: false, reason: "legacy_controller" };
    if (previous.version !== PAGE_VERSION || previous.protocol !== PAGE_PROTOCOL) return { ok: false, reason: "controller_version_mismatch" };
    try {
      if (previous.isAlive()) return pageInfo();
    } catch (_) { /* Old injected code may belong to a dead extension context. */ }
    // Unknown old listeners cannot be safely detached; do not initialize a second controller.
    return { ok: false, reason: "dead_controller" };
  }
  let extensionId;
  try { extensionId = chrome.runtime.id; } catch (_) { /* The runtime may have been invalidated. */ }
  if (!extensionId) return { ok: false, reason: "context_invalid" };
  // Different unpacked installations have separate runtimes but share the page DOM.
  const rootId = `sns-japanese-agent-root-${extensionId}`;
  if (document.getElementById(rootId)) return { ok: false, reason: "orphan_root" };

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
  let recoveryPending = false;
  let audioReleased = false;
  let reconnecting = false;
  let reconnectEpoch = 0;
  let reconnectRequestId;
  let panelDismissed = false;
  const dismissedReconnects = new Set();
  let formSourceIdentity;
  let preparationVersion = 0;
  let cleanupPreparation = () => {};
  let transcriptionEnabled = false;
  let transcriptionTimer;
  let transcriptionInFlight = false;
  let transcriptionEpoch = 0;
  let transcriptionCaptureId;
  let contextInvalidated = false;
  let enrichmentEnabled = false;
  let learningTimer;
  let learningInFlight = false;
  let learningEpoch = 0;
  let learningCaptureId;
  let learningRevision = "";
  let learningSourceIdentity = "";
  let captureFormAutoCollapsed = false;
  let captureFormUserOpened = false;
  const openAnalyses = new Set();

  const host = document.createElement("div");
  host.id = rootId;
  const shadow = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = `
    :host { all: initial; }
    .panel { position: fixed; top: 16px; right: 16px; z-index: 2147483647;
      width: min(390px, calc(100vw - 32px)); height: min(84vh, 820px);
      height: min(84dvh, 820px); max-height: calc(100dvh - 32px); overflow: hidden;
      display: flex; flex-direction: column; background: #f8faf8; color: #21382d;
      border: 1px solid #d8e3da; border-radius: 14px; box-shadow: 0 12px 36px #0005;
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, sans-serif; }
    .head { flex: 0 0 auto; padding: 13px 15px; border-bottom: 1px solid #dbe7df; display: flex; gap: 10px; align-items: start; }
    .title { font-size: 15px; font-weight: 700; flex: 1; }
    .close { cursor: pointer; border: 0; background: transparent; font-size: 18px; color: #647a6d; }
    button:disabled { opacity: .5; cursor: default !important; }
    .status { padding: 0 0 6px; color: #4a6554; white-space: pre-wrap; }
    .status.error { color: #b42318; }
    .capture-options { flex: 0 1 auto; min-height: 36px; overflow: hidden; border-bottom: 1px solid #dbe7df; }
    .capture-options[open] { max-height: 42%; overflow-y: auto; }
    .capture-summary { position: sticky; top: 0; z-index: 1; padding: 7px 15px; background: #f8faf8; color: #4a6554; cursor: pointer; font-size: 12px; }
    .learning-toolbar { flex: 0 0 auto; padding: 7px 12px; display: grid; gap: 5px; border-bottom: 1px solid #dbe7df; }
    .learning-status { max-height: 4.5em; overflow-y: auto; }
    .alignment-note { font-size: 11px; color: #798f80; }
    .toolbar-actions { display: flex; gap: 6px; align-items: center; justify-content: space-between; }
    .toolbar-actions button { min-width: 0; padding: 4px 7px !important; font-size: 12px; }
    .list { flex: 1 1 0; min-height: 0; overflow-y: auto; padding: 0 8px 8px; }
    @media (max-height: 420px) { .head { padding: 8px 12px; } .learning-status { max-height: 2.5em; } .capture-options[open] { max-height: 35%; } }
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
  title.textContent = "日语视频 · 学习";
  const version = document.createElement("span");
  version.className = "version";
  version.style.cssText = "font-size:11px;color:#647a6d";
  version.textContent = `v${PAGE_VERSION}`;
  const close = document.createElement("button");
  close.className = "close";
  close.type = "button";
  close.textContent = "×";
  close.title = "关闭面板";
  close.addEventListener("click", () => {
    panelDismissed = true;
    if (reconnecting) {
      dismissedReconnects.add(reconnectRequestId);
      if (dismissedReconnects.size > 8) dismissedReconnects.delete(dismissedReconnects.values().next().value);
      reconnectEpoch++;
    }
    stopTranscriptionPolling();
    stopLearningPolling();
    cancelPreparation();
    suspendLearningDisplay();
    if (configured || busy || recording || (reconnecting && !audioReleased)) report("capture-cancel");
    host.style.display = "none";
    subtitle.textContent = "";
  });
  head.append(title, version, close);
  const status = document.createElement("div");
  status.className = "status";
  const list = document.createElement("div");
  list.className = "list";
  const captureForm = document.createElement("details");
  captureForm.className = "capture-options";
  captureForm.open = true;
  const captureSummary = document.createElement("summary");
  captureSummary.className = "capture-summary";
  captureSummary.textContent = "采集设置与回放";
  const rememberFormOpen = () => { if (!captureForm.open) captureFormUserOpened = true; };
  captureSummary.addEventListener("click", rememberFormOpen);
  captureSummary.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) rememberFormOpen(); });
  const controls = document.createElement("div");
  controls.style.cssText = "padding:0 15px 12px;display:grid;gap:8px";
  controls.append(status);
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
  for (const input of [startInput, endInput, intervalInput]) {
    input.addEventListener("input", () => { formSourceIdentity = identity(); });
  }
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
  buttons.append(begin);
  controls.append(buttons);
  const connectionHint = document.createElement("div");
  connectionHint.className = "connection-hint";
  connectionHint.style.cssText = "font-size:12px;color:#4a6554";
  connectionHint.setAttribute("aria-live", "polite");
  connectionHint.hidden = true;
  controls.append(connectionHint);
  const metrics = document.createElement("div");
  metrics.style.cssText = "font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere";
  const replay = document.createElement("div");
  replay.style.cssText = "display:grid;gap:8px";
  const transcriptionStatus = document.createElement("div");
  transcriptionStatus.className = "transcription-status";
  transcriptionStatus.style.cssText = "font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere";
  transcriptionStatus.hidden = true;
  const learningStatus = document.createElement("div");
  learningStatus.className = "learning-status";
  learningStatus.style.cssText = "font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere";
  learningStatus.hidden = true;
  const loadLearning = document.createElement("button");
  loadLearning.type = "button";
  loadLearning.textContent = "读取已保存学习内容";
  loadLearning.style.cssText = cancel.style.cssText;
  loadLearning.title = "仅查询本机，不会开始录音或发起云端处理";
  loadLearning.addEventListener("click", () => { if (extensionContextIsValid()) startLearningPolling(); });
  controls.append(metrics, transcriptionStatus, replay);
  captureForm.append(captureSummary, controls);
  const learningToolbar = document.createElement("div");
  learningToolbar.className = "learning-toolbar";
  const alignmentNote = document.createElement("div");
  alignmentNote.className = "alignment-note";
  alignmentNote.textContent = "字幕时间由播放记录估算。";
  alignmentNote.hidden = true;
  const toolbarActions = document.createElement("div");
  toolbarActions.className = "toolbar-actions";
  toolbarActions.append(loadLearning, cancel);
  learningToolbar.append(learningStatus, alignmentNote, toolbarActions);
  panel.append(head, captureForm, learningToolbar, list);
  begin.addEventListener("click", () => {
    if (audioReleased && !recoveryPending) { void reconnectAudio(); return; }
    if (busy || !configured || !extensionContextIsValid()) return;
    try {
      const rangeStart = parseTime(startInput.value);
      const rangeEnd = endInput.value.trim() ? parseTime(endInput.value) : video?.duration;
      const batchIntervalMs = Number(intervalInput.value) * 1000;
      if (!Number.isInteger(batchIntervalMs) || batchIntervalMs < 1000 || batchIntervalMs > 30000) throw new Error("上传间隔应为 1–30 秒。");
      validateRange(rangeStart, rangeEnd, video?.duration);
      busy = true;
      updateBeginAvailability();
      setInputsDisabled(true);
      showStatus("正在跳转到所选起点，准备采集…");
      void sendToWorker({ target: "worker", type: "capture-begin", sessionId, rangeStart, rangeEnd, batchIntervalMs })
        .then((reply) => { if (!reply?.ok) throw new Error(reply?.error || "启动失败"); })
        .catch((error) => {
          if (contextInvalidated) return;
          busy = false;
          configured = false;
          updateBeginAvailability();
          cancel.disabled = false;
          setInputsDisabled(false);
          showStatus(`启动失败：${error.message}。请先点“中断／释放音频”，再点击扩展图标重试。`, true);
        });
    } catch (error) { showStatus(error.message, true); }
  });
  cancel.addEventListener("click", () => {
    reconnectEpoch++;
    stopTranscriptionPolling();
    stopLearningPolling();
    suspendLearningDisplay();
    cancelPreparation();
    cancel.disabled = true;
    report("capture-cancel");
  });
  const subtitle = document.createElement("div");
  subtitle.className = "subtitle";
  shadow.append(panel, subtitle);
  document.documentElement.append(host);

  function pageInfo() {
    return { ok: true, version: PAGE_VERSION, protocol: PAGE_PROTOCOL };
  }

  function showStatus(text, error = false) {
    if (contextInvalidated) return;
    host.style.display = panelDismissed ? "none" : "";
    status.textContent = text;
    status.classList.toggle("error", error);
    if (error) captureForm.open = true;
  }

  function updateBeginAvailability() {
    const canReconnect = audioReleased && !recoveryPending && !busy && !recording;
    begin.textContent = reconnecting ? "正在连接音频…" : canReconnect ? "重新连接音频" : "开始采集";
    begin.disabled = contextInvalidated || reconnecting || recoveryPending || busy || recording || (!configured && !canReconnect);
    connectionHint.textContent = reconnecting ? "正在连接音频，尚未开始录音。" : canReconnect
      ? "音频已释放。请先重新连接音频，连接成功后再开始采集。" : recoveryPending
      ? "请先点击「中断／释放音频」，再连接新的采集任务。" : "";
    connectionHint.hidden = !connectionHint.textContent;
  }

  async function reconnectAudio() {
    if (reconnecting || busy || recording || recoveryPending || !audioReleased || !extensionContextIsValid()) return;
    const epoch = ++reconnectEpoch;
    reconnectRequestId = epoch;
    reconnecting = true;
    updateBeginAvailability();
    setInputsDisabled(true);
    showStatus("正在重新连接音频；时间范围会保留，连接后请点击开始采集。");
    try {
      const reply = await sendToWorker({ target: "worker", type: "capture-rearm", requestId: epoch });
      if (contextInvalidated || epoch !== reconnectEpoch) return;
      if (!reply?.ok) throw new Error(reply?.error || "音频未重新连接，请点击浏览器工具栏 S 重新连接音频。");
      if (!configured || sessionId !== reply.sessionId) throw new Error("音频尚未就绪，请点击浏览器工具栏 S 重新连接音频。");
    } catch (error) {
      if (!contextInvalidated && epoch === reconnectEpoch) showStatus(error.message, true);
    } finally {
      reconnecting = false;
      if (!contextInvalidated) {
        updateBeginAvailability();
        if (audioReleased && !recoveryPending) setInputsDisabled(false);
      }
    }
  }

  function invalidateContext() {
    if (contextInvalidated) return;
    contextInvalidated = true;
    reconnectEpoch++;
    stopTranscriptionPolling();
    stopLearningPolling();
    cancelPreparation();
    stopPlayback();
    video?.removeEventListener("timeupdate", updateSubtitle);
    configured = false;
    busy = false;
    transcriptionEnabled = false;
    enrichmentEnabled = false;
    begin.disabled = true;
    cancel.disabled = true;
    setInputsDisabled(true);
    transcriptionStatus.hidden = true;
    learningStatus.hidden = true;
    alignmentNote.hidden = true;
    loadLearning.disabled = true;
    replay.replaceChildren();
    list.replaceChildren();
    subtitle.textContent = "";
    host.style.display = "";
    status.textContent = "扩展已更新或停用。请刷新当前 YouTube 页面，再点击 SNS 扩展图标。本机已确认保存的数据仍保留；正在采集的任务请检查本机状态。";
    status.classList.toggle("error", true);
    captureForm.open = true;
  }

  function extensionContextIsValid() {
    if (contextInvalidated) return false;
    try { if (chrome.runtime?.id) return true; } catch (_) { /* The old runtime may already be gone. */ }
    invalidateContext();
    return false;
  }

  async function sendToWorker(message) {
    if (!extensionContextIsValid()) throw new Error("Extension context invalidated.");
    try {
      // An invalidated runtime can throw before returning a Promise.
      return await chrome.runtime.sendMessage(message);
    } catch (error) {
      if (/extension context invalidated/i.test(error?.message || "")) invalidateContext();
      throw error;
    }
  }

  function report(type, data = {}) {
    void sendToWorker({ target: "worker", type, sessionId, ...data }).catch(() => {});
  }

  function mainVideoWithin(scope) {
    return scope?.querySelector("video.html5-main-video") || scope?.querySelector("video") || null;
  }

  function stopTranscriptionPolling() {
    clearTimeout(transcriptionTimer);
    transcriptionTimer = undefined;
    transcriptionCaptureId = undefined;
    transcriptionEpoch++;
  }

  function startTranscriptionPolling(captureId) {
    if (contextInvalidated || !transcriptionEnabled) return;
    if (transcriptionCaptureId === captureId) return;
    stopTranscriptionPolling();
    transcriptionCaptureId = captureId;
    transcriptionStatus.hidden = false;
    transcriptionStatus.textContent = "Google 日语转写：等待音频窗口就绪…";
    const epoch = transcriptionEpoch;
    const current = () => !contextInvalidated && transcriptionEpoch === epoch && transcriptionCaptureId === captureId && transcriptionEnabled;
    const schedule = () => { if (current()) transcriptionTimer = setTimeout(() => { void poll(); }, 2500); };
    const poll = async () => {
      if (!current()) return;
      if (transcriptionInFlight) { schedule(); return; }
      transcriptionInFlight = true;
      let terminal = false;
      try {
        const reply = await sendToWorker({ target: "worker", type: "capture-transcription-status", captureId });
        if (!current()) return;
        if (!reply?.ok || !reply.transcription) throw new Error("Status unavailable");
        const value = reply.transcription;
        const count = (number) => Number.isSafeInteger(number) && number >= 0 ? number : 0;
        const done = count(value.windows_done), total = count(value.windows_total), sentences = count(value.sentences);
        if (value.status === "complete") {
          transcriptionStatus.textContent = `Google 日语转写完成：${done}/${total} 个窗口，已整理 ${sentences} 句。` +
            (enrichmentEnabled ? "翻译和解析进度见下方。" : "翻译和假名解析尚未处理。");
          terminal = true;
        } else if (value.status === "needs_attention" || value.status === "request_budget_reached") {
          transcriptionStatus.textContent = `Google 日语转写需检查：${done}/${total} 个窗口，已整理 ${sentences} 句。` +
            (value.unknown_windows ? "存在结果未知的请求，不会自动重试；请检查本机任务，避免重复费用。" : "请检查本机任务，不会自动重试失败请求。");
          terminal = true;
        } else if (["error", "disabled", "paused"].includes(value.status)) {
          transcriptionStatus.textContent = `Google 日语转写已停止：${done}/${total} 个窗口，已整理 ${sentences} 句。请检查本机任务。`;
          terminal = true;
        } else {
          transcriptionStatus.textContent = `Google 日语转写：${done}/${total} 个窗口，已整理 ${sentences} 句；后台自动处理，无需逐步操作。`;
        }
      } catch (_) {
        if (current()) transcriptionStatus.textContent = "无法读取本机 Google 转写进度，请检查本机任务。不会自动重试云端请求。";
        terminal = true;
      } finally {
        transcriptionInFlight = false;
        if (current()) {
          if (terminal) stopTranscriptionPolling();
          else schedule();
        }
      }
    };
    void poll();
  }

  function stopLearningPolling() {
    clearTimeout(learningTimer);
    learningTimer = undefined;
    learningCaptureId = undefined;
    learningEpoch++;
  }

  function suspendLearningDisplay() {
    video?.removeEventListener("timeupdate", updateSubtitle);
    subtitle.textContent = "";
  }

  function clearLearningDisplay() {
    suspendLearningDisplay();
    sentences = [];
    rows = [];
    openAnalyses.clear();
    list.replaceChildren();
    activeId = "";
    learningRevision = "";
    alignmentNote.hidden = true;
    if (!busy && !recording) { captureForm.open = true; captureFormAutoCollapsed = false; }
  }

  function startLearningPolling(captureId) {
    if (contextInvalidated) return;
    if (captureId && captureId === learningCaptureId) return;
    const previousCaptureId = learningCaptureId;
    if (captureId && captureId !== learningCaptureId) clearLearningDisplay();
    stopLearningPolling();
    learningCaptureId = captureId;
    learningSourceIdentity = identity();
    const expectedSource = learningSourceIdentity;
    const epoch = learningEpoch;
    let disabledPolls = 0;
    let attentionSnapshot = "";
    let attentionObservations = 0;
    learningStatus.hidden = false;
    learningStatus.textContent = captureId ? "翻译和解析：等待完整句子就绪…" : "正在读取本机已保存的学习内容…";
    const current = () => !contextInvalidated && learningEpoch === epoch && identity() === expectedSource;
    const schedule = () => { if (current()) learningTimer = setTimeout(() => { void poll(); }, 2500); };
    const poll = async () => {
      if (!current()) return;
      if (learningInFlight) { schedule(); return; }
      learningInFlight = true;
      loadLearning.disabled = true;
      let terminal = false;
      try {
        const reply = await sendToWorker({ target: "worker", type: learningCaptureId ? "capture-learning-status" : "capture-learning-load",
          ...(learningCaptureId ? { captureId: learningCaptureId } : {}) });
        if (!current()) return;
        if (!reply?.ok || !reply.learning) throw new Error("Learning unavailable");
        const value = reply.learning;
        if (value.capture_id) {
          if (!captureId && previousCaptureId && value.capture_id !== previousCaptureId) clearLearningDisplay();
          learningCaptureId = value.capture_id;
        }
        const count = (item) => Number.isSafeInteger(item) && item >= 0 ? item : 0;
        const done = count(value.sentences_done), total = count(value.sentences_total);
        if (value.status !== "needs_attention") { attentionSnapshot = ""; attentionObservations = 0; }
        const ready = Array.isArray(value.result?.sentences) ? value.result.sentences : [];
        const revision = JSON.stringify([learningCaptureId, value.revision, ready]);
        if (revision !== learningRevision && ready.length) {
          renderResult(value.result, false);
          learningRevision = revision;
        }
        if (value.status === "not_found") {
          clearLearningDisplay();
          learningStatus.textContent = "本机还没有这个视频的学习内容。查询未开始录音，也未调用云端。";
          terminal = true;
        } else if (value.status === "complete") {
          learningStatus.textContent = `翻译和解析完成：${done}/${total} 句。点击解析展示已保存内容。`;
          terminal = true;
        } else if (value.status === "needs_attention") {
          learningStatus.textContent = `翻译和解析需检查：已完成 ${done}/${total} 句；已有内容可继续学习。不会自动重试未知或失败请求。`;
          terminal = true;
          if (enrichmentEnabled && count(value.batches_done) + count(value.blocked_batches) < count(value.batches_total)) {
            const snapshot = JSON.stringify([value.revision, done, total, count(value.batches_done), count(value.batches_total), count(value.blocked_batches)]);
            attentionObservations = snapshot === attentionSnapshot ? attentionObservations + 1 : 1;
            attentionSnapshot = snapshot;
            terminal = attentionObservations >= 2;
            if (!terminal) learningStatus.textContent += " 其余批次尚未完成，正在确认进度。";
          }
        } else if (["error", "paused", "disabled", "request_budget_reached"].includes(value.status)) {
          learningStatus.textContent = `翻译和解析需检查：已完成 ${done}/${total} 句；已有内容可继续学习。不会自动重试未知或失败请求。`;
          terminal = true;
        } else {
          learningStatus.textContent = `翻译和解析：已完成 ${done}/${total} 句，${count(value.batches_done)}/${count(value.batches_total)} 批；已有内容可直接展开。`;
        }
        if (!enrichmentEnabled && !terminal) {
          if (value.status === "not_started") {
            learningStatus.textContent = "本机尚未预生成翻译和解析；自动处理未启用。读取已有内容不会发起云端处理。";
            terminal = true;
          } else if (++disabledPolls >= 12) {
            learningStatus.textContent += " 自动处理已关闭，已停止查询；稍后可点击读取已保存学习内容。";
            terminal = true;
          }
        }
        if (value.invalid_sentences) learningStatus.textContent += ` ${count(value.invalid_sentences)} 句未通过校验，未展示。`;
      } catch (_) {
        if (current()) learningStatus.textContent = "无法读取本机学习内容。查询不会触发云端处理，请检查本机任务。";
        terminal = true;
      } finally {
        learningInFlight = false;
        if (current()) {
          loadLearning.disabled = false;
          if (!terminal) schedule();
        }
      }
    };
    void poll();
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
    panelDismissed = false;
    if (message.sessionId !== sessionId) {
      captureFormAutoCollapsed = false;
      captureFormUserOpened = false;
      captureForm.open = true;
    }
    stopTranscriptionPolling();
    stopLearningPolling();
    clearLearningDisplay();
    enrichmentEnabled = message.enrichmentEnabled === true;
    learningStatus.hidden = true;
    transcriptionEnabled = message.transcriptionEnabled === true;
    transcriptionStatus.hidden = !transcriptionEnabled;
    transcriptionStatus.textContent = enrichmentEnabled ? "开始采集后将自动分批发送 Google 转写，并调用 GPT 预生成翻译和解析。" :
      transcriptionEnabled ? "开始采集后将自动分批发送 Google 转写；不调用 GPT。" : "";
    cancelPreparation();
    cleanupPlayback();
    sessionId = message.sessionId;
    recoveryPending = false;
    audioReleased = false;
    sourceIdentity = identity();
    configured = false;
    busy = false;
    recording = false;
    updateBeginAvailability();
    cancel.disabled = false;
    setInputsDisabled(true);
    metrics.textContent = "";
    replay.replaceChildren();
    host.style.display = "";
    return { ok: true };
  }

  function recoverCapture(message) {
    const states = new Set(["failed", "unknown", "source_changed", "recording", "saving"]);
    if (typeof message.sessionId !== "string" || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(message.sessionId) || !states.has(message.recoveryState)) {
      return { ok: false, error: "采集恢复信息无效。" };
    }
    if ((recording || busy) && sessionId !== message.sessionId) return { ok: false, error: "当前页面已有采集任务。" };
    if ((recording || busy) && ["unknown", "recording", "saving"].includes(message.recoveryState)) {
      // A healthy controller keeps its playback listeners and active recording.
      cancel.disabled = false;
      host.style.display = "";
      return { ok: true };
    }
    cancelPreparation();
    cleanupPlayback();
    sessionId = message.sessionId;
    recoveryPending = true;
    audioReleased = false;
    configured = false;
    busy = false;
    recording = false;
    updateBeginAvailability();
    cancel.disabled = false;
    setInputsDisabled(true);
    captureForm.open = true;
    host.style.display = "";
    updateSeekAvailability();
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
        if (!extensionContextIsValid()) return;
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
    transcriptionEnabled = message.transcriptionEnabled === true;
    cancelPreparation();
    const version = preparationVersion;
    video = findVideo();
    if (!video) throw new Error("未找到视频播放器。请先打开并正常播放视频，再重试。");
    showStatus("正在读取视频信息；播放器转圈时请等待加载…");
    await waitForMetadata(video, message.sessionId, version);
    if (!extensionContextIsValid() || message.sessionId !== sessionId || preparationVersion !== version) throw new Error("视频准备已取消。");
    originalLoop = video.loop;
    configured = true;
    busy = false;
    recording = false;
    if (formSourceIdentity !== identity()) {
      startInput.value = "0:00";
      endInput.value = String(video.duration);
      intervalInput.value = "5";
      formSourceIdentity = identity();
    }
    updateBeginAvailability();
    cancel.disabled = false;
    setInputsDisabled(false);
    metrics.textContent = "";
    replay.replaceChildren();
    sentences = [];
    list.replaceChildren();
    subtitle.textContent = "";
    host.style.display = panelDismissed ? "none" : "";
    startLearningPolling();
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
    if (!extensionContextIsValid()) throw new Error("Extension context invalidated.");
    if (recoveryPending) throw new Error("请先释放上次采集会话。");
    if (recording || message.sessionId !== sessionId) throw new Error("录制会话无效。");
    if (identity() !== sourceIdentity || findVideo() !== video) throw new Error("当前视频已切换，请重新点击扩展图标。");
    if (playerShowsAd(video)) throw new Error("请等待当前播放器的广告结束后再开始采集。");
    validateRange(message.rangeStart, message.rangeEnd, video.duration);
    cancelPreparation();
    const version = preparationVersion;
    rangeStart = message.rangeStart;
    rangeEnd = message.rangeEnd;
    video.pause();
    video.playbackRate = 1;
    originalLoop = video.loop;
    video.loop = false;
    if (Math.abs(video.currentTime - rangeStart) > 0.025) {
      await new Promise((resolve, reject) => {
        const currentVideo = video;
        let settled = false;
        const finish = (error) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          currentVideo.removeEventListener("seeked", done);
          cleanupPreparation = () => {};
          if (error) reject(error); else resolve();
        };
        const timer = setTimeout(() => finish(new Error("跳转到采集起点超时。")), 8000);
        function done() { finish(); }
        cleanupPreparation = () => finish(new Error("视频准备已取消。"));
        currentVideo.addEventListener("seeked", done, { once: true });
        currentVideo.currentTime = rangeStart;
      });
    }
    if (!extensionContextIsValid() || version !== preparationVersion || message.sessionId !== sessionId) throw new Error("视频准备已取消。");
    showStatus(`正在采集 ${formatPreciseTime(rangeStart)}–${formatPreciseTime(rangeEnd)}；请保持正常播放。`);
    return { duration: video.duration, url: location.href, rangeStart, rangeEnd, batchIntervalMs: message.batchIntervalMs };
  }

  async function play(id) {
    if (!extensionContextIsValid()) throw new Error("Extension context invalidated.");
    if (recoveryPending) throw new Error("请先释放上次采集会话。");
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
    startTranscriptionPolling(sessionId);
    if (enrichmentEnabled) startLearningPolling(sessionId);
    updateSeekAvailability();
    if (contextInvalidated) throw new Error("Extension context invalidated.");
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
      stopTranscriptionPolling();
      stopLearningPolling();
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
      if (!extensionContextIsValid()) return false;
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
      if (!recording || !extensionContextIsValid()) return;
      clearTimeout(stallTimer);
      if (playbackStarted) observe("waiting");
      if (!recording) return;
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
      if (!extensionContextIsValid()) return;
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
      const requested = Promise.resolve().then(() => {
        if (contextInvalidated || !recording) throw new Error("播放准备已取消。");
        return currentVideo.play();
      });
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
    updateSeekAvailability();
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
      void sendToWorker({ target: "worker", type: "capture-replay", captureId: manifest.id })
        .then((reply) => { if (!reply?.ok) throw new Error(reply?.error || "无法打开回放"); })
        .catch((error) => showStatus(error.message, true));
    });
    const note = document.createElement("div");
    note.textContent = "可反复回放。要采集另一段，请重新点击扩展图标。";
    replay.append(label, open, note);
  }

  function mappedSentence(sentence) {
    return typeof sentence.start === "number" && Number.isFinite(sentence.start) && sentence.start >= 0 &&
      typeof sentence.end === "number" && Number.isFinite(sentence.end) && sentence.end >= sentence.start;
  }

  function readingPreservesSource(source, reading) {
    let positions = new Set([0]);
    let previous = "";
    const han = (value) => /[㐀-䶿一-鿿豈-﫿々〆ヶ\u{20000}-\u{2EBEF}]/u.test(value || "");
    for (const character of source) {
      const next = new Set();
      for (const position of positions) {
        const candidates = [position];
        const annotation = han(previous) && /^\([ぁ-ゖァ-ヺーゝゞヽヾ]+\)/u.exec(reading.slice(position));
        if (annotation) candidates.push(position + annotation[0].length);
        for (const candidate of candidates) if (reading.startsWith(character, candidate)) next.add(candidate + character.length);
      }
      positions = next;
      previous = character;
      if (!positions.size) return false;
    }
    return [...positions].some((position) => position === reading.length ||
      (han(previous) && /^\([ぁ-ゖァ-ヺーゝゞヽヾ]+\)$/u.test(reading.slice(position))));
  }

  function safeDisplayReading(sentence) {
    const reading = sentence.study?.style_version === 2 && sentence.study?.reading_text;
    if (typeof reading === "string" && reading && !/[㐀-䶿一-鿿豈-﫿々〆ヶ\u{20000}-\u{2EBEF}]/u.test(
      reading.replace(/[㐀-䶿一-鿿豈-﫿々〆ヶ\u{20000}-\u{2EBEF}]+\([ぁ-ゖァ-ヺーゝゞヽヾ]+\)/gu, "")) &&
      typeof sentence.translation_zh === "string" && sentence.translation_zh && Array.isArray(sentence.study.key_points) &&
      (typeof sentence.text !== "string" || readingPreservesSource(sentence.text, reading))) return reading;
    if (sentence.study?.style_version === 2) return "";
    // Legacy kana-only lines are readable without annotations. Never fall back to raw kanji.
    return typeof sentence.text === "string" && !/[㐀-䶿一-鿿豈-﫿々〆ヶ\u{20000}-\u{2EBEF}]/u.test(sentence.text) ? sentence.text : "";
  }

  function updateSeekAvailability() {
    for (const row of rows) {
      row.button.disabled = recording || recoveryPending || !mappedSentence(row.sentence);
      row.button.title = !mappedSentence(row.sentence) ? "时间待确认，无法跳转" : recoveryPending ? "请先释放上次采集会话；解析仍可展开" : recording ? "采集期间不能跳转；解析仍可展开" : "跳转播放这一句";
    }
  }

  function updateSubtitle() {
    if (!video || !sentences.length || identity() !== learningSourceIdentity) {
      subtitle.textContent = "";
      return;
    }
    const time = video.currentTime;
    const active = sentences.find((item) => mappedSentence(item) && item.start <= time && time <= item.end + 0.25);
    if (active?.id === activeId) return;
    activeId = active?.id || "";
    subtitle.textContent = active ? [active.displayReading, active.translation_zh].filter(Boolean).join("\n") : "";
    rows.forEach((row) => row.element.classList.toggle("active", row.sentence.id === activeId));
    if (active && !openAnalyses.size) rows.find((row) => row.sentence.id === activeId)?.element.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }

  function renderResult(result, showSummary = true) {
    video?.removeEventListener("timeupdate", updateSubtitle);
    video = findVideo();
    learningSourceIdentity = identity();
    sentences = (Array.isArray(result?.sentences) ? result.sentences : [])
      .filter((item) => item && typeof item.id === "string")
      .map((item) => ({ ...item, displayReading: safeDisplayReading(item) }))
      .filter((item) => item.displayReading);
    alignmentNote.hidden = !sentences.some((item) => mappedSentence(item) && item.mapping_uncertain === true);
    rows = [];
    const scrollTop = list.scrollTop;
    list.replaceChildren();
    const visibleIds = new Set(sentences.map((item) => item.id));
    for (const id of openAnalyses) if (!visibleIds.has(id)) openAnalyses.delete(id);
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
      time.textContent = mappedSentence(sentence)
        ? `${formatPreciseTime(sentence.start)}–${formatPreciseTime(sentence.end)}`
        : "时间待确认";
      const text = document.createElement("span");
      text.textContent = sentence.displayReading;
      row.append(time, text);
      if (sentence.translation_zh) {
        const translation = document.createElement("span");
        translation.className = "translation";
        translation.textContent = sentence.translation_zh;
        row.append(translation);
      }
      row.addEventListener("click", () => {
        if (!video || recording || recoveryPending || !mappedSentence(sentence)) return;
        video.currentTime = sentence.start;
        void Promise.resolve(video.play()).catch(() => showStatus("请点击视频播放器继续播放。", true));
        updateSubtitle();
      });
      entry.append(row);
      const study = sentence.study?.style_version === 2 ? sentence.study : null;
      if (study) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "analyze";
        const detail = document.createElement("div");
        detail.className = "study";
        const expanded = openAnalyses.has(sentence.id);
        detail.hidden = !expanded;
        toggle.textContent = expanded ? "收起解析" : "解析";
        toggle.setAttribute("aria-expanded", String(expanded));
        const original = document.createElement("p");
        original.textContent = `原句：${sentence.displayReading}`;
        const meaning = document.createElement("p");
        meaning.textContent = `意思：${sentence.translation_zh || "未生成"}`;
        detail.append(original, meaning);
        if (Array.isArray(study.key_points) && study.key_points.length) {
          const label = document.createElement("p");
          label.textContent = "关键拆解：";
          const points = document.createElement("ul");
          for (const point of study.key_points) {
            const item = document.createElement("li");
            item.textContent = point;
            points.append(item);
          }
          detail.append(label, points);
        }
        if (study.context_note) {
          const note = document.createElement("p");
          note.textContent = `语境待确认：${study.context_note}`;
          detail.append(note);
        }
        if (!mappedSentence(sentence)) {
          const note = document.createElement("p");
          note.textContent = "视频时间待确认；可以学习本句，暂不提供跳转。";
          detail.append(note);
        }
        if (study.asr_doubt) {
          const doubt = document.createElement("p");
          doubt.textContent = `ASR 存疑：${study.asr_doubt}`;
          detail.append(doubt);
        }
        toggle.addEventListener("click", () => {
          detail.hidden = !detail.hidden;
          if (detail.hidden) openAnalyses.delete(sentence.id); else openAnalyses.add(sentence.id);
          toggle.setAttribute("aria-expanded", String(!detail.hidden));
          toggle.textContent = detail.hidden ? "解析" : "收起解析";
        });
        entry.append(toggle, detail);
      }
      list.append(entry);
      rows.push({ element: entry, button: row, sentence });
    }
    list.scrollTop = scrollTop;
    updateSeekAvailability();
    video?.addEventListener("timeupdate", updateSubtitle);
    activeId = "";
    updateSubtitle();
    if (sentences.length && !captureFormAutoCollapsed && !captureFormUserOpened && !status.classList.contains("error")) {
      captureForm.open = false;
      captureFormAutoCollapsed = true;
    }
    if (showSummary) showStatus(sentences.length
      ? `已保存 ${sentences.length} 句学习内容；点击句子跳转，点击解析展开。`
      : "尚无通过假名标注校验的学习内容。原始转写保存在本机。");
  }

  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (!extensionContextIsValid() || sender.id !== chrome.runtime.id) return;
    const sendResponse = (value) => {
      if (!extensionContextIsValid()) return;
      try { respond(value); }
      catch (error) {
        if (/extension context invalidated/i.test(error?.message || "")) invalidateContext();
      }
    };
    if (Number.isSafeInteger(message.rearmRequestId) && dismissedReconnects.has(message.rearmRequestId)) {
      sendResponse({ ok: false, error: "音频连接已取消。" });
      return;
    }
    if (message.type === "page-handshake") {
      sendResponse(pageInfo());
    } else if (message.type === "status") {
      showStatus(message.text, message.error);
      sendResponse({ ok: true });
    } else if (message.type === "capture-show") {
      panelDismissed = false;
      host.style.display = "";
      if (sentences.length) { video?.addEventListener("timeupdate", updateSubtitle); activeId = ""; updateSubtitle(); }
      sendResponse({ ok: true });
    } else if (message.type === "capture-recovery") {
      sendResponse(recoverCapture(message));
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
      transcriptionEnabled = message.transcriptionEnabled === true;
      enrichmentEnabled = message.enrichmentEnabled === true;
      if (transcriptionEnabled) startTranscriptionPolling(message.manifest.id);
      if (enrichmentEnabled) startLearningPolling(message.manifest.id);
      try { showSaved(message.manifest); sendResponse({ ok: true }); }
      catch (error) { showStatus(error.message, true); sendResponse({ ok: false, error: error.message }); }
    } else if (message.type === "capture-stop-playback") {
      stopPlayback();
      sendResponse({ ok: true });
    } else if (message.type === "capture-failed") {
      stopTranscriptionPolling();
      stopLearningPolling();
      suspendLearningDisplay();
      cancelPreparation();
      configured = false;
      busy = false;
      updateBeginAvailability();
      cancel.disabled = false;
      setInputsDisabled(false);
      sendResponse({ ok: true });
    } else if (message.type === "capture-reset") {
      if (!message.complete) { stopTranscriptionPolling(); stopLearningPolling(); suspendLearningDisplay(); }
      cancelPreparation();
      stopPlayback();
      configured = false;
      busy = false;
      recoveryPending = false;
      audioReleased = true;
      cancel.disabled = true;
      updateBeginAvailability();
      setInputsDisabled(reconnecting);
      if (!message.complete) metrics.textContent = "";
      updateSeekAvailability();
      sendResponse({ ok: true });
    } else if (message.type === "prepare" || message.type === "play") {
      const action = message.type === "prepare" ? prepare(message) : play(message.sessionId);
      action.then((value) => sendResponse({ ok: true, ...value }))
        .catch((error) => sendResponse({ ok: false, error: error.message }));
      return true;
    }
  });
  window.__snsJapaneseAgentPage = { ...pageInfo(), isAlive: extensionContextIsValid };
  return pageInfo();
})();
