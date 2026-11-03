"""Precompute sentence translation and Japanese learning notes in one model call per batch."""

from __future__ import annotations

import copy
import json
import re
from typing import Callable

from config import setting

BATCH_SIZE = 12
STYLE_VERSION = 1
DEFAULT_MODEL = "gpt-6-luna"

STUDY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reading_text": {"type": "string"},
                    "translation_zh": {"type": "string"},
                    "key_points": {"type": "array", "items": {"type": "string"}},
                    "context_note": {"type": "string"},
                    "asr_doubt": {"type": "string"},
                },
                "required": ["id", "reading_text", "translation_zh", "key_points", "context_note", "asr_doubt"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

INSTRUCTIONS = """你是日语 SNS 视频台词解析助手。学习者正在学习《标准日本语》初级下册，已接触简体形、て形、た形、ない形、小句、授受表达、〜ている、〜てしまう、意志形和条件表达。
输入是按时间顺序排列的 ASR 句子，不是校对原文。只处理 target_ids；前后句仅供理解语境。严格保持每个 id 和 ASR 原文，不合并、拆分或擅自修正。
对每句给出：
1. reading_text：逐字保留 ASR 原文，仅在日文汉字后以半角括号添加句中实际读音，如「物理的(ぶつりてき)」「持(も)ってくれば」。去掉这些假名括注后，必须与输入原文完全相同。
2. translation_zh：自然、紧凑的简体中文。口语省略可补进中文；不确定处标「……？」。若 ASR 不通顺到整句无法可靠确定，必须写「目前无法可靠确定整句意思。」；不可一边标 ASR 存疑，一边给出未经核对的肯定译文。
3. key_points：按原句从前到后，只选影响理解的 1–4 个片段；整句存疑时可以为空。每条用短句写「片段：结构／词形；在这里的作用」，不要逐字解释简单词、扩展成课程或添加额外例句。动词写「基本形 → 句中形式 → 此处意思」；注意复合动词必须以完整复合动词为基本形：持(も)ってくれば ← 持(も)ってくる，绝不能写成「持つ → 持ってくれば」。自他动词不可写成同一动词的变形。小句用［］明确框出完整范围并说明是条件、引用、名词修饰或其他作用。助词说明在此句标记谁、对象、原因或地点。解释「って」「んですよ」「じゃないですか」等口语的具体作用和语气。重复出现的结构简短说明即可。片段中的日文汉字也要括号标假名，包括［］里的原句片段。
4. context_note：只有「これ」「ここ」「こう」等指示词或画面、语气确实影响理解时填写能确定的部分，并明确待确认之处；不猜具体所指。否则留空字符串。
5. asr_doubt：只有 ASR 不通顺、疑似识别错误、断句错误或口语残缺时填写：原文可疑片段、问题、可能读法及需要回听什么；无法确定就直说。不要仅因为口语省略或稍显生硬就标存疑。绝不把猜测写成确定事实。否则留空字符串。
以下三句是风格基准，输出时不要照抄到其他句子：
「これって物理的に普通にこれぶらさずにここに持ってくれば絶対成功なんですよ。」译为「这个从物理上说，只要不让它晃动，把它拿到这里来，就肯定能成功。」；「ぶらさずに」是「ぶらす → ぶらさずに」，这里为「不让它晃动地」；「［これぶらさずにここに持(も)ってくれば］」整体是条件部分；「持(も)ってくる → 持(も)ってくれば」。
「ってことは何が必要かって言うと膝なんですよ。」中「［何(なに)が必要(ひつよう)か］って言(い)うと」是疑问小句加口语引用，说话人自己提问再回答。
「これもノーカットでいらっしゃいますか？」里的「ノーカットでいらっしゃいますか」连接不自然；译文写「目前无法可靠确定整句意思。」并标 ASR 存疑，不要猜一个流畅的句子。
不要在字段之外输出总复习、额外例句或长篇说明。"""


def local_api_key() -> str | None:
    """Use only the operator's process environment or private local settings."""
    return (setting("OPENAI_API_KEY", "") or "").strip() or None


def openai_enricher(model: str) -> Callable:
    key = local_api_key()
    if not key:
        raise RuntimeError("未配置 OpenAI API Key；请运行 configure_openai_key.py。")
    from openai import OpenAI

    client = OpenAI(api_key=key, max_retries=0)

    def enrich(context: list[dict], target_ids: list[str]) -> tuple[list[dict], dict]:
        response = client.responses.create(
            model=model,
            reasoning={"effort": "low"},
            store=False,
            max_output_tokens=6000,
            instructions=INSTRUCTIONS,
            input=json.dumps({"context": context, "target_ids": target_ids}, ensure_ascii=False),
            text={"format": {
                "type": "json_schema", "name": "japanese_sentence_study_v1",
                "schema": STUDY_SCHEMA, "strict": True,
            }},
        )
        if response.status != "completed" or not response.output_text:
            raise RuntimeError(f"OpenAI 解析未完成：{response.status}")
        payload = json.loads(response.output_text)
        usage = response.usage
        return payload["items"], {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        }

    return enrich


def openai_furigana_fixer(model: str) -> Callable:
    key = local_api_key()
    if not key:
        raise RuntimeError("未配置 OpenAI API Key；请运行 configure_openai_key.py。")
    from openai import OpenAI

    client = OpenAI(api_key=key, max_retries=0)

    def fix(source: str) -> tuple[str, dict]:
        response = client.responses.create(
            model=model,
            reasoning={"effort": "low"},
            store=False,
            max_output_tokens=1200,
            instructions="只给日语原文每处汉字添加半角括号假名，其他字符逐字不变。数字后的量词汉字也必须标，如「1回→1回(かい)」。例如「もう1回恥ずかしいな。」写成「もう1回(かい)恥(は)ずかしいな。」。不得删改原文。",
            input=source,
            text={"format": {
                "type": "json_schema", "name": "furigana_repair_v1",
                "schema": {"type": "object", "properties": {"reading_text": {"type": "string"}},
                           "required": ["reading_text"], "additionalProperties": False},
                "strict": True,
            }},
        )
        if response.status != "completed" or not response.output_text:
            raise RuntimeError(f"OpenAI 假名修复未完成：{response.status}")
        reading = json.loads(response.output_text)["reading_text"]
        usage = response.usage
        return reading, {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        }

    return fix


def remove_furigana(text: str) -> str:
    return re.sub(r"\([ぁ-ゟ゠-ヿー]+\)", "", text)


def missing_furigana(text: str) -> bool:
    for match in re.finditer(r"[㐀-鿿々〆ヶ]+", text):
        if not re.match(r"\([ぁ-ゟ゠-ヿー]+\)", text[match.end():]):
            return True
    return False


def validate_batch(items: list[dict], targets: list[dict]) -> dict[str, dict]:
    expected = {item["id"]: item["text"] for item in targets}
    actual = [item.get("id") for item in items]
    if len(actual) != len(targets) or set(actual) != set(expected):
        raise RuntimeError("解析结果的句子 ID 不完整或重复，已停止保存以防错位。")

    validated = {}
    for item in items:
        sentence_id = item["id"]
        reading = item.get("reading_text")
        translation = item.get("translation_zh")
        points = item.get("key_points")
        context_note = item.get("context_note")
        asr_doubt = item.get("asr_doubt")
        if not isinstance(reading, str) or remove_furigana(reading) != expected[sentence_id]:
            raise RuntimeError(f"句子 {sentence_id} 的假名标注改动了 ASR 原文，已拒绝保存。")
        if missing_furigana(reading):
            raise RuntimeError(f"句子 {sentence_id} 有未标注假名的日文汉字，已拒绝保存。")
        if not isinstance(translation, str) or not translation.strip():
            raise RuntimeError(f"句子 {sentence_id} 的中文意思为空。")
        if not isinstance(points, list) or len(points) > 4 or any(not isinstance(p, str) or not p.strip() for p in points):
            raise RuntimeError(f"句子 {sentence_id} 的关键拆解格式无效。")
        if not isinstance(context_note, str) or not isinstance(asr_doubt, str):
            raise RuntimeError(f"句子 {sentence_id} 的语境或 ASR 存疑格式无效。")
        if not points and not asr_doubt.strip():
            raise RuntimeError(f"句子 {sentence_id} 缺少关键拆解或 ASR 存疑说明。")
        validated[sentence_id] = {
            "translation_zh": translation.strip(),
            "study": {
                "style_version": STYLE_VERSION,
                "reading_text": reading,
                "key_points": [point.strip() for point in points],
                "context_note": context_note.strip(),
                "asr_doubt": asr_doubt.strip(),
            },
        }
    return validated


def enrich_result(
    result: dict,
    *,
    model: str | None = None,
    enricher: Callable | None = None,
    furigana_fixer: Callable | None = None,
    progress: Callable | None = None,
) -> dict:
    """Enrich only missing or outdated sentences; caller can save after each batch."""
    model = model or setting("OPENAI_MODEL", DEFAULT_MODEL)
    updated = copy.deepcopy(result)
    sentences = updated.get("sentences", [])
    pending = [
        index for index, sentence in enumerate(sentences)
        if (
            not sentence.get("translation_zh")
            or sentence.get("study", {}).get("style_version") != STYLE_VERSION
            or not isinstance(sentence.get("study", {}).get("reading_text"), str)
            or remove_furigana(sentence["study"]["reading_text"]) != sentence["text"]
            or missing_furigana(sentence["study"]["reading_text"])
        )
    ]
    if not pending:
        updated["status"] = "enriched" if sentences else updated.get("status", "transcribed")
        return updated

    enricher = enricher or openai_enricher(model)
    total = (len(pending) + BATCH_SIZE - 1) // BATCH_SIZE
    usage = updated.setdefault("enrichment_usage", {"input_tokens": 0, "output_tokens": 0})
    failures = []

    def add_usage(batch_usage: dict) -> None:
        for key in ("input_tokens", "output_tokens"):
            usage[key] = usage.get(key, 0) + int(batch_usage.get(key, 0))

    for batch_number, offset in enumerate(range(0, len(pending), BATCH_SIZE), start=1):
        indexes = pending[offset:offset + BATCH_SIZE]
        first, last = indexes[0], indexes[-1]
        context = [
            {"id": sentence["id"], "text": sentence["text"]}
            for sentence in sentences[max(0, first - 2):min(len(sentences), last + 3)]
        ]
        targets = [sentences[index] for index in indexes]
        items, batch_usage = enricher(context, [item["id"] for item in targets])
        add_usage(batch_usage)
        grouped = {}
        for item in items:
            grouped.setdefault(item.get("id"), []).append(item)
        validated = {}
        for index in indexes:
            target = sentences[index]
            sentence_id = target["id"]
            candidates = grouped.get(sentence_id, [])
            if len(candidates) == 1:
                try:
                    validated.update(validate_batch(candidates, [target]))
                    continue
                except RuntimeError:
                    pass
                reading = candidates[0].get("reading_text")
                if isinstance(reading, str) and (remove_furigana(reading) != target["text"] or missing_furigana(reading)):
                    try:
                        fixer = furigana_fixer or openai_furigana_fixer(model)
                        fixed_reading, fix_usage = fixer(target["text"])
                        add_usage(fix_usage)
                        repaired = {**candidates[0], "reading_text": fixed_reading}
                        validated.update(validate_batch([repaired], [target]))
                        continue
                    except RuntimeError:
                        pass
            # A malformed sentence cannot block valid neighbors or later batches.
            last_error = "模型未返回唯一且有效的句子结果。"
            for _ in range(2):
                retry_context = [
                    {"id": item["id"], "text": item["text"]}
                    for item in sentences[max(0, index - 2):min(len(sentences), index + 3)]
                ]
                retry_items, retry_usage = enricher(retry_context, [sentence_id])
                add_usage(retry_usage)
                try:
                    validated.update(validate_batch(retry_items, [target]))
                    last_error = ""
                    break
                except RuntimeError as exc:
                    last_error = str(exc)
            if last_error:
                target["enrichment_error"] = last_error
                failures.append(sentence_id)
        for index in indexes:
            target = sentences[index]
            if target["id"] in validated:
                target.update(validated[target["id"]])
                target.pop("enrichment_error", None)
        updated["enrichment_model"] = model
        updated["status"] = "enriching" if batch_number < total else ("partially_enriched" if failures else "enriched")
        if batch_number == total:
            if failures:
                updated["enrichment_error"] = f"{len(failures)} 句尚未解析：{', '.join(failures)}"
            else:
                updated.pop("enrichment_error", None)
        if progress:
            progress(batch_number, total, updated)
    return updated
