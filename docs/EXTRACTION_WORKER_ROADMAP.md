# Extraction Worker Layer Roadmap

> **On-premise optimization planning.** This document describes a future change to the
> extraction stage of the download–extract–upload pipeline. No source changes are implied
> until individual phases are approved and executed.

## Objective

Evolve the current fixed-width extraction worker stage into an adaptive scheduler that
accounts for per-bundle processing cost. The current shared `asyncio.Queue` already gives
workers dynamic item claiming; this roadmap adds explicit scheduling and resource-admission
behaviour rather than replacing that basic queue with another equivalent queue. The goal is to maximise throughput on
on-premise hardware where bundle processing times vary widely (e.g., small texture-only
bundles finish in milliseconds while audio-heavy bundles with HCA decode, ffmpeg transcode,
and USM demux can take seconds), while keeping download and upload stages as simple,
IO-oriented pipelines.

## Current Problem

`run_pipeline` in `updater/pipeline.py` creates a fixed number of `extract_worker-*` coroutines
(`MAX_CONCURRENCY_EXTRACTS`, typically equal to `MAX_CONCURRENCY` / `os.cpu_count()`).
Each worker pulls `PipelineArtifact` items from a bounded `asyncio.Queue` and runs
`extract_asset_bundle` sequentially.

This design has two deficiencies:

1. **Uniform concurrency ignores heterogeneous cost.** A 200 KB texture bundle and a 50 MB
   audio bundle receive the same resource budget. Although workers dynamically claim the next
   queue item, all extraction slots can still be occupied by subprocess-bound work (HCA decode,
   ffmpeg, vgmstream), leaving trivial bundles waiting for a slot.

2. **CPU and process-pool contention is unobserved.** The process pool executor shared by
   `_extract_bundle_files_sync` runs at a fixed width (`MAX_CONCURRENCY`), but the pipeline
   has no visibility into whether audio/video sub-stages (`_audio_runtime`, `_video_runtime`,
   `updater.bundle.runtime`) are saturated. Back-pressure from subprocess limits does not flow back to
   the extraction scheduler.

The result is under-utilised CPU when IO-bound bundles dominate, and degraded latency when
compute-bound bundles flood the shared process pool.

## Target Architecture

```
                    ┌─────────────┐
                    │  download   │  (unchanged — IO-oriented, bounded queue)
                    │  workers    │
                    └──────┬──────┘
                           │  PipelineArtifact
                    ┌──────▼──────┐
                    │  scheduler  │  new dynamic scheduling layer
                    │  (phase 2+) │
                    └──────┬──────┘
                           │  resource-aware dispatch
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ extract  │ │ extract  │ │ extract  │  variable worker count
        │ worker 0 │ │ worker 1 │ │ worker N │  (phase 3+: media-aware)
        └────┬─────┘ └────┬─────┘ └────┬─────┘
             │             │             │
             └─────────────┼─────────────┘
                           │  PipelineArtifact
                    ┌──────▼──────┐
                    │   upload    │  (unchanged — IO-oriented, bounded queue)
                    │   workers   │
                    └─────────────┘
```

**Download and upload remain staged IO-oriented pipelines.** They are not part of this
roadmap. Only the extraction stage is restructured.

### Key Design Choices

- **Dynamic work-conserving scheduling:** Preserve the existing shared-queue behaviour in
  which extraction workers claim one ready artifact at a time and claim the next artifact
  immediately after completion. The new scheduler must improve admission and resource
  accounting, not merely reimplement this queue.

- **Optional cost hints, not correctness rules:** File size, bundle prefixes, and historical
  processing profiles may later influence queue preference, but unknown or misclassified
  bundles must remain processable. A conservative fallback is preferable to a routing
  decision that can starve or deadlock the queue.

- **Resource-aware media isolation (phase 3):** Heavy audio/video sub-stages run in
  dedicated pools with separate concurrency budgets (`_audio_runtime`, `_video_runtime`),
  preventing a burst of HCA decodes from starving texture extraction.

- **Preserved staging model:** Every `PipelineArtifact` retains its own bundle-specific
  staging directory. The scheduler does not change isolation semantics — it only changes
  *which* artifact is processed next and *how many* workers are active.

## Non-Goals

- **Changing the download stage.** Download remains a fixed-concurrency IO pipeline
  controlled by `MAX_CONCURRENCY_DOWNLOADS` and `DownloadDiskSpaceGate`.

