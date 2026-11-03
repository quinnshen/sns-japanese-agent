"""Configuration tests use only temporary files and synthetic values."""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import configure_openai_key
import enrich


class ConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / ".env.local"
        patcher = patch.object(config, "LOCAL_ENV_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_env_file_loads_without_mutating_process_environment(self):
        self.path.write_text('GOOGLE_CLOUD_PROJECT=operator-project\nOPENAI_API_KEY="fake-key"\n')
        self.assertEqual(config.setting("GOOGLE_CLOUD_PROJECT"), "operator-project")
        self.assertEqual(enrich.local_api_key(), "fake-key")
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        self.assertEqual(config.setting("ENABLE_ENRICHMENT", "0"), "0")

    def test_process_environment_overrides_file_including_empty_values(self):
        self.path.write_text("OPENAI_API_KEY=file-value\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            self.assertEqual(enrich.local_api_key(), "test-key")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertIsNone(enrich.local_api_key())

    def test_no_shell_execution_or_variable_interpolation(self):
        value = 'literal ${OTHER} $(not-a-command) # "quote" \\ slash\nnext'
        config.save_local_config({"OPENAI_API_KEY": value})
        self.assertEqual(config.read_local_config()["OPENAI_API_KEY"], value)

    def test_key_update_preserves_project_and_other_values_with_private_permissions(self):
        config.save_local_config({"GOOGLE_CLOUD_PROJECT": "operator-project", "ENABLE_ENRICHMENT": "1"})
        with patch.object(configure_openai_key.sys.stdin, "isatty", return_value=True), \
                patch.object(configure_openai_key, "getpass", return_value="fake-key"), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            configure_openai_key.main()
        self.assertNotIn("fake-key", captured.getvalue())
        self.assertEqual(config.read_local_config(), {
            "GOOGLE_CLOUD_PROJECT": "operator-project", "ENABLE_ENRICHMENT": "1", "OPENAI_API_KEY": "fake-key",
        })
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_legacy_one_line_key_can_be_migrated_without_losing_value(self):
        key = "sk-" + "synthetic-legacy-test"
        self.path.write_text(key + "\n")
        self.assertEqual(enrich.local_api_key(), key)
        config.update_local_config({"ENABLE_ENRICHMENT": "0"})
        self.assertTrue(self.path.read_text().startswith("# Private local settings."))
        self.assertEqual(config.read_local_config()["OPENAI_API_KEY"], key)

    def test_symlink_config_is_not_read_or_overwritten(self):
        target = self.path.with_name("target")
        target.write_text("unchanged")
        self.path.symlink_to(target)
        with self.assertRaises(RuntimeError):
            config.read_local_config()
        with self.assertRaises(RuntimeError):
            config.save_local_config({"OPENAI_API_KEY": "fake-key"})
        self.assertEqual(target.read_text(), "unchanged")

    def test_missing_file_and_noninteractive_configuration_do_not_create_secrets(self):
        self.assertEqual(config.read_local_config(), {})
        with patch.object(configure_openai_key.sys.stdin, "isatty", return_value=False), \
                patch.object(configure_openai_key, "getpass") as prompt:
            with self.assertRaises(SystemExit):
                configure_openai_key.main()
        prompt.assert_not_called()
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
