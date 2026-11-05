const SESSION_KEY = "activeCapture";
const API = "http://127.0.0.1:8766";
const PAGE_VERSION = "0.6.0";
const PAGE_PROTOCOL = 1;
let creatingOffscreen;
let opening = false;
let releasing;
const reviewOpening = new Set();
const reviewRestoring = new Map();
const REARM_BUSY = "正在连接音频，请稍候后重试。";
const REARM_ACTIVE = "已有采集任务，请先中断／释放音频后重试。";
const REARM_FALLBACK = "音频未连接，请点击浏览器工具栏 S，再点击生成学习内容。";

async function within(promise, milliseconds, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(message instanceof Error ? message : new Error(message)), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}

async function checkCurrent(session) {
  const active = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
  if (active?.sessionId !== session.sessionId) throw new Error("准备已取消，请重新点击扩展图标。");
}

async function ensureOffscreen() {
  const documentUrls = [chrome.runtime.getURL("offscreen.html")];
  if ((await within(chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"], documentUrls }),
    3000, "检查音频采集环境超时，请重试。")).length) return;
  creatingOffscreen ||= chrome.offscreen.createDocument({
    url: "offscreen.html", reasons: ["USER_MEDIA"],
    justification: "Capture the selected YouTube tab and upload acknowledged audio batches locally",
  }).finally(() => { creatingOffscreen = undefined; });
  await within(creatingOffscreen, 15_000, "创建音频采集环境超时，请重试。");
}

const pageMessage = (tabId, payload) => chrome.tabs.sendMessage(tabId, payload);
const offscreenMessage = (payload) => chrome.runtime.sendMessage({ target: "offscreen", ...payload });

async function ensurePage(tabId) {
  const failure = (code, text) => new Error(`${text} [${code}] 请刷新当前 YouTube 页面，再点击 SNS 扩展图标。`);
  const controllerFailures = {
    legacy_controller: "当前页面仍有旧版扩展脚本。",
    controller_version_mismatch: "页面控制器与扩展版本不一致。",
    dead_controller: "当前页面的扩展连接已失效。",
    orphan_root: "检测到本扩展遗留的界面，但没有有效控制器。",
    context_invalid: "当前页面的扩展环境已失效。",
  };
  let injected;
  const injectionTimeout = failure("inject_timeout", "页面脚本注入超过 3 秒。");
  try {
    injected = await within(chrome.scripting.executeScript({ target: { tabId }, files: ["review.js", "page.js"] }),
      3000, injectionTimeout);
  } catch (error) {
    if (error === injectionTimeout) throw error;
    throw failure("inject_throw", "无法将扩展脚本注入当前页面。");
  }
  const mainFrame = Array.isArray(injected) && injected.find((frame) => frame?.frameId === 0);
  if (!mainFrame) throw failure("missing_main_frame", "页面脚本没有返回主页面结果。");
  const value = mainFrame.result;
  if (value === undefined || value === null) throw failure("missing_injection_result", "页面脚本未完成初始化，或没有返回初始化结果。");
  if (value.ok !== true) {
    if (typeof value.reason === "string" && Object.prototype.hasOwnProperty.call(controllerFailures, value.reason)) {
      throw failure(value.reason, controllerFailures[value.reason]);
    }
    throw failure("injection_invalid_result", "页面脚本返回了无法确认的初始化结果。");
  }
  if (value.version !== PAGE_VERSION) throw failure("injection_version_mismatch", "注入的页面脚本与扩展版本不一致。");
  if (value.protocol !== PAGE_PROTOCOL) throw failure("injection_protocol_mismatch", "注入的页面脚本与扩展通信协议不一致。");
  let reply;
  const handshakeTimeout = failure("handshake_timeout", "页面脚本在 3 秒内未响应连接检查。");
  try {
    reply = await within(chrome.tabs.sendMessage(tabId, {
      type: "page-handshake", version: PAGE_VERSION, protocol: PAGE_PROTOCOL,
    }, { frameId: 0 }), 3000, handshakeTimeout);
  } catch (error) {
    if (error === handshakeTimeout) throw error;
    throw failure("handshake_throw", "无法连接已注入的页面脚本。");
  }
  if (reply === undefined || reply === null) throw failure("handshake_empty", "页面脚本没有返回连接检查结果。");
  if (reply.ok !== true) throw failure("handshake_invalid_result", "页面脚本返回了无效的连接检查结果。");
  if (reply.version !== PAGE_VERSION) throw failure("handshake_version_mismatch", "响应的页面脚本与扩展版本不一致。");
  if (reply.protocol !== PAGE_PROTOCOL) throw failure("handshake_protocol_mismatch", "响应的页面脚本与扩展通信协议不一致。");
}

async function status(tabId, text, error = false, metadata = {}) {
  await chrome.action.setBadgeText({ tabId, text: error ? "ERR" : "JA" });
  await chrome.action.setBadgeBackgroundColor({ tabId, color: error ? "#b42318" : "#176b45" });
  await chrome.action.setTitle({ tabId, title: text });
  await pageMessage(tabId, { type: "status", text, error, ...metadata }).catch(() => {});
}

