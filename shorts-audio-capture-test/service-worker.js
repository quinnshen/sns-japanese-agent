let creatingOffscreen;

async function ensureOffscreen() {
  const url = chrome.runtime.getURL("offscreen.html");
  const existing = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [url],
  });
  if (existing.length) return;

  if (!creatingOffscreen) {
    creatingOffscreen = chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA"],
      justification: "Measure five seconds of audio from the current tab",
    }).finally(() => { creatingOffscreen = undefined; });
  }
  await creatingOffscreen;
}

async function showStatus(tabId, text, color = "#176b45") {
  await chrome.action.setBadgeText({ tabId, text: color === "#b42318" ? "ERR" : "TEST" });
  await chrome.action.setBadgeBackgroundColor({ tabId, color });
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (message, accent) => {
        let box = document.getElementById("japanese-learning-audio-test-status");
        if (!box) {
          box = document.createElement("div");
          box.id = "japanese-learning-audio-test-status";
          Object.assign(box.style, {
            position: "fixed", top: "16px", right: "16px", zIndex: "2147483647",
            maxWidth: "360px", padding: "14px 18px", borderRadius: "10px",
            background: "#111827", color: "white", font: "14px/1.5 sans-serif",
            boxShadow: "0 4px 18px #0008", whiteSpace: "pre-line",
          });
          document.body.appendChild(box);
        }
        box.style.borderLeft = `5px solid ${accent}`;
        box.textContent = message;
      },
      args: [text, color],
    });
  } catch (error) {
    console.warn("Could not show page status:", error);
  }
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !/^https:\/\/www\.youtube\.com\/(shorts|watch)/.test(tab.url || "")) {
    if (tab.id) await showStatus(tab.id, "请先打开 YouTube 视频页面。", "#b42318");
    return;
  }

  try {
    await showStatus(tab.id, "正在捕获当前标签页的 5 秒音频…");
    await ensureOffscreen();
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    await chrome.runtime.sendMessage({
      target: "offscreen", type: "capture-five-seconds", streamId, tabId: tab.id,
    });
  } catch (error) {
    await showStatus(tab.id, `捕获未启动：${error.message}`, "#b42318");
  }
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.target !== "worker") return;
  if (message.type === "capture-progress") {
    void showStatus(message.tabId, message.text);
    return;
  }
  if (message.type !== "capture-result") return;
  const { tabId, result, error, transcription, transcriptionError } = message;
  const captureText = error
    ? `捕获失败：${error}`
    : `5 秒捕获完成\n音频数据：${result.bytes} 字节；最大音量 RMS：${result.maxRms.toFixed(4)}；音轨数：${result.audioTracks}`;
  const transcriptionText = transcription
    ? `\n\nGoogle 日语转写：${transcription.text || "（未识别到语音）"}\n词时间：${transcription.words.map(w => `${w.word} ${w.start}s–${w.end}s`).join("、") || "（无）"}\n时间相对本次录音起点。`
    : transcriptionError ? `\n\nGoogle 转写尚未完成：${transcriptionError}` : "";
  void showStatus(tabId, captureText + transcriptionText, error ? "#b42318" : "#176b45");
  void chrome.offscreen.closeDocument().catch(() => {});
});
