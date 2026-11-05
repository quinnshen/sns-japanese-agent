# Setup and usage

SNS Japanese Agent turns videos you enjoy into Japanese learning material: compare each sentence with its Chinese translation, explore grammar and expressions, save sentences, and revisit clips for review.

See the [project overview](../README.md) for the product philosophy, core features, and roadmap. The current learning interface, translations, and explanations are in Chinese.

## Current support

- Japanese audio is required; existing video captions are optional. Speakers are not identified separately.
- The extension currently supports **YouTube watch pages and Shorts**. Tab audio capture is the underlying capability; other Chrome video pages still need adapters for page detection, source links, and playback controls.
- Each selection can be up to **30 minutes**. This is an input limit; reliability with 30–40-minute videos has not yet been validated.
- Generation requires **continuous playback at 1× speed**. Pausing, seeking, switching videos, or an ad in the current player interrupts recording. Buffering for more than 4 seconds also interrupts it.
- OCR and video understanding are not implemented yet. Explanations retain uncertainty where meaning depends on visuals.

## A learning session

1. Open a video and click the **S** extension icon.
2. Choose a time range. Shorts and videos up to 3 minutes default to the whole video. Other videos default to the current playback position; enter an end time and click **生成学习内容** (Generate learning content).
3. Let the selection play normally. Audio is saved locally, and enabled transcription and analysis stages run in the background.
4. Once recording is saved and some sentences are ready, click **从片段起点学习** (Study from the start) to follow subtitles, replay sentences, and expand explanations.
5. Click **收藏** (Save) to keep a sentence and its full explanation. **学习下一段** (Study the next clip) prefills the next start time; confirm the range before generating again.

**复习** (Review) is a separate entry point. Select a specific range under **最近片段** (Recent clips), grouped by video, or open **收藏句子** (Saved sentences). Reviewing, expanding explanations, and saving sentences do not start new cloud generation.

## Installation

Requires Chrome 116+ and Python 3.11+. Both the extension and backend run on your own computer.

### Configure the local backend

From the repository root:

```sh
cd app/backend
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n .env.example .env.local
chmod 600 .env.local
```

`cp -n` preserves an existing local configuration. Store real credentials only in `.env.local`, which Git ignores.

### Configure cloud processing

Cloud calls are disabled by default; audio capture and playback work locally. To generate the full learning content, configure your own Google Cloud transcription service and OpenAI API key.

**Google transcription**

Enable billing and the Speech-to-Text API in your own Google Cloud project. Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) and set up [local Application Default Credentials (ADC)](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment):

```sh
gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

Replace `YOUR_PROJECT_ID` with your own project ID. The signed-in identity must have permission to use that project. ADC stays in the standard local configuration directory outside this repository; do not copy account passwords or credential JSON files into the project.

**Translation and explanations**

From `app/backend/`:

```sh
.venv/bin/python configure_openai_key.py
```

Paste your own key at the hidden-input prompt. The tool updates the private `.env.local` while preserving other settings. Do not put real keys in chat, source code, the extension, or command-line arguments.

Then set the following in `app/backend/.env.local`:

```dotenv
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
ENABLE_CAPTURE_TRANSCRIPTION=1
ENABLE_CAPTURE_ENRICHMENT=1
ENABLE_LEGACY_PIPELINE=0
ENABLE_ENRICHMENT=0
```

`OPENAI_MODEL` selects the model; see `.env.example` for its default. Recording and local playback require no cloud credentials. Transcription and explanation costs belong to the accounts you configure; the repository provides no public service using the author's quota.

### Start the service

From `app/backend/`:

```sh
.venv/bin/python server.py
```

The service listens only on `127.0.0.1:8766`. Keep the terminal running while using the application. Start it again after restarting your computer, and restart the service after changing backend code or configuration.

### Load the extension

1. Open `chrome://extensions` and enable **Developer mode**.
2. Click **Load unpacked** and select **`app/extension/`** in this repository.
3. Open a supported video page and click the **S** extension icon.

Load only the extension directory, not the repository root or any directory containing Python environments, SDKs, or credentials. The extension captures tab audio and does not need microphone permission.

After updating extension code, reload the extension in Chrome and refresh the video page.

## Generation, retries, and review

### First-time preparation

The first generation requires playing the selected clip. Transcription and explanations can start during recording. After recording finishes, you can study available sentences while the remaining explanations continue processing.

Clicking S only opens the panel. Audio access and the task begin after you confirm **生成学习内容** (Generate learning content). **调试信息** (Debug information) contains upload intervals, batch metrics, and private recording playback for troubleshooting.