- **Changing the upload stage.** Upload remains a fixed-concurrency IO pipeline controlled
  by `MAX_CONCURRENCY_UPLOAD_STAGE`. The per-storage upload logic in `_upload_stage` is
  unchanged.

- **Changing `PipelineArtifact` fields or staging contracts.** The artifact dataclass,
  staging identity (`_bundle_staging_identity`), and `_validate_artifact_outputs` containment
  checks remain as-is.

- **Changing the specialized post-processing flow.** Live2D motion restoration
  (`recover_live2d_model_outputs`), Charts rendering (`run_specialized_postprocess`), and
  their aggregate workspace routing (`_uses_aggregate_workspace`) are not restructured.

- **Changing crash-safety or state persistence.** The journal, metadata cache, and pending
  queue mechanisms in `state.py` are not part of this work.

- **Distributed or multi-host scheduling.** This roadmap targets a single on-premise
  machine. Cross-node work distribution is out of scope.

## Invariants

These invariants from the current pipeline must be preserved across all phases:

| # | Invariant | Source |
|---|-----------|--------|
| I1 | Two concurrent workers exporting the same relative path cannot overwrite each other. | `extract_asset_bundle` → per-artifact `extracted_save_path` |
| I2 | An artifact's exported paths are validated to be contained within its staging root. | `_validate_artifact_outputs` |
| I3 | Bundle files are removed only after extraction completes (success or failure). | `_cleanup_artifact` with `remove_bundle=True` after extract |
| I4 | Extracted temporary directories are removed only after upload completes or on cancellation. | `remove_extracted_after_upload` flag in `_cleanup_artifact` |
| I5 | Live2D `live2d/motion/` bundles skip motion extraction in `assets` mode. | `extract_asset_bundle` early return |
| I6 | Live2D model recovery operates on the aggregate workspace, not per-bundle staging. | `recover_live2d_model_outputs` |
| I7 | Failed tasks are appended to `failed_tasks` and returned by `run_pipeline` for retry. | `_extract_stage` / `_upload_stage` exception handlers |
| I8 | Cancellation propagates: cancelling the pipeline cancels all workers and cleans up queued artifacts. | `run_pipeline` `BaseException` handler |
| I9 | `PIPELINE_STAGE_QUEUE_SIZE` bounds the `extract_queue` to prevent unbounded memory growth. | `get_stage_queue_size` |
| I10 | `extract_asset_bundle` offloads sync work to a shared process pool via `run_in_executor`. | `_get_shared_extract_process_pool` |

## Phases

Phases are listed in dependency order. Each phase is independently shippable behind a
feature flag and does not change pipeline semantics until explicitly enabled.

---

### Phase 0: Measurement and Profiling Baseline

**Depends on:** Nothing.

**Work:**

- Add structured per-bundle timing to `_extract_stage`: record wall-clock duration,
  subprocess pool wait time (if measurable from the executor), and the count/sizes of
  exported files. Emit a structured log line per completed extraction with these fields.
- Add aggregate per-pipeline metrics: total extraction wall time, worker idle time
  (time between `extract_queue.get` returning a sentinel and the next real item), and
  queue depth histograms sampled at fixed intervals.
- Add optional file-level profiling (enabled by `EXTRACTION_PROFILING=True` in config)
  that writes a JSON-lines profile to the staging directory.
- Document the profiling output format.

**Acceptance criteria:**

- A standard `assets` run produces per-bundle timing logs with bundle name, duration,
  exported file count, and total output size.
- A pipeline summary log line reports aggregate extraction time and average queue depth.
- Enabling `EXTRACTION_PROFILING=True` writes a JSON-lines file without changing
  pipeline behaviour or correctness.
- Existing tests pass unchanged.

---

### Phase 1: Single-Bundle Extraction Boundary

**Depends on:** Phase 0.

**Work:**

- Extract the inner body of `_extract_stage`'s try-block into a pure async function
  `extract_single_bundle(artifact, config) -> PipelineArtifact` that takes an artifact
  with `bundle_save_path` set and returns it with `extracted_save_path` and
  `exported_list` populated, without touching queues.
- Refactor `_extract_stage` to call `extract_single_bundle` and handle queue
  management externally.
- This boundary becomes the unit of scheduling for subsequent phases.

**Acceptance criteria:**

