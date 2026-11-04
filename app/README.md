# SNS Japanese Agent

A Chrome extension and local Python service for Japanese video learning. The current development stage collects a selected YouTube audio range in acknowledged batches, saves the recording locally and provides private replay. See the [capture acceptance checklist](CAPTURE_ACCEPTANCE.md).

Real 90-second and five-minute capture acceptance has passed. The [Google window transcription stage](TRANSCRIPTION.md) can now process saved captures or start in the background while recording, when explicitly enabled. Precomputed translation and learning-note integration follows in the next stage; 30–40 minute validation follows end-to-end integration.

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

## Existing processing modules (next integration stages)

```text
User clicks extension on a YouTube watch or Shorts page
  → Play from the beginning and record tab audio in browser memory
  → Upload the completed recording to 127.0.0.1:8766
  → Convert to overlapping audio slices (45 seconds, 2-second overlap)
  → Google Speech-to-Text V2: Japanese text and word timestamps
  → Merge timestamps and form sentences
  → Optional OpenAI batches of up to 12 sentences:
      Chinese translation, key explanations, context/ASR doubts and kanji readings
  → Save local JSON and display the results alongside playback
```

The Google audio slices and OpenAI sentence batches are separate stages. Translation and explanations are generated in advance in the same model request. Displaying a saved analysis does not call the model again.

Original ASR text is retained separately from the annotated display text. The backend validates that removing the added readings reproduces the original text, retains valid model outputs and attempts targeted repair of invalid sentences. Successful analysis batches are saved for later reuse.

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

## Existing cloud configuration (not used by capture)

OpenAI processing is **off by default**. The current capture workflow never submits OpenAI work. Google window transcription uses its separate opt-in flag described above. The following OpenAI settings are retained for the existing backend CLI modules and the explicitly enabled legacy route.

From `app/backend/`:

```sh
.venv/bin/python configure_openai_key.py
```

Paste your key at the hidden terminal prompt. The script updates `OPENAI_API_KEY` in `backend/.env.local` with owner-only permissions (`0600`) and preserves other settings. This local file is ignored by Git. Old one-line key files remain readable and are converted to `KEY=VALUE` when the configuration tool saves them.

For the previous whole-file `/jobs` route, both `ENABLE_LEGACY_PIPELINE=1` and `ENABLE_ENRICHMENT=1` are required to enable the automatic learning-note step. Keep the legacy route disabled during current capture acceptance. The new capture endpoints never invoke that step.

```sh
.venv/bin/python server.py
```

All application entry points use the same configuration module: explicit process environment → `app/backend/.env.local` → non-secret defaults. An explicit empty environment value also overrides the file. The loader uses this exact path, never searches parent directories, never executes shell expressions and does not expand `${...}`. Keep `.env.local` outside the extension directory.

The tracked `.env.example` contains only placeholders and non-secret defaults. Keep Google ADC in the Google Cloud CLI's standard local credentials location, outside the repository. Do not put your Google account password, refresh token or credential JSON into `.env.local`. If you use an external service-account file through `GOOGLE_APPLICATION_CREDENTIALS`, configure that environment variable yourself with a path outside the repository; never commit the file.

The current default model is `gpt-6-luna`; set `OPENAI_MODEL` to choose a compatible model available to your own account. If processing fails, the completed transcript and previously saved analysis batches remain available. The UI reports an incomplete analysis rather than discarding the transcript.

Keep keys out of chat messages, extension files, source code and Git. The extension never receives the key. Do not copy `.env.local` when sharing this project.

## Saved results and reuse

Results are saved under `app/data/<job-id>.json`. They include the source video URL, Japanese ASR text, word/sentence timestamps and any generated Chinese translations and analysis. They can reveal your viewing and learning history. Treat this directory as private local data; it is ignored by Git.

The current extension creates a new capture ID for each selected range; it does not reuse a whole-video transcript as the result of a different selection. The previous result JSON files remain available for explicit backend commands.

To add or resume learning notes for a saved transcript without rerecording or calling Google, run from `app/backend/`:

```sh
.venv/bin/python enrich_cli.py ../data/YOUR_JOB_ID.json
```

This command makes OpenAI calls using your own configured key, even though the automatic server step is off by default. Existing complete sentences with the current analysis style and valid readings are reused.

Local-video transcription is also available from the backend directory:

```sh
.venv/bin/python cli.py ./video.mov
```

Replace `./video.mov` with the path to your own media. This CLI produces transcription JSON; there is no separate local-video playback interface in this milestone. Use `enrich_cli.py` afterward for optional learning notes.

## Current limits

- Maximum selected range: 30 minutes, including ranges inside longer source videos. Maximum stored audio: 200 MB; maximum individual upload: 10 MB. These are input guards, not a guarantee of reliable long-video processing.
- Upload interval is configurable from 1 to 30 seconds, default 5 seconds. Browser memory retains unacknowledged fragments; their queue is bounded at 16 MiB. On failure, capture stops and pending data stays until the user releases it.
- Playback must stay at 1× without seeking or pausing. Buffering longer than four seconds aborts recording. Shorter interruptions may still affect alignment; long-video synchronization has not been validated.
- The extension supports one active capture at a time. Confirmed batches persist across backend restarts; automatic browser-session recovery and downstream task recovery are future stages.
- Sentence boundaries are inferred from ASR punctuation and timing, not manually checked subtitles. Multiple speakers are not identified. Background noise, overlapping speech and colloquial speech may produce errors.
- No OCR, screenshots, video understanding or speaker recognition is included. Explanations cannot reliably resolve visual references from audio alone.

## Data flow and retention

| Data | Destination and retention |
|---|---|
| Current tab audio | Unacknowledged fragments stay in browser memory; confirmed fragments and combined recording remain locally in `data/captures/`; audio windows go to Google only when capture transcription is enabled |
| Uploaded local audio | Deleted when the normal job finishes, including handled success and failure; forced termination may leave files in `data/uploads/` |
| Converted audio slices | Decoded locally in memory for each transcription request; the application does not save separate WAV slice files |
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

The tests use mock cloud responses and do not require real credentials or consume cloud quota. `GET http://127.0.0.1:8766/health` reports capture capability, whether any server cloud processing is enabled, and the separate capture transcription capability, without returning API key values.

From the repository root, run the extension's offline contract and lifecycle checks with Node.js:

```sh
node scripts/test_capture_extension.mjs
node scripts/test_capture_page.mjs
node scripts/test_capture_worker.mjs
```

These checks use synthetic media, a simulated DOM and mocked Chrome APIs; real-browser capture acceptance remains separate.

The extension requests `activeTab`, `tabCapture`, `offscreen`, `scripting`, `storage` and access to `http://127.0.0.1:8766/*` for capture, page display, task state and communication with the local service. It contains no public cloud endpoint or account credentials.
