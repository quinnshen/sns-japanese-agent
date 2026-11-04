"""Merge timed ASR windows into sentences without models or credential access.

Audio seconds remain the authoritative coordinates. Video times are observations
provided by the local mapper and may be unknown or uncertain. Raw ASR result
text is retained separately from the deduplicated, timed transcript.
"""

from __future__ import annotations

import math

from transcription_audio import map_audio_time


_GAP_SECONDS = 1.2
_MAX_DUPLICATE_DRIFT_SECONDS = 0.35
_PUNCTUATION = frozenset("。、！？!?.,，：:；;「」『』（）()[]【】〈〉《》“”‘’\"'…—・")
_CLOSING_QUOTES = "」』）)]】〉》”’\"'"
_TERMINATORS = ("。", "！", "？", "!", "?", ".")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _punctuation_only(text):
    return all(character.isspace() or character in _PUNCTUATION for character in text)


def _terminal(text):
    return text.rstrip().rstrip(_CLOSING_QUOTES).rstrip().endswith(_TERMINATORS)


def _window(raw):
    if not isinstance(raw, dict):
        raise ValueError("Invalid transcription window")
    index = raw.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 10_000:
        raise ValueError("Invalid transcription window index")
    fields = ("core_start_sec", "core_end_sec", "audio_start_sec", "audio_end_sec")
    if any(not _number(raw.get(field)) for field in fields):
        raise ValueError("Invalid transcription window timing")
    if not (0 <= raw["audio_start_sec"] <= raw["core_start_sec"]
            < raw["core_end_sec"] <= raw["audio_end_sec"]):
        raise ValueError("Invalid transcription core boundaries")
    if not isinstance(raw.get("results"), list):
        raise ValueError("Invalid transcription window results")
    return raw


def _result_words(result, window, result_index, warnings, unmapped):
    location = {"window_index": window["index"], "result_index": result_index}
    if not isinstance(result, dict):
        warnings.append({"code": "invalid_result", **location})
        return []
    transcript = result.get("text", "")
    if not isinstance(transcript, str):
        warnings.append({"code": "invalid_result_text", **location})
        transcript = ""
    raw_words = result.get("words", [])
    if not isinstance(raw_words, list) or not raw_words:
        if transcript.strip():
            record = {**location, "text": transcript, "reason": "missing_word_timestamps"}
            unmapped.append(record)
            warnings.append({"code": "missing_word_timestamps", **location})
        return []
    words = []
    invalid = False
    for word_index, raw in enumerate(raw_words):
        if (not isinstance(raw, dict) or not isinstance(raw.get("text"), str)
                or not raw["text"].strip() or not _number(raw.get("start")) or not _number(raw.get("end"))
                or raw["start"] < 0 or raw["end"] < raw["start"]
                or raw["end"] > window["audio_end_sec"] - window["audio_start_sec"] + 0.05):
            invalid = True
            warnings.append({"code": "invalid_word_timestamps", **location, "word_index": word_index})
            continue
        words.append({
            "text": raw["text"], "original_text": raw["text"],
            "audio_start": window["audio_start_sec"] + raw["start"],
            "audio_end": window["audio_start_sec"] + raw["end"],
            "source_window_index": window["index"], "source_result_index": result_index,
            "source_word_index": word_index,
        })
    if invalid:
        unmapped.append({**location, "text": transcript, "reason": "invalid_word_timestamps"})
    if not transcript or not words:
        return words
    # Attach only punctuation/spacing actually present between exact word matches.
    # Text missing from the timed tokens remains explicit, never guessed or dropped silently.
    cursor = 0
    positions = []
    unmatched = []
    for word in words:
        position = transcript.find(word["original_text"], cursor)
        if position < 0:
            positions.append(None)
            continue
        positions.append(position)
        if not _punctuation_only(transcript[cursor:position]):
            unmatched.append(transcript[cursor:position])
        cursor = position + len(word["original_text"])
    if not _punctuation_only(transcript[cursor:]):
        unmatched.append(transcript[cursor:])
    mismatch = any(position is None for position in positions) or bool(unmatched)
    if mismatch:
        warnings.append({"code": "text_word_mismatch", **location})
        unmapped.append({**location, "text": transcript, "reason": "text_word_mismatch",
                         "unmatched_text": unmatched})
    for index, word in enumerate(words):
        position = positions[index]
        if position is None:
            continue
        after = position + len(word["original_text"])
        next_position = positions[index + 1] if index + 1 < len(words) else len(transcript)
        if next_position is not None:
            between = transcript[after:next_position]
            if _punctuation_only(between):
                word["text"] += between
    return words


def _owner(midpoint, windows):
    for window in windows:
        if window["core_start_sec"] <= midpoint < window["core_end_sec"]:
            return window["index"]
    return None


