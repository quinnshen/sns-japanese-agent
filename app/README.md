# SNS Japanese Agent

A Chrome extension and local Python service for Japanese video learning. Select a YouTube range, save acknowledged audio batches, then optionally transcribe and prepare Chinese translation, Japanese readings and learning notes. See the [capture acceptance checklist](CAPTURE_ACCEPTANCE.md).

Real 90-second and five-minute capture and Google transcription acceptance has passed. The [Google stage](TRANSCRIPTION.md) processes saved captures or starts in the background while recording. The [learning-note stage](LEARNING.md) independently batches available sentences and saves notes for local display; its backend has produced 37/37 validated notes from a saved 90-second transcript. The user has confirmed basic Chrome learning interactions work in version 0.5.2; explanation accuracy still needs review. Both cloud stages require separate opt-in settings; 30–40 minute validation follows end-to-end integration.

## Current capture workflow

```text
Click extension on a YouTube watch or Shorts page
  → Choose start/end video time and upload interval (default 5 seconds)
  → Play the selected range at 1× and continuously record tab audio
  → Upload numbered fragments with checksums and observed timing
  → Save each fragment and its manifest, then acknowledge receipt
  → Release each browser fragment after matching acknowledgement
  → Flush the final fragment and confirm the complete sequence
  → Verify and combine the continuous WebM recording
  → Open private replay in an extension page
```

Uploads are fragments of one recording container; do not decode each fragment independently.
An interrupted upload retains acknowledged batches locally. Unacknowledged browser data is
kept in the failed session until the user explicitly releases it; cross-browser-restart recovery
is outside this stage. Raw batches and combined audio remain in `app/data/captures/` until deleted.

The first icon click prepares the tab audio stream so Chrome's short-lived stream permission
can be consumed immediately. Recording starts only after you confirm the range. Cancel or
close the panel to release it; the unconfirmed preparation expires after five minutes.

Set `ENABLE_CAPTURE_TRANSCRIPTION=1` in the local backend configuration to enable Google windows as audio arrives. The extension announces this mode and displays transcription progress. The default is 0 for local capture only. Successful windows, request state and sentence JSON are private local artifacts; [transcription setup and recovery](TRANSCRIPTION.md) describes offline preparation, paid execution and explicit unknown-result retries. GPT processing remains separate.

## Incremental learning workflow

```text
Confirmed audio batches on the local backend
  → Assemble and decode the continuous recording prefix
  → Prepare Google windows (up to 45 seconds with overlap)
  → Google Speech-to-Text V2: Japanese text and word timestamps
  → Merge timestamps and finalize available sentences
  → Independently group up to 6 sentences with nearby text context
  → OpenAI: Chinese translation, readings, concise explanations and ASR doubts
  → Validate each sentence and save accepted notes locally
  → Display annotated Japanese and Chinese; expand saved analysis on click
```

The Google audio slices and OpenAI sentence batches are separate stages. Translation and explanations are generated in advance in the same model request. Displaying a saved analysis does not call the model again.

