# Development safety

The application is iterated as a continuing product under `app/`. The five-second integration test is a separate historical milestone. Use ordinary `feat:`, `fix:`, `refactor:` and `docs:` commit descriptions.

## Local settings

1. Create `app/backend/.env.local` from `.env.example`, with permissions `0600`.
2. Put your own project ID, OpenAI key and local feature/model settings in that file.
3. Keep Google ADC in its normal external Google Cloud CLI directory. Do not copy login credentials into the repository or extension.
4. Install Git hooks with `python3 scripts/setup_git_hooks.py` once per clone.

`.env.local` is a local plain-text configuration file, not an encrypted vault. The application reads it without echoing values or putting them into the extension. Process environment settings take precedence. API keys need not be deleted before commits.

## Submission checks

- `.gitignore` excludes local config, common credential files, virtual environments, downloaded SDKs, recordings and result data. Only `.env.example` is allowed as a configuration template.
- Pre-commit scans staged Git objects, not the working copy or ignored files. Force-adding a private path is still rejected.
- Pre-push scans the history being sent, so deleting a secret in a later commit does not make earlier disclosure safe.
- Checks do not print matching values. A rejection identifies the relative file path and rule, so you can remove the item from staging or change the code without copying the value into logs.

These are local development safeguards. They do not replace provider authentication or spending controls. Hooks are not automatically installed by cloning, and browser-based edits do not run local hooks. A server-side check runs only after uploading and cannot prevent the initial disclosure; keep the local checks enabled.

Run the same checks manually with `python3 scripts/check_secrets.py` for the staged snapshot, or `python3 scripts/check_secrets.py --all` for the current committed snapshot and historical commit emails/messages. Push hooks additionally inspect file contents in the full history being pushed.

If a real credential is ever published, revoke or rotate it through its provider. Adding `.gitignore` or deleting the latest version does not remove it from history or make it safe to reuse.

## Offline verification

```sh
python3 -m unittest discover -s scripts -p 'test_*.py' -v
app/backend/.venv/bin/python -m unittest discover -s app/backend -p 'test_*.py' -v
node scripts/test_capture_extension.mjs
node scripts/test_capture_page.mjs
node scripts/test_capture_worker.mjs
node scripts/test_review_panel.mjs
```

Tests use temporary Git repositories, temporary config files and synthetic credentials. They do not need your real key, Google login or paid API access.

Setup and user-facing behavior are documented in the [application guide](app/README.md). The separate [five-second integration test](docs/FIVE_SECOND_TEST.md) remains available for troubleshooting tab audio and transcription.
