# Short-video Japanese learning demo

A Chrome extension and local Python service that turn a Japanese YouTube video into a sentence timeline, synchronized Japanese/Chinese captions and precomputed learning notes. Click a sentence to seek; click its analysis button to reveal saved explanations without another model request.

This release preserves the current whole-video workflow. It has been exercised on short videos of approximately two minutes. Long-video reliability, incremental audio delivery and specified time ranges are not implemented in this milestone.

## How it works

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

Requires Chrome 116+, Python 3.11+ and your own Google Cloud project with billing and the Speech-to-Text API enabled. Follow the [repository's Google ADC setup](../README.md#common-prerequisites) first.

From the repository root:

```sh
cd short-video-demo/backend
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID .venv/bin/python server.py
```

The service listens only on `127.0.0.1:8766`. Replace `YOUR_PROJECT_ID` with **your own** project ID. Google uses your local ADC identity and the explicitly configured project for transcription and quota. No author credentials or account quota are provided.

In Chrome:

1. Open `chrome://extensions` and enable Developer mode.
2. Choose **Load unpacked** and select only `short-video-demo/extension/`.
3. Open a Japanese YouTube watch or Shorts page and click the extension icon.
4. Keep the tab open while it records from the beginning to the end at normal speed.
5. After processing, click sentences to seek. With learning-note generation enabled, Chinese captions and expandable explanations are also available.

No microphone permission is required. The extension captures digital audio from the selected tab.

## Optional Chinese translation and explanations

OpenAI processing is **off by default**. Japanese transcription works independently. To enable it, configure your own OpenAI API key on the computer running the backend.

From `short-video-demo/backend/`:

```sh
.venv/bin/python configure_openai_key.py
```

Paste your key at the hidden terminal prompt. The script writes it to `backend/.env.local` with owner-only permissions (`0600`). This local file is ignored by Git. The backend prefers `OPENAI_API_KEY` from its process environment and otherwise reads that local file; you may supply the environment variable through your own secret management instead.

Stop the backend, then restart it with learning-note generation enabled:

```sh
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID ENABLE_ENRICHMENT=1 .venv/bin/python server.py
```

The current default model is `gpt-6-luna`; set `OPENAI_MODEL` to choose a compatible model available to your own account. If processing fails, the completed transcript and previously saved analysis batches remain available. The UI reports an incomplete analysis rather than discarding the transcript.

Keep keys out of chat messages, extension files, source code and Git. The extension never receives the key. Do not copy `.env.local` when sharing this project.

## Saved results and reuse

Results are saved under `short-video-demo/data/<job-id>.json`. They include the source video URL, Japanese ASR text, word/sentence timestamps and any generated Chinese translations and analysis. They can reveal your viewing and learning history. Treat this directory as private local data; it is ignored by Git.

Clicking the extension again on a previously processed video loads the most recent local result instead of recording and billing for the same video again. The panel's **↻** action forces a new recording and transcription. An already-open panel does not automatically refresh after a separate command changes a saved file; click the extension again to reload it.

To add or resume learning notes for a saved transcript without rerecording or calling Google, run from `short-video-demo/backend/`:

```sh
.venv/bin/python enrich_cli.py ../data/YOUR_JOB_ID.json
```

This command makes OpenAI calls using your own configured key, even though the automatic server step is off by default. Existing complete sentences with the current analysis style and valid readings are reused.

Local-video transcription is also available from the backend directory:

```sh
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID .venv/bin/python cli.py ./video.mov
```

Replace `./video.mov` with the path to your own media. This CLI produces transcription JSON; there is no separate local-video playback interface in this milestone. Use `enrich_cli.py` afterward for optional learning notes.

## Current limits

- Maximum video duration: 30 minutes. Maximum completed audio upload: 200 MB. These are input guards, not a guarantee of reliable long-video processing.
- The full recording stays in browser memory until recording finishes. Transcription and analysis then run in sequence, so recording takes the video's playback time before cloud processing starts.
- Playback must stay at 1× without seeking or pausing. Buffering longer than four seconds aborts recording. Shorter interruptions may still affect alignment; long-video synchronization has not been validated.
- The service processes one transcription/analysis job at a time. There is no durable audio-chunk recovery workflow.
- Sentence boundaries are inferred from ASR punctuation and timing, not manually checked subtitles. Multiple speakers are not identified. Background noise, overlapping speech and colloquial speech may produce errors.
- No OCR, screenshots, video understanding or speaker recognition is included. Explanations cannot reliably resolve visual references from audio alone.

## Data flow and retention

| Data | Destination and retention |
|---|---|
| Tab audio | Held in browser memory, then temporarily saved by the local backend and sent to Google for transcription |
| Uploaded local audio | Deleted when the normal job finishes, including handled success and failure; forced termination may leave files in `data/uploads/` |
| Converted audio slices | Decoded locally in memory for each transcription request; the application does not save separate WAV slice files |
| Transcript and context text | Sent to OpenAI only when learning-note generation is enabled or explicitly run via CLI; video frames are not sent |
| Result JSON and source URLs | Retained locally until you delete them; no automatic retention limit |
| Google credentials / OpenAI key | Stay on the backend computer; not sent to the extension or included in repository files |

There is no application-managed Google Cloud Storage bucket. Remote processing and retention are governed by your provider account and its settings. After stopping the service, you can remove unneeded result JSON and any abandoned upload files to reclaim space. Removing a result also removes the corresponding local cache.

The local HTTP service is intended for one user's computer. It checks the loopback Host and allows extension origins or local clients without an Origin header before accessing saved data or starting work. This is not authentication against other programs or extensions on the same computer. Do not expose its port publicly or run it as a shared hosted service. Stop it when you have finished using the demo.

## Offline verification

From the repository root, after installing backend dependencies:

```sh
cd short-video-demo
backend/.venv/bin/python -m unittest discover -s backend -p 'test_*.py' -v
```

The tests use mock cloud responses and do not require real credentials or consume cloud quota. `GET http://127.0.0.1:8766/health` reports whether the project, learning-note feature and API key are configured, without returning credential values.

The extension requests `activeTab`, `tabCapture`, `offscreen`, `scripting`, `storage` and access to `http://127.0.0.1:8766/*` for capture, page display, task state and communication with the local service. It contains no public cloud endpoint or account credentials.