def _deduplicate(candidates, windows):
    """Match one-to-one only across adjacent windows, with identical raw tokens.

    Owning-core observations win. If small drift places both copies on the wrong
    side of the seam, retain one observed token and assign its midpoint to the
    appropriate core instead of losing both copies.
    """
    parents = list(range(len(candidates)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    by_window = {}
    for index, word in enumerate(candidates):
        by_window.setdefault(word["source_window_index"], []).append(index)
    for previous, current in zip(windows, windows[1:]):
        if (current["index"] != previous["index"] + 1
                or abs(previous["core_end_sec"] - current["core_start_sec"]) > 1e-6):
            continue
        pairs = []
        left_indices = by_window.get(previous["index"], [])
        right_indices = by_window.get(current["index"], [])
        for left_index in left_indices:
            left = candidates[left_index]
            for right_index in right_indices:
                right = candidates[right_index]
                if left["original_text"] != right["original_text"]:
                    continue
                overlap = min(left["audio_end"], right["audio_end"]) - max(left["audio_start"], right["audio_start"])
                same_point = (left["audio_start"] == left["audio_end"] == right["audio_start"] == right["audio_end"])
                start_drift = abs(left["audio_start"] - right["audio_start"])
                end_drift = abs(left["audio_end"] - right["audio_end"])
                if ((overlap > 0 or same_point)
                        and max(start_drift, end_drift) <= _MAX_DUPLICATE_DRIFT_SECONDS):
                    pairs.append((start_drift + end_drift, left_index, right_index))
        used_left, used_right = set(), set()
        for _, left, right in sorted(pairs):
            if left in used_left or right in used_right:
                continue
            parents[find(right)] = find(left)
            used_left.add(left)
            used_right.add(right)
    groups = {}
    for index, word in enumerate(candidates):
        groups.setdefault(find(index), []).append(word)
    selected = []
    discarded = 0
    for group in groups.values():
        def preference(word):
            midpoint = (word["audio_start"] + word["audio_end"]) / 2
            own_core = _owner(midpoint, windows) == word["source_window_index"]
            return (not own_core, word["source_window_index"], word["source_result_index"], word["source_word_index"])

        winner = min(group, key=preference)
        owner = _owner((winner["audio_start"] + winner["audio_end"]) / 2, windows)
        if owner is not None:
            selected.append({**winner, "window_index": owner})
            discarded += len(group) - 1
    selected.sort(key=lambda word: (word["audio_start"], word["audio_end"], word["source_window_index"], word["source_word_index"]))
    return selected, discarded


def _map_word(word, time_map, index, warnings):
    start = map_audio_time(time_map, word["audio_start"])
    end = map_audio_time(time_map, word["audio_end"])
    reasons = list(dict.fromkeys(start.get("reasons", []) + end.get("reasons", [])))
    if start["video_sec"] is None or end["video_sec"] is None:
        warnings.append({"code": "video_time_unmapped", "word_index": index})
    return {
        **word, "id": f"w{index + 1:06d}",
        "start": start["video_sec"], "end": end["video_sec"],
        "video_start": start["video_sec"], "video_end": end["video_sec"],
        "mapping_uncertain": bool(start["uncertain"] or end["uncertain"]),
        "mapping_quality": "uncertain" if "uncertain" in {start["quality"], end["quality"]} else "observed",
        "mapping_reasons": reasons,
    }


def _sentence(group, index, reason):
    last = max(group, key=lambda word: word["audio_end"])
    return {
        "id": f"s{index + 1:04d}", "text": "".join(word["text"] for word in group).strip(),
        "start": group[0]["start"], "end": last["end"],
        "video_start": group[0]["video_start"], "video_end": last["video_end"],
        "audio_start": min(word["audio_start"] for word in group),
        "audio_end": max(word["audio_end"] for word in group),
        "words": list(group), "boundary_reason": reason,
        "mapping_uncertain": any(word["mapping_uncertain"] for word in group),
    }


def merge_transcript(windows: list[dict], time_map: dict, *, complete: bool) -> dict:
    """Merge available windows; unfinished, unterminated tails remain pending.

    Neither a cloud result boundary nor a recognition-window boundary closes a
    sentence. ``capture_end`` closes storage coverage, not guaranteed semantics.
    Inputs are never modified. No pronunciation, translation, or ASR correction
    is generated by this module.
    """
    if not isinstance(windows, list) or not isinstance(time_map, dict) or not isinstance(complete, bool):
        raise ValueError("Invalid transcript merge input")
    ordered = sorted((_window(window) for window in windows), key=lambda item: item["index"])
    if len({window["index"] for window in ordered}) != len(ordered):
        raise ValueError("Duplicate transcription window index")
    for previous, current in zip(ordered, ordered[1:]):
        if current["core_start_sec"] < previous["core_end_sec"]:
            raise ValueError("Overlapping transcription cores")
    warnings, unmapped, raw_transcripts, candidates = [], [], [], []
    for previous, current in zip(ordered, ordered[1:]):
        if (current["index"] != previous["index"] + 1
                or abs(previous["core_end_sec"] - current["core_start_sec"]) > 1e-6):
            warnings.append({"code": "missing_window_coverage", "after_window_index": previous["index"],
                             "before_window_index": current["index"]})
    for window in ordered:
        for result_index, result in enumerate(window["results"]):
            text = result.get("text", "") if isinstance(result, dict) else ""
            raw_transcripts.append({"window_index": window["index"], "result_index": result_index,
                                    "text": text if isinstance(text, str) else ""})
            candidates.extend(_result_words(result, window, result_index, warnings, unmapped))
    selected, discarded = _deduplicate(candidates, ordered)
    words = [_map_word(word, time_map, index, warnings) for index, word in enumerate(selected)]
    sentences, group = [], []
    for word in words:
        if group and word["audio_start"] - max(item["audio_end"] for item in group) >= _GAP_SECONDS - 1e-9:
            sentences.append(_sentence(group, len(sentences), "gap"))
            group = []
        group.append(word)
        if _terminal(word["text"]):
            sentences.append(_sentence(group, len(sentences), "punctuation"))
            group = []
    pending = None
    if group:
        if complete:
            sentences.append(_sentence(group, len(sentences), "capture_end"))
        else:
            pending = _sentence(group, len(sentences), "pending")
    return {
        "schema_version": 1, "complete": complete,
        "words": words, "sentences": sentences, "pending_sentence": pending,
        "warnings": warnings, "raw_transcripts": raw_transcripts, "unmapped_results": unmapped,
        "transcript": "".join(word["text"] for word in words).strip(),
        "finalized_transcript": "".join(sentence["text"] for sentence in sentences),
        "deduplicated_word_count": discarded,
    }
