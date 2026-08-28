import asyncio
import logging
import os
import tempfile
from typing import List, Mapping

from anyio import Path, open_file

from updater.external_process import (
    EXTERNAL_PROCESS_TERMINATE_GRACE,
    TERMINATE_TASK_ATTRIBUTE,
    terminate_process,
    wait_for_process,
)
from updater.external_process import (
    get_external_process_timeout as _get_external_process_timeout,
)
from updater.modes import (  # noqa: F401  (re-exported until helpers.py is dissolved)
    filter_bundles_for_mode,
    filter_download_items_for_mode,
    get_mode_bundle_prefixes,
)
from updater.sanitize import (
    sanitize_url,
)
from updater.security import derive_remote_key, validate_contained_file

logger = logging.getLogger("asset_updater")


async def _terminate_process(process) -> None:
    await terminate_process(process, EXTERNAL_PROCESS_TERMINATE_GRACE)


async def _wait_for_process(process, timeout: float) -> int:
    return await wait_for_process(
        process,
        timeout,
        _terminate_process,
        task_attribute=TERMINATE_TASK_ATTRIBUTE,
        logger=logger,
    )


def _derive_storage_remote_path(remote_base: str, relative_key: str) -> str:
    """Append one validated object key to an opaque configured storage target.

    ``remote_base`` is an rclone destination, not a local filesystem path.  It
    is therefore deliberately preserved byte-for-byte (apart from choosing the
    separator when it has no trailing slash), which supports named remotes,
    local absolute-path remotes, and on-the-fly remote syntax.  Only
    ``relative_key`` is parsed and validated; it must be a normalized POSIX
    path relative to the extraction root.
    """
    if not isinstance(remote_base, str):
        raise TypeError("remote_base must be a text storage target")
    if "\x00" in remote_base:
        raise ValueError("remote_base contains a NUL byte")

    remote_key = derive_remote_key(relative_key)
    separator = "" if remote_base.endswith("/") else "/"
    return f"{remote_base}{separator}{remote_key}"


def _validate_upload_sources(
    exported_list: List[Path],
    extracted_save_path: Path,
) -> tuple[str, list[tuple[object, str]]]:
    """Validate every source file is contained below the extraction root.

    Returns the absolute root and ``(validated_path, relative_key)`` pairs,
    where ``relative_key`` is the normalized POSIX path below the root.
    """
    root_path = os.path.abspath(os.fspath(extracted_save_path))
    validated_sources: list[tuple[object, str]] = []
    for file_path in exported_list:
        source_path = os.path.abspath(os.fspath(file_path))
        relative_path = os.path.relpath(source_path, root_path)
        relative_key = relative_path.replace(os.sep, "/")
        validated_path = validate_contained_file(root_path, relative_key)
        validated_sources.append((validated_path, relative_key))
    return root_path, validated_sources


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

    _, validated_sources = _validate_upload_sources(exported_list, extracted_save_path)
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


def _is_batchable_rclone_upload(upload_program: str, upload_args: List[str]) -> bool:
    """Whether this target can take one batched ``rclone copy`` per artifact.

    Only the documented ``["copy", "src", "dst", ...]`` template is batched:
    ``copy`` with ``--files-from-raw`` reproduces exactly the per-file
    destination keys (relative paths are preserved under the destination), so
    spawning one process replaces one process per exported file.  Any other
    verb (``copyto``, ``sync``, custom programs) keeps the per-file path.
    """
    program_name = os.path.basename(upload_program)
    return (
        program_name in ("rclone", "rclone.exe")
        and len(upload_args) >= 3
        and upload_args[0] == "copy"
        and "src" in upload_args
        and "dst" in upload_args
    )