function finish(session, payload = {}) {
  // A reset can prompt a direct reconnect before closeDocument has settled. Keep
  // all later openings behind the complete release, including concurrent actions.
  let closed;
  const reported = Promise.resolve(releasing).then(async () => {
    const stored = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
    if (stored?.sessionId !== session.sessionId) return;
    await within(pageMessage(session.tabId, { type: "capture-reset", sessionId: session.sessionId, ...payload }), 3000, "面板关闭超时").catch(() => {});
    await chrome.storage.session.remove(SESSION_KEY);
    closed = chrome.offscreen.closeDocument().catch(() => {});
    await within(closed, 3000, "采集环境关闭超时").catch(() => {});
  });
  // UI cancellation remains bounded, while a delayed Chrome close must actually
  // settle before a replacement document can be opened.
  const work = reported.then(() => closed);
  releasing = work;
  const clear = () => { if (releasing === work) releasing = undefined; };
  void work.then(clear, clear);
  return reported;
}

function validVideoUrl(value) {
  try {
    const url = new URL(value);
    return url.origin === "https://www.youtube.com" &&
      ((url.pathname === "/watch" && /^[\w-]{11}$/.test(url.searchParams.get("v") || "")) || /^\/shorts\/[\w-]{11}\/?$/.test(url.pathname));
  } catch (_) { return false; }
}


function validRange(start, end) {
  return Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end - start >= 1 && end - start <= 1800;
}

function adFailureCode(value) {
  return value === "advertisement_started" ? value : undefined;
}

async function showCaptureRecovery(active, inspected, recoveryState) {
  const count = (value) => Number.isSafeInteger(value) && value >= 0 && value <= 1_000_000_000 ? value : null;
  const pendingBatches = count(inspected?.pendingBatches);
  const pendingBytes = count(inspected?.pendingBytes);
  const reply = await within(pageMessage(active.tabId, {
    type: "capture-recovery", sessionId: active.sessionId, recoveryState, pendingBatches, pendingBytes,
    ...(validRange(active.rangeStart, active.rangeEnd) ? { rangeStart: active.rangeStart, rangeEnd: active.rangeEnd } : {}),
    ...(adFailureCode(active.failureCode) ? { code: active.failureCode } : {}),
    transcriptionEnabled: active.transcriptionEnabled === true,
    enrichmentEnabled: active.enrichmentEnabled === true,
  }), 3000, "无法显示旧采集任务的恢复控件，请刷新视频页面后重试。");
  if (!reply?.ok) throw new Error("无法显示旧采集任务的恢复控件，请刷新视频页面后重试。");
  let message;
  if (recoveryState === "recording") message = "已有采集正在进行，未重新开始录音。可点击“中断／释放音频”明确结束。";
  else if (recoveryState === "saving") message = "已有采集正在保存批次，未重新开始录音。可等待完成，或点击“中断／释放音频”明确结束。";
  else {
    const prefix = recoveryState === "source_changed" ? "视频已切换，旧的采集准备不能复用。" :
      recoveryState === "failed" ? "上次采集已停止。" : "无法确认上次采集状态。";
    const pending = pendingBatches === null ? "待确认数据量未知。" : pendingBatches === 0 ? "没有待确认批次。" :
      `待确认 ${pendingBatches} 批${pendingBytes === null ? "" : `（${pendingBytes} 字节）`}仍保留在浏览器。`;
    message = `${prefix}${pending}请点击“中断／释放音频”释放旧采集环境，再重新点击扩展图标。本机已保存的批次仍保留。`;
  }
  await status(active.tabId, message, !["recording", "saving"].includes(recoveryState));
}

