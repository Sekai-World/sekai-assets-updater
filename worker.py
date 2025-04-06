import asyncio
import logging
import tempfile
from typing import Dict, Tuple, Union

from anyio import Path

from bundle import download_deobfuscate_bundle, extract_asset_bundle
from helpers import refresh_cookie, upload_to_storage

logger = logging.getLogger("asset_updater")


async def worker(
    name: str,
    queue: asyncio.Queue[Tuple[str, Dict[str, str]]],
    config,
    headers: Dict[str, str],
    cookie: str = None,
):
    while not queue.empty():
        url, bundle = await queue.get()

        logger.debug("worker %s processing %s", name, bundle.get("bundleName", url))

        headers, cookie = await refresh_cookie(config, headers, cookie)

        bundle_save_path: Union[Path, None] = None
        tmp_bundle_save_file = None
        if isinstance(config.ASSET_LOCAL_BUNDLE_CACHE_DIR, Path):
            # Save the bundle to the local directory
            bundle_save_path: Path = (
                config.ASSET_LOCAL_BUNDLE_CACHE_DIR / bundle["bundleName"]
            )
            # Create the directory if it doesn't exist
            await bundle_save_path.parent.mkdir(parents=True, exist_ok=True)

            # Download the bundle
            await download_deobfuscate_bundle(
                url,
                bundle_save_path,
                headers=headers,
            )
        else:
            # Save the bundle to the temp directory
            tmp_bundle_save_file = tempfile.NamedTemporaryFile()
            bundle_save_path = Path(tmp_bundle_save_file.name)

            # Download the bundle
            await download_deobfuscate_bundle(
                url,
                bundle_save_path,
                headers=headers,
            )

        # Get the extracted save path
        extracted_save_path: Union[Path, None] = None
        tmp_extracted_save_dir = None
        if isinstance(config.ASSET_LOCAL_EXTRACTED_DIR, Path):
            # Save the extracted assets to the local directory
            extracted_save_path = config.ASSET_LOCAL_EXTRACTED_DIR
            # Create the directory if it doesn't exist
            await extracted_save_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # Save the extracted assets to the temp directory
            tmp_extracted_save_dir = tempfile.TemporaryDirectory(delete=False)
            extracted_save_path = Path(tmp_extracted_save_dir.name)

        try:
            # Extract the bundle
            exported_list = await extract_asset_bundle(
                bundle_save_path,
                bundle,
                extracted_save_path,
                unity_version=config.UNITY_VERSION,
                config=config,
            )
            logger.debug("Extracted %s to %s", bundle["bundleName"], exported_list)

            # Upload the bundle to remote storage
            if config.ASSET_REMOTE_STORAGE:
                for storage in config.ASSET_REMOTE_STORAGE:
                    if storage["type"] == "normal":
                        # Upload the bundle to normal storage
                        await upload_to_storage(
                            exported_list,
                            extracted_save_path,
                            storage["base"],
                            storage["cmd"],
                            max_concurrent_uploads=config.MAX_CONCURRENCY_UPLOADS,
                        )
        finally:
            # Clean up the temporary bundle file
            if tmp_bundle_save_file:
                tmp_bundle_save_file.close()
            # Clean up the temporary extracted directory
            if tmp_extracted_save_dir:
                tmp_extracted_save_dir.cleanup()

        queue.task_done()
