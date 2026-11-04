"""Pure, deterministic planning and per-sentence learning-note validation.

No configuration, credential, model or filesystem access belongs here. Model
annotations may add readings; the original ASR characters must all survive.
"""

from __future__ import annotations

import copy
import re

STYLE_VERSION = 2
DEFAULT_MAX_SENTENCES = 6
DEFAULT_MAX_CHARS = 1800
DEFAULT_CONTEXT_SENTENCES = 2
_KANJI = re.compile(r"[㐀-䶿一-鿿豈-﫿々〆ヶ\U00020000-\U0002EBEF]")
_KANJI_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿々〆ヶ\U00020000-\U0002EBEF]+")
_KANA = re.compile(r"[ぁ-ゖァ-ヺーゝゞヽヾ]")
_READING = re.compile(r"\([ぁ-ゖァ-ヺーゝゞヽヾ]+\)")
_QUOTED = re.compile(r'「([^「」]*)」|『([^『』]*)』|［([^［］]*)］|【([^【】]*)】|"([^"\n]*)"|\x27([^\x27\n]*)\x27|`([^`\n]*)`')
_FIELDS = frozenset({"id", "reading_text", "translation_zh", "key_points", "context_note", "asr_doubt"})


def sentence_records(sentences) -> list[dict]:
    """Copy only bounded, unique IDs and unmodified nonempty ASR text."""
    if not isinstance(sentences, list) or len(sentences) > 10_000:
        raise ValueError("invalid_learning_sentences")
    result, seen = [], set()
    for sentence in sentences:
        if not isinstance(sentence, dict):
            raise ValueError("invalid_learning_sentences")
        identifier, text = sentence.get("id"), sentence.get("text")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier)
                or identifier == "__response__" or identifier in seen
                or not isinstance(text, str) or not text.strip() or len(text) > 20_000):
            raise ValueError("invalid_learning_sentences")
        seen.add(identifier)
        result.append({"id": identifier, "text": text})
    return result


def plan_batches(sentences, complete: bool, max_sentences=DEFAULT_MAX_SENTENCES,
                 max_chars=DEFAULT_MAX_CHARS, context_sentences=DEFAULT_CONTEXT_SENTENCES) -> list[dict]:
    """Greedily group sentence targets independently of audio-recognition windows.

    An unfinished group is held. A closed group also waits for all requested
    right-hand context, so appending later sentences cannot change emitted
    inputs. Completed input may flush a short tail and missing final context.
    Oversized sentences are rejected rather than split or silently exceed a
    caller's character budget. Character budgets count target text only.
    """
    if not isinstance(complete, bool):
        raise ValueError("invalid_learning_completion")
    if (isinstance(max_sentences, bool) or not isinstance(max_sentences, int) or max_sentences < 1
            or isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1
            or isinstance(context_sentences, bool) or not isinstance(context_sentences, int) or context_sentences < 0):
        raise ValueError("invalid_learning_batch_limits")
    records = sentence_records(sentences)
    if any(len(record["text"]) > max_chars for record in records):
        raise ValueError("learning_sentence_too_long")
    batches, start = [], 0
    while start < len(records):
        end, size = start, 0
        while end < len(records) and end - start < max_sentences:
            length = len(records[end]["text"])
            if end > start and size + length > max_chars:
                break
            size += length
            end += 1
            if size == max_chars:
                break
        closed = end < len(records) or end - start == max_sentences or size == max_chars
        if not complete and (not closed or end + context_sentences > len(records)):
            break
        targets = copy.deepcopy(records[start:end])
        batches.append({
            "index": len(batches), "targets": targets,
            "context": copy.deepcopy(records[max(0, start - context_sentences):end + context_sentences]),
            "target_ids": [target["id"] for target in targets],
        })
        start = end
    return batches


def missing_furigana(text: str) -> bool:
    return any(_READING.match(text, match.end()) is None for match in _KANJI_RUN.finditer(text))


def reading_preserves_source(source: str, reading: str) -> bool:
    """Match literal source plus optional new readings at Han boundaries only.

    Keep both literal and inserted-annotation branches: a source's original
    ``(かな)`` must not be globally removed or mistaken for newly added text.
    Cursor sets avoid recursion limits on long sentences.
    """
    if not isinstance(source, str) or not isinstance(reading, str):
        return False
    positions = {0}
    for index, character in enumerate(source):
        candidates = set(positions)
        if index and _KANJI.fullmatch(source[index - 1]):
            for position in positions:
                annotation = _READING.match(reading, position)
                if annotation:
                    candidates.add(annotation.end())
        positions = {position + 1 for position in candidates if position < len(reading) and reading[position] == character}
        if not positions:
            return False
    if source and _KANJI.fullmatch(source[-1]):
        for position in list(positions):
            annotation = _READING.match(reading, position)
            if annotation:
                positions.add(annotation.end())
    return len(reading) in positions


