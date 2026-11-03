const SESSION_KEY = "activeCapture";
let creatingOffscreen;

async function ensureOffscreen() {
  const url = chrome.runtime.getURL("offscreen.html");
  const existing = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"], documentUrls: [url],
  });
  if (existing.length) return;
  if (!creatingOffscreen) {
    creatingOffscreen = chrome.offscreen.createDocument({
      url: "offscreen.html", reasons: ["USER_MEDIA"],
      justification: "Record the selected YouTube video's audio until playback ends",
    }).finally(() => { creatingOffscreen = undefined; });
  }
  await creatingOffscreen;
}

async function pageMessage(tabId, payload) {
  return chrome.tabs.sendMessage(tabId, payload);
}

async function status(tabId, text, error = false) {
  await chrome.action.setBadgeText({ tabId, text: error ? "ERR" : "JA" });
  await chrome.action.setBadgeBackgroundColor({ tabId, color: error ? "#b42318" : "#176b45" });
  try { await pageMessage(tabId, { type: "status", text, error }); } catch (_) { /* tab closed */ }
}

async function finish(session) {
  const stored = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
  if (stored?.sessionId === session.sessionId) {
    await chrome.storage.session.remove(SESSION_KEY);
  }
  await chrome.offscreen.closeDocument().catch(() => {});
}

async function openAgent(tab, forceCapture = false) {
  if (!tab.id || !/^https:\/\/www\.youtube\.com\/(shorts\/|watch\?)/.test(tab.url || "")) {
    if (tab.id) await status(tab.id, "请先打开 YouTube 视频或 Shorts 页面。", true);
    return;
  }
  const existing = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
  if (existing) {
    await status(tab.id, "已有视频正在录制或转写，请等待它完成。", true);
    return;
  }
  if (!forceCapture) {
    try {
      const response = await fetch(`http://127.0.0.1:8766/results?url=${encodeURIComponent(tab.url)}`);
      if (response.ok) {
        const { result } = await response.json();
        await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["page.js"] });
        await pageMessage(tab.id, { type: "result", result });
        await status(tab.id, `已加载本机保存的 ${result.sentences.length} 句${result.status === "enriched" ? "双语台词和解析" : "转写"}。`, Boolean(result.enrichment_error));
        return;
      }
    } catch (_) { /* The normal capture path reports local-service failures. */ }
  }
  const session = { tabId: tab.id, sessionId: crypto.randomUUID() };
  await chrome.storage.session.set({ [SESSION_KEY]: session });
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["page.js"] });
    await status(tab.id, "正在准备视频与音频捕获…");
    const prepared = await pageMessage(tab.id, { type: "prepare", sessionId: session.sessionId });
    if (!prepared?.ok) throw new Error(prepared?.error || "视频准备失败");
    await ensureOffscreen();
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    await chrome.runtime.sendMessage({
      target: "offscreen", type: "capture-start", ...session, streamId,
      duration: prepared.duration, url: prepared.url,
    });
  } catch (error) {
    await status(tab.id, `启动失败：${error.message}`, true);
    await finish(session);
  }
}

chrome.action.onClicked.addListener((tab) => { void openAgent(tab); });

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.target !== "worker") return;
  if (message.type === "recapture" && sender.tab?.id) {
    void chrome.tabs.get(sender.tab.id).then((tab) => openAgent(tab, true))
      .catch((error) => status(sender.tab.id, `重新录制失败：${error.message}`, true));
    return;
  }
  void (async () => {
    const session = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
    if (!session || session.sessionId !== message.sessionId) return;
    const { tabId } = session;
    if (sender.tab && sender.tab.id !== tabId) return;
    switch (message.type) {
      case "capture-ready": {
        await status(tabId, "正在从视频开头录制音频；请保持播放，不要拖动进度条。");
        const reply = await pageMessage(tabId, { type: "play", sessionId: session.sessionId });
        if (!reply?.ok) throw new Error(reply?.error || "视频无法播放");
        break;
      }
      case "page-started":
        await chrome.runtime.sendMessage({ target: "offscreen", type: "playback-started", ...session, startedAt: message.startedAt });
        break;
      case "page-ended":
        await status(tabId, "视频录制完成，正在上传给本机服务…");
        await chrome.runtime.sendMessage({ target: "offscreen", type: "capture-finish", ...session, startedAt: message.startedAt });
        break;
      case "page-error":
        await chrome.runtime.sendMessage({ target: "offscreen", type: "capture-abort", ...session });
        await status(tabId, `录制中断：${message.error}`, true);
        await finish(session);
        break;
      case "capture-progress":
        await status(tabId, message.text);
        break;
      case "capture-complete":
        await pageMessage(tabId, { type: "result", result: message.result });
        await status(tabId, message.result.enrichment_error
          ? `已保存 ${message.result.sentences.length} 句日语转写；解析未全部完成：${message.result.enrichment_error}`
          : `已保存 ${message.result.sentences.length} 句${message.result.status === "enriched" ? "日中双语及解析" : "日语转写"}。点击句子可跳转播放。`,
          Boolean(message.result.enrichment_error));
        await finish(session);
        break;
      case "capture-error":
        await status(tabId, `转写失败：${message.error}`, true);
        await finish(session);
        break;
    }
  })().catch(async (error) => {
    const session = (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY];
    if (!session) return;
    await chrome.runtime.sendMessage({ target: "offscreen", type: "capture-abort", ...session }).catch(() => {});
    await status(session.tabId, `处理失败：${error.message}`, true);
    await finish(session);
  });
});