- `extract_single_bundle` can be called standalone with a pre-populated
  `PipelineArtifact` and returns a fully populated artifact.
- `_extract_stage` delegates to `extract_single_bundle`; the pipeline behaves
  identically.
- `ruff check` and existing tests pass.
- No public API of `run_pipeline` or `PipelineArtifact` changes.

---

### Phase 2: Dynamic Scheduler

**Depends on:** Phase 1.

**Work:**

- Implement an `ExtractionScheduler` class around the existing bounded `extract_queue`.
  It should preserve dynamic worker claiming while adding explicit admission/resource
  accounting; do not introduce fixed per-worker batches.
- Keep the first implementation work-conserving and avoid requiring exact bundle
  classification. FIFO with resource admission is an acceptable baseline; any cost-aware
  preference must be bounded so large or unknown bundles cannot be starved.
- Keep the extraction worker count bounded by an explicit configured maximum. Adaptive
  resizing and cost-based ordering are optional follow-ups informed by Phase 0 data, not
  prerequisites for the first scheduler implementation.
- Expose scheduler configuration: `EXTRACT_SCHEDULER_MODE` (`"fixed"` for backward
  compatibility, `"adaptive"` for the new behaviour).
- Keep `"fixed"` as the default so the pipeline behaves identically until explicitly
  opted in.

**Acceptance criteria:**

- With `EXTRACT_SCHEDULER_MODE="fixed"`, behaviour is identical to the current pipeline.
- With `EXTRACT_SCHEDULER_MODE="adaptive"`, workers retain dynamic queue claiming while
  resource admission prevents a burst of heavy work from consuming every extraction slot.
  If cost hints are enabled, they do not starve large or unknown bundles.
- Worker count never exceeds `EXTRACT_MAX_WORKERS` (default: current
  `MAX_CONCURRENCY_EXTRACTS`).
- All pipeline invariants (I1–I10) hold under both modes.
- Existing tests pass with default config.

---

### Phase 3: Resource-Aware Media Isolation

**Depends on:** Phase 2.

**Work:**

- Separate the process pool used by `_extract_bundle_files_sync` into two pools:
  a "media" pool for audio/video subprocesses (HCA decode, ffmpeg encode, USM demux)
  and a "core" pool for Unity object extraction and texture saving.
- Expose `EXTRACT_MEDIA_CONCURRENCY` and `EXTRACT_CORE_CONCURRENCY` config knobs
  (defaulting to the current `MAX_CONCURRENCY` values for backward compatibility).
- The scheduler in Phase 2 uses pool saturation signals (active task count vs
  configured concurrency) to make dispatch decisions: if the media pool is saturated,
  prefer dispatching bundles estimated to have no audio/video sub-assets.
- Preserve the existing behaviour where `extract_asset_bundle` calls
  `run_in_executor` on the shared pool; change the pool reference based on a bundle
  classification tag added by the scheduler.

**Acceptance criteria:**

- A batch of texture-only bundles is not blocked by concurrent audio-heavy bundles.
- Media pool saturation is observable via structured logs.
- Default config values produce the same concurrency as the current pipeline.
- Existing tests pass.

---

### Phase 4: Correctness and Recovery

**Depends on:** Phase 3.

**Work:**

- Verify that all Phase 0–3 changes preserve crash-safety: an interruption during
  dynamic scheduling must still clean up all `PipelineArtifact` instances (invariant I8).
- Add a cancellation test: cancel `run_pipeline` while the scheduler has dispatched
  items and workers are mid-extraction; verify no temporary files are leaked.
- Add a scheduler fault-injection test: simulate a worker exception during adaptive
  scheduling; verify the artifact is appended to `failed_tasks` (invariant I7) and
  the scheduler continues processing remaining items.
- Add a regression test for Live2D scope: verify that dynamic scheduling does not
  change which bundles skip motion extraction (invariant I5) or how aggregate workspace
  routing works (invariant I6).

**Acceptance criteria:**

- Cancellation mid-extraction leaves no temporary files in staging or bundle cache
  directories.
- A worker exception during adaptive scheduling does not stall the pipeline.
- Live2D bundles are scheduled and processed with the same semantics as before.
- All existing and new tests pass.

---

### Phase 5: Observability and Profiles

**Depends on:** Phase 4.

**Work:**

- Extend Phase 0 profiling to include scheduler decisions: log the estimated cost
  class, dispatch order, and worker pool saturation at each dispatch point.