async function openAgent(tab, { freshOnly = false, rearmRequestId } = {}) {
  if (opening) return { ok: false, error: REARM_BUSY };
  opening = true;
  let session;
  const metadata = freshOnly ? { rearmRequestId } : {};
  const notify = (tabId, text, error = false) => status(tabId, text, error, metadata);
  const configureMessage = (tabId, payload) => pageMessage(tabId, { ...payload, ...metadata });
  try {
    if (releasing) await within(releasing, 4000, "上次音频环境仍在关闭，请稍后重新点击扩展图标。");
    if (!tab.id || !validVideoUrl(tab.url)) throw new Error("请先打开 YouTube 视频或 Shorts 页面。");
    if (freshOnly) {
      if (reviewOpening.has(tab.id) || reviewRestoring.has(tab.id)) return { ok: false, error: "正在打开复习片段，请稍候。" };
      const currentTab = await chrome.tabs.get(tab.id);
      if (currentTab.id !== tab.id || currentTab.active !== true || currentTab.url !== tab.url) {
        return { ok: false, error: REARM_FALLBACK };
      }
      tab = currentTab;
    }
    await ensurePage(tab.id);
    const active = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
    // Reconnect never inspects, releases, resumes, or replaces an existing queue.
    if (freshOnly && active) return { ok: false, error: REARM_ACTIVE };
    if (active) {
      const contexts = await within(chrome.runtime.getContexts({
        contextTypes: ["OFFSCREEN_DOCUMENT"], documentUrls: [chrome.runtime.getURL("offscreen.html")],
      }), 3000, "检查采集环境超时");
      if (contexts.length) {
        if (active.tabId !== tab.id) await ensurePage(active.tabId);
        await pageMessage(active.tabId, { type: "capture-show" }).catch(() => {});
        if (active.tabId !== tab.id) await notify(tab.id, "另一个标签页已有采集任务，请先结束该任务。", true);
        else {
          let inspected;
          try {
            inspected = await within(offscreenMessage({ type: "capture-inspect", sessionId: active.sessionId }), 3000,
              "无法确认采集状态。");
          } catch (_) {
            await showCaptureRecovery(active, undefined, "unknown");
            return;
          }
          if (inspected?.ok && inspected.phase === "ready") {
            if (active.sourceUrl !== tab.url) {
              await showCaptureRecovery(active, inspected, "source_changed");
              return;
            }
            session = active;
            const configuring = await within(configureMessage(tab.id, { type: "capture-configuring", ...active }),
              3000, "采集准备界面未响应。");
            if (configuring?.ok !== true) throw new Error("采集准备已取消。");
            const reply = await within(configureMessage(tab.id, { type: "capture-configure", ...active }), 12_000,
              "视频信息读取超时，请先正常播放视频后重试。");
            if (!reply?.ok) throw new Error(reply?.error || "请先正常播放视频后重试。");
            active.phase = "ready";
            await checkCurrent(active);
            await chrome.storage.session.set({ [SESSION_KEY]: active });
            await notify(tab.id, active.enrichmentEnabled
              ? "音频已准备。开始采集后将分批发送 Google 转写，并自动预生成翻译和解析。"
              : active.transcriptionEnabled
              ? "音频已准备。选择范围后点击开始；开始采集后将分批发送 Google 转写。"
              : "音频已准备。选择范围后点击开始；尚未录音。");
          } else if (inspected?.ok && inspected.phase === "failed") {
            const sameVideo = canonicalVideo(active.sourceUrl)?.id === canonicalVideo(tab.url)?.id;
            await showCaptureRecovery(active, inspected, sameVideo ? "failed" : "source_changed");
          } else if (inspected?.ok && ["recording", "saving"].includes(inspected.phase)) {
            await showCaptureRecovery(active, inspected, inspected.phase);
          } else {
            await showCaptureRecovery(active, inspected?.ok ? inspected : undefined, "unknown");
          }
        }
        return;
      }
      // No offscreen document means its in-memory queue is already gone; keep disk batches untouched.
      await chrome.storage.session.remove(SESSION_KEY);
    }
    if (!freshOnly) {
      await notify(tab.id, "已打开学习面板。选择复习片段，或生成新的学习内容。");
      await showIdle(tab.id);
      await restorePendingReview(tab, true);
      return { ok: true, idle: true };
    }
    await notify(tab.id, "正在连接本机采集服务…");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    let health;
    try {
      const response = await fetch(`${API}/health`, { signal: controller.signal });
      if (!response.ok) throw new Error("本机服务不可用");
      health = await response.json();
    } finally { clearTimeout(timeout); }
    if (!health.capabilities?.capture_batches) throw new Error("请启动支持分批采集的新版本本机后端。");
    session = { tabId: tab.id, sessionId: crypto.randomUUID(), sourceUrl: tab.url, phase: "preparing",
      transcriptionEnabled: health.capabilities?.capture_transcription === true,
      enrichmentEnabled: health.capabilities?.capture_enrichment === true && health.capabilities?.capture_transcription === true };
    await chrome.storage.session.set({ [SESSION_KEY]: session });
    const configuring = await within(configureMessage(tab.id, { type: "capture-configuring", ...session }),
      3000, "采集准备界面未响应。");
    if (configuring?.ok !== true) throw new Error("采集准备已取消。");
    await notify(tab.id, "正在准备音频采集环境…若视频尚未加载，请先让它正常播放。");
    await ensureOffscreen();
    await checkCurrent(session);
    await notify(tab.id, "正在请求当前标签页的音频访问…");
    // Obtain and immediately consume the short-lived stream ID. The user first
    // authorizes this tab through the toolbar; a panel reconnect may reuse that grant.
    // The recorder starts only after the user confirms the selected range in the panel.
    const streamId = await within(chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id }), 10_000,
      "请求标签页音频访问超时。请先正常播放视频，再重新点击扩展图标。");
    await checkCurrent(session);
    await notify(tab.id, "正在连接标签页音频与扬声器…");
    const armed = await within(offscreenMessage({ type: "capture-arm", ...session, streamId }), 16_000,
      "音频准备超时。请先正常播放视频，再重新点击扩展图标。");
    if (!armed?.ok) throw new Error(armed?.error || "无法准备标签页音频");
    await checkCurrent(session);
    session.phase = "ready";
    await chrome.storage.session.set({ [SESSION_KEY]: session });
    await notify(tab.id, "音频已连接，正在读取视频信息…");
    const reply = await within(configureMessage(tab.id, { type: "capture-configure", ...session }), 12_000,
      "视频信息读取超时，请先正常播放视频后重试。");
    if (!reply?.ok) throw new Error(reply?.error || "无法读取视频；请先让视频正常播放后重试。");
    await checkCurrent(session);
    session.phase = "ready";
    await chrome.storage.session.set({ [SESSION_KEY]: session });
    await notify(tab.id, session.enrichmentEnabled
      ? "选择采集范围后开始。将分批发送 Google 转写，并自动调用 GPT 预生成翻译和解析；点击解析只展示已保存内容。"
      : session.transcriptionEnabled
      ? "选择采集范围和上传间隔，再点击开始。开始采集后将分批发送 Google 转写，不调用 GPT。"
      : "选择采集范围和上传间隔，再点击开始。尚未录音，不调用云端服务。");
    return { ok: true, sessionId: session.sessionId };
  } catch (error) {
    const failure = freshOnly ? REARM_FALLBACK : `准备失败：${error.message}`;
    if (tab.id) await notify(tab.id, failure, true).catch(() => {});
    if (session) {
      await within(offscreenMessage({ type: "capture-abort", sessionId: session.sessionId }), 3000, "音频准备取消超时").catch(() => {});
      await finish(session);
    }
    return { ok: false, error: freshOnly ? REARM_FALLBACK : failure };
  } finally { opening = false; }
}

