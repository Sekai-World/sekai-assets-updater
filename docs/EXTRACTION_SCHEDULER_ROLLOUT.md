# Extraction Scheduler Rollout Guide

Extraction-worker roadmap Phase 6 (#27, parent #20). This runbook covers
enabling, validating, tuning, and rolling back the adaptive extraction
scheduler. The shipped code stays **inactive until explicitly enabled**:
`EXTRACT_SCHEDULER_MODE = "fixed"` (the default) keeps the homogeneous staged
extract stage byte-for-byte identical to the pre-roadmap behavior.

## What shipped

| Phase | Issue | Delivered |
|-------|-------|-----------|
| 0 | #22 | Always-on per-bundle timing/queue-depth metrics, opt-in JSON-lines profile ([EXTRACTION_PROFILING.md](EXTRACTION_PROFILING.md)) |
| 1 | #23 | `extract_single_bundle` boundary — one artifact in, populated artifact out, no queues |
| 2 | #21 | `ExtractionScheduler` admission queue behind `EXTRACT_SCHEDULER_MODE="adaptive"`; `EXTRACT_MAX_WORKERS` cap |
| 3 | #26 | Core/media extract pool split (`EXTRACT_CORE_CONCURRENCY`, `EXTRACT_MEDIA_CONCURRENCY`), advisory `EXTRACT_MEDIA_BUNDLE_HINTS` classification, media slot cap (`EXTRACT_ADAPTIVE_MEDIA_SLOTS`) |
| 4 | #25 | Fault-injection and recovery test gate: cancellation, process-pool crash, same-identity isolation, Live2D invariants (I5/I6) |
| 5 | #24 | Profile v3 (queue wait, input bytes, cost class), INFO performance report, CLI `--profile`, Prometheus textfile export |

Phases 0–5 are merged and covered by the test suite; the scheduler, pools,
and observability are shared code paths exercised in both modes.

## Configuration reference

| Knob | Default | Effect |
|------|---------|--------|
| `EXTRACT_SCHEDULER_MODE` | `"fixed"` | `"adaptive"` routes extraction through the admission scheduler. Rollback = set `"fixed"` (or remove the key). |
| `EXTRACT_MAX_WORKERS` | `None` (= `MAX_CONCURRENCY_EXTRACTS`) | Adaptive worker upper bound; never exceeds the fixed width. |
| `EXTRACT_ADAPTIVE_MEDIA_SLOTS` | `None` (= half the workers, ≥1) | Concurrent media-classified extraction slots; light/unknown bundles always keep at least one slot. |
| `EXTRACT_MEDIA_BUNDLE_HINTS` | `None` | Advisory regex hints over `bundleName` (e.g. `[r"^songs/", r"^movie/"]`). Unclassified bundles are light and always admissible. |
| `EXTRACT_CORE_CONCURRENCY` | `None` (= `MAX_CONCURRENCY_EXTRACTS`) | Extract pool width for light/unclassified bundles. |
| `EXTRACT_MEDIA_CONCURRENCY` | `None` (= `MAX_CONCURRENCY_EXTRACTS`) | Extract pool width for media-classified bundles (allocated lazily). |
| `EXTRACTION_PROFILING` / `EXTRACTION_PROFILE_PATH` / `PROMETHEUS_METRICS_PATH` | off | Observability; never affects extraction (see [EXTRACTION_PROFILING.md](EXTRACTION_PROFILING.md)). |

Audio/video sub-stage budgets are unchanged and independent
(`MAX_CONCURRENCY_HCA_DECODES`, `MAX_CONCURRENCY_AUDIO_ENCODERS`,
`MAX_CONCURRENCY_VIDEO_TRANSCODES`, `MAX_CONCURRENCY_USM_DEMUXES`).

## Pre-canary evidence (already green)

- **Output equivalence**: adaptive mode without hints produces byte-identical
  uploads to fixed mode (`test_adaptive_mode_without_hints_produces_fixed_mode_outputs`).
- **Correctness gate**: cancellation leaks nothing (I8), `BrokenProcessPool`
  keeps the pipeline alive and records failures (I7), same-identity artifacts
  stay isolated (I1), Live2D motion skip and aggregate workspace routing are
  unchanged by scheduling (I5/I6) — see the Phase 4 suite.
- **Media isolation**: a media-heavy burst cannot occupy every slot and
  light bundles keep moving (`test_adaptive_mode_limits_media_concurrency_and_keeps_light_moving`).
- **Security/containment**: the phase1 security suite and `_validate_artifact_outputs`
  containment checks are mode-independent and stay green in CI.

## Canary procedure

1. **Pick the corpus**: one region config and a representative bundle set —
   at minimum one full run's `dl_list` (light texture bundles plus
   audio/video-heavy `songs/`, `live_pv/`, `movie/` families).
