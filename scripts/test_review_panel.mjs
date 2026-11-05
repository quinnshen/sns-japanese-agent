/** Offline review UI behavior with saved metadata and an independent favorite. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const source = await readFile(new URL("../app/extension/review.js", import.meta.url), "utf8");
const ids = Array.from({ length: 5 }, (_, index) => `00000000-0000-4000-8000-00000000000${index + 1}`);
const videoA = "https://www.youtube.com/watch?v=AbCdEf012_-";
const videoB = "https://www.youtube.com/watch?v=XyZuvw987_-";

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.textContent = "";
    this.disabled = this.hidden = false;
  }
  append(...children) { for (const child of children) { this.children.push(child); child.parent = this; } }
  replaceChildren(...children) { for (const child of this.children) child.parent = null; this.children = []; this.append(...children); }
  getRootNode() { return this.parent?.getRootNode() || this; }
  querySelectorAll(selector) {
    const match = (node) => selector.startsWith(".") ? node.className === selector.slice(1) : node.tagName === selector;
    return this.children.flatMap((child) => [...(match(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, listener) { (this.events ||= {})[type] = listener; }
  async click(isTrusted = true) { if (!this.disabled) await this.events?.click?.({ isTrusted, preventDefault() {} }); }
}

function harness(send, onOpenClip = () => ({ ok: true })) {
  const sandbox = { window: {}, document: { createElement: (tag) => new Element(tag) }, URL };
  vm.runInNewContext(source, sandbox);
  const panel = sandbox.window.__snsJapaneseReview.create({ send, onOpenClip });
  const root = new Element("main");
  root.append(panel.element);
  const control = (label) => panel.element.querySelectorAll("button").find((item) => item.textContent === label);
  return { panel, root, control };
}

function clip(capture_id, source_url, range_start_sec) {
  return { capture_id, source_url, range_start_sec, range_end_sec: range_start_sec + 15, updated_at: Date.parse("2026-08-27T01:20:00Z") / 1000, sentences_done: 3 };
}

const saved = {
  capture_id: ids[3], sentence_id: "s0002", source_url: `${videoB}&origin_token=do-not-forward`,
  sentence: { id: "s0002", start: 75.2, end: 77.9, translation_zh: "收藏的译文",
    study: { style_version: 2, reading_text: "練習(れんしゅう)", key_points: ["<b>这是纯文本</b>"], context_note: "收藏的语境", asr_doubt: "收藏的疑点" } },
};

{
  const messages = [], opened = [];
  let resolveFirst;
  const { panel, control } = harness(async (message) => {
    messages.push(message);
    return { ok: true, clips: [clip(ids[0], videoA, 15), clip(ids[1], "https://www.youtube.com/shorts/AbCdEf012_-", 60),
      clip(ids[2], videoB, 9), clip(ids[4], "https://other.example/watch?v=AbCdEf012_-", 0)], favorites: [saved] };
  }, (id) => {
    opened.push(id);
    return opened.length === 1 ? new Promise((resolve) => { resolveFirst = resolve; }) : { ok: true };
  });
  assert.equal(panel.element.hidden, true);
  assert.equal(messages.length, 0, "Closed review panel must not read the library");
  await panel.open();
  assert.equal(panel.element.hidden, false);
  assert.equal(panel.element.querySelectorAll("h3").length, 2, "Watch and Shorts variants of one video share a group");
  let rows = panel.element.querySelectorAll(".sns-review-clip");
  assert.equal(rows.length, 3);
  await rows[1].click(false);
  assert.equal(opened.length, 0, "Page-synthesized clicks cannot open a clip");
  const pending = rows[1].click();
  assert.equal(opened[0], ids[1], "The explicit clip selection is preserved even within one video group");
  await control("收藏句子").click();
  const [card] = panel.element.querySelectorAll("article");
  assert.ok(card, "A favorite remains readable without any matching clip in the library");
  assert.equal(card.querySelectorAll("li")[0].textContent, "<b>这是纯文本</b>");
  assert.equal(card.querySelectorAll("b").length, 0);
  assert.ok(card.querySelectorAll("p").some((item) => item.textContent === "语境待确认：收藏的语境"));
  assert.ok(card.querySelectorAll("p").some((item) => item.textContent === "ASR 存疑：收藏的疑点"));
  assert.equal(card.querySelectorAll("a")[0].href, `${videoB}&t=75`);
  await control("最近片段").click();
  rows = panel.element.querySelectorAll(".sns-review-clip");
  assert.ok(rows.every((row) => row.disabled));
  resolveFirst({ ok: true });
  await pending;
  assert.ok(rows.every((row) => !row.disabled), "A tab switch during selection must not leave newly rendered buttons disabled");
  await rows[2].click();
  assert.equal(opened[1], ids[2]);
  assert.equal(messages.length, 1, "Review navigation does not mutate the library");
}

{
  const messages = [];
  let persisted = true;
  const { panel, root, control } = harness(async (message) => {
    messages.push(message);
    if (message.type === "review-library") return { ok: true, clips: [], favorites: persisted ? [saved] : [] };
    persisted = message.saved;
    return { ok: true, saved: persisted };
  });
  const first = panel.favoriteButton(ids[3], "s0002");
  const second = panel.favoriteButton(ids[3], "s0002");
  root.append(first, second);
  assert.equal(first.textContent, "收藏");
  await first.click(false);
  assert.equal(messages.length, 0, "Page-synthesized clicks cannot mutate favorites");
  await first.click();
  assert.equal(messages[0].saved, true, "An unhydrated 收藏 button must never silently remove a persisted bookmark");
  assert.equal(first.attributes["aria-pressed"], "true");
  assert.equal(second.textContent, "已收藏");
  await panel.open();
  await control("收藏句子").click();
  await control("取消收藏").click();
  assert.equal(persisted, false);
  assert.equal(first.attributes["aria-pressed"], "false");
  assert.equal(second.textContent, "收藏");
  assert.equal(panel.element.querySelectorAll("article").length, 0);
}

{
  let reads = 0;
  const { panel, control } = harness(async () => {
    if (++reads === 1) throw new Error("backend unavailable");
    return { ok: true, clips: [], favorites: [] };
  });
  await panel.open();
  assert.equal(control("最近片段").disabled, true);
  await control("重新读取").click();
  for (let tick = 0; tick < 10; tick++) await Promise.resolve();
  assert.equal(control("最近片段").disabled, false);
  assert.match(panel.element.querySelectorAll(".sns-review-content")[0].children[1].textContent, /还没有可复习的片段/);
  panel.close();
  assert.equal(panel.element.hidden, true);
}

{
  let upgraded = false;
  const upgrade = { ok: false, code: "backend_upgrade_required", error: "Untrusted diagnostic must not be displayed" };
  const { panel, root, control } = harness(async (message) => {
    if (!upgraded) return upgrade;
    if (message.type === "review-library") return { ok: true, clips: [], favorites: [] };
    return { ok: true, saved: message.saved };
  });
  const favorite = panel.favoriteButton(ids[3], "s0002");
  root.append(favorite);
  await panel.open();
  assert.equal(panel.element.querySelectorAll(".sns-review-feedback")[0].textContent, "请重启本机后端，加载收藏与复习功能。");
  assert.equal(panel.element.querySelectorAll(".sns-review-content")[0].querySelectorAll("p").length, 0,
    "An old backend must not look like a genuinely empty review library");
  await favorite.click();
  assert.equal(favorite.textContent, "请重启本机后端");
  assert.equal(favorite.title, "请重启本机后端，加载收藏与复习功能。");
  assert.equal(favorite.attributes["aria-pressed"], "false");
  upgraded = true;
  await control("重新读取").click();
  for (let tick = 0; tick < 12; tick++) await Promise.resolve();
  assert.equal(panel.element.querySelectorAll(".sns-review-feedback")[0].textContent, "");
  assert.equal(favorite.textContent, "收藏", "Successful refresh clears the obsolete backend-upgrade notice");
  assert.match(panel.element.querySelectorAll(".sns-review-content")[0].children[1].textContent, /还没有可复习的片段/);
}

console.log("PASS review grouping, exact selection, retained favorite text, mutation synchronization, read retry, and backend-upgrade guidance");
