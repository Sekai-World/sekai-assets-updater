# Asset Updater Remediation Roadmap

## Purpose

This roadmap records the remediation work identified by the July 2026 project review. Its goals are to make the updater safe to run against untrusted or malformed asset metadata, preserve recovery state across crashes, and make concurrent processing deterministic.

## Guiding Rules

- Treat CDN metadata, UnityFS container paths, and bundle-provided filenames as untrusted input.
- Do not mark state as complete before the corresponding work is durably recoverable.
- Keep each bundle's staged outputs isolated until its upload is complete.
- Write persistent state atomically and validate it before use.
- Add focused regression tests with every remediation item.

## Phase 0: Test and Validation Foundation

**Priority:** P1 prerequisite

**Status:** Complete (2026-07-22)

### Work

- Separate executable/debug scripts from test discovery (`test_download_extract.py`, `test_download_audio.py`, `test_queue.py`, and `test_cridecoder.py`) or make them valid isolated tests.
- Fix `test_finish_dl_list.py` to use the project's JSON dependency instead of the missing `json_compat` module.
- Define a supported test command and add its dependencies to `pyproject.toml`.
- Add focused test fixtures for temporary directories, local `aiohttp` servers, fake subprocesses, and synthetic UnityFS paths.

### Acceptance criteria

- The documented test command runs from a clean `uv sync` environment.
- Test discovery does not import or execute local-data debugging scripts.
- Existing audio pipeline tests remain green.

## Phase 1: Path Containment and File-write Safety

**Priority:** P1

**Status:** Complete (2026-07-23)

### Risks addressed

- `bundleName`, UnityFS paths, cue sheet names, and USM names can escape configured download/extraction roots.
- Existing symlinks can redirect writes outside intended roots.

### Work

- Implement one shared safe-path helper for all downloaded, extracted, generated, and uploaded paths.
- Reject absolute paths, empty/invalid paths, and `..` components before joining paths.
- Resolve candidates and require that they remain below the configured root.
- Defend against symlink traversal in writable output trees.
- Apply the helper to bundle cache paths, UnityFS extraction paths, ACB filenames, USM filenames, and remote upload path derivation.
- Write generated files through a temporary file and atomically replace the final target where practical.

### Acceptance criteria

- Inputs such as `/tmp/out`, `../../out`, and UnityFS paths containing `..` are rejected.
- A pre-created symlink cannot redirect a write outside the configured root.
- Exported files and derived remote paths are always contained by their expected roots.

## Phase 2: Bundle-isolated Extraction Staging

**Priority:** P1

**Status:** Complete (2026-07-23)

### Risks addressed

- Concurrent extract workers write into the same local extracted directory.
- One bundle can overwrite another bundle's exports before upload.

### Work

- Give every `PipelineArtifact` a bundle-specific staging directory, including when `ASSET_LOCAL_EXTRACTED_DIR` is configured.
- Keep `exported_list` scoped to that immutable staging directory.
- Define the post-upload persistence behavior explicitly: retain per-bundle output, or atomically publish into a shared destination using deterministic collision rules.
- Ensure ACB/USM post-processing and cleanup operate only within the artifact's staging area.

### Acceptance criteria

- Two extract workers exporting the same relative name do not overwrite one another.
- Uploading artifact A cannot upload content written by artifact B.
- Temporary staging directories are removed only after their own upload completes.

## Phase 3: Crash-safe Queue and Metadata State

**Priority:** P1

**Status:** Complete (2026-07-23)

### Risks addressed

- Metadata may be committed before `dl_list.json`, allowing a crash to silently skip downloads.
- Direct cache writes can leave truncated JSON after an interruption.

### Work

- Separate download-list calculation from metadata cache persistence.
- Persist the pending queue before advancing the metadata checkpoint, or introduce a durable transaction journal/marker.
- Replace direct cache writes with same-directory temporary files, flush/fsync, and atomic replacement.
- Validate cache JSON schema on load and recover from temporary, backup, or journal files when possible.
- Preserve a retryable queue when any stage fails or the process is interrupted.

### Acceptance criteria

- A simulated crash after metadata preparation but before queue commit cannot cause a subsequent no-op.
- Interrupted writes do not replace the last valid cache file.
- Invalid cache data produces actionable recovery behavior rather than an opaque JSON parsing failure.

## Phase 4: Download Integrity and Retry Semantics

**Priority:** P1

**Status:** Complete (2026-07-24)

### Risks addressed

