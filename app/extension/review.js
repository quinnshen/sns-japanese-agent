(() => {
  if (window.__snsJapaneseReview) return;

  const captureIdValid = (id) => typeof id === "string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(id);
  const sentenceIdValid = (id) => typeof id === "string" && /^[a-zA-Z0-9_-]{1,100}$/.test(id);
  const validTime = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER;
  const favoriteKey = (captureId, sentenceId) => `${captureId}:${sentenceId}`;
  const BACKEND_UPGRADE_MESSAGE = "请重启本机后端，加载收藏与复习功能。";

  function videoId(source) {
    try {
      const url = new URL(source);
      if (url.origin !== "https://www.youtube.com" || url.username || url.password) return null;
      const id = url.pathname === "/watch" ? url.searchParams.get("v") : /^\/shorts\/([\w-]{11})\/?$/.exec(url.pathname)?.[1];
      return /^[\w-]{11}$/.test(id || "") ? id : null;
    } catch (_) { return null; }
  }

  function formatTime(value) {
    const tenths = Math.round(value * 10);
    const seconds = Math.floor(tenths / 10);
    const minutes = Math.floor(seconds / 60);
    const fraction = tenths % 10;
    const tail = `${String(seconds % 60).padStart(2, "0")}${fraction ? `.${fraction}` : ""}`;
    return minutes < 60 ? `${minutes}:${tail}` : `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${tail}`;
  }

  function formatDate(value) {
    if (typeof value !== "string" && !validTime(value)) return "";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isFinite(date.getTime())
      ? new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date) : "";
  }

  function node(tag, label, className) {
    const item = document.createElement(tag);
    if (label !== undefined) item.textContent = label;
    if (className) item.className = className;
    return item;
  }

  function button(label, className) {
    const item = node("button", label, className);
    item.type = "button";
    return item;
  }

  function create({ send, onOpenClip }) {
    const element = node("section", undefined, "sns-review");
    element.hidden = true;
    element.setAttribute("aria-label", "复习");
    const style = node("style");
    style.textContent = `
      .sns-review { flex:1 1 0; min-height:0; display:flex; flex-direction:column; }
      .sns-review[hidden], .sns-review [hidden] { display:none; }
      .sns-review-nav { display:flex; gap:6px; padding:10px 12px; border-bottom:1px solid #dbe7df; }
      .sns-review button, .sns-review-source, .sns-favorite-button {
        border:1px solid #bdcec1; border-radius:6px; background:white; color:#355944;
        padding:4px 8px; font:inherit; font-size:12px; text-align:left; cursor:pointer;
      }
      .sns-favorite-button { margin:0 9px 9px 0; }
      .sns-review button[aria-pressed="true"], .sns-favorite-button[aria-pressed="true"] { background:#e6f1e8; border-color:#7ca88a; }
      .sns-review button:focus-visible, .sns-favorite-button:focus-visible, .sns-review-source:focus-visible { outline:2px solid #527b61; outline-offset:2px; }
      .sns-review-feedback { margin:0; padding:8px 12px 0; color:#4a6554; font-size:12px; }
      .sns-review-feedback:empty { display:none; }
      .sns-review-content { flex:1 1 0; min-height:0; overflow:auto; padding:4px 12px 14px; }
      .sns-review-meta { color:#526d5c; font-size:12px; margin:4px 0; }
      .sns-review h3 { margin:10px 0 5px; font-size:13px; }
      .sns-review .sns-review-clip { display:block; width:100%; margin:5px 0; padding:8px; }
      .sns-review-clip strong, .sns-review-clip span { display:block; }
      .sns-review-card { padding:10px 9px; margin:6px 0; border:1px solid #dbe7df; border-radius:8px; background:white; overflow-wrap:anywhere; }
      .sns-review-card p, .sns-review-card ul { margin:4px 0; }
      .sns-review-card ul { padding-left:19px; }
      .sns-review-card summary { cursor:pointer; color:#355944; margin:6px 0; }
      .sns-review-actions { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
      .sns-review-source { text-decoration:none; }
    `;
    const nav = node("div", undefined, "sns-review-nav");
    nav.setAttribute("role", "group");
    nav.setAttribute("aria-label", "复习类别");
    const tabs = { clips: button("最近片段"), favorites: button("收藏句子") };
    nav.append(tabs.clips, tabs.favorites);
    const feedback = node("p", "", "sns-review-feedback");
    feedback.setAttribute("role", "status");
    const content = node("div", undefined, "sns-review-content");
    element.append(style, nav, feedback, content);

    let tab = "clips";
    let clips = [];
    let favorites = [];
    let savedKeys = new Set();
    let visibleRequest = 0;
    let openingClip = false;
    const pendingFavorites = new Set();
    const favoriteFailures = new Map();
    let libraryFailure = "";
    let libraryQueue = Promise.resolve();

    function enqueue(work) {
      const task = libraryQueue.then(work);
      libraryQueue = task.catch(() => {});
      return task;
    }

    function message(type, data = {}) {
      return send({ target: "worker", type, ...data });
    }

    function syncButton(item) {
      const { captureId, sentenceId } = item.dataset;
      const key = favoriteKey(captureId, sentenceId);
      const saved = savedKeys.has(key);
      const failure = favoriteFailures.get(key);
      item.setAttribute("aria-pressed", String(saved));
      item.disabled = pendingFavorites.has(key) || !captureIdValid(captureId) || !sentenceIdValid(sentenceId);
      item.textContent = pendingFavorites.has(key) ? "保存中…" : failure ? "请重启本机后端" : saved ? "已收藏" : "收藏";
      item.title = failure || (saved ? "取消收藏这句" : "收藏这句及完整解析");
    }

    function syncFavoriteButtons() {
      for (const item of element.getRootNode?.()?.querySelectorAll?.(".sns-favorite-button") || []) syncButton(item);
      for (const item of content.querySelectorAll?.(".sns-review-remove") || []) {
        item.disabled = pendingFavorites.has(item.dataset.favoriteKey);
        item.textContent = item.disabled ? "取消中…" : favoriteFailures.has(item.dataset.favoriteKey) ? "请重启本机后端" : "取消收藏";
        item.title = favoriteFailures.get(item.dataset.favoriteKey) || "";
      }
    }

    function syncClipButtons() {
      for (const item of content.querySelectorAll?.(".sns-review-clip") || []) item.disabled = openingClip;
    }

    function refreshFavorites() {
      return enqueue(async () => {
        libraryFailure = "";
        try {
          const reply = await message("review-library");
          if (!reply?.ok || !Array.isArray(reply.clips) || !Array.isArray(reply.favorites)) {
            if (reply?.code === "backend_upgrade_required") libraryFailure = BACKEND_UPGRADE_MESSAGE;
            return false;
          }
          favoriteFailures.clear();
          const seen = new Set();
          clips = reply.clips.filter((clip) => {
            if (!captureIdValid(clip?.capture_id) || seen.has(clip.capture_id) || !videoId(clip.source_url) ||
                !validTime(clip.range_start_sec) || !validTime(clip.range_end_sec) || clip.range_end_sec <= clip.range_start_sec ||
                !Number.isSafeInteger(clip.sentences_done) || clip.sentences_done < 1) return false;
            seen.add(clip.capture_id);
            return true;
          });
          savedKeys = new Set();
          favorites = reply.favorites.filter((item) => {
            const sentence = item?.sentence;
            const study = sentence?.study;
            if (!captureIdValid(item?.capture_id) || !sentenceIdValid(item.sentence_id) || sentence?.id !== item.sentence_id ||
                !videoId(item.source_url) || study?.style_version !== 2 || typeof study.reading_text !== "string" || !study.reading_text ||
                typeof sentence.translation_zh !== "string" || !sentence.translation_zh || !Array.isArray(study.key_points) ||
                study.key_points.some((point) => typeof point !== "string")) return false;
            const key = favoriteKey(item.capture_id, item.sentence_id);
            if (savedKeys.has(key)) return false;
            savedKeys.add(key);
            return true;
          });
          syncFavoriteButtons();
          return true;
        } catch (_) { return false; }
      });
    }

    async function changeFavorite(captureId, sentenceId, saved, item) {
      const key = favoriteKey(captureId, sentenceId);
      if (!captureIdValid(captureId) || !sentenceIdValid(sentenceId) || pendingFavorites.has(key)) return false;
      favoriteFailures.delete(key);
      pendingFavorites.add(key);
      syncFavoriteButtons();
      item.disabled = true;
      const changed = await enqueue(async () => {
        try {
          const reply = await message("review-favorite", { captureId, sentenceId, saved });
          if (!reply?.ok || reply.saved !== saved) {
            if (reply?.code === "backend_upgrade_required") favoriteFailures.set(key, BACKEND_UPGRADE_MESSAGE);
            return false;
          }
          if (saved) savedKeys.add(key);
          else {
            savedKeys.delete(key);
            favorites = favorites.filter((favorite) => favoriteKey(favorite.capture_id, favorite.sentence_id) !== key);
          }
          return true;
        } catch (_) { return false; }
      });
      pendingFavorites.delete(key);
      syncFavoriteButtons();
      return changed;
    }

    function favoriteButton(captureId, sentenceId) {
      const item = button("收藏", "sns-favorite-button");
      item.dataset.captureId = captureId || "";
      item.dataset.sentenceId = sentenceId || "";
      item.setAttribute("aria-label", "收藏这句");
      syncButton(item);
      item.addEventListener("click", async (event) => {
        if (item.disabled || event?.isTrusted === false) return;
        const saved = !savedKeys.has(favoriteKey(captureId, sentenceId));
        const changed = await changeFavorite(captureId, sentenceId, saved, item);
        syncButton(item);
        if (!changed && !favoriteFailures.has(favoriteKey(captureId, sentenceId))) item.textContent = saved ? "收藏失败，重试" : "取消失败，重试";
      });
      return item;
    }

    function renderClips() {
      if (!clips.length) {
        content.append(node("p", "还没有可复习的片段。生成学习内容后，就能在这里选择。"));
        return;
      }
      const groups = new Map();
      for (const clip of clips) {
        const id = videoId(clip.source_url);
        if (!groups.has(id)) {
          const group = node("section");
          group.append(node("h3", `视频 · ${id}`));
          groups.set(id, group);
          content.append(group);
        }
        const item = button(undefined, "sns-review-clip");
        const duration = `${formatTime(clip.range_start_sec)} – ${formatTime(clip.range_end_sec)}`;
        const updated = formatDate(clip.updated_at);
        item.disabled = openingClip;
        item.append(node("strong", duration), node("span", `${clip.sentences_done} 句可复习${updated ? ` · ${updated}` : ""}`, "sns-review-meta"));
        item.addEventListener("click", async (event) => {
          if (openingClip || element.hidden || event?.isTrusted === false) return;
          openingClip = true;
          syncClipButtons();
          feedback.textContent = "正在打开选中的片段…";
          const request = visibleRequest;
          try {
            const result = await onOpenClip(clip.capture_id);
            if (result?.ok === false) throw new Error();
            if (!element.hidden && request === visibleRequest) feedback.textContent = result?.navigated ? "正在打开原视频…" : "";
          } catch (_) {
            if (!element.hidden && request === visibleRequest) feedback.textContent = "无法打开这个片段。它可能已清理，请刷新复习列表。";
          } finally {
            openingClip = false;
            syncClipButtons();
          }
        });
        groups.get(id).append(item);
      }
    }

    function renderFavorites() {
      if (!favorites.length) {
        content.append(node("p", "精读时点「收藏」，就能在这里复习句子和解析。"));
        return;
      }
      for (const item of favorites) {
        const { sentence } = item;
        const id = videoId(item.source_url);
        const study = sentence.study;
        const hasTime = validTime(sentence.start) && validTime(sentence.end) && sentence.end >= sentence.start;
        const card = node("article", undefined, "sns-review-card");
        const label = `视频 · ${id} · ${hasTime ? formatTime(sentence.start) : "时间待确认"}`;
        card.append(node("p", label, "sns-review-meta"), node("p", study.reading_text), node("p", sentence.translation_zh));
        const details = node("details");
        details.append(node("summary", "完整解析"));
        if (study.key_points.length) {
          const list = node("ul");
          for (const point of study.key_points) list.append(node("li", point));
          details.append(list);
        }
        if (typeof study.context_note === "string" && study.context_note) details.append(node("p", `语境待确认：${study.context_note}`));
        if (typeof study.asr_doubt === "string" && study.asr_doubt) details.append(node("p", `ASR 存疑：${study.asr_doubt}`));
        const actions = node("div", undefined, "sns-review-actions");
        const source = node("a", hasTime ? "回听原视频" : "查看原视频", "sns-review-source");
        const url = new URL(`https://www.youtube.com/watch?v=${id}`);
        if (hasTime) url.searchParams.set("t", String(Math.floor(sentence.start)));
        source.href = url.href;
        source.target = "_blank";
        source.rel = "noopener noreferrer";
        source.addEventListener("click", (event) => { if (event?.isTrusted === false) event.preventDefault(); });
        const remove = button("取消收藏", "sns-review-remove");
        remove.dataset.favoriteKey = favoriteKey(item.capture_id, item.sentence_id);
        remove.disabled = pendingFavorites.has(remove.dataset.favoriteKey);
        if (remove.disabled) remove.textContent = "取消中…";
        remove.addEventListener("click", async (event) => {
          if (remove.disabled || event?.isTrusted === false) return;
          const request = visibleRequest;
          const changed = await changeFavorite(item.capture_id, item.sentence_id, false, remove);
          remove.disabled = false;
          if (element.hidden || request !== visibleRequest || tab !== "favorites") return;
          feedback.textContent = changed ? "已取消收藏。" : favoriteFailures.get(favoriteKey(item.capture_id, item.sentence_id)) || "无法取消收藏，请重试。";
          if (changed) {
            render();
            tabs.favorites.focus?.();
          }
        });
        actions.append(source, remove);
        card.append(details, actions);
        content.append(card);
      }
    }

    function render() {
      content.replaceChildren();
      if (tab === "clips") {
        content.append(node("p", "普通片段保留 7 天。", "sns-review-meta"));
        renderClips();
      } else renderFavorites();
    }

    function switchTab(name) {
      tab = name;
      for (const [name, item] of Object.entries(tabs)) item.setAttribute("aria-pressed", String(name === tab));
      content.scrollTop = 0;
      if (tabs.clips.disabled) return;
      feedback.textContent = "";
      render();
    }
    tabs.clips.addEventListener("click", () => switchTab("clips"));
    tabs.favorites.addEventListener("click", () => switchTab("favorites"));
    switchTab("clips");

    async function open() {
      element.hidden = false;
      const request = ++visibleRequest;
      feedback.textContent = "正在读取本机复习记录…";
      tabs.clips.disabled = tabs.favorites.disabled = true;
      content.replaceChildren();
      const loaded = await refreshFavorites();
      if (element.hidden || request !== visibleRequest) return;
      tabs.clips.disabled = tabs.favorites.disabled = !loaded;
      if (!loaded) {
        feedback.textContent = libraryFailure || "无法读取本机复习记录，请确认本机服务已启动。";
        const retry = button("重新读取");
        retry.addEventListener("click", () => { void open(); });
        content.append(retry);
        return;
      }
      feedback.textContent = "";
      render();
    }

    function close() {
      visibleRequest++;
      element.hidden = true;
    }

    return { element, open, close, favoriteButton, refreshFavorites };
  }

  window.__snsJapaneseReview = Object.freeze({ create });
})();
