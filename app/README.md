# SNS Japanese Agent

A Chrome extension and local Python service for Japanese video learning. Select a YouTube range, save acknowledged audio batches, then optionally transcribe and prepare Chinese translation, Japanese readings and learning notes. See the [capture acceptance checklist](CAPTURE_ACCEPTANCE.md).

Real 90-second and five-minute capture and Google transcription acceptance has passed. The [Google stage](TRANSCRIPTION.md) processes saved captures or starts in the background while recording. The [learning-note stage](LEARNING.md) independently batches available sentences and saves notes for local display; its backend has produced 37/37 validated notes from a saved 90-second transcript. The user has confirmed basic Chrome learning interactions work in version 0.5.2; explanation accuracy still needs review. Both cloud stages require separate opt-in settings; 30–40 minute validation follows end-to-end integration.

## Learn and review

1. On a YouTube video, click **S** and choose a range. Shorts and videos up to three minutes default to the whole video. For longer videos the start defaults to the current position; choose an end, or select the whole video when it fits the 30-minute limit.
2. Click **生成学习内容**. The extension connects audio, seeks to the start and plays the selected range at normal speed. Keep playback uninterrupted. Transcription and analysis can start while the audio is being recorded.
3. Once the recording is saved and a sentence is ready, choose **从片段起点学习**. Listen again, expand the analysis or save a sentence with **收藏**. Remaining analysis can finish in the background. **学习下一段** prefills the previous segment's end; click Generate again after choosing the new end.
4. To read something already saved, explicitly open **复习 → 最近片段** or **收藏句子**. Recent clips are grouped by video and open the exact range selected. If Chrome requires permission again after navigating to another video, click **S** on that video to open the selected clip.

During generation, **停止生成** stops the current recording. If the player reports an ad, the extension stops recording and keeps confirmed data. Resume playback in the player, wait for the ad to finish, then choose **重试本段** to start the original range again. This is a new generation, not a continuation of the interrupted recording.

## Audio capture

```text
Click extension on a YouTube watch or Shorts page
  → Confirm a start/end range and generate learning content
  → Play the selected range at 1× and continuously record tab audio
  → Upload numbered fragments with checksums and observed timing
  → Save each fragment and its manifest, then acknowledge receipt
  → Release each browser fragment after matching acknowledgement
  → Flush the final fragment and confirm the complete sequence
  → Verify and combine the continuous WebM recording
  → Start studying, or open private audio replay under 调试信息
```

Uploads are fragments of one recording container; do not decode each fragment independently.
An interrupted upload retains acknowledged batches locally. Unacknowledged browser data is
kept in the failed session until the user explicitly releases it; cross-browser-restart recovery
is outside this stage. Confirmed batches and combined audio are kept under the local retention policy below.

The first icon click opens the panel. The tab audio stream is requested only after you confirm the range.

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
4. Set start/end time in the panel, then choose **生成学习内容**.
5. Keep the tab open at normal speed. When the recording is saved, study the ready sentences or choose **调试信息 → 打开私有录音回放** to check the recording.

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

The extension creates a new capture ID for each selected range. Recent clips lists each completed range with accepted learning notes separately, even when they come from the same video. Reviewing a saved clip or favorite does not record audio or start cloud processing.

Favorites are stored independently in `app/data/learning_library/favorites.sqlite3`. Each holds the Japanese with readings, Chinese translation, complete analysis, and the source video and time when available. It remains readable after the original capture expires; listening again uses the original video.

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
| Completed and interrupted captures | The backend checks at startup and hourly; captures expire seven days after the terminal recording manifest was saved. It skips recording, queued and active processing, as well as jobs whose safety cannot be confirmed. Cleanup includes that capture's recording, transcript and notes |
| Favorite sentences | Saved independently until removed from favorites |
| Google credentials / OpenAI key | Stay on the backend computer; not sent to the extension or included in repository files |

The seven-day policy applies to existing local captures as well as new ones. It does not clean legacy whole-file jobs or provider-side data. Remote processing and retention follow your provider account settings. After stopping the service, you can remove unneeded legacy result JSON or abandoned uploads to reclaim space.

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
node scripts/test_review_panel.mjs
```

These checks use synthetic media, a simulated DOM and mocked Chrome APIs; real-browser capture acceptance remains separate.

The extension requests `activeTab`, `tabCapture`, `offscreen`, `scripting`, `storage` and access to `http://127.0.0.1:8766/*` for capture, page display, task state and communication with the local service. It contains no public cloud endpoint or account credentials.