async def _upload_batch_with_rclone(
    validated_uploads: list[tuple[object, str]],
    root_path: str,
    remote_base: str,
    upload_program: str,
    upload_args: List[str],
    max_concurrent_uploads: int,
    config,
) -> None:
    relative_keys = [relative_key for _, relative_key in validated_uploads]
    args = upload_args[:]
    args[args.index("src")] = root_path
    args[args.index("dst")] = remote_base

    list_descriptor, list_path = tempfile.mkstemp(prefix=".upload-batch-", suffix=".txt")
    try:
        with os.fdopen(list_descriptor, "w", encoding="utf-8") as list_file:
            list_file.write("\n".join(relative_keys) + "\n")
        args.extend(
            [
                "--files-from-raw",
                list_path,
                "--transfers",
                str(max(1, int(max_concurrent_uploads))),
            ]
        )
        # One process moves the whole artifact; scale the hang timeout with the
        # batch so large artifacts are not killed mid-transfer.
        batch_timeout = _get_external_process_timeout(config) * max(
            1, -(-len(relative_keys) // max(1, int(max_concurrent_uploads)))
        )
        logger.debug(
            "Uploading %d files from %s to %s in one batch",
            len(relative_keys),
            root_path,
            sanitize_url(remote_base),
        )
        upload_process = await asyncio.create_subprocess_exec(upload_program, *args)
        await _wait_for_process(upload_process, batch_timeout)
        if upload_process.returncode != 0:
            safe_remote_base = sanitize_url(remote_base)
            logger.error("Failed to batch-upload %s to %s", root_path, safe_remote_base)
            raise RuntimeError(
                f"1 upload(s) failed: batch upload of {len(relative_keys)} files "
                f"to {safe_remote_base} exited with {upload_process.returncode}"
            )
        logger.info(
            "Successfully uploaded %d files to %s",
            len(relative_keys),
            sanitize_url(remote_base),
        )
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


async def upload_to_storage(
    exported_list: List[Path],
    extracted_save_path: Path,
    remote_base: str,
    upload_program: str,
    upload_args: List[str],
    max_concurrent_uploads: int = 5,
    config=None,
):
    """Upload the extracted assets to remote storage with concurrency"""

    root_path, validated_sources = _validate_upload_sources(exported_list, extracted_save_path)
    validated_uploads: list[tuple[object, str]] = [
        (validated_path, _derive_storage_remote_path(remote_base, relative_key))
        for validated_path, relative_key in validated_sources
    ]
    validated_batch_keys: list[tuple[object, str]] = [
        (validated_path, derive_remote_key(relative_key))
        for validated_path, relative_key in validated_sources
    ]

    if validated_uploads and _is_batchable_rclone_upload(upload_program, upload_args):
        await _upload_batch_with_rclone(
            validated_batch_keys,
            root_path,
            remote_base,
            upload_program,
            upload_args,
            max_concurrent_uploads,
            config,
        )
        return

    semaphore = asyncio.Semaphore(max_concurrent_uploads)

    async def upload_file(file_path: object, remote_path: str):
        """Upload a single file to remote storage"""
        async with semaphore:
            # Construct the upload command
            program: str = upload_program
            args: list[str] = upload_args[:]
            args[args.index("src")] = str(file_path)
            args[args.index("dst")] = remote_path
            process_timeout = _get_external_process_timeout(config)
            logger.debug(
                "Uploading %s to %s",
                file_path,
                sanitize_url(remote_path),
            )

            # Execute the command
            upload_process = await asyncio.create_subprocess_exec(program, *args)
            await _wait_for_process(upload_process, process_timeout)
            if upload_process.returncode != 0:
                safe_remote_path = sanitize_url(remote_path)
                logger.error("Failed to upload %s to %s", file_path, safe_remote_path)
                raise RuntimeError(f"Failed to upload {file_path} to {safe_remote_path}")
            else:
                logger.info(
                    "Successfully uploaded %s to %s",
                    file_path,
                    sanitize_url(remote_path),
                )

    # Run uploads concurrently and fail the worker if any upload fails.
    results = await asyncio.gather(
        *(upload_file(file_path, remote_path) for file_path, remote_path in validated_uploads),
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


async def upload_directory(
    source_dir: Path,
    remote_path: Path,
    upload_program: str,
    upload_args: List[str],
    config=None,
) -> None:
    """Upload a complete specialized output directory in one storage operation."""
    source_path = os.path.abspath(os.fspath(source_dir))
    if not os.path.isdir(source_path):
        raise ValueError(f"Directory upload source does not exist: {source_path}")

    args = upload_args[:]
    args[args.index("src")] = source_path
    args[args.index("dst")] = str(remote_path)
    safe_remote_path = sanitize_url(str(remote_path))
    logger.debug("Uploading directory %s to %s", source_path, safe_remote_path)

    process = await asyncio.create_subprocess_exec(upload_program, *args)
    await _wait_for_process(process, _get_external_process_timeout(config))
    if process.returncode != 0:
        raise RuntimeError(f"Failed to upload directory {source_path} to {safe_remote_path}")
    logger.info("Successfully uploaded directory %s to %s", source_path, safe_remote_path)
