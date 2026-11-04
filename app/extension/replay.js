const API = "http://127.0.0.1:8766";
const id = new URL(location.href).searchParams.get("id") || "";
const status = document.getElementById("status");
const audio = document.getElementById("audio");
const retry = document.getElementById("retry");
let blobUrl;
let controller;
async function load() {
  if (!/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(id)) { status.textContent = "录音任务 ID 无效。"; return; }
  retry.hidden = true;
  status.textContent = "正在读取本机录音…";
  controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60_000);
  try {
    // Fetch from the extension origin, never expose the private recording URL to the YouTube page.
    const response = await fetch(`${API}/captures/${id}/audio`, { signal: controller.signal, credentials: "omit" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    if (!blob.size || blob.size > 200_000_000) throw new Error("音频为空或超出大小限制");
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    blobUrl = URL.createObjectURL(blob);
    audio.src = blobUrl;
    audio.hidden = false;
    status.textContent = `任务 ${id} · ${(blob.size / 1024).toFixed(1)} KB`;
  } catch (error) {
    status.textContent = `无法读取录音：${error.message}。请确认本机后端正在运行。`;
    retry.hidden = false;
  } finally { clearTimeout(timer); }
}
audio.addEventListener("error", () => { status.textContent = "浏览器无法解码这份录音；请保留原始批次进行检查。保存完整不等于已经通过解码验收。"; });
retry.addEventListener("click", () => { void load(); });
window.addEventListener("pagehide", () => { controller?.abort(); if (blobUrl) URL.revokeObjectURL(blobUrl); });
void load();