chrome.action.onClicked.addListener((tab) => { void openAgent(tab); });

async function handleMessage(message, sender) {
  const session = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
  if (!session || session.sessionId !== message.sessionId) throw new Error("采集会话已失效，请重新点击扩展图标。");
  const pageSender = sender.id === chrome.runtime.id && sender.tab?.id === session.tabId && sender.frameId === 0;
  const recorderSender = sender.id === chrome.runtime.id && !sender.tab && sender.url === chrome.runtime.getURL("offscreen.html");
  const pageTypes = ["capture-begin", "page-started", "page-ended", "page-error", "capture-cancel", "playback-event"];
  const recorderTypes = ["capture-progress", "capture-complete", "capture-error", "capture-expired"];
  if (!(pageSender && pageTypes.includes(message.type)) && !(recorderSender && recorderTypes.includes(message.type))) {
    throw new Error("采集消息来源无效。");
  }
  switch (message.type) {
    case "capture-begin": {
      if (session.phase !== "ready") throw new Error("音频尚未准备好或采集已开始，请查看面板状态。");
      const currentTab = await chrome.tabs.get(session.tabId);
      if (currentTab.url !== session.sourceUrl) throw new Error("视频页面已切换，请重新点击扩展。");
      const prepared = await within(pageMessage(session.tabId, {
        type: "prepare", sessionId: session.sessionId, rangeStart: message.rangeStart,
        rangeEnd: message.rangeEnd, batchIntervalMs: message.batchIntervalMs,
      }), 12_000, "视频跳转准备超时，请中断后重试。");
      if (!prepared?.ok) {
        const code = adFailureCode(prepared?.code);
        if (code && validRange(message.rangeStart, message.rangeEnd)) {
          await checkCurrent(session);
          Object.assign(session, { rangeStart: message.rangeStart, rangeEnd: message.rangeEnd, failureCode: code });
          await chrome.storage.session.set({ [SESSION_KEY]: session });
        }
        throw Object.assign(new Error(prepared?.error || "视频准备失败"), { code });
      }
      await checkCurrent(session);
      if (!validRange(prepared.rangeStart, prepared.rangeEnd) || !Number.isFinite(prepared.duration) || prepared.rangeEnd > prepared.duration) {
        throw new Error("视频准备返回了无效的采集范围。");
      }
      session.rangeStart = prepared.rangeStart;
      session.rangeEnd = prepared.rangeEnd;
      session.phase = "recording";
      await chrome.storage.session.set({ [SESSION_KEY]: session });
      const started = await offscreenMessage({ type: "capture-start", ...session, ...prepared });
      if (!started?.ok) throw new Error(started?.error || "无法开始录音");
      await checkCurrent(session);
      const played = await within(pageMessage(session.tabId, { type: "play", sessionId: session.sessionId }),
        10_000, "视频播放未能启动，请中断后先确认视频能正常播放。");
      if (!played?.ok) throw new Error(played?.error || "视频无法播放");
      return { ok: true };
    }
    case "page-started": {
      const result = await offscreenMessage({
        type: "playback-started", sessionId: session.sessionId,
        playbackStartedAtMs: message.playbackStartedAtMs, playbackStartVideoSec: message.playbackStartVideoSec,
      });
      if (!result?.ok) throw new Error(result?.error || "播放时间保存失败");
      break;
    }
    case "playback-event": {
      const result = await offscreenMessage({ type: "playback-event", sessionId: session.sessionId, event: message.event });
      if (!result?.ok) throw new Error(result?.error || "播放观测保存失败");
      break;
    }
    case "page-ended": {
      await status(session.tabId, "录音已结束，正在确认最后批次与完整性…");
      const result = await offscreenMessage({ type: "capture-finish", sessionId: session.sessionId, playbackEndVideoSec: message.playbackEndVideoSec, playbackEvents: message.playbackEvents });
      if (!result?.ok) throw new Error(result?.error || "保存录音失败");
      break;
    }
    case "capture-cancel": {
      await pageMessage(session.tabId, { type: "capture-stop-playback", sessionId: session.sessionId }).catch(() => {});
      let confirmed = false;
      try {
        const reply = await within(offscreenMessage({ type: "capture-abort", sessionId: session.sessionId }),
          4000, "采集中断确认超时");
        confirmed = reply?.ok === true && reply.abortConfirmed === true;
      } catch (_) { /* Closing the explicitly cancelled document is still required. */ }
      finally { await finish(session); }
      await status(session.tabId, confirmed
        ? "本段已停止，已保存的内容仍保留。"
        : "录音已停止，但本机服务未确认任务已结束。请检查本机服务。", !confirmed);
      return { ok: true, abortConfirmed: confirmed };
    }
    case "page-error": {
      if (adFailureCode(message.failure?.code)) {
        session.failureCode = message.failure.code;
        await chrome.storage.session.set({ [SESSION_KEY]: session });
      }
      await pageMessage(session.tabId, { type: "capture-stop-playback", sessionId: session.sessionId }).catch(() => {});
      await within(offscreenMessage({ type: "capture-fail", sessionId: session.sessionId, error: message.error,
        failure: message.failure, playbackEndVideoSec: message.playbackEndVideoSec, playbackEvents: message.playbackEvents }),
        4000, "采集错误状态确认超时，请点击中断释放。");
      break;
    }
    case "capture-progress":
      await pageMessage(session.tabId, { type: "capture-progress", sessionId: session.sessionId, progress: message.progress }).catch(() => {});
      break;
    case "capture-complete":
      await pageMessage(session.tabId, { type: "capture-saved", sessionId: session.sessionId, manifest: message.manifest,
        transcriptionEnabled: session.transcriptionEnabled === true,
        enrichmentEnabled: session.enrichmentEnabled === true }).catch(() => {});
      await status(session.tabId, session.enrichmentEnabled
        ? "所有音频批次已保存。Google 转写、翻译和解析在后台处理，已生成的学习内容会自动显示。"
        : session.transcriptionEnabled
        ? "所有音频批次已保存并合并，可回放检查。Google 转写在后台处理，进度见下方；未调用 GPT。"
        : "所有批次已保存并合并，可回放检查。此次未调用 Google 或 GPT。");
      await finish(session, { complete: true });
      break;
    case "capture-expired":
    case "capture-error":
      await pageMessage(session.tabId, { type: "capture-stop-playback", sessionId: session.sessionId }).catch(() => {});
      await checkCurrent(session);
      session.phase = "failed";
      const code = adFailureCode(message.code) || adFailureCode(session.failureCode);
      if (code) session.failureCode = code;
      await chrome.storage.session.set({ [SESSION_KEY]: session });
      await pageMessage(session.tabId, { type: "capture-failed", sessionId: session.sessionId, ...(code ? { code } : {}) }).catch(() => {});
      await status(session.tabId, `采集未完成：${message.error}。已确认批次保留在本机。` +
        (message.pendingBatches
          ? `待确认 ${message.pendingBatches} 批（${message.pendingBytes || 0} 字节）保留在浏览器，点击“中断／释放音频”后丢弃。`
          : "没有待确认批次。点击“中断／释放音频”结束本次任务，再点击扩展图标重新采集。"), true);
      break;
  }
  return { ok: true };
}

