"""In-process OpenDAL streaming upload backend."""

import asyncio
import logging
import os
from typing import List, Mapping

from anyio import Path, open_file

from updater.storage.remote import derive_remote_key, validate_upload_sources

logger = logging.getLogger("asset_updater")

_OPENDAL_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024


async def upload_to_storage_opendal(
    exported_list: List[Path],
    extracted_save_path: Path,
    storage: Mapping,
    max_concurrent_uploads: int = 5,
    config=None,
):
    """Upload extracted assets through an in-process OpenDAL operator.

    ``storage`` is one ``ASSET_REMOTE_STORAGE`` entry with
    ``backend: "opendal"``: ``scheme`` names the OpenDAL service (e.g.
    ``"s3"``, ``"fs"``), ``options`` carries the service configuration
    (bucket, endpoint, credentials, root, ...), and the optional ``prefix``
    is prepended to every object key. Files stream in chunks, so large
    outputs never load fully into memory, and transient failures retry via
    OpenDAL's retry layer. No subprocess is involved.
    """
    import opendal

    scheme = storage.get("scheme")
    if not isinstance(scheme, str) or not scheme:
        raise ValueError("opendal storage requires a non-empty 'scheme'")
    options = storage.get("options") or {}
    if not isinstance(options, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in options.items()
    ):
        raise ValueError("opendal storage 'options' must map strings to strings")
    prefix = storage.get("prefix") or ""

    _, validated_sources = validate_upload_sources(exported_list, extracted_save_path)
    validated_uploads = [
        (validated_path, derive_remote_key(relative_key, prefix))
        for validated_path, relative_key in validated_sources
    ]
    if not validated_uploads:
        return

    operator = opendal.AsyncOperator(scheme, **dict(options)).layer(
        opendal.layers.RetryLayer(max_times=3)
    )
    semaphore = asyncio.Semaphore(max(1, int(max_concurrent_uploads)))

    async def upload_file(file_path: object, remote_key: str) -> None:
        async with semaphore:
            logger.debug("Uploading %s to opendal %s:%s", file_path, scheme, remote_key)
            writer = await operator.open(remote_key, "wb")
            try:
                async with await open_file(os.fspath(file_path), "rb") as source:
                    while chunk := await source.read(_OPENDAL_UPLOAD_CHUNK_BYTES):
                        await writer.write(chunk)
            finally:
                await writer.close()
            logger.info("Successfully uploaded %s to opendal %s:%s", file_path, scheme, remote_key)

    results = await asyncio.gather(
        *(upload_file(file_path, remote_key) for file_path, remote_key in validated_uploads),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
    errors = [result for result in results if isinstance(result, Exception)]
    if errors:
        raise RuntimeError(f"{len(errors)} upload(s) failed; first error: {errors[0]}") from errors[
            0
        ]