- Add a pipeline completion report that summarises: per-cost-class throughput
  (bundles/second), worker utilisation, queue wait time distribution, and subprocess
  pool utilisation.
- Add a `--profile` CLI flag to `main.py` that enables profiling for a single run
  and writes the output to a timestamped JSON-lines file.
- Add optional Prometheus-compatible metrics export (controlled by config) for
  ongoing production monitoring.

**Acceptance criteria:**

- A `--profile` run produces a machine-readable report with per-bundle timing,
  scheduler decisions, and aggregate statistics.
- The pipeline completion report is emitted at INFO level and includes all
  summary metrics.
- Profiling overhead is negligible when disabled.
- Existing tests pass.

---

### Phase 6: Canary and Rollout

**Depends on:** Phase 5.

**Work:**

- Ship all phases behind `EXTRACT_SCHEDULER_MODE="fixed"` (default) so the new code
  is deployed but inactive.
- Run a canary: set `EXTRACT_SCHEDULER_MODE="adaptive"` on a single region config
  and compare throughput and resource utilisation against the fixed baseline.
- Document rollback: set `EXTRACT_SCHEDULER_MODE="fixed"` to revert.
- After canary validation, update the default to `"adaptive"` and document the
  change in a release note.
- Add a migration guide: config changes needed, new config knobs, and how to
  interpret profiling output.

**Acceptance criteria:**

- Canary run on a production region shows measurable throughput improvement (target:
  ≥10% reduction in total extraction wall time for audio-heavy batches) with no
  regression in error rate.
- Rollback to `"fixed"` mode is a single config change with no code revert needed.
- Migration guide documents all new config knobs with examples.
- Existing tests pass with both `"fixed"` and `"adaptive"` modes.

---

## Rollback Strategy

Every phase ships behind a configuration flag that defaults to the current behaviour.
Rollback at any point is:

1. Set `EXTRACT_SCHEDULER_MODE="fixed"` (or remove the flag entirely to use the
   default).
2. No code revert required; the new scheduling code is inactive.

For phases 3+ (media isolation), also revert `EXTRACT_MEDIA_CONCURRENCY` and
`EXTRACT_CORE_CONCURRENCY` to their defaults (which equal `MAX_CONCURRENCY`).

If a phase introduces a regression that is not flag-gated (e.g., a change to
`extract_single_bundle` that affects correctness), revert the phase commit and
re-run the existing test suite.

## Issue Breakdown

The implementation is tracked by the following active GitHub issues. The numbers below
refer to the repository issues, not phase-local placeholders.

| Issue | Phase(s) | Title | Dependencies |
|-------|-----------|-------|--------------|
| [#20](https://github.com/Sekai-World/sekai-assets-updater/issues/20) | All | Adaptive extraction worker scheduling (parent) | — |
| [#22](https://github.com/Sekai-World/sekai-assets-updater/issues/22) | 0 | Benchmark bundle extraction cost and long-tail behavior | — |
| [#23](https://github.com/Sekai-World/sekai-assets-updater/issues/23) | 1 | Extract a reusable single-bundle extraction task | #22 (informational) |
| [#21](https://github.com/Sekai-World/sekai-assets-updater/issues/21) | 2 | Add dynamic extraction worker scheduling | #22, #23 |
| [#26](https://github.com/Sekai-World/sekai-assets-updater/issues/26) | 3 | Isolate audio and video extraction resource budgets | #21 |
| [#25](https://github.com/Sekai-World/sekai-assets-updater/issues/25) | 4 | Preserve cache, staging, and recovery guarantees with extraction workers | #21, #23, #26 |
| [#24](https://github.com/Sekai-World/sekai-assets-updater/issues/24) | 5 | Add extraction scheduler metrics and processing profiles | #21, #25, #26 |
| [#27](https://github.com/Sekai-World/sekai-assets-updater/issues/27) | 6 | Canary and roll out adaptive extraction scheduling | #21, #24, #25, #26 |

Recommended execution order:

```text
#22 → #23 → #21 → #26 → #25 → #24 → #27
```

Issue #23 may start its interface design while #22 is being measured, but its final
boundary should incorporate any relevant benchmark findings. Issue #25 is a correctness
gate after the scheduler and media budgets exist; the adaptive scheduler must not become
the default before it passes. Issue #24 may be implemented incrementally, but its
production profile/reporting acceptance belongs after the correctness gate.
