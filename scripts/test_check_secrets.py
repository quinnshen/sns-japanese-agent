"""Offline guard tests. Fixtures never contain an actual credential or use a cloud API."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("secret_guard", HERE / "check_secrets.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class RuleTests(unittest.TestCase):
    def test_local_files_are_always_blocked(self):
        for path in (
            ".env", "backend/.env.local", "backend/.env.production", "nested/data/private.json",
            ".tools/google-cloud-sdk/lib.py", "nested/application_default_credentials.json",
            "private-service-account.json", "recording.webm", "secret.pem",
        ):
            with self.subTest(path=path):
                self.assertTrue(guard.path_rules(path))
        self.assertFalse(guard.path_rules("backend/.env.example"))

    def test_tokens_and_private_values_are_blocked(self):
        fixtures = (
            ("api-token", "sk-" + "x" * 30),
            ("google-api-key", "AI" + "za" + "a" * 35),
            ("github-token", "gh" + "p_" + "a" * 36),
            ("aws-access-key", "AK" + "IA" + "A" * 16),
            ("google-oauth-token", "1/" + "/" + "a" * 30),
            ("private-key", "-----BEGIN " + "PRIVATE KEY-----"),
            ("google-credential-json", '{"type": ' + '"authorized_user"}'),
            ("private-email", "someone" + "@" + "personal.test"),
            ("personal-absolute-path", "/" + "Users/" + "person/source.py"),
            ("non-placeholder-sensitive-value", "OPENAI_" + "API_KEY" + "=" + chr(34) + "non-placeholder-value" + chr(34)),
        )
        for rule, content in fixtures:
            with self.subTest(rule=rule):
                self.assertIn(rule, guard.content_rules("configuration.txt", content.encode()))

    def test_empty_and_example_values_are_allowed(self):
        key_name = "OPENAI_" + "API_KEY"
        fields = [key_name + "=", "GOOGLE_CLOUD_" + "PROJECT=YOUR_PROJECT_ID"]
        self.assertFalse(guard.content_rules(".env.example", "\n".join(fields).encode()))
        self.assertFalse(guard.content_rules("guide.md", b"contact@example.com"))
        self.assertFalse(guard.content_rules("guide.md", b"123+username@users.noreply.github.com"))
        self.assertFalse(guard.content_rules("test.py", b'api_key="operator-key-for-offline-test"'))

    def test_template_requires_placeholders(self):
        assignment = "GOOGLE_CLOUD_" + "PROJECT=personal-project-name"
        self.assertIn("non-placeholder-template-value", guard.content_rules(".env.example", assignment.encode()))

    def test_secret_in_filename_is_not_printed(self):
        token = "sk-" + "z" * 30
        self.assertNotIn(token, guard.display_path(token + ".txt"))
        self.assertIn("redacted-path", guard.display_path(token + ".txt"))


class GitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = os.environ.copy()
        for key in list(self.environment):
            if key.startswith("GIT_"):
                self.environment.pop(key)
        self.environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        self.git("init", "-q")
        self.git("config", "user.name", "Guard Test")
        self.git("config", "user.email", "123+guard-test@users.noreply.github.com")

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *arguments, input_text=None):
        return subprocess.run(arguments, cwd=self.root, env=self.environment, text=True, input=input_text, capture_output=True)

    def git(self, *arguments):
        result = self.command("git", *arguments)
        if result.returncode:
            self.fail("Temporary repository operation failed.")
        return result.stdout.strip()

    def scan(self, *arguments, input_text=None):
        return self.command(sys.executable, str(HERE / "check_secrets.py"), *arguments, input_text=input_text)

    def write(self, name, text):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_ignored_configuration_is_never_opened(self):
        self.write(".gitignore", ".env.*\n!.env.example\n")
        self.write(".env.local", "sk-" + "a" * 40)
        self.write(".env.example", "OPENAI_" + "API_KEY=YOUR_OPENAI_API_KEY\n")
        self.git("add", ".gitignore", ".env.example")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "")

    def test_force_added_configuration_is_blocked(self):
        self.write(".gitignore", ".env.*\n")
        self.write(".env.local", "OPENAI_" + "API_KEY=\n")
        self.git("add", ".gitignore")
        self.git("add", "-f", ".env.local")
        result = self.scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn("local-environment-file", result.stdout + result.stderr)

    def test_index_content_not_clean_worktree_is_checked(self):
        token = "sk-" + "b" * 40
        self.write("source.py", token)
        self.git("add", "source.py")
        self.write("source.py", "clean worktree\n")
        result = self.scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn("api-token", result.stdout)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_local_identity_is_checked(self):
        self.git("config", "user.email", "person" + "@" + "private.test")
        result = self.scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn("email-must-use-github-noreply", result.stdout)
        self.assertNotIn("private.test", result.stdout + result.stderr)

    def test_head_mode_ignores_unstaged_changes_and_checks_history_email(self):
        self.write("file.txt", "safe\n")
        self.git("add", "file.txt")
        self.git("commit", "-qm", "Initial")
        self.write("file.txt", "sk-" + "c" * 40)
        self.assertEqual(self.scan("--all").returncode, 0)
        self.git("config", "user.email", "person" + "@" + "private.test")
        self.git("commit", "--allow-empty", "-qm", "Private identity")
        result = self.scan("--all")
        self.assertEqual(result.returncode, 1)
        self.assertIn("email-must-use-github-noreply", result.stdout)

    def test_push_checks_deleted_secret_in_earlier_commit(self):
        token = "sk-" + "d" * 40
        self.write("accident.txt", token)
        self.git("add", "accident.txt")
        self.git("commit", "-qm", "Accident")
        self.git("rm", "-q", "accident.txt")
        self.git("commit", "-qm", "Remove")
        tip = self.git("rev-parse", "HEAD")
        protocol = "refs/heads/main " + tip + " refs/heads/main " + "0" * 40 + "\n"
        result = self.scan("--pre-push", input_text=protocol)
        self.assertEqual(result.returncode, 1)
        self.assertIn("api-token", result.stdout)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_commit_messages_are_checked_without_echoing_values(self):
        token = "sk-" + "m" * 40
        personal_path = "/" + "Users/" + "person/private.txt"
        private_email = "person" + "@" + "private.test"
        self.write("safe.txt", "safe\n")
        self.git("add", "safe.txt")
        self.git("commit", "-qm", "Diagnostic\n" + token + "\n" + personal_path + "\n" + private_email)
        tip = self.git("rev-parse", "HEAD")
        protocol = "refs/heads/main " + tip + " refs/heads/main " + "0" * 40 + "\n"
        for mode, input_text in (("--all", None), ("--pre-push", protocol)):
            with self.subTest(mode=mode):
                result = self.scan(mode, input_text=input_text)
                self.assertEqual(result.returncode, 1)
                self.assertIn("commit-message-api-token", result.stdout)
                self.assertIn("commit-message-personal-absolute-path", result.stdout)
                self.assertIn("commit-message-private-email", result.stdout)
                for value in (token, personal_path, private_email):
                    self.assertNotIn(value, result.stdout + result.stderr)

    def test_push_delete_is_allowed_and_malformed_protocol_fails(self):
        protocol = "(delete) " + "0" * 40 + " refs/heads/main " + "1" * 40 + "\n"
        self.assertEqual(self.scan("--pre-push", input_text=protocol).returncode, 0)
        malformed = self.scan("--pre-push", input_text="not the hook protocol\n")
        self.assertEqual(malformed.returncode, 1)
        self.assertIn("unable-to-complete-check", malformed.stdout)

    def copy_installation(self):
        (self.root / "scripts").mkdir()
        (self.root / ".githooks").mkdir()
        for name in ("setup_git_hooks.py", "check_secrets.py"):
            shutil.copyfile(HERE / name, self.root / "scripts" / name)
        for name in ("pre-commit", "pre-push"):
            shutil.copyfile(HERE.parent / ".githooks" / name, self.root / ".githooks" / name)

    def test_setup_is_local_idempotent_and_hook_actually_blocks_commit(self):
        self.copy_installation()
        installer = self.root / "scripts" / "setup_git_hooks.py"
        self.assertEqual(self.command(sys.executable, str(installer)).returncode, 0)
        self.assertEqual(self.command(sys.executable, str(installer)).returncode, 0)
        self.assertEqual(self.git("config", "--local", "--get", "core.hooksPath"), ".githooks")
        self.write(".env.local", "placeholder\n")
        self.git("add", ".env.local")
        result = self.command("git", "commit", "-qm", "Must fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local-environment-file", result.stdout + result.stderr)

    def test_setup_preserves_existing_hook_configuration(self):
        self.copy_installation()
        self.git("config", "core.hooksPath", "existing-hooks")
        result = self.command(sys.executable, str(self.root / "scripts" / "setup_git_hooks.py"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.git("config", "--local", "--get", "core.hooksPath"), "existing-hooks")

    def test_setup_preserves_existing_default_hook(self):
        self.copy_installation()
        self.write(".git/hooks/pre-commit", "existing hook\n")
        result = self.command(sys.executable, str(self.root / "scripts" / "setup_git_hooks.py"))
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(self.command("git", "config", "--get", "core.hooksPath").returncode, 0)

    def test_setup_does_not_shadow_a_global_hook_policy(self):
        self.copy_installation()
        self.environment["GIT_CONFIG_GLOBAL"] = str(self.root / "temporary-global-config")
        self.git("config", "--global", "core.hooksPath", "global-hooks")
        result = self.command(sys.executable, str(self.root / "scripts" / "setup_git_hooks.py"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.git("config", "--global", "--get", "core.hooksPath"), "global-hooks")
        self.assertNotEqual(self.command("git", "config", "--local", "--get", "core.hooksPath").returncode, 0)


if __name__ == "__main__":
    unittest.main()