2. **Baseline (fixed)**: run with `EXTRACT_SCHEDULER_MODE="fixed"`,
   `EXTRACTION_PROFILING=True`, `PROMETHEUS_METRICS_PATH` set, and an empty
   bundle cache/extracted root. Keep the resulting JSON-lines profile and the
   uploaded output manifest (object keys + sizes, or content hashes if your
   storage exposes them).
3. **Canary (adaptive)**: same corpus and empty roots;
   `EXTRACT_SCHEDULER_MODE="adaptive"` with hints seeded from the baseline
   profile's long-tail families (media-heavy prefixes), everything else
   default. Same observability settings.
4. **Compare**:
   - *Outputs*: manifest keys, file counts, and sizes/hashes per bundle
     identity must match between runs (identical extraction semantics are
     asserted by the equivalence suite; the canary re-proves it on real
     bundles).
   - *Failure/retry behavior*: `failed` counts and the failed-task rows fed
     back into `DL_LIST_CACHE_PATH` must be equivalent (transient CDN errors
     excepted).
   - *Performance*: from the two profiles — total run wall time, extraction
     wall time, `wait_sec_p95` (light-bundle wait), per-class
     `bundles_per_sec`, `worker_utilisation`, and long-tail completion
     (p95/p99 of `duration_sec` grouped by `cost_class` and name prefix).
   - *Live2D/Charts*: re-run the specialized post-processing (or a full
     assets+live2d mode run) in adaptive mode and diff the published
     `model_list.json` and chart outputs against the fixed run.
5. **Gate**: no output/manifest differences, no failure-rate regression, and
   ≥10% extraction wall-time reduction on the audio-heavy portion (roadmap
   target) with no `wait_sec_p95` regression for light bundles. A gate miss
   is a rollback, not a tuning iteration on production.

## Rollback and cleanup

- **Rollback** is one config change: `EXTRACT_SCHEDULER_MODE="fixed"` (or
  delete the key). No code revert; the scheduler, media pool, and profiling
  code paths are dormant in fixed mode (the media pool is only constructed
  after the first media-classified routing).
- **Pending work** survives a rollback: `run_pipeline` returns failed items
  and the driver rewrites them into `DL_LIST_CACHE_PATH` for the next run
  regardless of scheduler mode; the in-run pending queue is an in-memory
  staging detail that is drained and cleaned on cancellation (I8).
- **Partial runs**: cancelled or crashed runs leave no temporary bundle or
  staging files behind (Phase 4 cancellation suite). Persistent caches are
  per-bundle files written once per download and reused by identity; a
  partially-downloaded bundle never enters the cache because the cache path
  is written atomically by the download stage.
- **Stale artifacts**: staging directories under `ASSET_LOCAL_EXTRACTED_DIR`
  are per-`uuid4` identities; a stale directory from a killed run is never
  read (each run extracts into fresh identities) and can be deleted by age.
  The run-scoped temp variant is cleaned by the OS temp policy.

## Default switchover

Flipping `EXTRACT_SCHEDULER_MODE`'s default to `"adaptive"` is a deliberate,
documented decision made **after** a passing canary on a production region:

1. Canary gates pass (above) on the target region config.
2. Update the default in `updater/pipeline/__init__.py`
   (`get_extract_scheduler_mode`), `config.example.py`, and this document in
   one PR titled `feat!: default extraction scheduler to adaptive`.
3. Release-note the change: new knobs, profiling flags, rollback (single
   config change back to `"fixed"`).

Until that PR lands, `"fixed"` remains the default for every config.
