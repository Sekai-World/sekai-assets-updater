# Asset Updater Pipeline Plan

## Goals

- Split download, extract, and upload into separate stages to improve throughput.
- Keep disk usage bounded so the updater does not fill the local filesystem.
- Preserve the current recovery behavior based on `dl_list.json`.
- Validate each step with a narrow check before moving to the next stage.

## Constraints

- The current single worker flow intentionally avoids unbounded disk growth by downloading, extracting, and uploading serially per bundle.
- Any pipeline refactor must keep temporary bundle files and temporary extracted files short-lived.
- If free disk space drops below a configured reserve, new downloads must wait until enough space is available.

## Phase 1: Download Disk Space Gate

Objective: prevent concurrent downloads from overcommitting the download filesystem.

Implementation:

- Add config knobs for a minimum free disk reserve and the disk-space recheck interval.
- Add a shared async disk-space gate that:
  - checks the filesystem backing the download target,
  - accounts for in-flight reserved download bytes,
  - blocks new downloads until `free_bytes - reserved_bytes >= reserve + bundle_file_size`.
- Apply the gate immediately before `download_deobfuscate_bundle` starts.

Validation:

- Type/syntax check the touched files.
- Run a small targeted async check that exercises reservation and release behavior.

Status:

- Completed.
- Implemented in the current worker flow before each bundle download starts.
- Verified with a focused async reservation/release check.

## Phase 2: Worker Stage Split

Objective: separate `download -> extract -> upload` while preserving bounded disk usage.

Implementation:

- Introduce per-stage queues with configurable concurrency.
- Pass a small artifact object between stages containing:
  - bundle metadata,
  - downloaded bundle path,
  - extracted directory path,
  - cleanup flags.
- Keep queue sizes bounded so download cannot outrun extract/upload.

Validation:

- Run a focused behavior check on the pipeline driver with a tiny synthetic workload.
- Confirm failure handling still writes the pending list.

Status:

- Completed.
- Implemented bounded download, extract, and upload stage queues with configurable stage concurrency.
- Verified with synthetic pipeline checks including failed task persistence.

## Phase 3: Stage Cleanup Guarantees

Objective: release disk space as soon as each bundle no longer needs it.

Implementation:

- Delete temporary bundle files immediately after extract succeeds when local bundle cache is disabled.
- Delete temporary extracted directories immediately after upload succeeds when local extracted cache is disabled.
- On failure, keep only the minimum artifacts needed for retry or diagnostics.

Validation:

- Run a narrow end-to-end dry run on one bundle with temp paths.
- Confirm temporary files disappear after downstream success.

Status:

- Completed.
- Temporary bundle files are removed after successful extract when bundle caching is disabled.
- Temporary extracted directories are removed after upload stage completion when extracted caching is disabled.
- Failure paths clean temporary artifacts while preserving failed bundle metadata for retry.

## Phase 4: Session Reuse And Throughput Tuning

Objective: recover network overhead once the pipeline is structurally safe.

Implementation:

- Reuse shared `aiohttp` sessions where practical.
- Tune stage-specific concurrency instead of relying on a single global worker count.

Validation:

- Compare logs and runtime on a small representative bundle set.

Status:

- Completed.
- Pipeline downloads share one aiohttp session across download workers.
- Added stage-specific concurrency knobs while preserving legacy defaults where practical.

## Current Step

All planned phases are complete.