def _japanese_fragment(fragment: str, source: str) -> bool:
    if _KANA.search(fragment):
        return True
    plain = _READING.sub("", fragment).strip()
    return bool(plain and _KANJI.search(plain) and plain in source)


def _source_reading_spans(source: str, reading: str) -> list[tuple[int, int, str]]:
    """Map existing annotations to whole source tokens, without guessing kana.

    Literal source kana-parentheses make insertion provenance ambiguous, so no
    recovery is attempted for that sentence. A reading attached to a Han run
    also owns adjacent preceding digits (e.g. 1回), and may not be cut in half.
    """
    if (_READING.search(source) or not reading_preserves_source(source, reading)
            or missing_furigana(reading)):
        return []
    spans, annotated_ends = [], set()
    cursor = 0
    for index, character in enumerate(source):
        if cursor >= len(reading) or reading[cursor] != character:
            return []
        cursor += 1
        annotation = _READING.match(reading, cursor) if _KANJI.fullmatch(character) else None
        if annotation:
            start = index
            while (start > 0 and start not in annotated_ends
                   and (_KANJI.fullmatch(source[start - 1]) or source[start - 1] in "0123456789０１２３４５６７８９")):
                start -= 1
            spans.append((start, index + 1, annotation.group()))
            annotated_ends.add(index + 1)
            cursor = annotation.end()
    return spans if cursor == len(reading) else []


def _annotated_exact_quote(fragment: str, source: str, spans: list[tuple[int, int, str]]) -> str | None:
    if not fragment or not _KANJI.search(fragment) or _READING.search(fragment):
        return None
    candidate, position, occurrences = None, source.find(fragment), 0
    while position >= 0:
        occurrences += 1
        if occurrences > 100:
            return None  # Excessively repeated fragments are not worth guessing.
        end = position + len(fragment)
        selected = []
        for start, stop, annotation in spans:
            if start < end and stop > position:
                if start < position or stop > end:
                    return None  # A compound/counter reading crosses this quote.
                selected.append((stop - position, annotation))
        parts, previous = [], 0
        for stop, annotation in selected:
            parts.extend((fragment[previous:stop], annotation))
            previous = stop
        parts.append(fragment[previous:])
        annotated = "".join(parts)
        if missing_furigana(annotated) or (candidate is not None and candidate != annotated):
            return None
        candidate = annotated
        position = source.find(fragment, position + 1)
    return candidate


def recover_note_readings(item: dict, source: str) -> dict:
    """Copy existing source readings into exact, wholly unannotated quotations.

    Source and reading_text are never changed, and this is not pronunciation
    correction. Different readings for repeated source fragments, partial
    annotations, token-boundary ambiguity and literal source parentheses all
    leave the quotation unchanged for the normal validator to reject if needed.
    The input item is never mutated; only note fields can receive derived text.
    """
    if (not isinstance(item, dict) or set(item) != _FIELDS
            or not isinstance(item["reading_text"], str) or len(item["reading_text"]) > 32_000):
        return item
    if (not isinstance(item["key_points"], list)
            or any(not isinstance(point, str) for point in item["key_points"])
            or any(not isinstance(item[field], str) for field in ("translation_zh", "context_note", "asr_doubt"))):
        return item
    spans = _source_reading_spans(source, item["reading_text"])
    if not spans:
        return item

    def recover(text):
        def replace(match):
            group = next(index for index, value in enumerate(match.groups(), 1) if value is not None)
            fragment = match.group(group)
            annotated = _annotated_exact_quote(fragment, source, spans)
            if annotated is None:
                return match.group()
            start, end = match.span(group)
            relative_start, relative_end = start - match.start(), end - match.start()
            return match.group()[:relative_start] + annotated + match.group()[relative_end:]
        return _QUOTED.sub(replace, text)

    result = dict(item)
    for field in ("translation_zh", "context_note", "asr_doubt"):
        result[field] = recover(item[field])
    result["key_points"] = [recover(point) for point in item["key_points"]]
    return result


