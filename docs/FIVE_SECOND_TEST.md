# Five-second integration test

This separate early test checks the path from **Chrome tab audio → local backend → Japanese transcription with word timestamps**. For everyday learning, use the [application](../app/README.md).

## Run the test

You need Chrome 116+ and Python 3.11+. Cloud transcription also requires your own Google Cloud project and Application Default Credentials (ADC); see [cloud configuration](../app/README.md#configure-cloud-processing).

From the repository root:

```sh
cd shorts-audio-capture-bridge
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID .venv/bin/python transcribe_server.py
```

Replace `YOUR_PROJECT_ID` with your own project ID. The service listens only on `127.0.0.1:8765`.

Open `chrome://extensions`, enable Developer mode, and load the repository's `shorts-audio-capture-test/` directory. Open a YouTube watch page or Short, then click the extension while Japanese dialogue is playing.

The extension records five seconds of tab audio, displays the byte count, peak RMS, and audio track count, and sends the audio to the local backend. Google Speech-to-Text V2 returns Japanese text and word timestamps relative to the recording start. This test does not write recordings to disk or call OpenAI, and it does not generate translations or explanations.

If the local backend is not running, capture measurements are still available, but transcription shows a connection error.

## Permissions and costs

- `activeTab`: access the tab where the user clicks the extension.
- `tabCapture` and `offscreen`: record tab audio in the background without microphone access.
- `scripting`: display results on the page.
- `http://127.0.0.1:8765/*`: connect to the local test backend.

The test uses the operator's own credentials and project. The repository supplies no author account access or cloud credits. It checks the audio path; it does not download videos or identify speakers.

## Offline checks

From the repository root:

```sh
cd shorts-audio-capture-bridge
.venv/bin/python -m unittest discover -p 'test_*.py' -v
```

These tests mock Google authentication and transcription. They need no real credentials and incur no cloud charges.
