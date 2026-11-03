import unittest

from enrich import BATCH_SIZE, enrich_result, validate_batch


def item(sentence_id, text, meaning="是的。"):
    return {
        "id": sentence_id,
        "reading_text": text,
        "translation_zh": meaning,
        "key_points": ["「そう」：承接前文的说法。"],
        "context_note": "",
        "asr_doubt": "",
    }


class EnrichmentTests(unittest.TestCase):
    def test_one_batch_attaches_translation_and_analysis_without_changing_asr(self):
        original = {"status": "transcribed", "sentences": [
            {"id": "s0001", "text": "そうです。", "start": 1.0},
            {"id": "s0002", "text": "物理的に。", "start": 2.0},
        ]}
        calls = []

        def fake(context, ids):
            calls.append((context, ids))
            return [item("s0002", "物理的(ぶつりてき)に。", "从物理上说。"), item("s0001", "そうです。")], {"input_tokens": 25, "output_tokens": 12}

        result = enrich_result(original, enricher=fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual([s["text"] for s in result["sentences"]], ["そうです。", "物理的に。"])
        self.assertEqual(result["sentences"][1]["study"]["reading_text"], "物理的(ぶつりてき)に。")
        self.assertEqual(result["sentences"][1]["translation_zh"], "从物理上说。")
        self.assertEqual(result["enrichment_usage"]["output_tokens"], 12)
        self.assertEqual(result["status"], "enriched")
        self.assertNotIn("study", original["sentences"][0])

    def test_cached_study_is_not_sent_again(self):
        original = {"sentences": [
            {"id": "s0001", "text": "はい。", "translation_zh": "好的。", "study": {"style_version": 1, "reading_text": "はい。"}},
            {"id": "s0002", "text": "またね。"},
        ]}
        calls = []

        def fake(_context, ids):
            calls.append(ids)
            return [item("s0002", "またね。", "回头见。")], {}

        result = enrich_result(original, enricher=fake)
        self.assertEqual(calls, [["s0002"]])
        self.assertEqual(result["sentences"][0]["translation_zh"], "好的。")

    def test_cached_sentence_with_missing_furigana_is_reprocessed(self):
        original = {"sentences": [
            {"id": "s0001", "text": "1回。", "translation_zh": "一次。",
             "study": {"style_version": 1, "reading_text": "1回。"}},
        ]}
        calls = []

        def fake(_context, ids):
            calls.append(ids)
            return [item("s0001", "1回(かい)。", "一次。")], {}

        result = enrich_result(original, enricher=fake)
        self.assertEqual(calls, [["s0001"]])
        self.assertEqual(result["sentences"][0]["study"]["reading_text"], "1回(かい)。")

    def test_wrong_id_or_modified_asr_is_rejected(self):
        targets = [{"id": "s0001", "text": "物理的に。"}]
        with self.assertRaisesRegex(RuntimeError, "ID"):
            validate_batch([item("wrong", "物理的(ぶつりてき)に。")], targets)
        with self.assertRaisesRegex(RuntimeError, "ASR"):
            validate_batch([item("s0001", "物理的(ぶつりてき)です。")], targets)
        with self.assertRaisesRegex(RuntimeError, "未标注"):
            validate_batch([item("s0001", "物理的に。")], targets)

    def test_bad_sentence_is_retried_without_blocking_neighbors(self):
        original = {"sentences": [
            {"id": "s0001", "text": "物理的に。"},
            {"id": "s0002", "text": "そうです。"},
        ]}
        calls = []

        def fake(_context, ids):
            calls.append(ids)
            if len(ids) == 2:
                return [item("s0001", "物理的です。"), item("s0002", "そうです。")], {"input_tokens": 10}
            return [item("s0001", "物理的(ぶつりてき)に。")], {"input_tokens": 2}

        fixes = []
        def fix(source):
            fixes.append(source)
            return "物理的(ぶつりてき)に。", {"input_tokens": 2}

        result = enrich_result(original, enricher=fake, furigana_fixer=fix)
        self.assertEqual(calls, [["s0001", "s0002"]])
        self.assertEqual(fixes, ["物理的に。"])
        self.assertEqual(result["status"], "enriched")
        self.assertEqual(result["enrichment_usage"]["input_tokens"], 12)
        self.assertTrue(all(sentence.get("study") for sentence in result["sentences"]))

    def test_persistent_bad_sentence_does_not_block_later_batch(self):
        original = {"sentences": [
            {"id": f"s{number:04d}", "text": "物理的に。"}
            for number in range(1, BATCH_SIZE + 2)
        ]}

        def fake(_context, ids):
            return [item(sentence_id, "物理的です。" if sentence_id == "s0001" else "物理的(ぶつりてき)に。") for sentence_id in ids], {}

        result = enrich_result(original, enricher=fake,
            furigana_fixer=lambda _source: ("物理的です。", {}))
        self.assertEqual(result["status"], "partially_enriched")
        self.assertEqual(result["enrichment_error"], "1 句尚未解析：s0001")
        self.assertNotIn("study", result["sentences"][0])
        self.assertTrue(all(sentence.get("study") for sentence in result["sentences"][1:]))

    def test_batches_and_progress(self):
        original = {"sentences": [
            {"id": f"s{number:04d}", "text": "そうです。"}
            for number in range(1, BATCH_SIZE + 2)
        ]}
        batch_sizes, saved = [], []

        def fake(_context, ids):
            batch_sizes.append(len(ids))
            return [item(sentence_id, "そうです。") for sentence_id in ids], {}

        result = enrich_result(original, enricher=fake,
            progress=lambda done, total, current: saved.append((done, total, current["status"])))
        self.assertEqual(batch_sizes, [BATCH_SIZE, 1])
        self.assertEqual(saved, [(1, 2, "enriching"), (2, 2, "enriched")])
        self.assertEqual(len([s for s in result["sentences"] if s.get("study")]), BATCH_SIZE + 1)


if __name__ == "__main__":
    unittest.main()
