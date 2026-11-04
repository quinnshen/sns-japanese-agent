# Audio capture protocol v1

Stage 1 saves local browser recordings only. It never schedules transcription or enrichment.
All routes use the existing loopback Host/Origin boundary and bind to port 8766.
JSON requests use `Content-Type: application/json`; batch requests carry binary audio.

## Create

`POST /captures`

```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "source_url": "https://www.youtube.com/watch?v=AbCdEf012_-",
  "video_duration_sec": 600,
  "range_start_sec": 30,
  "range_end_sec": 150,
  "batch_interval_ms": 5000,
  "mime_type": "audio/webm;codecs=opus"
}
```

The client supplies a UUID. Same ID and metadata is an idempotent retry; different metadata conflicts.
Initial limits: selected range at most 30 minutes, batch interval 1000–30000 ms, total 200 MB,
single batch at most 10 MB. The full source video may be longer than the selected range.
Response: a capture manifest with `id`, `status`, `received_batches`, `total_bytes`, and `batches`.

## Timing

`POST /captures/{id}/timing`

```json
{
  "recording_started_at_ms": 1700000000000,
  "playback_started_at_ms": 1700000000120,
  "playback_start_video_sec": 30.01
}
```

These are observed browser timestamps, not inferred from the batch interval. Save the actual
video position at the first `playing` event. A repeated identical timing message is idempotent.

## Upload

`PUT /captures/{id}/batches/{sequence}` (zero-based sequence)

- `Content-Type`: the capture MIME type.
- `X-Batch-SHA256`: lowercase SHA-256 of the raw request body.
- `X-Capture-Start-Ms`, `X-Capture-End-Ms`: elapsed monotonic browser time at consecutive
  batch emissions, relative to recorder start. These are observations, not exact audio PTS.
- `X-Blob-Timecode-Ms`: `BlobEvent.timecode`, if finite; may be omitted.

Save the raw bytes and metadata before acknowledging. Response:

```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "sequence": 0,
  "stored": true,
  "duplicate": false,
  "sha256": "body hash",
  "size_bytes": 1000,
  "received_batches": 1,
  "total_bytes": 1000
}
```

Same sequence, bytes and metadata returns a duplicate acknowledgement; conflicting reuse is 409.
Out-of-order upload is accepted. Every batch from one recorder session belongs to one continuous
container; individual batches are not assumed independently decodable. Release browser data
only after verifying its acknowledgement. Retry transient local errors at most 3 times.

## Finish

`POST /captures/{id}/complete`

```json
{
  "expected_batches": 24,
  "recording_stopped_at_ms": 1700000120300,
  "playback_end_video_sec": 150.02,
  "playback_events": [
    {"type": "playing", "at_ms": 1700000000120, "video_sec": 30.01}
  ]
}
```

Wait for recorder `stop` (and hence the final `dataavailable`), then all batch acknowledgements.
Missing sequence numbers return 409 with `missing_sequences`; a missing or invalid timing record,
extra batches, conflicting completion metadata or corruption also prevents a completed capture.
Verify batch hashes and byte counts, concatenate in order into `recording.webm` using an atomic
write, then persist `status: complete`. Completion is idempotent. A complete manifest also has
`expected_batches`, `audio_sha256`, `audio_size_bytes`, `audio_url`, and observed completion timing.
This status confirms storage integrity; decoding and listening are separate acceptance checks.
Optional `playback_events` preserve observed `playing`/`waiting` changes, so short buffering is
visible to later time-alignment work. Timing observations do not claim sample-accurate audio PTS.

## Inspect and replay

- `GET /captures/{id}` returns the manifest, including received sequence numbers and missing numbers
  once an expected count has been declared.
- `GET /captures/{id}/audio` serves the combined recording for complete captures, with byte ranges.
- `POST /captures/{id}/abort` records an interrupted capture; acknowledged batches remain on disk.

The abort body may be `{}` for compatibility, or include `failure`,
`recording_stopped_at_ms`, `playback_end_video_sec`, and `playback_events`.
The optional `failure` object contains a short snake_case `code`, fixed safe `message`,
and optional `at_ms`, `video_sec`, `video_connected`, `player_connected`, `ad_showing`,
`identity_changed`, and `video_replaced` observations. No URLs, DOM text, local paths,
credentials, or arbitrary fields belong in a failure diagnostic. The first failure is
preserved when a user later releases the failed capture. Stop and playback observations
are retained when available; an advertisement or changed video may reset its media clock,
so abort observations do not imply a continuous timeline.

Aborted captures remain `aborted`: they are not complete recordings and are not served
by the completed-audio endpoint. A recorder failure can leave an unflushed tail after the
last acknowledged batch. Never infer its duration from the failed UI timer alone.

Capture data is private: `app/data/captures/{id}/manifest.json`, `batches/{sequence:06d}.webm`,
and `recording.webm`. No source URL, account information or recording data belongs in Git.
Error JSON has `error` (a stable safe description), optionally `missing_sequences`.
