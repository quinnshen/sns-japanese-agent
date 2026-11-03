import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import collect_words, punctuate_result_words, sentences_from_words, transcribe_media


class PipelineTests(unittest.TestCase):
    def test_google_transcript_punctuation_is_attached_to_timed_words(self):
        result = {
            "text": "選手です。画面上の選手です。",
            "words": [
                {"text": "選手", "start": 0, "end": 0.2},
                {"text": "です", "start": 0.2, "end": 0.4},
                {"text": "画面", "start": 0.5, "end": 0.7},
                {"text": "上", "start": 0.7, "end": 0.8},
                {"text": "の", "start": 0.8, "end": 0.9},
                {"text": "選手", "start": 0.9, "end": 1.1},
                {"text": "です", "start": 1.1, "end": 1.3},
            ],
        }
        words = punctuate_result_words(result)
        self.assertEqual(words[1]["text"], "です。")
        self.assertEqual(words[-1]["text"], "です。")
        self.assertEqual(len(sentences_from_words(words)), 2)

    def test_overlap_keeps_one_copy_and_maps_to_video_time(self):
        first = [{"words": [{"text": "前。", "start": 42.0, "end": 42.3},
                            {"text": "重複", "start": 44.0, "end": 44.2}]}]
        second = [{"words": [{"text": "重複", "start": 1.0, "end": 1.2},
                             {"text": "後。", "start": 2.0, "end": 2.3}]}]
        words = collect_words(first, 0, 43, False) + collect_words(second, 43, 86, True)
        self.assertEqual([word["text"] for word in words], ["前。", "重複", "後。"])
        self.assertEqual(words[1]["start"], 44.0)

    def test_exactly_45_seconds_uses_one_google_request(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("pipeline.wav_slice", return_value=b"wav") as wav:
                result = transcribe_media(
                    Path(directory) / "unused.webm", 45.0,
                    recognize=lambda _: [{"words": [{"text": "こんにちは。", "start": 0.1, "end": 0.8}]}],
                )
        self.assertEqual(wav.call_count, 1)
        self.assertEqual(result["chunk_count"], 1)
        self.assertEqual(result["sentences"][0]["text"], "こんにちは。")

    def test_gap_and_punctuation_create_sentences(self):
        words = [
            {"text": "そう。", "start": 0.0, "end": 0.5},
            {"text": "次", "start": 2.0, "end": 2.3},
            {"text": "です。", "start": 2.3, "end": 2.8},
        ]
        sentences = sentences_from_words(words)
        self.assertEqual([item["text"] for item in sentences], ["そう。", "次です。"])
        self.assertEqual([(item["start"], item["end"]) for item in sentences], [(0.0, 0.5), (2.0, 2.8)])


if __name__ == "__main__":
    unittest.main()
