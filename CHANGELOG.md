# Development changelog

This log summarizes committed development milestones. See the [application guide](app/README.md) for setup and usage, and the [roadmap](README.md#roadmap) for planned work.

## 0.6.2 · 2026-11-05

- Keep the study view synchronized with locally saved results after partial validation failures, including later transcription windows and independent text batches.
- Release completed polling sessions so the final capture event can refresh the same clip. Status refreshes do not retry cloud requests.
- Distinguish an unavailable or incompatible local backend from tab-audio connection errors, with actionable startup and restart guidance.
- Document recovery from stale page UI after reloading the extension; refreshing the video page is still required.

## 0.6.0 · 2026-11-05

- Introduce a clear prepare → study → next clip flow, with a separate review entry point.
- Add recent clips and saved sentences, including the original dialogue, translation, and full explanation.
- Automatically clean up regular clips after the default 7-day retention period; keep saved sentences separately.
- Check backend version and capabilities, and prompt for an upgrade when an older service lacks the save and review endpoints.

## 0.5.4 · 2026-11-04

- Fix audio reconnection and starting a new capture after closing a previous task.
- Preserve the selected range and wait for the previous task to close before starting another.

## 0.5.2 · 2026-11-04

- Precompute Chinese translations, kana readings, and grammar and expression explanations.
- Add bilingual subtitles, sentence replay, and access to saved explanations.
- Persist text batches and validation results independently, with targeted repairs capped by request count.
- Improve panel scrolling and timestamp hints.

## 0.4.3 · 2026-11-04

- Add Google transcription windows and word timestamps.
- Resolve overlapping content and sentences that span windows, and map timestamps to the source video.
- Save window results, reuse successful requests, and process recording data incrementally.

## 0.3.2 · 2026-11-04

- Let users select a video range, record continuously, and upload audio batches to the local backend.
- Release browser data after the backend acknowledges storage; detect duplicate and missing batches.
- Save continuous recordings for private replay, validated with real short clips and a five-minute capture.

## 0.2.1 · 2026-11-03

- Centralize private local configuration for the existing short-video learning workflow.
- Make ownership of credentials, projects, and billing explicit.
- Add commit and push checks to catch private configuration, credentials, and learning data before publication.

## Historical milestone: five-second integration test · 2026-11-03

Retain a separate test extension (version 0.2.0) and local backend to verify tab audio capture, Japanese transcription, and word timestamps. See the [five-second test guide](docs/FIVE_SECOND_TEST.md).
