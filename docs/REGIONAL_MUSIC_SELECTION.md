# Automatic regional music selection

## Enable once

```python
ENABLE_REGION_EXCLUSIVE_MUSIC = True
# Replace per-song includes, preserving other patterns you need.
DL_INCLUDE_LIST = [r"^music/"]
```

The option defaults to `False`, including in older configurations that omit it.
JP keeps its existing behavior even when the option is enabled. Production
configuration and previously published assets are not automatically modified.

The automatic scope is an additional restriction, not a force-include rule.
Existing include/exclude patterns still apply. Broaden a per-song include list
once instead of adding each new song manually.

## Song-level selection only

Each enabled non-JP run fetches three public tables:

- JP `musics.json`.
- Target-region `musics.json` and `musicVocals.json`.

The selected song IDs are **target-region song IDs minus JP song IDs**.
`musicVocals` only maps those selected songs to their audio resources; it is
not compared against JP vocals. TW uses the upstream `tc` master-data server.

Shared songs are excluded, including different vocal bundle names, translated
captions, changed credited singers, and instrumental resource renames. Even
EN song `502`'s `English ver.` is outside this intentionally song-only scope.

"Regional" means absent from the **current JP baseline**, not necessarily
exclusive to one non-JP server or originally released outside JP. The set can
include songs removed from JP but still present elsewhere. If JP later adds a
selected song, future runs stop selecting it; existing outputs are not deleted.

## Bundle and chart scope

| Namespace | Selection |
| --- | --- |
| `music/short/`, `music/long/` | Exact vocal bundle names belonging to selected songs |
| `music/jacket/` | Selected songs' exact `musics.assetbundleName` values |
| `music/music_score/` | Selected song IDs parsed from score-directory prefixes |
| Other `music/` namespaces | Excluded until a supported mapping exists |
| Non-`music/` namespaces | Existing user filters and specialized-mode behavior |

`musics.assetbundleName` identifies the jacket, for example `jacket_s_001`.
Audio names come from the target region's `musicVocals.assetbundleName`.
Zero-padded score directories such as `0001_01` use their numeric song ID.
All mapped audio versions of a selected song remain eligible.

Chart rendering and upload use the same song scope, including when old local
score/chart directories exist. A cached pending queue cannot reintroduce music
outside the current scope. With no selected song IDs, Charts skips its source
copy. Otherwise, remote fallback still copies the score tree using the existing
normal-storage command, then scopes rendering and upload; command-specific
transfer filters are not added.

Streaming-live/event sound, virtual-live models, music-related UI atlases, MV
assets, cards, stamps, events, and login bonuses retain their original filters.

## Failure and incremental state

- Invalid, empty, or unavailable master tables stop an enabled run; there is no
  fallback to all music or a persistent master-data fallback cache.
- A successfully computed empty song selection is valid.
- Excluded bundles are not marked successfully processed. An expanded song set
  can select previously unprocessed bundles even when their checksum is unchanged.
- Complete asset manifests and successful-processing provenance are retained;
  metadata-only runs remain observational.
- Disabled runs, JP runs, and standalone Live2D runs do not require these new
  master-data requests.
- Latest public tables are not revision-pinned, atomic snapshots. A structurally
  valid but lagging table can temporarily change the computed difference set.

## Verification evidence

The public tables checked on **2026-10-27** produced these song-level counts:

| Region | Song IDs absent from JP |
| --- | ---: |
| EN | 26 |
| TW (`tc`) | 8 |
| KR | 16 |
| CN | 22 |

Source URLs are `https://sekai-world.github.io/sekai-master-db-diff/` for JP
and `https://sekai-world.github.io/sekai-master-db-{region}-diff/` otherwise.
Counts describe metadata selection, before intersecting the asset manifest and
user filters; they are not a promise that every resource is already downloadable.

CN shared songs `660`, `662`, `695`, and `723` have different credited singers
in master data, but actual samples disproved the inferred audio difference.
Using CN app `6.4.0` / manifest `16` and JP app `7.1.0` / manifest `7.1.0.35`,
all eight corresponding short/long bundle pairs had identical deobfuscated
SHA-256 hashes. All 16 paired extracted audio tracks had identical native PCM
parameters and sample hashes. Singer metadata alone therefore does not establish
a region-specific recording. Regression tests exclude shared-song vocal
differences rather than treating them as selected songs.
