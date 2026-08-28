"""Argparse entry point: dynamic config loading and event-loop startup."""

import asyncio
import logging
from typing import cast

from updater.cli import configuration
from updater.cli.configuration import validate_config
from updater.cli.logging_setup import setup_logging_queue
from updater.cli.runner import main
from updater.model import ConfigLike
from updater.runtime import shutdown_process_pools

logger = logging.getLogger("asset_updater")


def cli():
    # Accept command line arguments
    import argparse

    parser = argparse.ArgumentParser(description="Start the asset updater with given config.")
    parser.add_argument(
        "--mode",
        choices=("assets", "live2d", "charts"),
        default="assets",
        help="Processing scope (default: assets).",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        help="Path to the config python file.",
        required=True,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only output warnings and errors.",
    )
    parser.add_argument(
        "--update-asset-bundle-info-only",
        action="store_true",
        help=(
            "Fetch and update asset_bundle_info.json only; do not generate dl_list.json "
            "and do not start download tasks."
        ),
    )
    parser.add_argument(
        "--force-full-download",
        action="store_true",
        help=(
            "Ignore cached json metadata and cached dl_list.json, rebuild a full "
            "dl_list.json from current metadata, then download/process all matched bundles."
        ),
    )
    args = parser.parse_args()

    # Load the config python file as dynamic module
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("config", args.config)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load config module from {args.config}")

    loaded_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded_config)
    sys.modules["config"] = loaded_config
    configuration.config = cast(ConfigLike, loaded_config)
    validate_config(configuration.config, mode=args.mode)

    # Set the logging level
    log_level = logging.INFO
    if args.quiet:
        log_level = logging.WARNING
    elif args.verbose:
        log_level = logging.DEBUG

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    setup_logging_queue()

    logger.debug(
        "CLI options | config=%s | log_level=%s | mode=%s | force_full_download=%s",
        args.config,
        logging.getLevelName(log_level),
        "metadata-only" if args.update_asset_bundle_info_only else "full-pipeline",
        args.force_full_download,
    )

    # Run the main function
    try:
        asyncio.run(
            main(
                update_asset_bundle_info_only=args.update_asset_bundle_info_only,
                force_full_download=args.force_full_download,
                mode=args.mode,
            )
        )
    finally:
        # Reap pooled extraction (Live2D/bundle), audio, and video worker
        # processes even when the pipeline fails or is cancelled so the CLI
        # exits cleanly. Cleanup errors must never mask the pipeline result.
        try:
            shutdown_process_pools()
        except Exception:
            logger.exception("Failed to shut down process pools")
