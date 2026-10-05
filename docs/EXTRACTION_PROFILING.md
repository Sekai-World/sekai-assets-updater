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

## JSON-lines profile (`EXTRACTION_PROFILING`)

Setting `EXTRACTION_PROFILING = True` additionally writes a versioned JSON-lines
profile file with one record per bundle plus a final run summary record.

### Output location

- `EXTRACTION_PROFILE_PATH` when set (explicit path);
- otherwise `extraction-profile-<pipeline_id>.jsonl` next to
  `ASSET_LOCAL_EXTRACTED_DIR`;
- otherwise the same file name in the working directory.

### Record schemas

All records carry `"version": 1` and the `pipeline_id`. `item` is the sanitized
bundle label (same value as pipeline log lines; no URLs, no credentials).

Bundle record (successful extraction):

```json
{"record": "bundle", "version": 1, "pipeline_id": "1a2b3c4d", "ts": 1760000000.123, "item": "bundle-label", "status": "ok", "duration_sec": 0.411973, "output_count": 37, "output_bytes": 2841923}
```

Bundle record (failed extraction):

```json
{"record": "bundle", "version": 1, "pipeline_id": "1a2b3c4d", "ts": 1760000001.456, "item": "bundle-label", "status": "error", "duration_sec": 0.032011, "output_count": 0, "output_bytes": 0, "error_class": "RuntimeError"}
```

Only the exception **class name** is recorded — never the message — because
error messages can embed URLs or filesystem paths. Cancelled extractions are
not recorded (cancellation is a run-level event, not a bundle failure).

Run summary record (written when the pipeline finishes; `status` is
`completed` or `aborted`):

```json
{"record": "summary", "version": 1, "pipeline_id": "1a2b3c4d", "ts": 1760000002.789, "status": "completed", "bundles": 4310, "failed": 2, "total_extraction_sec": 1820.412341, "total_idle_sec": 931.702115, "output_count": 91230, "output_bytes": 5368709120, "queue_depth_avg": 0.4215, "queue_depth_max": 6}
```

### guarantees

- Profile writing is append-only and flushed per record; an interrupted run
  still contains every record emitted before the interruption.
- IO failures degrade to a `profile_write_failed` warning; profiling then
  disables itself for the rest of the run instead of affecting extraction.
- The profile is intended for offline analysis of long-tail bundle families
  and concurrency planning (#22 baseline reports: sort `duration_sec` for
  p50/p95/p99, group by name prefix or output media type for long-tail
  families). It is not consumed by the pipeline itself.
