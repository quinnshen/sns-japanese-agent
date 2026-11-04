"""Pure learning-note planning/validation tests with invented sentences only."""

import copy
import unittest

from learning_text import STYLE_VERSION, missing_furigana, plan_batches, reading_preserves_source, recover_note_readings, validate_items


def sentences(count, text="これは文です。"):
    return [{"id": f"s{index + 1:04d}", "text": text, "study": {"ignored": True}} for index in range(count)]


def item(identifier="s0001", reading="これでいい。", **changes):
    value = {
        "id": identifier, "reading_text": reading, "translation_zh": "这样就可以。",
        "key_points": ["「でいい」：表示当前方式已经足够。"], "context_note": "", "asr_doubt": "",
    }
    value.update(changes)
    return value


class PlanningTests(unittest.TestCase):
    def test_incomplete_full_group_waits_for_two_future_sentences(self):
        self.assertEqual(plan_batches(sentences(6), complete=False), [])
        self.assertEqual(plan_batches(sentences(7), complete=False), [])
        batch = plan_batches(sentences(8), complete=False)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["target_ids"], [f"s{index:04d}" for index in range(1, 7)])
        self.assertEqual(len(batch[0]["context"]), 8)

    def test_emitted_groups_and_context_remain_stable_when_input_grows(self):
        first = plan_batches(sentences(8), complete=False)
        grown = plan_batches(sentences(20), complete=False)
        self.assertEqual(grown[:len(first)], first)
        self.assertEqual(len(grown), 3)
        self.assertEqual(grown[1]["context"][0]["id"], "s0005")
        self.assertEqual(grown[1]["context"][-1]["id"], "s0014")

    def test_character_boundary_can_close_a_smaller_stable_group(self):
        source = sentences(6, text="かなかな")
        groups = plan_batches(source, complete=False, max_chars=9)
        self.assertEqual([len(batch["targets"]) for batch in groups], [2, 2])
        self.assertEqual(groups[1]["target_ids"], ["s0003", "s0004"])

    def test_complete_input_flushes_tail_with_available_context(self):
        groups = plan_batches(sentences(7), complete=True)
        self.assertEqual([len(batch["targets"]) for batch in groups], [6, 1])
        self.assertEqual(groups[1]["context"][0]["id"], "s0005")
        self.assertEqual(groups[1]["index"], 1)

    def test_source_and_batch_context_are_independent_copies(self):
        source = sentences(8)
        original = copy.deepcopy(source)
        groups = plan_batches(source, complete=False)
        self.assertEqual(source, original)
        self.assertEqual(set(groups[0]["targets"][0]), {"id", "text"})
        source[0]["text"] = "変更"
        self.assertEqual(groups[0]["targets"][0]["text"], original[0]["text"])
        groups[0]["targets"][0]["text"] = "different"
        self.assertEqual(groups[0]["context"][0]["text"], original[0]["text"])

    def test_invalid_or_oversized_source_stops_locally(self):
        cases = [
            lambda: plan_batches(sentences(1), complete=1),
            lambda: plan_batches(sentences(1), complete=True, max_sentences=0),
            lambda: plan_batches(sentences(1), complete=True, context_sentences=-1),
            lambda: plan_batches(sentences(1, "かな"), complete=True, max_chars=1),
            lambda: plan_batches([{"id": "same", "text": "あ"}, {"id": "same", "text": "い"}], complete=True),
        ]
        for run in cases:
            with self.assertRaises(ValueError):
                run()


