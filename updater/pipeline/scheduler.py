"""Admission-control scheduling for the extraction stage.

Extraction-worker roadmap Phase 2 (#21). The scheduler wraps a bounded
pending queue between the download stage and the extract workers. Workers
still claim one artifact at a time and claim the next only after completion;
admission adds a media-slot cap so a burst of heavy, hint-classified bundles
cannot consume every extraction slot while light work waits.

Classification is advisory and name-based only: unknown bundles always
classify as light and stay immediately admissible, so hints can never starve
or deadlock the queue. Correctness decisions, retries, and cleanup stay with
the staged engine in ``updater/pipeline/__init__.py``.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import TYPE_CHECKING, Any, Dict

from updater.runtime import LIGHT_COST_CLASS, MEDIA_COST_CLASS

if TYPE_CHECKING:
    from updater.pipeline import PipelineArtifact

__all__ = [
    "LIGHT_COST_CLASS",
    "MEDIA_COST_CLASS",
    "ExtractionScheduler",
    "classify_bundle_cost",
]


def classify_bundle_cost(
    bundle: Dict[str, Any],
    media_hints: tuple[re.Pattern[str], ...],
) -> str:
    """Return the advisory cost class for a bundle from configured name hints."""

    name = bundle.get("bundleName") or ""
    if any(hint.search(name) for hint in media_hints):
        return MEDIA_COST_CLASS
    return LIGHT_COST_CLASS


class ExtractionScheduler:
    """Bounded pending queue with class-based admission for extraction workers.

    ``put`` mirrors the ``asyncio.Queue`` interface used by the download stage,
    including bounded backpressure via ``capacity``. ``claim`` hands out the
    first admissible pending artifact in FIFO order, skipping media-classified
    artifacts only while their slot cap is exhausted (a bounded reordering);
    ``release`` must be called once per claimed artifact to free its slot.
    ``qsize`` keeps the pipeline queue-depth sampler working in both modes.
    """

    def __init__(
        self,
        *,
        capacity: int,
        media_slots: int,
        media_hints: tuple[re.Pattern[str], ...] = (),
    ) -> None:
        self._capacity = max(1, int(capacity))
        # A zero cap would make hint-classified bundles unprocessable forever.
        self._media_slots = max(1, int(media_slots))
        self._media_hints = media_hints
        self._pending: deque[PipelineArtifact] = deque()
        self._active = 0
        self._active_media = 0
        self._closed = False
        self._space_available = asyncio.Condition()
        self._admission = asyncio.Condition()

    async def put(self, artifact: PipelineArtifact) -> None:
        """Submit a downloaded artifact, blocking while the pending queue is full."""

        async with self._space_available:
            await self._space_available.wait_for(self._has_space)
            if self._closed:
                raise RuntimeError("extraction scheduler is closed")
            self._pending.append(artifact)
        async with self._admission:
            self._admission.notify_all()

    async def claim(self) -> PipelineArtifact | None:
        """Return the next admissible artifact, or None once closed and drained.

        The claimed artifact's ``cost_class`` is stamped with the advisory
        classification so downstream extraction can route it to the matching
        process pool (extraction-worker roadmap Phase 3).
        """

        async with self._admission:
            await self._admission.wait_for(self._has_admissible)
            if not self._pending:
                return None
            index = next(
                index
                for index, candidate in enumerate(self._pending)
                if self._admissible(candidate)
            )
            artifact = self._pending[index]
            del self._pending[index]
            artifact.cost_class = self._cost(artifact)
            self._active += 1
            if artifact.cost_class == MEDIA_COST_CLASS:
                self._active_media += 1
        async with self._space_available:
            self._space_available.notify_all()
        return artifact

    async def release(self, artifact: PipelineArtifact) -> None:
        """Free the extraction slot held by a claimed artifact."""

        self._active -= 1
        if artifact.cost_class == MEDIA_COST_CLASS:
            self._active_media -= 1
        async with self._admission:
            self._admission.notify_all()

    async def wait_idle(self) -> None:
        """Resolve once every submitted artifact has been claimed and released."""

        async with self._admission:
            await self._admission.wait_for(self._is_idle)

    async def close(self) -> None:
        """Stop accepting artifacts; pending ones remain claimable before drain."""

        async with self._admission:
            self._closed = True
            self._admission.notify_all()

    def qsize(self) -> int:
        """Number of pending artifacts, mirroring ``asyncio.Queue.qsize``."""

        return len(self._pending)

    def active_count(self) -> int:
        """Number of artifacts currently claimed by extraction workers."""

        return self._active

    def active_media_count(self) -> int:
        """Number of media-classified artifacts currently claimed."""

        return self._active_media

    async def drain_for_cleanup(self) -> list[PipelineArtifact]:
        """Pop pending artifacts for cancellation-path cleanup."""

        async with self._admission:
            artifacts = list(self._pending)
            self._pending.clear()
            async with self._space_available:
                self._space_available.notify_all()
        return artifacts

    def _cost(self, artifact: PipelineArtifact) -> str:
        return classify_bundle_cost(artifact.bundle, self._media_hints)

    def _admissible(self, artifact: PipelineArtifact) -> bool:
        return self._cost(artifact) == LIGHT_COST_CLASS or self._active_media < self._media_slots

    def _has_admissible(self) -> bool:
        if not self._pending:
            return self._closed
        return any(self._admissible(artifact) for artifact in self._pending)

    def _has_space(self) -> bool:
        return self._closed or len(self._pending) < self._capacity

    def _is_idle(self) -> bool:
        return not self._pending and self._active == 0
