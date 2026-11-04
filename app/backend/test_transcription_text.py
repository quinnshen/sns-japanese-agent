import copy
import json
import unittest

from transcription_text import merge_transcript


def window(index, results):
    core_start = index * 43
    return {
        "index": index, "core_start_sec": core_start, "core_end_sec": core_start + 43,
        "audio_start_sec": max(0, core_start - 1), "audio_end_sec": core_start + 44,
        "results": results,
    }


def result(text, *words):
    return {"text": text, "words": [
        {"text": text, "start": start, "end": end} for text, start, end in words
    ]}


def mapping():
    return {
        "audio_duration_sec": 200, "quality": "observed", "warnings": ["synthetic_observed_mapping"],
        "segments": [{"audio_start_sec": 0, "audio_end_sec": 200,
                      "video_start_sec": 480, "rate": 1, "uncertain": True}],
    }


class TranscriptMergeTests(unittest.TestCase):
    def merge(self, windows, *, complete=False, time_map=None):
        return merge_transcript(windows, time_map if time_map is not None else mapping(), complete=complete)

    def test_sentence_crosses_result_and_window_boundaries_without_false_splits(self):
        first = result("これを", ("これ", 42.1, 42.3), ("を", 42.3, 42.5))
        first["words"][-1]["result_end"] = True
        second = result("ここに持ってくれば", ("ここに", 1.1, 1.4), ("持ってくれば", 1.4, 1.8))
        third = result("成功です。", ("成功", 1.8, 2.1), ("です", 2.1, 2.5))
        windows = [window(0, [first]), window(1, [second, third])]
        before = copy.deepcopy(windows)
        merged = self.merge(windows)
        self.assertEqual(len(merged["sentences"]), 1)
        sentence = merged["sentences"][0]
        self.assertEqual(sentence["text"], "これをここに持ってくれば成功です。")
        self.assertEqual(sentence["boundary_reason"], "punctuation")
        self.assertAlmostEqual(sentence["audio_start"], 42.1)
        self.assertAlmostEqual(sentence["audio_end"], 44.5)
        self.assertAlmostEqual(sentence["start"], 522.1)
        self.assertAlmostEqual(sentence["end"], 524.5)
        self.assertEqual(windows, before, "ASR input must never be silently rewritten")

    def test_unfinished_tail_stays_pending_then_joins_next_window(self):
        first = window(0, [result("これ", ("これ", 42.7, 42.9))])
        pending = self.merge([first])
        self.assertEqual(pending["sentences"], [])
        self.assertEqual(pending["pending_sentence"]["text"], "これ")
        self.assertEqual(pending["transcript"], "これ")
        self.assertEqual(pending["finalized_transcript"], "")
        merged = self.merge([first, window(1, [result("です。", ("です", 1.05, 1.3))])])
        self.assertEqual([sentence["text"] for sentence in merged["sentences"]], ["これです。"])
        self.assertIsNone(merged["pending_sentence"])

    def test_complete_unterminated_tail_is_explicit_capture_end(self):
        merged = self.merge([window(0, [result("これを", ("これ", 1, 1.2), ("を", 1.2, 1.3))])], complete=True)
        self.assertEqual(merged["sentences"][0]["text"], "これを")
        self.assertEqual(merged["sentences"][0]["boundary_reason"], "capture_end")
        self.assertIsNone(merged["pending_sentence"])
        self.assertNotIn("translation_zh", merged["sentences"][0])

    def test_actual_asr_punctuation_is_attached_and_closes_sentences(self):
        raw = result("何が必要か。膝です。", ("何", 0, 0.2), ("が", 0.2, 0.3),
                     ("必要", 0.3, 0.6), ("か", 0.6, 0.7), ("膝", 0.8, 1), ("です", 1, 1.2))
        merged = self.merge([window(0, [raw])])
        self.assertEqual([sentence["text"] for sentence in merged["sentences"]], ["何が必要か。", "膝です。"])
        self.assertEqual(merged["words"][3]["original_text"], "か")
        self.assertEqual(merged["words"][3]["text"], "か。")
        self.assertEqual(merged["raw_transcripts"][0]["text"], raw["text"])
        self.assertNotIn("(", merged["transcript"], "This stage must not invent pronunciation annotations")

    def test_observed_gap_closes_previous_sentence_but_not_last_tail(self):
        merged = self.merge([window(0, [result("前後", ("前", 0, 0.3), ("後", 1.5, 1.7))])])
        self.assertEqual([sentence["text"] for sentence in merged["sentences"]], ["前"])
        self.assertEqual(merged["sentences"][0]["boundary_reason"], "gap")
        self.assertEqual(merged["pending_sentence"]["text"], "後")

    def test_sentence_timing_covers_overlapping_word_ends(self):
        merged = self.merge([window(0, [result("長い語末。", ("長い語", 0, 2), ("末", 1, 1.5))])])
        sentence = merged["sentences"][0]
        self.assertEqual(sentence["audio_end"], 2)
        self.assertEqual(sentence["end"], 482)

    def test_exact_midpoint_at_seam_has_one_right_hand_owner(self):
        merged = self.merge([
            window(0, [result("はい", ("はい", 42.9, 43.1))]),
            window(1, [result("はい。", ("はい", 0.9, 1.1))]),
        ])
        self.assertEqual(len(merged["words"]), 1)
        self.assertEqual(merged["words"][0]["window_index"], 1)
        self.assertEqual(merged["words"][0]["source_window_index"], 1)
        self.assertEqual(merged["transcript"], "はい。")

    def test_small_drift_cannot_duplicate_token_when_both_copies_own_their_core(self):
        merged = self.merge([
            window(0, [result("はい。", ("はい", 42.8, 43.05))]),
            window(1, [result("はい。", ("はい", 1, 1.2))]),
        ])
        self.assertEqual(merged["transcript"], "はい。")
        self.assertEqual(len(merged["words"]), 1)
        self.assertEqual(merged["deduplicated_word_count"], 1)

    def test_small_drift_cannot_lose_token_when_both_copies_fall_outside_their_own_core(self):
        merged = self.merge([
            window(0, [result("はい。", ("はい", 42.95, 43.25))]),
            window(1, [result("はい。", ("はい", 0.75, 1.05))]),
        ])
        self.assertEqual(merged["transcript"], "はい。")
        self.assertEqual(len(merged["words"]), 1)
        word = merged["words"][0]
        self.assertEqual(word["window_index"], 1)
        self.assertTrue(43 <= (word["audio_start"] + word["audio_end"]) / 2 < 86)

    def test_real_repeated_words_across_seam_survive_context_deduplication(self):
        merged = self.merge([
            window(0, [result("はいはい", ("はい", 42.8, 42.95), ("はい", 43.05, 43.2))]),
            window(1, [result("はいはい。", ("はい", 0.8, 0.95), ("はい", 1.05, 1.2))]),
        ])
        self.assertEqual(merged["transcript"], "はいはい。")
        self.assertEqual(len(merged["words"]), 2)
        self.assertEqual(merged["deduplicated_word_count"], 2)

    def test_different_asr_tokens_are_not_guessed_into_one_token(self):
        merged = self.merge([
            window(0, [result("はい", ("はい", 42.8, 43.05))]),
            window(1, [result("ええ。", ("ええ", 1, 1.2))]),
        ])
        self.assertEqual([word["original_text"] for word in merged["words"]], ["はい", "ええ"])

    def test_unprocessed_future_core_is_not_claimed_by_previous_window_context(self):
        first = window(0, [result("先次", ("先", 42.5, 42.7), ("次", 43.1, 43.3))])
        initial = self.merge([first])
        self.assertEqual(initial["transcript"], "先")
        merged = self.merge([first, window(1, [])])
        self.assertEqual(merged["transcript"], "先次")

    def test_transcript_without_timed_words_is_preserved_and_warned(self):
        merged = self.merge([window(0, [{"text": "聞き取れた文です。", "words": []}])], complete=True)
        self.assertEqual(merged["words"], [])
        self.assertEqual(merged["raw_transcripts"][0]["text"], "聞き取れた文です。")
        self.assertEqual(merged["unmapped_results"][0]["text"], "聞き取れた文です。")
        self.assertIn("missing_word_timestamps", [warning["code"] for warning in merged["warnings"]])

    def test_missing_text_in_timed_words_is_not_silently_dropped(self):
        merged = self.merge([window(0, [result("これは猫です。", ("これ", 0, 0.2), ("は", 0.2, 0.3), ("です", 0.5, 0.7))])])
        self.assertIn("text_word_mismatch", [warning["code"] for warning in merged["warnings"]])
        self.assertEqual(merged["unmapped_results"][0]["text"], "これは猫です。")
        self.assertIn("猫", merged["unmapped_results"][0]["unmatched_text"])

    def test_invalid_word_times_do_not_discard_raw_asr_or_generate_invalid_json(self):
        for invalid in (True, -1, float("nan"), float("inf")):
            with self.subTest(invalid=repr(invalid)):
                merged = self.merge([window(0, [result("原文。", ("原文", invalid, 1))])])
                self.assertEqual(merged["words"], [])
                self.assertEqual(merged["raw_transcripts"][0]["text"], "原文。")
                self.assertIn("invalid_word_timestamps", [warning["code"] for warning in merged["warnings"]])
                json.dumps(merged, allow_nan=False)

    def test_before_first_playing_keeps_audio_time_and_marks_video_time_unknown(self):
        time_map = mapping()
        time_map["segments"] = [
            {"audio_start_sec": 0, "audio_end_sec": 0.5, "video_start_sec": None, "rate": 0, "uncertain": True},
            {"audio_start_sec": 0.5, "audio_end_sec": 200, "video_start_sec": 480, "rate": 1, "uncertain": True},
        ]
        merged = self.merge([window(0, [result("はい。", ("はい", 0.1, 0.3))])], time_map=time_map)
        word = merged["words"][0]
        self.assertEqual(word["audio_start"], 0.1)
        self.assertIsNone(word["start"])
        self.assertIsNone(word["end"])
        self.assertTrue(word["mapping_uncertain"])
        self.assertIn("video_time_unmapped", [warning["code"] for warning in merged["warnings"]])

    def test_observed_waiting_segment_is_used_without_total_duration_rescaling(self):
        time_map = mapping()
        time_map.update(quality="uncertain", warnings=["buffering_observed_alignment_is_approximate"])
        time_map["segments"] = [
            {"audio_start_sec": 0, "audio_end_sec": 2, "video_start_sec": 480, "rate": 1, "uncertain": True},
            {"audio_start_sec": 2, "audio_end_sec": 4, "video_start_sec": 482, "rate": 0, "uncertain": True},
            {"audio_start_sec": 4, "audio_end_sec": 200, "video_start_sec": 482, "rate": 1, "uncertain": True},
        ]
        merged = self.merge([window(0, [result("待つ次。", ("待つ", 3, 3.2), ("次", 5, 5.2))])], time_map=time_map)
        self.assertEqual(merged["words"][0]["start"], 482)
        self.assertEqual(merged["words"][1]["start"], 483)
        self.assertEqual(merged["words"][1]["mapping_quality"], "uncertain")


if __name__ == "__main__":
    unittest.main()
