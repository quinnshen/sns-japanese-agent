const SESSION_KEY = "activeCapture";
const API = "http://127.0.0.1:8766";
let creatingOffscreen;
let opening = false;

async function within(promise, milliseconds, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), milliseconds);
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

async function status(tabId, text, error = false) {
  await chrome.action.setBadgeText({ tabId, text: error ? "ERR" : "JA" });
  await chrome.action.setBadgeBackgroundColor({ tabId, color: error ? "#b42318" : "#176b45" });
  await pageMessage(tabId, { type: "status", text, error }).catch(() => {});
}

async function finish(session, payload = {}) {
  const stored = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
  if (stored?.sessionId !== session.sessionId) return;
  await within(pageMessage(session.tabId, { type: "capture-reset", sessionId: session.sessionId, ...payload }), 3000, "面板关闭超时").catch(() => {});
  await chrome.storage.session.remove(SESSION_KEY);
  await within(chrome.offscreen.closeDocument(), 3000, "采集环境关闭超时").catch(() => {});
}

function validVideoUrl(value) {
  try {
    const url = new URL(value);
    return url.origin === "https://www.youtube.com" &&
      ((url.pathname === "/watch" && /^[\w-]{11}$/.test(url.searchParams.get("v") || "")) || /^\/shorts\/[\w-]{11}\/?$/.test(url.pathname));
  } catch (_) { return false; }
}