function safeTranscriptionStatus(value) {
  const allowed = new Set(["not_started", "scheduled", "running", "processing", "transcribing", "waiting_for_audio",
    "prepared", "request_budget_reached", "paused", "disabled", "complete", "error", "needs_attention"]);
  const count = (number) => Number.isSafeInteger(number) && number >= 0 && number <= 1_000_000 ? number : 0;
  const blocked = Array.isArray(value?.blocked_windows) ? value.blocked_windows : [];
  return {
    status: allowed.has(value?.status) ? value.status : "error",
    windows_done: count(value?.windows_done), windows_total: count(value?.windows_total),
    sentences: count(value?.sentences),
    unknown_windows: blocked.filter((item) => item?.status === "unknown").length,
    failed_windows: blocked.filter((item) => item?.status === "failed").length,
  };
}

async function transcriptionStatus(captureId) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(`${API}/captures/${captureId}/transcription`, { signal: controller.signal, credentials: "omit" });
    if (!response.ok) throw new Error("Local transcription status unavailable");
    // The backend response can include private ASR text. Only fixed status and counts leave the worker.
    return { ok: true, transcription: safeTranscriptionStatus(await response.json()) };
  } catch (_) {
    return { ok: false, error: "无法读取本机转写状态，请检查本机任务。" };
  } finally { clearTimeout(timer); }
}

function validCaptureId(value) {
  return typeof value === "string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(value);
}

function validSentenceId(value) {
  return typeof value === "string" && /^[a-zA-Z0-9_-]{1,100}$/.test(value);
}

function canonicalVideo(value) {
  if (typeof value !== "string" || value.length > 2048) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !["www.youtube.com", "youtube.com"].includes(url.hostname) || url.port || url.username || url.password) return null;
    const short = /^\/shorts\/([a-zA-Z0-9_-]{11})\/?$/.exec(url.pathname);
    const watch = url.pathname === "/watch" && url.searchParams.getAll("v");
    const id = short?.[1] || (watch?.length === 1 ? watch[0] : "");
    if (!/^[a-zA-Z0-9_-]{11}$/.test(id)) return null;
    return { id, url: short ? `https://www.youtube.com/shorts/${id}` : `https://www.youtube.com/watch?v=${id}` };
  } catch (_) { return null; }
}

function safeTimestamp(value) {
  return Number.isFinite(value) && value >= 0 && value <= 8.64e12 ? value : null;
}

