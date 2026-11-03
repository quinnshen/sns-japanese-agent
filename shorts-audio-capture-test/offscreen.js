let busy = false;

chrome.runtime.onMessage.addListener((message) => {
  if (message.target !== "offscreen" || message.type !== "capture-five-seconds") return;
  if (busy) return;
  busy = true;
  void capture(message).finally(() => { busy = false; });
});

async function capture({ streamId, tabId }) {
  let stream;
  let context;
  let sampleTimer;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId },
      },
      video: false,
    });

    context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);
    source.connect(context.destination); // Keep YouTube audible during capture.
    await context.resume();

    const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus" : "audio/webm";
    const recorder = new MediaRecorder(stream, { mimeType });
    const chunks = [];
    recorder.ondataavailable = (event) => {
      if (event.data.size) chunks.push(event.data);
    };
    const stopped = new Promise((resolve, reject) => {
      recorder.onstop = resolve;
      recorder.onerror = (event) => reject(event.error || new Error("MediaRecorder error"));
    });

    let maxRms = 0;
    const samples = new Float32Array(analyser.fftSize);
    sampleTimer = setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      let power = 0;
      for (const value of samples) power += value * value;
      maxRms = Math.max(maxRms, Math.sqrt(power / samples.length));
    }, 100);

    recorder.start();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    clearInterval(sampleTimer);
    recorder.stop();
    await stopped;

    const blob = new Blob(chunks, { type: mimeType });
    const result = { bytes: blob.size, maxRms, audioTracks: stream.getAudioTracks().length };
    stream.getTracks().forEach((track) => track.stop());
    stream = undefined;
    await context.close();
    context = undefined;
    await chrome.runtime.sendMessage({
      target: "worker", type: "capture-progress", tabId,
      text: `5 秒音频已录到（${blob.size} 字节），正在请求本机 Google 转写服务…`,
    });

    let transcription;
    let transcriptionError;
    try {
      const response = await fetch("http://127.0.0.1:8765/transcribe", {
        method: "POST",
        headers: { "Content-Type": mimeType, "X-Japanese-Agent": "capture-test" },
        body: blob,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      transcription = payload;
    } catch (error) {
      transcriptionError = error.message;
    }
    await chrome.runtime.sendMessage({
      target: "worker", type: "capture-result", tabId,
      result, transcription, transcriptionError,
    });
  } catch (error) {
    await chrome.runtime.sendMessage({
      target: "worker", type: "capture-result", tabId, error: error.message,
    });
  } finally {
    clearInterval(sampleTimer);
    stream?.getTracks().forEach((track) => track.stop());
    await context?.close();
  }
}
