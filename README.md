# SNS Japanese Agent

This first milestone tests one small part of a Japanese video learning agent: capture **five seconds** of a playing YouTube tab and obtain Japanese transcription with word timestamps.

## Current scope

1. Click the Chrome extension on a YouTube watch or Shorts page.
2. Record five seconds of tab audio in memory and display its byte count, peak RMS and audio track count.
3. Send the recording to a bridge on `127.0.0.1:8765`.
4. The bridge calls Google Speech-to-Text V2 with `ja-JP`, automatic punctuation and word timestamps.

Timestamps are relative to the recording start. This milestone does not download videos, identify speakers, translate, or generate grammar explanations. It makes **no OpenAI API calls**. Recordings are not written to disk by the application.

## Project layout

```text
shorts-audio-capture-test/    Chrome Manifest V3 extension
shorts-audio-capture-bridge/  Local Python bridge and offline tests
```

Keep the bridge and its Python environment outside the extension directory. Chrome scans the unpacked extension directory, so it should contain only the four extension files.

## Credentials and billing

This repository contains no API keys, Google account credentials, service account files, author project IDs, or hosted transcription endpoint. Cloning it grants no access to the author's Google Cloud or OpenAI accounts.

Google transcription requires **your own** Google Cloud project, enabled billing, Speech-to-Text API access and Application Default Credentials (ADC) on the computer running the bridge. You must explicitly set `GOOGLE_CLOUD_PROJECT`; there is no project fallback. The bridge uses that project for both its recognizer resource and quota project. Your authenticated identity must have permission to use that project. See Google's [quota project documentation](https://docs.cloud.google.com/docs/quotas/set-quota-project).

Without your own project and credentials, local audio capture can still work, but cloud transcription cannot. No OpenAI key is needed for this milestone. Keep ADC files in the normal Google Cloud CLI user configuration location, outside this repository. Never copy a configured Python environment, SDK directory, `.env` file, key file, or cloud account configuration into the extension or repository.

## Setup

Requires Chrome 116+, Python 3.11+ and the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install).

Create or select your own Google Cloud project, enable billing and the [Speech-to-Text API](https://console.cloud.google.com/apis/library/speech.googleapis.com), then configure [local ADC](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment):

```sh
gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

Replace `YOUR_PROJECT_ID` with your own project ID in both commands below and above.

From the repository root, start the local bridge:

```sh
cd shorts-audio-capture-bridge
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID .venv/bin/python transcribe_server.py
```

In Chrome, open `chrome://extensions`, enable Developer mode and choose **Load unpacked**. Select only `shorts-audio-capture-test/`. Open a YouTube video or Short, start playback and click the extension while Japanese speech is playing. The on-page result appears after recording and transcription.

The bridge binds only to `127.0.0.1`. It is intended for a personal computer, not public hosting. Stop it when the test is complete. If it is not running, the extension still reports capture measurements and shows a connection error for transcription.

## Extension permissions

- `activeTab`: operate on the tab where you click the extension.
- `tabCapture`: capture that tab's audio, not the microphone.
- `offscreen`: record five seconds without keeping a popup open.
- `scripting`: display status and results on the current page.
- `http://127.0.0.1:8765/*`: send audio to your local bridge.

## Offline checks

These tests mock authentication and Speech-to-Text. They do not contact Google or consume cloud quota.

```sh
cd shorts-audio-capture-bridge
.venv/bin/python -m unittest discover -p 'test_*.py' -v
```