function safeClip(value, captureId) {
  const source = canonicalVideo(value?.source_url);
  const updatedAt = safeTimestamp(value?.updated_at);
  if (!validCaptureId(value?.capture_id) || (captureId && value.capture_id !== captureId) || !source || updatedAt === null ||
      !validRange(value.range_start_sec, value.range_end_sec) || !Number.isSafeInteger(value.sentences_done) ||
      value.sentences_done < 0 || value.sentences_done > 1_000_000) return null;
  return { capture_id: value.capture_id, source_url: source.url, range_start_sec: value.range_start_sec,
    range_end_sec: value.range_end_sec, updated_at: updatedAt, sentences_done: value.sentences_done };
}

function annotatedReading(text, reading) {
  if (typeof text !== "string" || typeof reading !== "string" || !reading || reading.length > 32000) return false;
  const han = (value) => /[㐀-䶿一-鿿豈-﫿々〆ヶ\u{20000}-\u{2EBEF}]/u.test(value || "");
  if (han(reading.replace(/[㐀-䶿一-鿿豈-﫿々〆ヶ\u{20000}-\u{2EBEF}]+\([ぁ-ゖァ-ヺーゝゞヽヾ]+\)/gu, ""))) return false;
  // Preserve literal kana parentheses already in ASR; only newly inserted readings may be skipped.
  let positions = new Set([0]);
  let previous = "";
  for (const character of text) {
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
  for (const position of positions) {
    if (position === reading.length) return true;
    if (han(previous) && /^\([ぁ-ゖァ-ヺーゝゞヽヾ]+\)$/u.test(reading.slice(position))) return true;
  }
  return false;
}

function safeLearningResult(value) {
  const sentences = [];
  const ids = new Set();
  const text = (item, limit) => typeof item === "string" && item.length <= limit ? item : "";
  const seconds = (item) => typeof item === "number" && Number.isFinite(item) && item >= 0 ? item : null;
  for (const item of Array.isArray(value?.sentences) ? value.sentences.slice(0, 10000) : []) {
    const study = item?.study;
    if (!study || study.style_version !== 2 || !validSentenceId(item.id) || ids.has(item.id)) continue;
    if (!annotatedReading(item.text, study.reading_text)) continue;
    const translation = text(item.translation_zh, 16000);
    const points = Array.isArray(study.key_points) ? study.key_points : [];
    if (!translation || !Array.isArray(study.key_points) || points.length > 8 || points.some((point) => !text(point, 8000))) continue;
    ids.add(item.id);
    // The learner receives validated notes only, never the raw ASR text or unknown backend fields.
    sentences.push({ id: item.id, start: seconds(item.start), end: seconds(item.end),
      audio_start: seconds(item.audio_start), audio_end: seconds(item.audio_end),
      mapping_uncertain: item.mapping_uncertain === true || seconds(item.start) === null || seconds(item.end) === null,
      translation_zh: translation, study: { style_version: 2, reading_text: study.reading_text,
        key_points: points.slice(), context_note: text(study.context_note, 8000), asr_doubt: text(study.asr_doubt, 8000) } });
  }
  return { sentences };
}

function safeLearningStatus(value, captureId) {
  const statuses = new Set(["not_found", "not_started", "queued", "scheduled", "running", "processing", "prepared", "waiting_for_audio",
    "waiting_for_transcript", "waiting_for_transcription", "waiting_for_sentences", "request_budget_reached", "needs_attention", "paused", "disabled", "complete", "error"]);
  const count = (item) => Number.isSafeInteger(item) && item >= 0 && item <= 1_000_000 ? item : 0;
  const result = safeLearningResult(value?.result);
  return { capture_id: validCaptureId(captureId) ? captureId : null,
    status: statuses.has(value?.status) ? value.status : "error",
    batches_done: count(value?.batches_done), batches_total: count(value?.batches_total),
    sentences_done: count(value?.sentences_done), sentences_total: count(value?.sentences_total),
    invalid_sentences: count(value?.invalid_sentences),
    blocked_batches: Array.isArray(value?.blocked_batches) ? value.blocked_batches.length : count(value?.blocked_batches),
    result_available: result.sentences.length > 0,
    revision: Number.isSafeInteger(value?.revision) && value.revision >= 0 ? value.revision : 0,
    clip: safeClip(value?.clip, captureId), result };
}

async function learningStatus(path, captureId) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(`${API}${path}`, { signal: controller.signal, credentials: "omit" });
    if (!response.ok) throw new Error("Local learning status unavailable");
    const value = await response.json();
    return { ok: true, learning: safeLearningStatus(value, captureId || value.capture_id) };
  } catch (_) {
    return { ok: false, error: "无法读取本机学习内容。查询不会触发云端处理。" };
  } finally { clearTimeout(timer); }
}

async function reviewRequest(path, payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const options = { signal: controller.signal, credentials: "omit", redirect: "error" };
    if (payload !== undefined) Object.assign(options, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const response = await fetch(`${API}${path}`, options);
    if (response.status === 404 && ["/learning/library", "/learning/favorites"].includes(path)) {
      throw Object.assign(new Error("Local review API needs an update"), { code: "backend_upgrade_required" });
    }
    if (!response.ok) throw new Error("Local review request unavailable");
    return await response.json();
  } finally { clearTimeout(timer); }
}