- Empty, truncated, or malformed HTTP 200 responses are accepted as successful downloads.
- Cancellation may be retried and common transient connection failures are not retried consistently.

### Work

- Download to a temporary file and atomically promote it only after validation.
- Validate Content-Length when available, actual byte count, and metadata hash/CRC where the source provides it.
- Add a lightweight bundle-format/loadability validation before accepting the cache entry.
- Re-raise `asyncio.CancelledError` immediately.
- Retry suitable connection failures, timeouts, 429 responses, and transient 5xx responses with capped exponential backoff and jitter.
- Do not retry permanent client/configuration failures by default.

### Acceptance criteria

- HTTP 200 with an empty or truncated body does not create a successful cache entry.
- A canceled pipeline stops without starting another retry.
- `ClientConnectorError`, disconnects, and payload failures follow the configured retry policy.

## Phase 5: Secrets, Process Lifetime, and Configuration Consistency

**Priority:** P1

**Status:** Complete (2026-07-26)

### Work

- Introduce a log-sanitization helper and redact `Cookie`, `Authorization`, API keys, and signed URL parameters before logging requests or failures.
- Add configurable timeouts to ffmpeg, vgmstream, and other external decoder processes.
- On timeout or cancellation, terminate then kill child processes and await their exit.
- Centralize HTTP request construction so proxy, headers, cookie, and timeout settings are applied consistently to every metadata and cookie request.
- Validate configuration at startup, including positive concurrency values and required external command availability when selected.
- Fix `config.example.py` to be importable and use clearly documented, valid-length placeholder AES values.

### Acceptance criteria

- Logs never include a raw Cookie or Authorization value.
- A hanging external command is terminated within its configured timeout.
- Configured proxy and request headers are applied consistently to all relevant requests.
- Invalid concurrency values, including `MAX_CONCURRENCY_UPLOADS=0`, fail clearly rather than deadlocking.

## Phase 6: Correctness Fixes in Update Selection and Asset Handling

**Priority:** P2

**Status:** Complete (2026-07-25)

### Work

- Correct priority-list ordering so matching patterns run before unmatched bundles.
- Review Nuverse update selection: compare bundle checksum changes even when `assetver` is unchanged unless assetver is documented and guaranteed as the sole version authority.
- Use validated URL-template formatting consistently and reject missing values instead of formatting `None` into URLs.
- Pass the configured bundle cache root explicitly to cross-bundle ACB lookup instead of inferring it from a directory named `bundle`.
- Fix `utils.binary.BinaryStream.readStringToNull()` to raise on EOF and restore stream offsets with `try/finally`.

### Acceptance criteria

- Priority patterns execute in declared order, followed by unmatched bundles.
- A checksum-only change produces a download candidate under the documented regional policy.
- A custom bundle cache directory name still supports cross-bundle ACB lookup.
- Unterminated binary strings fail with `EOFError` rather than looping indefinitely.

## Phase 7: Pipeline Integration and Regression Coverage

**Priority:** P2

**Status:** Complete (2026-07-27)

### Work

- Add integration tests for bounded `download -> extract -> upload` queues, sentinels, cleanup, cancellation, and failed-task persistence.
- Add fault-injection tests for atomic cache writes and the metadata/queue commit boundary.
- Add security regression tests for all path-bearing fields and symlink cases.
- Add local-server tests for download integrity, retries, status-code handling, and cookie/header redaction.
- Add fake-process tests for timeout, termination, and cancellation behavior.
- Add CI to run formatting/linting and the documented test suite on supported Python versions.

### Acceptance criteria

- Each P1 remediation has a regression test.
- The pipeline test suite covers success, stage failure, cancellation, and recovery.
- CI runs successfully without locally cached game assets or external network access.

## Delivery Order

1. Phase 0 — establish a reliable test entry point.
2. Phase 1 — contain all writes before processing untrusted inputs.
3. Phase 2 — isolate concurrent artifact outputs.
4. Phase 3 — make pending work and metadata crash-safe.
5. Phase 4 — prevent corrupted downloads from entering the pipeline.
6. Phase 5 — protect credentials and bound external work.
7. Phase 6 — correct selection and parser edge cases.
8. Phase 7 — lock the behavior in with integration coverage and CI.

## Definition of Done

The remediation is complete when all P1 phases have passed their acceptance criteria, documented tests run from a clean environment, no path derived from external data can escape its configured root, interrupted runs cannot silently lose pending work, and credentials are absent from application logs.
