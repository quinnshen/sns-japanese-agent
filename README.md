# SNS Japanese Agent

A Japanese video learning application for YouTube watch pages and Shorts, developed through successive versions. The original five-second integration test is retained separately:

| Milestone | What it does | Local service |
|---|---|---|
| [Five-second capture test](#five-second-capture-test) | Capture five seconds of tab audio and obtain Japanese transcription with word timestamps | `127.0.0.1:8765` |
| [Japanese learning application](app/README.md) | Current capture stage: select a video range, upload acknowledged audio batches, verify local storage and replay the recording | `127.0.0.1:8766` |

Batch capture is [verified with real 90-second and five-minute selections](app/CAPTURE_ACCEPTANCE.md). The current [Google window transcription stage](app/TRANSCRIPTION.md) reuses those recordings, caches successful requests and supports incremental processing. Cloud processing remains opt-in. The current 30-minute selection limit does not mean long-video reliability has been validated.

## Project layout

```text
shorts-audio-capture-test/    Five-second Chrome Manifest V3 extension
shorts-audio-capture-bridge/  Five-second local Python bridge and offline tests
app/
  extension/                Selected-range audio capture Chrome Manifest V3 extension
  backend/                  Local capture storage, replay and existing processing modules
  README.md                 Setup, workflow, limitations and data retention
```

Keep Python environments, downloaded SDKs and credentials outside extension directories. Load only the relevant `extension/` or `shorts-audio-capture-test/` directory in Chrome, not the repository root.

## Development and local configuration

Develop and commit directly in this repository. Keep the reusable application in `app/`; version commits by the change they make, such as `feat: ...`, `fix: ...` or `refactor: ...`.

| File / directory | Purpose | Tracked by Git? |
|---|---|---|
| `app/backend/.env.example` | Placeholder configuration and public defaults | Yes |
| `app/backend/.env.local` | Your project ID, API key and local settings | No |
| `.gitignore` | Excludes local settings, credentials, data and tools | Yes |
| `.githooks/`, `scripts/` | Commit and push checks for accidental disclosures | Yes |
| `app/data/` | Private transcripts, source URLs and temporary uploads | No |

Enable local Git checks once per clone:

```sh
python3 scripts/setup_git_hooks.py
```

Normal `git add` respects `.gitignore`. The pre-commit check also scans the staged snapshot to reject private configuration files even if force-added, along with recognized secrets and private metadata. The pre-push check checks outgoing commit history, including files deleted in later commits. Checks report paths and rule names, never matched secret values. The scripts can also be run manually; see [development safety](DEVELOPMENT.md).

`.gitignore` does not remove files already tracked by Git. Hooks must be installed on each clone, can be bypassed and cannot detect every possible secret. Keep real credentials in ignored local files from the start; no deletion step is needed before ordinary commits.

## Your credentials, your billing

The repository provides application code, not access to the author's cloud accounts. There is no hosted transcription endpoint or bundled cloud credential.

- Google transcription requires **your own** Google Cloud project, billing, enabled Speech-to-Text API and Application Default Credentials (ADC). Set `GOOGLE_CLOUD_PROJECT` explicitly; the services use it for the recognizer and quota project. Your authenticated identity must have permission to use that project.
- The five-second test makes **no OpenAI API calls**. The application calls OpenAI only when you enable learning-note generation and configure **your own** API key.
- Current batch capture and offline replay need no Google or OpenAI credentials and make no cloud requests. The previous whole-file cloud route additionally requires the explicit `ENABLE_LEGACY_PIPELINE=1` setting.
- Credentials stay in the local backend environment or a local ignored configuration file. The extension does not receive Google credentials or an OpenAI key.
- Do not commit `.env` files, ADC files, service account keys, local result data, configured Python environments or SDK directories. Keep Google ADC in the normal Google Cloud CLI user configuration location, outside this repository.

Cloud usage is billed to the accounts you configure. See Google's [quota project documentation](https://docs.cloud.google.com/docs/quotas/set-quota-project).

## Common prerequisites

Use Chrome 116+, Python 3.11+ and the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install).

Create or select your own Google Cloud project, enable billing and the [Speech-to-Text API](https://console.cloud.google.com/apis/library/speech.googleapis.com), then configure [local ADC](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment):

```sh
gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

Replace `YOUR_PROJECT_ID` with your own project ID in every command. These credentials authenticate the computer running the backend; they are not supplied by this repository.

## Five-second capture test

From the repository root:

```sh
cd shorts-audio-capture-bridge
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID .venv/bin/python transcribe_server.py
```

In Chrome, open `chrome://extensions`, enable Developer mode and choose **Load unpacked**. Select only `shorts-audio-capture-test/`. Open a YouTube video or Short, start playback and click the extension while Japanese speech is playing.

The extension captures five seconds of the tab's audio, displays the byte count, peak RMS and audio track count, and sends it to the local bridge. Google Speech-to-Text V2 returns Japanese text and word timestamps relative to the recording start. The application does not write these recordings to disk.

This test does not download videos, identify speakers, translate or generate grammar explanations. If the bridge is not running, the extension can still report capture measurements but transcription shows a connection error.

Its permissions are `activeTab` (the clicked tab), `tabCapture` (tab audio, not the microphone), `offscreen` (background recording), `scripting` (on-page results), and access to `http://127.0.0.1:8765/*` (the local bridge).

Run its offline tests from the repository root:

```sh
cd shorts-audio-capture-bridge
.venv/bin/python -m unittest discover -p 'test_*.py' -v
```

These tests mock Google authentication and transcription, so they do not consume cloud quota.

## Japanese learning application

Follow [the application setup guide](app/README.md) and [capture acceptance checklist](app/CAPTURE_ACCEPTANCE.md). Both services bind to loopback only and are intended for a personal computer, not public hosting.
