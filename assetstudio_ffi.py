import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

MAX_FRAME_SIZE = 256 * 1024 * 1024
PAYLOAD_BUNDLE_MAGIC = b"HARUKI_ASSET_PAYLOAD_BUNDLE_V1"
PAYLOAD_BUNDLE_V2_MAGIC = 0x4250_4148
PAYLOAD_BUNDLE_V2_VERSION = 2
PAYLOAD_BUNDLE_V2_HEADER_LEN = 20
RGBA_IR_MAGIC = b"HARUKI_RGBAIR_V1"
RGBA_IR_HEADER_LEN = 32


@dataclass(frozen=True)
class AssetStudioRead:
    asset: dict[str, Any]
    response: dict[str, Any]
    payload: bytes


class AssetStudioFfiError(RuntimeError):
    pass


class AssetStudioFfiWorker:
    def __init__(self, worker_path: str, library_path: str):
        self._next_id = 1
        self._process = subprocess.Popen(
            [worker_path, "--ffi-library", library_path, "--server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def close(self) -> None:
        if self._process.stdin:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

    def call(self, operation: str, request: dict[str, Any]) -> tuple[int, dict[str, Any], bytes]:
        request_id = self._next_id
        self._next_id += 1
        self._write_frame(
            json.dumps(
                {"id": request_id, "request": {"operation": operation, "request": request}},
                ensure_ascii=False,
            ).encode()
        )
        response = json.loads(self._read_frame())
        if response.get("id") != request_id:
            raise AssetStudioFfiError(f"unexpected assetstudio response id: {response.get('id')}")
        if response.get("error"):
            raise AssetStudioFfiError(response["error"])

        payload = b""
        payload_len = response.get("payload_len") or 0
        payload_file = response.get("payload_file")
        if payload_file:
            payload_path = Path(payload_file)
            payload = payload_path.read_bytes()
            payload_path.unlink(missing_ok=True)
        elif payload_len:
            payload = self._read_frame()

        return response.get("status") or 0, response.get("response") or {}, payload

    def _write_frame(self, payload: bytes) -> None:
        if self._process.stdin is None:
            raise AssetStudioFfiError("assetstudio worker stdin is closed")
        self._process.stdin.write(len(payload).to_bytes(8, "little"))
        self._process.stdin.write(payload)
        self._process.stdin.flush()

    def _read_frame(self) -> bytes:
        if self._process.stdout is None:
            raise AssetStudioFfiError("assetstudio worker stdout is closed")
        len_bytes = self._process.stdout.read(8)
        if len(len_bytes) != 8:
            stderr = self._read_stderr()
            raise AssetStudioFfiError(f"assetstudio worker stopped before response: {stderr}")
        length = int.from_bytes(len_bytes, "little")
        if length > MAX_FRAME_SIZE:
            raise AssetStudioFfiError(f"assetstudio worker frame too large: {length} bytes")
        payload = self._process.stdout.read(length)
        if len(payload) != length:
            raise AssetStudioFfiError("assetstudio worker returned truncated frame")
        return payload

    def _read_stderr(self) -> str:
        if self._process.stderr is None:
            return ""
        if self._process.poll() is None:
            return ""
        return self._process.stderr.read().decode(errors="replace").strip()


def configured_library_path(config) -> str:
    value = _config_get(config, "ASSET_STUDIO_FFI_LIBRARY_PATH") or os.getenv(
        "HARUKI_ASSET_STUDIO_FFI_LIBRARY_PATH"
    )
    if not value:
        value = _default_library_path()
    if not value:
        raise AssetStudioFfiError(
            "ASSET_STUDIO_FFI_LIBRARY_PATH or HARUKI_ASSET_STUDIO_FFI_LIBRARY_PATH is required"
        )
    return str(value)


def configured_worker_path(config) -> str:
    return str(
        _config_get(config, "ASSET_STUDIO_FFI_WORKER_PATH")
        or os.getenv("HARUKI_ASSET_STUDIO_FFI_WORKER_PATH")
        or _default_worker_path()
        or "assetstudio_ffi_worker"
    )


def configured_read_batch_size(config) -> int:
    try:
        return max(1, int(_config_get(config, "ASSET_STUDIO_FFI_READ_BATCH_SIZE", 64)))
    except (TypeError, ValueError):
        return 64


def _config_get(config, key: str, default=None):
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _default_worker_path() -> str | None:
    path = _project_root() / ".tools" / "assetstudio-ffi" / "bin" / "assetstudio_ffi_worker"
    return str(path) if path.exists() else None


def _default_library_path() -> str | None:
    base = _project_root() / ".tools" / "assetstudio-ffi"
    matches = sorted(base.glob("assetstudio-ffi-*/HarukiAssetStudioFFI.dylib"))
    return str(matches[-1]) if matches else None


def export_assetstudio_objects(
    bundle_path: Path,
    unity_version: str | None,
    config,
) -> list[AssetStudioRead]:
    worker = AssetStudioFfiWorker(configured_worker_path(config), configured_library_path(config))
    try:
        open_response = _expect_response(
            worker.call(
                "context_open",
                {
                    "input_path": str(bundle_path),
                    "asset_types": [
                        "tex2d",
                        "tex2dArray",
                        "sprite",
                        "textAsset",
                        "monoBehaviour",
                        "MonoScript",
                        "audio",
                        "video",
                        "movieTexture",
                    ],
                    "unity_version": unity_version,
                    "filter_exclude_mode": False,
                    "filter_with_regex": False,
                    "filter_by_name": None,
                    "filter_by_container": None,
                    "filter_by_path_ids": [],
                    "load_all_assets": True,
                    "include_assets": False,
                },
            ),
            "context_open",
        )
        context_id = open_response["context_id"]
        try:
            assets = _list_assets(worker, context_id, open_response)
            return _read_assets(worker, context_id, assets, configured_read_batch_size(config))
        finally:
            _expect_response(
                worker.call("context_close", {"context_id": context_id}),
                "context_close",
                require_success=False,
            )
    finally:
        worker.close()


def _expect_response(
    output: tuple[int, dict[str, Any], bytes],
    operation: str,
    require_success: bool = True,
) -> dict[str, Any]:
    status, response_envelope, _ = output
    if response_envelope.get("operation") != operation:
        raise AssetStudioFfiError(
            f"unexpected assetstudio response operation: {response_envelope.get('operation')}"
        )
    response = response_envelope.get("response") or {}
    if require_success and not (status == 0 and response.get("success")):
        raise AssetStudioFfiError(response.get("error") or f"{operation} failed")
    return response


def _list_assets(
    worker: AssetStudioFfiWorker,
    context_id: int,
    open_response: dict[str, Any],
) -> list[dict[str, Any]]:
    assets = list(open_response.get("assets") or [])
    if assets and not open_response.get("has_more_assets"):
        return assets

    assets = []
    offset = 0
    while True:
        response = _expect_response(
            worker.call(
                "context_list_objects",
                {"context_id": context_id, "offset": offset, "limit": 4096},
            ),
            "context_list_objects",
        )
        assets.extend(response.get("assets") or [])
        next_offset = response.get("next_offset")
        if next_offset is None:
            return assets
        offset = next_offset


def _read_assets(
    worker: AssetStudioFfiWorker,
    context_id: int,
    assets: list[dict[str, Any]],
    batch_size: int,
) -> list[AssetStudioRead]:
    results: list[AssetStudioRead] = []
    readable = [asset for asset in assets if _read_kind_for_asset(asset) is not None]
    for i in range(0, len(readable), batch_size):
        chunk = readable[i : i + batch_size]
        status, response_envelope, payload = worker.call(
            "context_read_objects",
            {
                "context_id": context_id,
                "objects": [
                    {
                        "path_id": asset["path_id"],
                        "kind": _read_kind_for_asset(asset),
                        "image_format": "raw_rgba",
                    }
                    for asset in chunk
                ],
            },
        )
        if response_envelope.get("operation") != "context_read_objects":
            raise AssetStudioFfiError("unexpected context_read_objects response")
        response = response_envelope.get("response") or {}
        reads = response.get("reads") or []
        if status != 0 or len(reads) != len(chunk):
            raise AssetStudioFfiError(response.get("error") or "context_read_objects failed")
        payloads = parse_payload_bundle(payload) if payload else {}
        for asset, read_response in zip(chunk, reads):
            if not read_response.get("success"):
                continue
            results.append(
                AssetStudioRead(
                    asset=asset,
                    response=read_response,
                    payload=payloads.get(str(asset["path_id"]), b""),
                )
            )
    return results


def _read_kind_for_asset(asset: dict[str, Any]) -> str | None:
    asset_type = (
        asset.get("type") or asset.get("asset_type") or ""
    ).replace("_", "").replace("-", "").lower()
    if asset_type in {"texture2d", "texture2darray", "texture2darrayimage", "sprite"}:
        return "image"
    if asset_type == "textasset":
        return "text_bytes"
    if asset_type in {"monobehaviour", "monobehavior", "monoscript"}:
        return "typetree_json"
    if asset_type == "audioclip":
        return "audio"
    if asset_type in {"videoclip", "movietexture"}:
        return "video"
    return None


def parse_payload_bundle(payload: bytes) -> dict[str, bytes]:
    cursor = 0
    if len(payload) >= 4 and int.from_bytes(payload[:4], "little") == PAYLOAD_BUNDLE_V2_MAGIC:
        cursor = 4
        version, cursor = _read_u16(payload, cursor)
        if version != PAYLOAD_BUNDLE_V2_VERSION:
            raise AssetStudioFfiError(f"unsupported payload bundle version: {version}")
        header_len, cursor = _read_u16(payload, cursor)
        if header_len < PAYLOAD_BUNDLE_V2_HEADER_LEN or header_len > len(payload):
            raise AssetStudioFfiError(f"invalid payload bundle header length: {header_len}")
        count, cursor = _read_u32(payload, cursor)
        _, cursor = _read_u64(payload, cursor)
        return _parse_payload_bundle_interleaved(payload, header_len, count)

    if not payload.startswith(PAYLOAD_BUNDLE_MAGIC):
        raise AssetStudioFfiError("invalid payload bundle magic")
    cursor = len(PAYLOAD_BUNDLE_MAGIC)
    count, cursor = _read_u32(payload, cursor)
    try:
        return _parse_payload_bundle_grouped(payload, cursor, count)
    except AssetStudioFfiError:
        return _parse_payload_bundle_interleaved(payload, cursor, count)


def _parse_payload_bundle_interleaved(payload: bytes, cursor: int, count: int) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    for _ in range(count):
        name_len, cursor = _read_u32(payload, cursor)
        data_len, cursor = _read_u64(payload, cursor)
        name = payload[cursor : cursor + name_len].decode()
        cursor += name_len
        entries[name] = payload[cursor : cursor + data_len]
        cursor += data_len
    return entries


def _parse_payload_bundle_grouped(payload: bytes, cursor: int, count: int) -> dict[str, bytes]:
    headers: list[tuple[str, int]] = []
    for _ in range(count):
        name_len, cursor = _read_u32(payload, cursor)
        data_len, cursor = _read_u64(payload, cursor)
        name = payload[cursor : cursor + name_len].decode()
        cursor += name_len
        headers.append((name, data_len))

    entries: dict[str, bytes] = {}
    for name, data_len in headers:
        entries[name] = payload[cursor : cursor + data_len]
        cursor += data_len
    return entries


def _read_u16(payload: bytes, cursor: int) -> tuple[int, int]:
    return int.from_bytes(payload[cursor : cursor + 2], "little"), cursor + 2


def _read_u32(payload: bytes, cursor: int) -> tuple[int, int]:
    return int.from_bytes(payload[cursor : cursor + 4], "little"), cursor + 4


def _read_u64(payload: bytes, cursor: int) -> tuple[int, int]:
    return int.from_bytes(payload[cursor : cursor + 8], "little"), cursor + 8


def image_from_payload(payload: bytes) -> Image.Image:
    if not payload.startswith(RGBA_IR_MAGIC):
        from io import BytesIO

        return Image.open(BytesIO(payload))
    if len(payload) < RGBA_IR_HEADER_LEN:
        raise AssetStudioFfiError("raw RGBA image payload is too short")
    width = int.from_bytes(payload[16:20], "little")
    height = int.from_bytes(payload[20:24], "little")
    stride = int.from_bytes(payload[24:28], "little")
    pixel_format = int.from_bytes(payload[28:32], "little")
    if pixel_format != 1:
        raise AssetStudioFfiError(f"unsupported raw RGBA pixel format: {pixel_format}")
    row_bytes = width * 4
    pixels = payload[RGBA_IR_HEADER_LEN : RGBA_IR_HEADER_LEN + stride * height]
    if stride != row_bytes:
        pixels = b"".join(pixels[y * stride : y * stride + row_bytes] for y in range(height))
    return Image.frombytes("RGBA", (width, height), pixels)


def safe_payload_bundle_path(name: str) -> Path:
    path = Path()
    for part in Path(name).parts:
        if part not in {"", ".", ".."}:
            path /= part
    return path if path.parts else Path("payload.bin")