### Interruptions and ads

Click **停止生成** (Stop generation) to stop recording. If an ad interrupts recording, resume playback in the player, wait for the ad to finish, then select **重试本段** (Retry this clip).

Retrying creates a new recording task for the original range and may incur new transcription and explanation costs. The application waits for backend confirmation that the old task has stopped, and will not start the retry if the video has changed. Acknowledged data remains saved, and cloud requests already sent may still finish.

### Incomplete explanations

ASR transcripts may contain recognition errors, omitted speech, or incorrect sentence boundaries. The original ASR is stored separately; displayed Japanese may add kana readings but must not silently rewrite the source sentence.

You can study neighboring sentences that passed validation. Incomplete or invalid sentences remain pending; failed requests and requests with unknown outcomes are not automatically resent. Use [targeted retries with a request limit](LEARNING.md#恢复重试与请求限制) when repairs are needed (technical guide in Chinese).

### Recent clips and saved sentences

- **最近片段** (Recent clips) lists the full ranges with generated learning content. Multiple clips from the same video can be selected separately.
- Opening a clip takes you to its source video. If Chrome needs renewed permission, click S on the source page to continue opening the selected clip.
- **收藏句子** (Saved sentences) keeps the original sentence with readings, Chinese translation, full explanation, source link, and available timestamps. Saved sentences remain readable after the original clip expires. Replaying the source depends on the original video remaining accessible.

## Local storage and cleanup

| Data | Retention |
|---|---|
| Recordings, transcripts, explanations, and task state | Stored in `app/data/captures/<capture-id>/`, separately for each selection. |
| Ordinary clips | Checked at backend startup and hourly while running. Terminal tasks are cleaned up after **7 days**, including their recordings, transcripts, and explanations not saved separately. |
| Tasks recording, queued, or processing | Skipped during cleanup. Unknown states, abnormal files, and lock conflicts are also retained conservatively. |
| Saved sentences | Stored independently in `app/data/learning_library/favorites.sqlite3` until removed from saved sentences. |
| Legacy whole-file tasks and uploads | Not covered by this clip cleanup policy. Manage them manually with the service stopped. |

The 7 days are measured from the last save time of the terminal recording manifest. Reading a clip does not extend retention, and the policy also applies to existing clips. Save the sentences you want to keep or back up local data before older records expire. Data does not sync automatically between computers.

The data directory contains source links, recordings, and learning history and is excluded from Git. Files and directories use private permissions. Do not copy real data into public test fixtures or issues.

## Privacy and costs

- Google receives audio needed for each recognition window. OpenAI receives transcripts and textual context. Video frames are not sent.
- Calls use your own project and API key. Cloud processing is disabled by default, and review does not regenerate content.
- Expanding a saved explanation does not call a model. Successful cloud results are reused; requests with unknown outcomes are not retried blindly.
- Credentials stay in the local backend. The extension receives neither Google login credentials nor an OpenAI API key.
- Local retention does not change cloud providers' data retention settings and is not a hard spending cap.

The service is intended for a personal computer. Loopback Host and Origin checks restrict access but do not authenticate other programs on the same machine. Do not expose the port publicly or use it as a shared hosted service.

## Troubleshooting

**Saving fails, or review cannot load**

Check that the current backend is running. Updating the extension does not replace a running Python service. The panel prompts you to restart the backend if an older version lacks the required endpoints. `GET http://127.0.0.1:8766/health` returns the service version and capabilities, not keys.

**Some explanations are missing**

Check whether cloud processing is enabled and whether the panel reports pending sentences. Raw responses and validated sentences remain on disk; see the [learning-note guide](LEARNING.md) for recovery rules (Chinese).

**Video playback fails or stops during generation**

Confirm that the video itself plays and ads have finished, then retry. Pausing and seeking freely during recording are not supported yet. Long-video recovery and reliability remain on the roadmap.

## Development and diagnostics

- [Transcription, time mapping, and recovery](TRANSCRIPTION.md) (Chinese)
- [Explanation style, validation, caching, and request limits](LEARNING.md) (Chinese)
- [Capture acceptance](CAPTURE_ACCEPTANCE.md) and [batch protocol](CAPTURE_PROTOCOL.md) (Chinese)
- [Commit safety and offline tests](../DEVELOPMENT.md)
- [Historical five-second integration test](../docs/FIVE_SECOND_TEST.md)

Backend and extension tests use temporary data, synthetic audio, and mocked cloud services. They require no real API key and consume no cloud quota.