class AnnotationTests(unittest.TestCase):
    def test_source_aware_matching_preserves_original_kana_parentheses(self):
        pairs = [
            ("そう(かな)。", "そう(かな)。"),
            ("何(なに)が必要か", "何(なに)が必要(ひつよう)か"),
            ("彼は(たぶん)帰る", "彼(かれ)は(たぶん)帰(かえ)る"),
            ("物理的に持ってくれば", "物理的(ぶつりてき)に持(も)ってくれば"),
            ("物理", "物(ぶつ)理(り)"),
        ]
        for source, reading in pairs:
            with self.subTest(source=source):
                self.assertTrue(reading_preserves_source(source, reading))
                self.assertFalse(missing_furigana(reading))

    def test_rewrites_deletions_and_annotations_after_plain_kana_are_rejected(self):
        for source, reading in [
            ("そう(かな)。", "そう。"), ("そう。", "そう(かな)。"),
            ("帰る", "来(く)る"), ("帰る", "帰(かえ)る。"),
        ]:
            with self.subTest(source=source, reading=reading):
                self.assertFalse(reading_preserves_source(source, reading))
        self.assertTrue(missing_furigana("必要(ひつよう)な物"))

    def test_long_sentence_uses_no_recursive_match(self):
        source = "帰る" * 800
        self.assertTrue(reading_preserves_source(source, "帰(かえ)る" * 800))

    def test_invalid_annotation_does_not_discard_a_valid_neighbor(self):
        targets = [{"id": "s0001", "text": "帰る"}, {"id": "s0002", "text": "これでいい。"}]
        result = validate_items([item(reading="来(く)る"), item("s0002")], targets)
        self.assertEqual(result["invalid"], {"s0001": "source_text_changed"})
        self.assertEqual(list(result["valid"]), ["s0002"])
        self.assertEqual(result["valid"]["s0002"]["study"]["style_version"], STYLE_VERSION)

    def test_duplicate_missing_and_unknown_ids_remain_explicit(self):
        targets = [{"id": f"s000{index}", "text": "これでいい。"} for index in range(1, 4)]
        result = validate_items([item(), item(), item("s0002"), item("unknown"), {}], targets)
        self.assertEqual(set(result["valid"]), {"s0002"})
        self.assertEqual(result["invalid"]["s0001"], "duplicate_item_id")
        self.assertEqual(result["invalid"]["s0003"], "missing_item")
        self.assertEqual(result["invalid"]["unknown"], "unknown_item_id")
        self.assertEqual(result["invalid"]["__response__"], "invalid_item_shape")

    def test_all_original_source_parentheses_survive_in_valid_items(self):
        targets = [{"id": "s0001", "text": "彼は(たぶん)帰る"}]
        output = item(reading="彼(かれ)は(たぶん)帰(かえ)る",
                      key_points=["「帰(かえ)る」：基本形；表示回去。"])
        self.assertEqual(validate_items([output], targets)["invalid"], {})
        output["reading_text"] = "彼(かれ)は帰(かえ)る"
        self.assertEqual(validate_items([output], targets)["invalid"]["s0001"], "source_text_changed")

    def test_non_source_japanese_quotes_across_all_note_fields_require_readings(self):
        targets = [{"id": "s0001", "text": "帰る"}]
        for field in ("translation_zh", "context_note", "asr_doubt", "key_points"):
            value = ["「帰れば」：回去的话。"] if field == "key_points" else "需要确认「帰れば」的作用。"
            output = item(reading="帰(かえ)る", **{field: value})
            with self.subTest(field=field):
                self.assertEqual(validate_items([output], targets)["invalid"]["s0001"], "missing_note_reading")

    def test_chinese_explanations_are_not_mistaken_for_japanese(self):
        targets = [{"id": "s0001", "text": "帰る"}]
        output = item(reading="帰(かえ)る", key_points=["「帰(かえ)る」：基本形；在这里表示回去，不是整个语法课程。"],
                      translation_zh="回去。", context_note="中文里的「这个位置」需要画面确认。")
        self.assertEqual(validate_items([output], targets)["invalid"], {})

    def test_unquoted_form_change_and_source_kanji_head_need_readings(self):
        targets = [{"id": "s0001", "text": "成功なら帰る"}]
        for point in ("成功：名词。", "词形：帰る → 帰れば；条件表达。"):
            output = item(reading="成功(せいこう)なら帰(かえ)る", key_points=[point])
            self.assertEqual(validate_items([output], targets)["invalid"]["s0001"], "missing_note_reading")

    def test_quoted_form_changes_do_not_parse_the_following_chinese_explanation(self):
        cases = [
            ("見た。", "見(み)た。", "「見(み)る → 見(み)た」：动词た形，表示曾经看过。"),
            ("ありますね。", "ありますね。", "「ある → あります」：动词ます形，表示存在；「ね」带有确认语气。"),
            ("見た。", "見(み)た。", "「見(み)る」 → 「見(み)た」：动词た形，表示曾经看过。"),
        ]
        for source, reading, point in cases:
            with self.subTest(point=point):
                output = item(reading=reading, key_points=[point])
                targets = [{"id": "s0001", "text": source}]
                self.assertEqual(validate_items([output], targets)["invalid"], {})

    def test_unquoted_form_boundaries_stop_before_chinese_after_a_colon(self):
        targets = [{"id": "s0001", "text": "見た。"}]
        valid = [
            "词形：見(み)る → 見(み)た：动词た形，表示曾经看过。",
            "ある → あります：动词ます形，表示存在。",
            "词形：見(み)る → 見(み)て → 見(み)た：动词た形。",
        ]
        for point in valid:
            with self.subTest(point=point):
                output = item(reading="見(み)た。", key_points=[point])
                self.assertEqual(validate_items([output], targets)["invalid"], {})
        for point in (
            "词形：見る → 見(み)た：动词た形，表示曾经看过。",
            "词形：見(み)る → 見た：动词た形，表示曾经看过。",
            "「見る → 見(み)た」：动词た形，表示曾经看过。",
        ):
            with self.subTest(point=point):
                output = item(reading="見(み)た。", key_points=[point])
                self.assertEqual(validate_items([output], targets)["invalid"]["s0001"], "missing_note_reading")

    def test_non_source_quote_missing_reading_in_asr_doubt_is_still_rejected(self):
        targets = [{"id": "s0001", "text": "見た。"}]
        output = item(reading="見(み)た。", asr_doubt="「見る」可能不完整；需回听，不能根据动词た形猜测。")
        self.assertEqual(validate_items([output], targets)["invalid"]["s0001"], "missing_note_reading")
        output["asr_doubt"] = "「見(み)る」可能不完整；需回听，不能根据动词た形猜测。"
        self.assertEqual(validate_items([output], targets)["invalid"], {})

    def test_uncertain_sentence_can_have_no_points_when_doubt_is_explicit(self):
        output = item(key_points=[], translation_zh="目前无法可靠确定整句意思。", asr_doubt="原文可能不完整，需要回听。")
        targets = [{"id": "s0001", "text": "これでいい。"}]
        self.assertEqual(validate_items([output], targets)["invalid"], {})
        output["asr_doubt"] = ""
        self.assertEqual(validate_items([output], targets)["invalid"]["s0001"], "missing_explanation")


