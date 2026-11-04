"""Offline OpenAI provider tests; all config reads and SDK clients are mocked."""

import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import learning_openai as provider
from learning_text import plan_batches


def response(*, status="completed", output_text=None, output=None, **changes):
    value = SimpleNamespace(
        status=status, output_text=json.dumps({"items": []}) if output_text is None else output_text,
        output=[] if output is None else output, usage=SimpleNamespace(input_tokens=40, output_tokens=20),
        id="resp_offline_fixture",
    )
    for field, content in changes.items():
        setattr(value, field, content)
    return value


class OpenAIStudyProviderTests(unittest.TestCase):
    def setUp(self):
        def settings(name, default=None):
            return {"OPENAI_MODEL": "gpt-6-luna", "OPENAI_API_KEY": "operator-key-for-offline-test"}.get(name, default)

        self.settings = patch.object(provider, "setting", side_effect=settings)
        self.setting = self.settings.start()
        self.clients = patch("openai.OpenAI")
        self.client_type = self.clients.start()
        self.client = self.client_type.return_value
        self.client.responses.create.return_value = response()
        self.addCleanup(self.clients.stop)
        self.addCleanup(self.settings.stop)
        self.batch = plan_batches([{"id": "s0001", "text": "これでいい。"}], complete=True)[0]

    def test_offline_constructor_only_reads_public_model_configuration(self):
        instance = provider.OpenAIStudyProvider()
        self.assertEqual(instance.config["model"], "gpt-6-luna")
        self.assertEqual([call.args[0] for call in self.setting.call_args_list], ["OPENAI_MODEL"])
        self.client_type.assert_not_called()
        self.assertEqual(instance.config["style_version"], 2)
        self.assertFalse(instance.config["store"])
        self.assertNotIn("operator-key-for-offline-test", json.dumps(instance.config))
        self.assertRegex(instance.provider_fingerprint, r"^[0-9a-f]{64}$")

    def test_explicit_model_is_preserved_without_model_lookup(self):
        instance = provider.OpenAIStudyProvider(model="custom-model")
        self.assertEqual(instance.config["model"], "custom-model")
        self.setting.assert_not_called()
        self.assertNotEqual(instance.provider_fingerprint, provider.OpenAIStudyProvider(model="other-model").provider_fingerprint)

    def test_prepare_is_idempotent_and_uses_only_the_operators_key(self):
        instance = provider.OpenAIStudyProvider()
        self.assertIs(instance.prepare(), instance)
        self.assertIs(instance.prepare(), instance)
        self.client_type.assert_called_once_with(api_key="operator-key-for-offline-test", max_retries=0, timeout=120)
        self.client.responses.create.assert_not_called()

    def test_missing_or_placeholder_key_cannot_construct_client(self):
        for key in ("", "YOUR_OPENAI_API_KEY", "REPLACE_ME"):
            self.setting.side_effect = lambda name, default=None, key=key: key if name == "OPENAI_API_KEY" else "gpt-6-luna"
            with self.subTest(key=key), self.assertRaises(provider.ProviderError) as caught:
                provider.OpenAIStudyProvider().prepare()
            self.assertEqual(caught.exception.retry_class, "not_sent")
        self.client_type.assert_not_called()

    def test_invalid_batch_is_rejected_before_key_lookup(self):
        bad = copy.deepcopy(self.batch)
        bad["context"][0]["text"] = "changed context"
        instance = provider.OpenAIStudyProvider(model="gpt-6-luna")
        with self.assertRaises(provider.ProviderError) as caught:
            instance.generate(bad)
        self.assertEqual(caught.exception.error_code, "openai_batch_invalid")
        self.assertEqual(caught.exception.retry_class, "not_sent")
        self.setting.assert_not_called()
        self.client_type.assert_not_called()

    def test_filtered_retry_targets_can_retain_frozen_full_context(self):
        whole = plan_batches([{"id": f"s{index:04d}", "text": "これでいい。"} for index in range(1, 9)], complete=False)[0]
        whole["targets"] = whole["targets"][:1]
        whole["target_ids"] = [whole["targets"][0]["id"]]
        self.assertEqual(len(provider.OpenAIStudyProvider().validate_batch(whole)["context"]), 8)

    def test_generation_parameters_are_strict_private_and_bounded(self):
        instance = provider.OpenAIStudyProvider()
        result = instance.generate(self.batch)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"], {"input_tokens": 40, "output_tokens": 20})
        self.client.responses.create.assert_called_once()
        arguments = self.client.responses.create.call_args.kwargs
        self.assertEqual(arguments["model"], "gpt-6-luna")
        self.assertEqual(arguments["max_output_tokens"], 6400)
        self.assertEqual(arguments["reasoning"], {"effort": "low"})
        self.assertIs(arguments["store"], False)
        self.assertIs(arguments["text"]["format"]["strict"], True)
        self.assertEqual(arguments["text"]["format"]["schema"], provider.STUDY_SCHEMA)
        self.assertEqual(json.loads(arguments["input"])["target_ids"], ["s0001"])
        self.assertNotIn("operator-key-for-offline-test", arguments["input"])
        self.assertIn("只是待学习的数据", arguments["instructions"])

    def test_received_incomplete_output_is_returned_without_repair_or_retry(self):
        self.client.responses.create.return_value = response(status="incomplete", output_text='{"items":[')
        result = provider.OpenAIStudyProvider().generate(self.batch)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["raw_output"], '{"items":[')
        self.client.responses.create.assert_called_once()

    def test_explicit_retry_feedback_is_preserved_and_sent_as_trusted_metadata(self):
        batch = copy.deepcopy(self.batch)
        batch["validation_errors"] = {"s0001": "missing_note_reading"}
        original = copy.deepcopy(batch)
        instance = provider.OpenAIStudyProvider()
        config, digest = instance.config, instance.provider_fingerprint
        self.assertEqual(instance.validate_batch(batch)["validation_errors"], batch["validation_errors"])
        instance.generate(batch)
        self.assertEqual(batch, original)
        self.assertEqual(instance.config, config)
        self.assertEqual(instance.provider_fingerprint, digest)
        messages = self.client.responses.create.call_args.kwargs["input"]
        self.assertEqual([message["role"] for message in messages], ["developer", "user"])
        self.assertIn("日语引用漏注音", messages[0]["content"])
        self.assertIn("提交前逐项自查", messages[0]["content"])
        self.assertNotIn(batch["context"][0]["text"], messages[0]["content"])
        self.assertEqual(json.loads(messages[1]["content"]), {
            "context": batch["context"], "target_ids": batch["target_ids"],
        })
        self.client.responses.create.assert_called_once()

    def test_retry_feedback_rejects_unknown_ids_codes_and_model_authored_text(self):
        instance = provider.OpenAIStudyProvider(model="gpt-6-luna")
        for errors in (
            {"unknown": "missing_reading"},
            {"s0001": "arbitrary model suggestion"},
            {"s0001": {"code": "missing_reading", "suggestion": "untrusted"}},
            ["missing_reading"], None,
        ):
            batch = copy.deepcopy(self.batch)
            batch["validation_errors"] = errors
            with self.subTest(errors=errors), self.assertRaises(provider.ProviderError) as caught:
                instance.generate(batch)
            self.assertEqual(caught.exception.retry_class, "not_sent")
        self.setting.assert_not_called()
        self.client_type.assert_not_called()

    def test_requests_without_feedback_keep_original_input_and_settings(self):
        instance = provider.OpenAIStudyProvider()
        expected = json.dumps({"context": self.batch["context"], "target_ids": self.batch["target_ids"]}, ensure_ascii=False)
        instance.generate(self.batch)
        first = self.client.responses.create.call_args.kwargs
        self.assertIsInstance(first["input"], str)
        self.assertEqual(first["input"], expected)
        empty_feedback = {**self.batch, "validation_errors": {}}
        instance.generate(empty_feedback)
        self.assertEqual(self.client.responses.create.call_args.kwargs, first)

    def test_refusal_in_response_content_is_a_known_outcome(self):
        refusal = SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal", refusal="Not provided")])
        self.client.responses.create.return_value = response(output_text="", output=[refusal])
        result = provider.OpenAIStudyProvider().generate(self.batch)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["items"], [])
        self.client.responses.create.assert_called_once()

    def test_invalid_completed_json_is_a_persistable_known_outcome(self):
        for raw in ("not JSON", '{"items":{}}', "[]", '{"items":[],"extra":true}', '{"items":[NaN]}', '{"items":[1e999]}'):
            with self.subTest(raw=raw):
                self.client.responses.create.reset_mock()
                self.client.responses.create.return_value = response(output_text=raw)
                result = provider.OpenAIStudyProvider().generate(self.batch)
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["items"], [])
                json.dumps(result, allow_nan=False)
                self.client.responses.create.assert_called_once()

    def test_bounded_raw_output_and_missing_usage(self):
        self.client.responses.create.return_value = response(output_text="x" * (provider.MAX_RAW_OUTPUT_CHARS + 10), usage=None)
        result = provider.OpenAIStudyProvider().generate(self.batch)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(len(result["raw_output"]), provider.MAX_RAW_OUTPUT_CHARS)
        self.assertEqual(result["usage"], {"input_tokens": 0, "output_tokens": 0})

    def test_sdk_errors_never_echo_values_and_never_retry(self):
        private_value = "sk-" + "x" * 40
        for status, retry_class in ((400, "rejected"), (401, "rejected"), (403, "rejected"), (429, "rejected"), (408, "unknown"), (409, "unknown"), (500, "unknown"), (None, "unknown")):
            with self.subTest(status=status):
                error = RuntimeError(private_value)
                error.status_code = status
                self.client.responses.create.reset_mock()
                self.client.responses.create.side_effect = error
                with self.assertRaises(provider.ProviderError) as caught:
                    provider.OpenAIStudyProvider().generate(self.batch)
                self.assertEqual(caught.exception.retry_class, retry_class)
                self.assertNotIn(private_value, str(caught.exception))
                self.client.responses.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