function safeFavorite(value) {
  const source = canonicalVideo(value?.source_url);
  const savedAt = safeTimestamp(value?.saved_at);
  if (!validCaptureId(value?.capture_id) || !validSentenceId(value?.sentence_id) || !source || savedAt === null) return null;
  const [sentence] = safeLearningResult({ sentences: [value.sentence] }).sentences;
  if (sentence?.id !== value.sentence_id) return null;
  return { capture_id: value.capture_id, sentence_id: value.sentence_id, source_url: source.url, saved_at: savedAt, sentence };
}

function pendingReviewKey(tabId) { return `pendingReview:${tabId}`; }

async function showIdle(tabId) {
  const reply = await within(pageMessage(tabId, { type: "capture-idle" }), 3000, "学习面板未响应，请刷新页面后重试。");
  if (reply?.ok !== true) throw new Error("学习面板未响应，请刷新页面后重试。");
}

async function selectReviewClip(tabId, captureId) {
  const reply = await within(pageMessage(tabId, { type: "review-selected", captureId }), 3000, "复习面板未响应，请重新打开扩展。");
  if (reply?.ok !== true) throw new Error("复习面板未响应，请重新打开扩展。");
}

function restorePendingReview(tab, pageReady = false) {
  const tabId = tab.id;
  if (!Number.isSafeInteger(tabId) || tabId <= 0 || !validVideoUrl(tab.url)) return Promise.resolve(false);
  const running = reviewRestoring.get(tabId);
  if (running) return running;
  const work = (async () => {
    const key = pendingReviewKey(tabId);
    const pending = (await chrome.storage.session.get(key))[key];
    if (!validCaptureId(pending?.captureId) || canonicalVideo(tab.url)?.id !== pending.videoId) return false;
    const active = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
    if (active?.tabId === tabId) return false;
    if (!pageReady) { await ensurePage(tabId); await showIdle(tabId); }
    const current = await chrome.tabs.get(tabId);
    if (current.id !== tabId || canonicalVideo(current.url)?.id !== pending.videoId) return false;
    const latest = (await chrome.storage.session.get(key))[key];
    if (latest?.captureId !== pending.captureId || latest.videoId !== pending.videoId) return false;
    await selectReviewClip(tabId, pending.captureId);
    await chrome.storage.session.remove(key);
    return true;
  })();
  reviewRestoring.set(tabId, work);
  const clear = () => { if (reviewRestoring.get(tabId) === work) reviewRestoring.delete(tabId); };
  void work.then(clear, clear);
  return work;
}

async function openReviewClip(tabId, sourceUrl, captureId) {
  if (opening || reviewOpening.has(tabId) || reviewRestoring.has(tabId)) return { ok: false, error: "页面正在切换，请稍候后重试。" };
  reviewOpening.add(tabId);
  try {
    const ensureCanOpen = async () => {
      const current = await chrome.tabs.get(tabId);
      if (current.id !== tabId || !validVideoUrl(current.url) || canonicalVideo(current.url)?.id !== canonicalVideo(sourceUrl)?.id) {
        return { error: "视频页面已切换，请重新打开复习面板。" };
      }
      const active = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
      if (active?.tabId === tabId) return { error: "请先中断／释放当前采集，再打开复习片段。" };
      return current;
    };
    const before = await ensureCanOpen();
    if (before.error) return { ok: false, error: before.error };
    const value = await reviewRequest(`/captures/${captureId}/learning`).catch(() => null);
    const clip = safeClip(value?.clip, captureId);
    if (!clip) return { ok: false, error: "无法确认该片段的原视频，请重新读取复习列表。" };
    const current = await ensureCanOpen();
    if (current.error) return { ok: false, error: current.error };
    const target = canonicalVideo(clip.source_url);
    const key = pendingReviewKey(tabId);
    if (canonicalVideo(current.url)?.id === target.id) {
      await selectReviewClip(tabId, captureId);
      await chrome.storage.session.remove(key);
      return { ok: true, navigated: false };
    }
    await chrome.storage.session.set({ [key]: { captureId, videoId: target.id } });
    try { await chrome.tabs.update(tabId, { url: target.url }); }
    catch (_) {
      await chrome.storage.session.remove(key);
      return { ok: false, error: "无法打开原视频，请重新选择复习片段。" };
    }
    return { ok: true, navigated: true };
  } finally { reviewOpening.delete(tabId); }
}