class NoteReadingRecoveryTests(unittest.TestCase):
    def test_exact_source_quotes_reuse_readings_in_every_note_field_without_mutation(self):
        source = "今日は帰る。"
        output = item(reading="今日(きょう)は帰(かえ)る。", translation_zh="「今日は」表示今天；回去。",
                      key_points=["「帰る」：基本形。"], context_note="「今日は帰る」需要语境确认。",
                      asr_doubt="「帰る」这个片段需要回听。")
        before = copy.deepcopy(output)
        result = validate_items([output], [{"id": "s0001", "text": source}])
        self.assertEqual(result["invalid"], {})
        self.assertEqual(output, before)
        notes = result["valid"]["s0001"]
        self.assertEqual(notes["translation_zh"], "「今日(きょう)は」表示今天；回去。")
        self.assertEqual(notes["study"]["key_points"], ["「帰(かえ)る」：基本形。"])
        self.assertEqual(notes["study"]["context_note"], "「今日(きょう)は帰(かえ)る」需要语境确认。")
        self.assertEqual(notes["study"]["asr_doubt"], "「帰(かえ)る」这个片段需要回听。")
        self.assertEqual(notes["study"]["reading_text"], before["reading_text"])

    def test_chinese_explanation_unrelated_quotes_and_existing_annotations_are_unchanged(self):
        output = item(reading="帰(かえ)る。", key_points=["「帰(かえ)る」：动词基本形，表示回去。"],
                      context_note="中文里的「这个位置」需要画面确认。", translation_zh="回去。",
                      asr_doubt="「帰(く)る」是已有标注，不覆盖它。")
        self.assertEqual(recover_note_readings(output, "帰る。"), output)

    def test_repeated_homographs_require_one_unique_existing_reading(self):
        output = item(reading="生(なま)と生(せい)", key_points=["「生」：需结合上下文。"])
        self.assertEqual(recover_note_readings(output, "生と生"), output)
        result = validate_items([output], [{"id": "s0001", "text": "生と生"}])
        self.assertEqual(result["invalid"], {"s0001": "missing_note_reading"})
        output["reading_text"] = "生(なま)と生(なま)"
        recovered = recover_note_readings(output, "生と生")
        self.assertEqual(recovered["key_points"], ["「生(なま)」：需结合上下文。"])

    def test_quotes_must_contain_whole_compound_or_numeric_counter_reading(self):
        cases = [
            ("物理的", "物理的(ぶつりてき)", "物理", "物理的"),
            ("1回", "1回(いっかい)", "回", "1回"),
        ]
        for source, reading, partial, whole in cases:
            with self.subTest(source=source):
                output = item(reading=reading, key_points=[f"「{partial}」：需要确认。"])
                self.assertEqual(recover_note_readings(output, source), output)
                output["key_points"] = [f"「{whole}」：需要确认。"]
                recovered = recover_note_readings(output, source)
                self.assertEqual(recovered["key_points"], [f"「{reading}」：需要确认。"])

    def test_literal_source_kana_parentheses_disable_recovery(self):
        output = item(reading="彼(かれ)は(たぶん)帰(かえ)る", key_points=["「帰る」：回去。"])
        source = "彼は(たぶん)帰る"
        self.assertEqual(recover_note_readings(output, source), output)
        self.assertEqual(validate_items([output], [{"id": "s0001", "text": source}])["invalid"],
                         {"s0001": "missing_note_reading"})

    def test_wrong_source_and_missing_source_readings_never_enable_recovery(self):
        for source, reading, error in (
            ("帰る", "来(く)る", "source_text_changed"),
            ("持つ物", "持(も)つ物", "missing_reading"),
        ):
            with self.subTest(source=source):
                output = item(reading=reading, key_points=[f"「{source}」：需要确认。"])
                self.assertEqual(recover_note_readings(output, source), output)
                self.assertEqual(validate_items([output], [{"id": "s0001", "text": source}])["invalid"],
                                 {"s0001": error})

    def test_partly_annotated_quotes_are_not_repaired(self):
        output = item(reading="持(も)つ物(もの)", key_points=["「持(も)つ物」：需要确认。"])
        self.assertEqual(recover_note_readings(output, "持つ物"), output)
        self.assertEqual(validate_items([output], [{"id": "s0001", "text": "持つ物"}])["invalid"],
                         {"s0001": "missing_note_reading"})


if __name__ == "__main__":
    unittest.main()