def _note_missing_reading(text: str, source: str, *, key_point=False) -> bool:
    # Chinese explanation is not Japanese text. Require readings on identifiable
    # quoted Japanese and explicit form-change expressions, not all Han prose.
    for match in _QUOTED.finditer(text):
        fragment = next(value for value in match.groups() if value is not None)
        if _japanese_fragment(fragment, source) and missing_furigana(fragment):
            return True
    # Quoted text has already been checked. Hide it before interpreting prose
    # punctuation: a quoted arrow must not make its following Chinese
    # explanation (such as "动词た形") look like another Japanese form.
    unquoted = _QUOTED.sub(lambda match: " " * len(match.group()), text)
    if key_point and re.search(r"[:：]", unquoted):
        head = re.split(r"[:：]", unquoted, maxsplit=1)[0].strip()
        if _japanese_fragment(head, source) and missing_furigana(head):
            return True
    # For unquoted changes, a left colon introduces the form while a right
    # colon ends it. Inspect the adjacent clauses, never text after a right
    # colon. Arrow boundaries also handle chains without rechecking prose.
    boundary = r"[→←⇒;；,，。\n:：]"
    for arrow in re.finditer(r"[→←⇒]", unquoted):
        adjacent = (
            re.split(boundary, unquoted[:arrow.start()])[-1].strip(),
            re.split(boundary, unquoted[arrow.end():], maxsplit=1)[0].strip(),
        )
        for part in adjacent:
            if _japanese_fragment(part, source) and missing_furigana(part):
                return True
    return False


def _item_error(item: dict, source: str) -> str | None:
    if set(item) != _FIELDS:
        return "invalid_item_shape"
    reading, translation = item["reading_text"], item["translation_zh"]
    points, context, doubt = item["key_points"], item["context_note"], item["asr_doubt"]
    if not isinstance(reading, str) or len(reading) > 32_000 or not reading_preserves_source(source, reading):
        return "source_text_changed"
    if missing_furigana(reading):
        return "missing_reading"
    if not isinstance(translation, str) or not translation.strip() or len(translation) > 6000:
        return "invalid_translation"
    if (not isinstance(points, list) or len(points) > 4
            or any(not isinstance(point, str) or not point.strip() or len(point) > 6000 for point in points)):
        return "invalid_key_points"
    if not isinstance(context, str) or not isinstance(doubt, str) or max(len(context), len(doubt)) > 6000:
        return "invalid_notes"
    if not points and not doubt.strip():
        return "missing_explanation"
    if (any(_note_missing_reading(point, source, key_point=True) for point in points)
            or any(_note_missing_reading(value, source) for value in (translation, context, doubt))):
        return "missing_note_reading"
    return None


def validate_items(items, targets) -> dict:
    """Validate independently: malformed annotations never discard valid neighbors.

    Invalid target IDs, missing IDs and unknown response IDs are recorded with
    fixed codes. A malformed entry with no usable ID uses ``__response__`` for
    diagnostics; it never replaces a target or changes sentence association.
    """
    expected = {record["id"]: record["text"] for record in sentence_records(targets)}
    if not isinstance(items, list) or len(items) > 100:
        return {"valid": {}, "invalid": {identifier: "invalid_items" for identifier in expected}}
    groups, invalid = {}, {}
    for item in items:
        identifier = item.get("id") if isinstance(item, dict) else None
        if not isinstance(identifier, str) or not identifier or len(identifier) > 200:
            invalid["__response__"] = "invalid_item_shape"
        elif identifier not in expected:
            invalid[identifier] = "unknown_item_id"
        else:
            groups.setdefault(identifier, []).append(item)
    valid = {}
    for identifier, source in expected.items():
        candidates = groups.get(identifier, [])
        if not candidates:
            invalid[identifier] = "missing_item"
            continue
        if len(candidates) != 1:
            invalid[identifier] = "duplicate_item_id"
            continue
        item = recover_note_readings(candidates[0], source)
        code = _item_error(item, source)
        if code:
            invalid[identifier] = code
            continue
        valid[identifier] = {
            "translation_zh": item["translation_zh"].strip(),
            "study": {
                "style_version": STYLE_VERSION, "reading_text": item["reading_text"],
                "key_points": [point.strip() for point in item["key_points"]],
                "context_note": item["context_note"].strip(), "asr_doubt": item["asr_doubt"].strip(),
            },
        }
    return {"valid": valid, "invalid": invalid}