async function openAgent(tab) {
  if (opening) return;
  opening = true;
  let session;
  try {
    if (!tab.id || !validVideoUrl(tab.url)) throw new Error("请先打开 YouTube 视频或 Shorts 页面。");
    const active = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
    if (active) {
      const contexts = await within(chrome.runtime.getContexts({
        contextTypes: ["OFFSCREEN_DOCUMENT"], documentUrls: [chrome.runtime.getURL("offscreen.html")],
      }), 3000, "检查采集环境超时");
      if (contexts.length) {
        await chrome.scripting.executeScript({ target: { tabId: active.tabId }, files: ["page.js"] });
        await pageMessage(active.tabId, { type: "capture-show" }).catch(() => {});
        if (active.tabId !== tab.id) await status(tab.id, "另一个标签页已有采集任务，请先结束该任务。", true);
        else {
          const inspected = await within(offscreenMessage({ type: "capture-inspect", sessionId: active.sessionId }), 3000,
            "无法确认采集状态；请点击面板的中断按钮释放后重试。");
          if (inspected?.ok && inspected.phase === "ready") {
            session = active;
            await pageMessage(tab.id, { type: "capture-configuring", ...active });
            const reply = await within(pageMessage(tab.id, { type: "capture-configure", ...active }), 12_000,
              "视频信息读取超时，请先正常播放视频后重试。");
            if (!reply?.ok) throw new Error(reply?.error || "请先正常播放视频后重试。");
            active.phase = "ready";
            await checkCurrent(active);
            await chrome.storage.session.set({ [SESSION_KEY]: active });
            await status(tab.id, "音频已准备。选择范围后点击开始；尚未录音。");
          } else if (inspected?.ok && inspected.phase === "failed") {
            await pageMessage(tab.id, { type: "capture-failed", sessionId: active.sessionId }).catch(() => {});
            await status(tab.id, `上次采集已停止，待确认 ${inspected.pendingBatches || 0} 批仍保留；请点击中断按钮明确释放后重试。`, true);
          } else if (!inspected?.ok) {
            await status(tab.id, "无法确认采集状态；请点击面板的中断按钮释放后重试。", true);
          }
        }
        return;
      }
      // No offscreen document means its in-memory queue is already gone; keep disk batches untouched.
      await chrome.storage.session.remove(SESSION_KEY);
    }
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["page.js"] });
    await status(tab.id, "正在连接本机采集服务…");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    let health;
    try {
      const response = await fetch(`${API}/health`, { signal: controller.signal });
      if (!response.ok) throw new Error("本机服务不可用");
      health = await response.json();
    } finally { clearTimeout(timeout); }
    if (!health.capabilities?.capture_batches) throw new Error("请启动支持分批采集的新版本本机后端。");
    session = { tabId: tab.id, sessionId: crypto.randomUUID(), sourceUrl: tab.url, phase: "preparing" };
    await chrome.storage.session.set({ [SESSION_KEY]: session });
    await pageMessage(tab.id, { type: "capture-configuring", ...session });
    await status(tab.id, "正在准备音频采集环境…若视频尚未加载，请先让它正常播放。");
    await ensureOffscreen();
    await checkCurrent(session);
    await status(tab.id, "正在请求当前标签页的音频访问…");
    // Obtain and immediately consume the short-lived stream ID during the extension action.
    // The recorder starts only after the user confirms the selected range in the panel.
    const streamId = await within(chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id }), 10_000,
      "请求标签页音频访问超时。请先正常播放视频，再重新点击扩展图标。");
    await checkCurrent(session);
    await status(tab.id, "正在连接标签页音频与扬声器…");
    const armed = await within(offscreenMessage({ type: "capture-arm", ...session, streamId }), 16_000,
      "音频准备超时。请先正常播放视频，再重新点击扩展图标。");
    if (!armed?.ok) throw new Error(armed?.error || "无法准备标签页音频");
    await checkCurrent(session);
    session.phase = "ready";
    await chrome.storage.session.set({ [SESSION_KEY]: session });
    await status(tab.id, "音频已连接，正在读取视频信息…");
    const reply = await within(pageMessage(tab.id, { type: "capture-configure", ...session }), 12_000,
      "视频信息读取超时，请先正常播放视频后重试。");
    if (!reply?.ok) throw new Error(reply?.error || "无法读取视频；请先让视频正常播放后重试。");
    await checkCurrent(session);
    session.phase = "ready";
    await chrome.storage.session.set({ [SESSION_KEY]: session });
    await status(tab.id, "选择采集范围和上传间隔，再点击开始。尚未录音，不调用云端服务。");
  } catch (error) {
    if (tab.id) await status(tab.id, `准备失败：${error.message}`, true);
    if (session) {
      await within(offscreenMessage({ type: "capture-abort", sessionId: session.sessionId }), 3000, "音频准备取消超时").catch(() => {});
      await finish(session);
    }
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
      if (!prepared?.ok) throw new Error(prepared?.error || "视频准备失败");
      await checkCurrent(session);
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
        confirmed = Boolean(reply?.ok);
      } catch (_) { /* Closing the explicitly cancelled document is still required. */ }
      finally { await finish(session); }
      await status(session.tabId, confirmed
        ? "已中断并释放浏览器待确认数据。后端已确认的批次仍保留在本机。"
        : "已释放浏览器采集环境，但后端中断状态未确认；已保存的批次保留，请检查本机任务。", !confirmed);
      break;
    }
    case "page-error": {
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
      await pageMessage(session.tabId, { type: "capture-saved", sessionId: session.sessionId, manifest: message.manifest }).catch(() => {});
      await status(session.tabId, "所有批次已保存并合并，可回放检查。此次未调用 Google 或 GPT。");
      await finish(session, { complete: true });
      break;
    case "capture-expired":
    case "capture-error":
      await pageMessage(session.tabId, { type: "capture-stop-playback", sessionId: session.sessionId }).catch(() => {});
      session.phase = "failed";
      await chrome.storage.session.set({ [SESSION_KEY]: session });
      await pageMessage(session.tabId, { type: "capture-failed", sessionId: session.sessionId }).catch(() => {});
      await status(session.tabId, `采集未完成：${message.error}。已确认批次保留在本机。` +
        (message.pendingBatches
          ? `待确认 ${message.pendingBatches} 批（${message.pendingBytes || 0} 字节）保留在浏览器，点击“中断／释放音频”后丢弃。`
          : "没有待确认批次。点击“中断／释放音频”结束本次任务，再点击扩展图标重新采集。"), true);
      break;
  }
  return { ok: true };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.target !== "worker") return;
  if (message.type === "capture-replay") {
    if (sender.id !== chrome.runtime.id || !sender.tab || sender.frameId !== 0 || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(message.captureId || "")) return;
    chrome.tabs.create({ url: `${chrome.runtime.getURL("replay.html")}?id=${encodeURIComponent(message.captureId)}` })
      .then(() => sendResponse({ ok: true })).catch(() => sendResponse({ ok: false, error: "无法打开回放页面" }));
    return true;
  }
  handleMessage(message, sender).then(sendResponse).catch(async (error) => {
    const session = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
    if (session && session.sessionId === message.sessionId && sender.id === chrome.runtime.id &&
        ((sender.tab?.id === session.tabId && sender.frameId === 0) || (!sender.tab && sender.url === chrome.runtime.getURL("offscreen.html")))) {
      await within(offscreenMessage({ type: "capture-fail", sessionId: session.sessionId, error: error.message }),
        4000, "采集错误状态确认超时").catch(() => {});
      session.phase = "failed";
      await chrome.storage.session.set({ [SESSION_KEY]: session });
      await pageMessage(session.tabId, { type: "capture-failed", sessionId: session.sessionId }).catch(() => {});
    }
    sendResponse({ ok: false, error: error.message });
  });
  return true;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void chrome.storage.session.get(SESSION_KEY).then(async (data) => {
    const session = data[SESSION_KEY];
    if (session?.tabId !== tabId) return;
    await offscreenMessage({ type: "capture-abort", sessionId: session.sessionId }).catch(() => {});
    await finish(session);
  });
});
