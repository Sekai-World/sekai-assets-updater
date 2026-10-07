# Extraction Profiling

Extraction-worker roadmap Phase 0 (#22) measurement baseline. Profiling is
**advisory only**: it never changes extraction behavior, correctness decisions,
or resource limits, and it is safe to enable in production.

## What is always collected

Regardless of configuration, every pipeline run emits:

1. **Per-bundle timing (DEBUG)** — one structured log line per completed
   extraction, from the `asset_updater` logger:

   ```text
   PIPELINE | id=<pipeline_id> | worker=extract_worker-0 | stage=extract | action=bundle_timing | item=<sanitized bundle name> | duration_sec=0.412 | outputs=37 | output_bytes=2841923
   ```

   - `duration_sec` — wall-clock time of the single-bundle extraction boundary
     (`extract_single_bundle`), covering the Unity process-pool extraction plus
     audio/video fan-out and output validation;
   - `outputs` — validated exported file count;
   - `output_bytes` — total size of the exported files.

2. **Run summary (INFO)** — one aggregate line when the extract stage completes:

   ```text
   PIPELINE | id=<pipeline_id> | stage=extract | status=summary | bundles=4310 | failed=2 | extraction_sec=1820.4 | idle_sec=931.7 | outputs=91230 | output_bytes=5368709120 | queue_depth_avg=0.42 | queue_depth_max=6
   ```

   - `bundles` / `failed` — attempted extractions and failures;
   - `extraction_sec` — total per-bundle extraction wall time across workers
     (overlaps when workers run concurrently);
   - `idle_sec` — total time extract workers spent blocked waiting for the
     next queue item; large values alongside a long total run indicate the
     extraction stage is not the bottleneck;
   - `queue_depth_avg` / `queue_depth_max` — sampled `extract_queue` depth
     (immediate sample at pipeline start, then every
     `_QUEUE_DEPTH_SAMPLE_INTERVAL_SEC` = 1.0 s).

3. **Performance report (INFO, extraction-worker roadmap Phase 5)** — one
   aggregate line after the summary when the pipeline completes:

   ```text
   PIPELINE | id=<pipeline_id> | stage=extract | status=report | worker_utilisation=0.4120 | wait_sec_avg=0.0312 | wait_sec_max=1.2045 | wait_sec_p95=0.4021 | media_active_max=2 | bundles_per_sec=light:12.30,media:0.50
   ```

   - `worker_utilisation` — total extraction seconds over
     (wall time × extract worker count); persistently far below 1.0 means the
     extraction stage is not the bottleneck;
   - `wait_sec_avg` / `wait_sec_max` / `wait_sec_p95` — per-bundle queue wait
     (time from download-stage enqueue to extract-worker claim); `n/a` when no
     bundle was observed waiting;
   - `media_active_max` — maximum concurrently active media-classified
     extractions sampled during the run (adaptive mode);
   - `bundles_per_sec` — per-cost-class throughput
     (`class:rate` pairs; per-class extraction seconds overlap across workers,
     so treat rates as utilisation indicators, not end-to-end speed).

## JSON-lines profile (`EXTRACTION_PROFILING`)

Setting `EXTRACTION_PROFILING = True` additionally writes a versioned JSON-lines
profile file with one record per bundle plus a final run summary record. The
CLI `--profile` flag enables profiling for a single run: it turns on
`EXTRACTION_PROFILING` and defaults the output to a timestamped
`extraction-profile-<YYYYmmddTHHMMSS>.jsonl` in the working directory unless
`EXTRACTION_PROFILE_PATH` is already set in the config.

### Output location

- `EXTRACTION_PROFILE_PATH` when set (explicit path);
- otherwise `extraction-profile-<pipeline_id>.jsonl` next to
  `ASSET_LOCAL_EXTRACTED_DIR`;
- otherwise the same file name in the working directory.

### Record schemas

All records carry `"version": 3` and the `pipeline_id`. `item` is the sanitized
bundle label (same value as pipeline log lines; no URLs, no credentials).

Bundle record (successful extraction):

```json
{"record": "bundle", "version": 3, "pipeline_id": "1a2b3c4d", "ts": 1760000000.123, "item": "bundle-label", "status": "ok", "duration_sec": 0.411973, "output_count": 37, "output_bytes": 2841923, "cost_class": "media", "queue_wait_sec": 0.031402, "input_bytes": 52428800}
```

Bundle record (failed extraction):

```json
{"record": "bundle", "version": 3, "pipeline_id": "1a2b3c4d", "ts": 1760000001.456, "item": "bundle-label", "status": "error", "duration_sec": 0.032011, "output_count": 0, "output_bytes": 0, "error_class": "RuntimeError", "cost_class": "light", "queue_wait_sec": 0.012345, "input_bytes": 8192}
```

Only the exception **class name** is recorded — never the message — because
error messages can embed URLs or filesystem paths. Cancelled extractions are
not recorded (cancellation is a run-level event, not a bundle failure).

Optional per-bundle fields are omitted when unknown:

- `cost_class` (version 2) is the advisory routing tag stamped by the adaptive
  extraction scheduler on claim (`"light"` or `"media"` from
  `EXTRACT_MEDIA_BUNDLE_HINTS`, extraction-worker roadmap Phase 3). It is
  omitted in fixed-scheduler-mode and standalone runs.
- `queue_wait_sec` (version 3) is the time from download-stage enqueue to
  extract-worker claim. Splitting `queue_wait_sec` from `duration_sec` is how
  a production run distinguishes scheduling delay from
  extraction/transcode cost; media-classified bundles concentrate the
  transcode work inside `duration_sec` (HCA decode, ffmpeg encode, and USM
  demux run inside the single-bundle extraction boundary, so decode/transcode
  sub-splitting is a media-class indicator rather than a separate field).
- `input_bytes` (version 3) is the downloaded bundle file size observed just
  before extraction.

Comparing `cost_class` against the measured `duration_sec` and `input_bytes`
per bundle is the intended feedback loop for tuning the hints: they stay
advisory and never gate correctness.

Run summary record (written when the pipeline finishes; `status` is
`completed` or `aborted`; aggregate statistics are omitted on `aborted`):

```json
{"record": "summary", "version": 3, "pipeline_id": "1a2b3c4d", "ts": 1760000002.789, "status": "completed", "bundles": 4310, "failed": 2, "total_extraction_sec": 1820.412341, "total_idle_sec": 931.702115, "output_count": 91230, "output_bytes": 5368709120, "queue_depth_avg": 0.4215, "queue_depth_max": 6, "max_active_workers": 4, "cost_classes": {"light": {"bundles": 4100, "extraction_sec": 310.2, "bundles_per_sec": 13.2187}, "media": {"bundles": 210, "extraction_sec": 1510.2, "bundles_per_sec": 0.1391}}, "wait_sec_avg": 0.0312, "wait_sec_max": 1.2045, "wait_sec_p95": 0.4021, "worker_utilisation": 0.412, "media_active_max": 2}
```

- `cost_classes` — per-cost-class attempted bundles, total extraction
  seconds, and bundles-per-second throughput;
- `wait_sec_avg` / `wait_sec_max` / `wait_sec_p95` — queue-wait distribution
  across recorded bundles;
- `worker_utilisation` — extraction busy seconds over wall time × worker
  count (completed runs);
- `max_active_workers` / `media_active_max` — sampled scheduler saturation
  maxima (adaptive mode; 0 in fixed mode).

## Prometheus textfile export (`PROMETHEUS_METRICS_PATH`)

Setting `PROMETHEUS_METRICS_PATH` (for example
`/var/lib/node_exporter/textfile/sekai_updater.prom`) writes a
Prometheus textfile-format snapshot after every completed pipeline run, for a
textfile collector such as node_exporter — the batch updater needs no
long-lived HTTP endpoint. Metrics:

- `sekai_updater_extraction_bundles_total{cost_class="light"|"media"|"unclassified"}` — attempted extractions per class;
- `sekai_updater_extraction_failed_total` — failed extractions;
- `sekai_updater_extraction_seconds_total` — total extraction seconds across workers;
- `sekai_updater_queue_depth_max` — maximum sampled extraction queue depth;
- `sekai_updater_media_active_max` — maximum concurrently active media-classified extractions;
- `sekai_updater_worker_utilisation_ratio` — extraction busy time over worker capacity.

Metric names carry no bundle names, URLs, or storage configuration. Write
failures degrade to a `metrics_write_failed` warning and never affect
extraction.

### guarantees

- Profile writing is append-only and flushed per record; an interrupted run
  still contains every record emitted before the interruption.
- IO failures degrade to a `profile_write_failed` warning; profiling then
  disables itself for the rest of the run instead of affecting extraction.
- **Profiles are write-only for the pipeline**: nothing in the updater reads
  profile files back, so a corrupt, truncated, or stale file can never change
  correctness decisions or bypass resource limits — it is ignored by
  construction and simply keeps accumulating until deleted.
- **Invalidation**: records are keyed by sanitized bundle label and wall-clock
  timestamp, not by content hash — comparing bundles across runs is only
  meaningful while `UNITY_VERSION`, texture/audio output configuration, and
  the bundle content itself are unchanged; after any of those change, start a
  new profile file (new `--profile` run or new `EXTRACTION_PROFILE_PATH`)
  instead of comparing against old records.
- The profile is intended for offline analysis of long-tail bundle families
  and concurrency planning (#22 baseline reports: sort `duration_sec` for
  p50/p95/p99, group by name prefix or output media type for long-tail
  families). It is not consumed by the pipeline itself.