async function handleReview(message, sender) {
  if (message.type === "review-library") {
    const value = await reviewRequest("/learning/library");
    return { ok: true,
      clips: Array.isArray(value?.clips) ? value.clips.slice(0, 10_000).map((item) => safeClip(item)).filter(Boolean) : [],
      favorites: Array.isArray(value?.favorites) ? value.favorites.slice(0, 10_000).map(safeFavorite).filter(Boolean) : [],
    };
  }
  if (message.type === "review-favorite") {
    const value = await reviewRequest("/learning/favorites", { capture_id: message.captureId, sentence_id: message.sentenceId, saved: message.saved });
    if (typeof value?.saved !== "boolean") throw new Error("Invalid local favorite acknowledgement");
    return { ok: true, saved: value.saved };
  }
  return openReviewClip(sender.tab.id, sender.url || sender.tab.url, message.captureId);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.target !== "worker") return;
  if (["review-library", "review-favorite", "review-open"].includes(message.type)) {
    if (sender.id !== chrome.runtime.id || !Number.isSafeInteger(sender.tab?.id) || sender.tab.id <= 0 || sender.frameId !== 0 ||
        !validVideoUrl(sender.url || sender.tab.url) || (message.type !== "review-library" && !validCaptureId(message.captureId)) ||
        (message.type === "review-favorite" && (!validSentenceId(message.sentenceId) || typeof message.saved !== "boolean"))) {
      sendResponse({ ok: false, error: "复习请求无效。" });
      return;
    }
    handleReview(message, sender).then(sendResponse).catch((error) => sendResponse(error?.code === "backend_upgrade_required"
      ? { ok: false, code: "backend_upgrade_required", error: "请重启本机后端，加载收藏与复习功能。" }
      : { ok: false, error: "无法完成本机复习操作，请重试。" }));
    return true;
  }
  if (message.type === "capture-rearm") {
    if (sender.id !== chrome.runtime.id || !Number.isSafeInteger(sender.tab?.id) || sender.tab.id <= 0 ||
        sender.frameId !== 0 || !validVideoUrl(sender.url) ||
        !Number.isSafeInteger(message.requestId) || message.requestId <= 0 || message.requestId > 1_000_000_000) {
      sendResponse({ ok: false, error: REARM_FALLBACK });
      return;
    }
    // Derive the target exclusively from the isolated page's verified sender.
    // openAgent rechecks the active tab after any in-progress release settles.
    openAgent({ id: sender.tab.id, url: sender.url }, { freshOnly: true, rearmRequestId: message.requestId })
      .then(sendResponse).catch(() => sendResponse({ ok: false, error: REARM_FALLBACK }));
    return true;
  }
  if (message.type === "capture-learning-status" || message.type === "capture-learning-load") {
    if (sender.id !== chrome.runtime.id || !sender.tab || sender.frameId !== 0 ||
        !validVideoUrl(sender.url || sender.tab.url) ||
        (message.type === "capture-learning-status" && !validCaptureId(message.captureId))) {
      sendResponse({ ok: false, error: "学习内容请求无效。" });
      return;
    }
    const action = async () => {
      if (message.type === "capture-learning-status") return learningStatus(`/captures/${message.captureId}/learning`, message.captureId);
      const currentTab = await chrome.tabs.get(sender.tab.id);
      if (!validVideoUrl(currentTab.url)) return { ok: false, error: "请在当前 YouTube 视频读取学习内容。" };
      return learningStatus(`/learning/latest?url=${encodeURIComponent(currentTab.url)}`);
    };
    action().then(sendResponse).catch(() => sendResponse({ ok: false, error: "无法读取本机学习内容。" }));
    return true;
  }
  if (message.type === "capture-transcription-status") {
    if (sender.id !== chrome.runtime.id || !sender.tab || sender.frameId !== 0 ||
        !validVideoUrl(sender.url || sender.tab.url) ||
        !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(message.captureId || "")) {
      sendResponse({ ok: false, error: "转写状态请求无效。" });
      return;
    }
    transcriptionStatus(message.captureId).then(sendResponse);
    return true;
  }
  if (message.type === "capture-replay") {
    if (sender.id !== chrome.runtime.id || !sender.tab || sender.frameId !== 0 || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(message.captureId || "")) return;
    chrome.tabs.create({ url: `${chrome.runtime.getURL("replay.html")}?id=${encodeURIComponent(message.captureId)}` })
      .then(() => sendResponse({ ok: true })).catch(() => sendResponse({ ok: false, error: "无法打开回放页面" }));
    return true;
  }
  handleMessage(message, sender).then(sendResponse).catch(async (error) => {
    const directCode = adFailureCode(error.code);
    const session = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
    if (session && session.sessionId === message.sessionId && sender.id === chrome.runtime.id &&
        ((sender.tab?.id === session.tabId && sender.frameId === 0) || (!sender.tab && sender.url === chrome.runtime.getURL("offscreen.html")))) {
      const code = directCode || adFailureCode(session.failureCode);
      await within(offscreenMessage({ type: "capture-fail", sessionId: session.sessionId, error: error.message,
        ...(code ? { failure: { code, message: "当前播放器正在播放广告。" } } : {}) }),
        4000, "采集错误状态确认超时").catch(() => {});
      const latest = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
      if (latest?.sessionId === session.sessionId) {
        latest.phase = "failed";
        if (code) latest.failureCode = code;
        await chrome.storage.session.set({ [SESSION_KEY]: latest });
        await pageMessage(session.tabId, { type: "capture-failed", sessionId: session.sessionId, ...(code ? { code } : {}) }).catch(() => {});
      }
    }
    sendResponse({ ok: false, error: error.message, ...(directCode ? { code: directCode } : {}) });
  });
  return true;
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete") void restorePendingReview({ id: tabId, url: tab.url }).catch(() => {});
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void chrome.storage.session.remove(pendingReviewKey(tabId)).catch(() => {});
  void chrome.storage.session.get(SESSION_KEY).then(async (data) => {
    const session = data[SESSION_KEY];
    if (session?.tabId !== tabId) return;
    await offscreenMessage({ type: "capture-abort", sessionId: session.sessionId }).catch(() => {});
    await finish(session);
  });
});