Original ASR text is retained separately from annotated display text. Validation allows only added kana annotations, preserving existing source parentheses. Valid neighbors survive a failed sentence. The new capture pipeline does not automatically resend failed or unknown requests; [explicit retry and request budgets](LEARNING.md#恢复重试与请求限制) are documented separately. Opening saved notes never starts cloud work.

## Setup

Requires Chrome 116+ and Python 3.11+. Current capture and replay need no Google/OpenAI credentials. The [Google ADC setup](../README.md#common-prerequisites) is needed only when explicitly using cloud processing later.

From the repository root:

```sh
cd app/backend
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env.local
chmod 600 .env.local
```

Run `.venv/bin/python server.py`. The service listens only on `127.0.0.1:8766`. Keep `ENABLE_LEGACY_PIPELINE=0` during capture acceptance. You can skip copying `.env.example` for capture-only use; the cloud defaults are disabled.

In Chrome:

1. Open `chrome://extensions` and enable Developer mode.
2. Choose **Load unpacked** and select only `app/extension/`.
3. Open a Japanese YouTube watch or Shorts page and click the extension icon.
4. Set start/end time and the upload interval in the panel, then choose **开始采集**.
5. Keep the tab open at normal speed. When all batches are confirmed, choose **打开私有录音回放**.

No microphone permission is required. The extension captures digital audio from the selected tab.

## Cloud configuration

Both cloud stages are **off by default**. Set `ENABLE_CAPTURE_TRANSCRIPTION=1` for Google and `ENABLE_CAPTURE_ENRICHMENT=1` for automatic OpenAI learning notes. The panel announces the enabled mode before recording. For an existing capture, the explicit learning endpoint or CLI can add notes without calling Google again. See [learning setup and recovery](LEARNING.md).

From `app/backend/`:

```sh
.venv/bin/python configure_openai_key.py
```

Paste your key at the hidden terminal prompt. The script updates `OPENAI_API_KEY` in `backend/.env.local` with owner-only permissions (`0600`) and preserves other settings. This local file is ignored by Git. Old one-line key files remain readable and are converted to `KEY=VALUE` when the configuration tool saves them.

The previous whole-file `/jobs` route uses separate flags, `ENABLE_LEGACY_PIPELINE=1` and `ENABLE_ENRICHMENT=1`. Keep them disabled for the incremental workflow. Capture endpoints use the new independent stages.

```sh
.venv/bin/python server.py
```

All application entry points use the same configuration module: explicit process environment → `app/backend/.env.local` → non-secret defaults. An explicit empty environment value also overrides the file. The loader uses this exact path, never searches parent directories, never executes shell expressions and does not expand `${...}`. Keep `.env.local` outside the extension directory.

The tracked `.env.example` contains only placeholders and non-secret defaults. Keep Google ADC in the Google Cloud CLI's standard local credentials location, outside the repository. Do not put your Google account password, refresh token or credential JSON into `.env.local`. If you use an external service-account file through `GOOGLE_APPLICATION_CREDENTIALS`, configure that environment variable yourself with a path outside the repository; never commit the file.

The current default model is `gpt-6-luna`; set `OPENAI_MODEL` to choose a compatible model available to your own account. If processing fails, the completed transcript and previously saved analysis batches remain available. The UI reports an incomplete analysis rather than discarding the transcript.

Keep keys out of chat messages, extension files, source code and Git. The extension never receives the key. Do not copy `.env.local` when sharing this project.

## Saved results and reuse

Current results are saved under `app/data/captures/<capture-id>/`: recording batches, Google windows, ASR JSON, SQLite state and learning notes. They include source URLs and viewing/learning history. The entire directory is private local data ignored by Git. Legacy whole-file jobs remain under `app/data/<job-id>.json`.

The extension creates a new capture ID for each selected range. Opening the panel can also load saved learning notes for the current video without recording. If several ranges exist, the current interface loads one saved task, preferring a task with ready notes; it does not combine all ranges or present a range library yet.

To prepare the new learning stage offline, run from `app/backend/`:

```sh
.venv/bin/python capture_learn_cli.py ../data/captures/YOUR_CAPTURE_ID
```

This command makes no cloud calls. Add `--run-openai --max-new-requests 1` to permit one new paid request with your own key. Successful notes are reused. The older `enrich_cli.py ../data/YOUR_JOB_ID.json` is a separate legacy command and does make OpenAI calls.

Local-video transcription is also available from the backend directory:

```sh
.venv/bin/python cli.py ./video.mov
```

Replace `./video.mov` with the path to your own media. This CLI produces transcription JSON; there is no separate local-video playback interface in this milestone. Use `enrich_cli.py` afterward for optional learning notes.

## Current limits

- Maximum selected range: 30 minutes, including ranges inside longer source videos. Maximum stored audio: 200 MB; maximum individual upload: 10 MB. These are input guards, not a guarantee of reliable long-video processing.
- Upload interval is configurable from 1 to 30 seconds, default 5 seconds. Browser memory retains unacknowledged fragments; their queue is bounded at 16 MiB. On failure, capture stops and pending data stays until the user releases it.
- Playback must stay at 1× without seeking or pausing. Buffering longer than four seconds aborts recording. Shorter interruptions may still affect alignment; long-video synchronization has not been validated.
- The extension supports one active capture at a time. Confirmed batches and downstream results persist across backend restarts. Backend work can be explicitly resumed; automatic browser-session recovery is not implemented.
- Sentence boundaries are inferred from ASR punctuation and timing, not manually checked subtitles. Multiple speakers are not identified. Background noise, overlapping speech and colloquial speech may produce errors.
- No OCR, screenshots, video understanding or speaker recognition is included. Explanations cannot reliably resolve visual references from audio alone.

## Data flow and retention

| Data | Destination and retention |
|---|---|
| Current tab audio | Unacknowledged fragments stay in browser memory; confirmed fragments and combined recording remain locally in `data/captures/`; audio windows go to Google only when capture transcription is enabled |
| Uploaded local audio | Deleted when the normal job finishes, including handled success and failure; forced termination may leave files in `data/uploads/` |
| Converted audio | Capture processing retains a local PCM cache for reuse; Google request windows are cut from it |
| Transcript and context text | Sent to OpenAI only when learning-note generation is enabled or explicitly run via CLI; video frames are not sent |
| Result JSON and source URLs | Retained locally until you delete them; no automatic retention limit |
| Google credentials / OpenAI key | Stay on the backend computer; not sent to the extension or included in repository files |

There is no application-managed Google Cloud Storage bucket. Remote processing and retention are governed by your provider account and its settings. After stopping the service, you can remove unneeded result JSON and any abandoned upload files to reclaim space. Removing a result also removes the corresponding local cache.

The local HTTP service is intended for one user's computer. It checks the loopback Host and allows extension origins or local clients without an Origin header before accessing saved data or starting work. This is not authentication against other programs or extensions on the same computer. Do not expose its port publicly or run it as a shared hosted service. Stop it when you have finished using the application.

## Offline verification

From the repository root, after installing backend dependencies:

```sh
cd app
backend/.venv/bin/python -m unittest discover -s backend -p 'test_*.py' -v
```

The tests use mock cloud responses and do not require real credentials or consume cloud quota. `GET http://127.0.0.1:8766/health` reports capture capability and separate transcription/enrichment settings, without returning API key values.

From the repository root, run the extension's offline contract and lifecycle checks with Node.js:

```sh
node scripts/test_capture_extension.mjs
node scripts/test_capture_page.mjs
node scripts/test_capture_worker.mjs
```

These checks use synthetic media, a simulated DOM and mocked Chrome APIs; real-browser capture acceptance remains separate.

The extension requests `activeTab`, `tabCapture`, `offscreen`, `scripting`, `storage` and access to `http://127.0.0.1:8766/*` for capture, page display, task state and communication with the local service. It contains no public cloud endpoint or account credentials.
