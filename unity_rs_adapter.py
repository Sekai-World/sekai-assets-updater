"""Small application-facing adapter for the :mod:`unity_rs` binding.

The rest of the extractor should depend on this module rather than on the
native binding's object model.  The adapter deliberately exposes only the
records and operations used by this application; it is not intended to be a
second UnityPy implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import orjson
import unity_rs
from PIL import Image

# Unity's stable built-in class IDs.  Custom MonoBehaviours all use 114 and
# are distinguished by their MonoScript class name in their TypeTree.
CLASS_ID_NAMES = {
    1: "GameObject",
    4: "Transform",
    21: "Material",
    28: "Texture2D",
    43: "Mesh",
    48: "Shader",
    49: "TextAsset",
    74: "AnimationClip",
    83: "AudioClip",
    89: "Cubemap",
    91: "AnimatorController",
    95: "AnimatorOverrideController",
    114: "MonoBehaviour",
    115: "MonoScript",
    142: "AssetBundle",
    152: "MovieTexture",
    187: "Texture2DArray",
    213: "Sprite",
    224: "RectTransform",
    329: "VideoClip",
    687078895: "SpriteAtlas",
}


class UnityRsAdapterError(RuntimeError):
    """Base class for errors raised at the application/backend boundary."""


class UnityRsLoadError(UnityRsAdapterError):
    """A bundle could not be loaded by unity-rs."""


class UnsupportedUnityObjectError(UnityRsAdapterError):
    """The application requested an object shape not covered by the adapter."""


class MissingContainerError(UnityRsAdapterError):
    """An object has no resolved container path."""


class UnsupportedReferenceError(UnityRsAdapterError):
    """A reference cannot be resolved from the loaded collection."""


@dataclass(frozen=True, slots=True)
class UnityObjectEntry:
    """Stable identity and metadata for one serialized Unity object."""

    file_index: int
    object_index: int
    path_id: int
    class_id: int
    name: str | None
    container: str | None
    source_path: str


@dataclass(frozen=True, slots=True)
class AudioPayload:
    """One bounded payload returned for a Unity ``AudioClip``."""

    name: str
    extension: str
    payload_kind: str
    data: bytes


class _TypeInfo:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class _AttrDict(dict[str, Any]):
    """Dict preserving UnityPy-style attribute access for legacy algorithms."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _PPtr(_AttrDict):
    """A dict-shaped PPtr with bounded resolution in the current environment."""

    def __init__(
        self,
        file_id: int,
        path_id: int,
        resolver: Callable[[int, int], "UnityRsObject | None"],
    ) -> None:
        super().__init__(m_FileID=file_id, m_PathID=path_id)
        self._file_id = file_id
        self._path_id = path_id
        self._resolver = resolver

    def deref(self) -> "UnityRsObject | None":
        return self._resolver(self._file_id, self._path_id)


def _convert_value(
    value: Any,
    resolver: Callable[[int, int], "UnityRsObject | None"],
) -> Any:
    if isinstance(value, dict):
        if set(value) == {"m_FileID", "m_PathID"}:
            file_id = value["m_FileID"]
            path_id = value["m_PathID"]
            if isinstance(file_id, int) and isinstance(path_id, int):
                return _PPtr(file_id, path_id, resolver)
        result = _AttrDict()
        for key, child in value.items():
            result[key] = _convert_value(child, resolver)
        return result
    if isinstance(value, list):
        return [_convert_value(child, resolver) for child in value]
    return value


def _rgba_image(value: Any) -> Image.Image:
    width = getattr(value, "width", None)
    height = getattr(value, "height", None)
    pixels = getattr(value, "rgba", None)
    if not isinstance(width, int) or not isinstance(height, int) or not isinstance(pixels, bytes):
        raise UnsupportedUnityObjectError(
            "unity-rs image reader did not return width, height and RGBA bytes"
        )
    expected = width * height * 4
    if len(pixels) != expected:
        raise UnsupportedUnityObjectError(
            f"unity-rs image returned {len(pixels)} bytes, expected {expected}"
        )
    return Image.frombytes("RGBA", (width, height), pixels)


@dataclass(slots=True)
class RenderedImage:
    """One decoded RGBA texture that keeps its native handle for Rust encoding.

    The native ``unity_rs.RgbaImage`` encoders (notably PNG with the ``fast``
    compression profile) are far faster than round-tripping through PIL, so the
    extraction pipeline keeps this wrapper until the moment a specific output
    format is written.  ``to_pil`` converts lazily for formats that stay on the
    PIL side (lossy WebP).
    """

    native: Any
    width: int
    height: int
    _pil: Image.Image | None = field(default=None, repr=False)

    def encode_png(self, compression: str | int = "fast") -> bytes | None:
        """Encode to PNG in Rust; ``None`` when the native encoder is missing."""
        encode = getattr(self.native, "encode", None)
        if encode is None:
            return None
        return encode("png", compression=compression)

    def to_pil(self) -> Image.Image:
        if self._pil is None:
            self._pil = _rgba_image(self.native)
        return self._pil


def _rendered_image(value: Any) -> RenderedImage:
    width = getattr(value, "width", None)
    height = getattr(value, "height", None)
    if not isinstance(width, int) or not isinstance(height, int):
        raise UnsupportedUnityObjectError("unity-rs image reader did not return width and height")
    # Pixel-buffer validation is deferred: pulling ``value.rgba`` here would
    # copy the whole frame across the FFI boundary even when the image is
    # encoded natively and the bytes are never needed on the Python side.
    # ``to_pil`` still validates through ``_rgba_image``.
    return RenderedImage(native=value, width=width, height=height)


class UnityRsEnvironment:
    """Loaded bundle collection with the narrow interface used by extraction."""

    def __init__(self, studio: unity_rs.UnityRs) -> None:
        self._studio = studio
        native_objects = sorted(
            studio.objects(), key=lambda value: (value.file_index, value.object_index)
        )
        self.objects = [UnityRsObject(self, value) for value in native_objects]
        self._by_identity = {(obj.file_index, obj.path_id): obj for obj in self.objects}
        self.container: dict[str, UnityRsObject] = {}
        self._container_paths: dict[tuple[int, int], str] = {}

        read_asset_bundle = getattr(studio, "read_asset_bundle", None)
        if read_asset_bundle is not None:
            for obj in self.objects:
                if obj.class_id != 142:
                    continue
                for item in read_asset_bundle(obj.file_index, obj.path_id).container:
                    if len(item) != 4:
                        continue
                    container_path, _preload_index, _preload_size, identity = item
                    if not isinstance(container_path, str) or not isinstance(identity, tuple):
                        continue
                    target = self._by_identity.get(identity)
                    if target is not None:
                        self.container[container_path] = target
                        self._container_paths[identity] = container_path

        # Some serialized files do not carry an AssetBundle container table.
        # Preserve the native ObjectInfo hint as a fallback for those inputs.
        if not self.container:
            for obj in self.objects:
                if obj.container is not None:
                    self.container.setdefault(obj.container, obj)

    @property
    def studio(self) -> unity_rs.UnityRs:
        return self._studio

    def resolve_reference(
        self, file_id: int, path_id: int, source_file_index: int
    ) -> UnityRsObject | None:
        if file_id != 0:
            # The binding exposes cross-file identity in ObjectInfo, but does
            # not expose each serialized file's external table.  Never guess
            # the target file for a non-zero file ID. Callers must opt into a
            # collection-level resolver before dereferencing such a pointer.
            raise UnsupportedReferenceError(
                f"cannot resolve non-zero PPtr file ID {file_id} from file {source_file_index}"
            )
        return self._by_identity.get((source_file_index, path_id))

    def object_by_identity(self, file_index: int, path_id: int) -> UnityRsObject | None:
        return self._by_identity.get((file_index, path_id))


class UnityRsObject:
    """Application wrapper around one native ``ObjectInfo``."""

    __slots__ = ("_environment", "_info", "_read_cache")

    def __init__(self, environment: UnityRsEnvironment, info: Any) -> None:
        self._environment = environment
        self._info = info
        self._read_cache: Any = _UNREAD

    @property
    def file_index(self) -> int:
        return self._info.file_index

    @property
    def object_index(self) -> int:
        return self._info.object_index

    @property
    def path_id(self) -> int:
        return self._info.path_id

    @property
    def class_id(self) -> int:
        return self._info.class_id

    @property
    def name(self) -> str | None:
        return self._info.name

    @property
    def container(self) -> str | None:
        return self._environment._container_paths.get(
            (self.file_index, self.path_id), self._info.container
        )

    @property
    def source_path(self) -> str:
        return self._info.source_path

    @property
    def type(self) -> _TypeInfo:
        return _TypeInfo(CLASS_ID_NAMES.get(self.class_id, str(self.class_id)))

    @property
    def serialized_type(self) -> Any:
        # The old caller uses this only as a capability probe before asking
        # read_typetree().  unity-rs performs the actual validation there.
        return SimpleNamespace(node=True)

    def entry(self) -> UnityObjectEntry:
        return UnityObjectEntry(
            file_index=self.file_index,
            object_index=self.object_index,
            path_id=self.path_id,
            class_id=self.class_id,
            name=self.name,
            container=self.container,
            source_path=self.source_path,
        )

    def read_typetree(self) -> dict[str, Any]:
        raw = self._environment.studio.read_type_tree_json(self.file_index, self.path_id)
        tree = orjson.loads(raw)
        if not isinstance(tree, dict):
            raise UnsupportedUnityObjectError(f"TypeTree for {self.path_id} is not an object")
        return tree

    def read(self) -> Any:
        if self._read_cache is not _UNREAD:
            return self._read_cache

        studio = self._environment.studio
        if self.class_id == 49:
            value = _TextAsset(
                name=self.name or "",
                script=studio.read_text(self.file_index, self.path_id),
                path_id=self.path_id,
            )
        elif self.class_id == 28:
            value = _TextureAsset(
                _rendered_image(studio.read_texture(self.file_index, self.path_id))
            )
        elif self.class_id == 213:
            value = _SpriteAsset(_rendered_image(studio.read_sprite(self.file_index, self.path_id)))
        elif self.class_id == 187:
            native_images = studio.read_texture_array(self.file_index, self.path_id)
            value = _TextureArrayAsset([_rendered_image(image) for image in native_images])
        elif self.class_id == 83:
            native = studio.read_audio_clip(self.file_index, self.path_id)
            value = _AudioClipAsset(
                name=native.name,
                extension=native.extension,
                payload_kind=native.payload_kind,
                data=native.data,
            )
        else:
            value = _convert_value(
                self.read_typetree(),
                lambda file_id, path_id: self._environment.resolve_reference(
                    file_id, path_id, self.file_index
                ),
            )
        self._read_cache = value
        return value

    def read_image(self) -> RenderedImage:
        if self.class_id == 28:
            return _rendered_image(
                self._environment.studio.read_texture(self.file_index, self.path_id)
            )
        if self.class_id == 213:
            return _rendered_image(
                self._environment.studio.read_sprite(self.file_index, self.path_id)
            )
        raise UnsupportedUnityObjectError(
            f"object class {self.type.name} does not provide a single image"
        )

    def read_texture_array_images(self) -> list[RenderedImage]:
        if self.class_id != 187:
            raise UnsupportedUnityObjectError(
                f"object class {self.type.name} is not a Texture2DArray"
            )
        return [
            _rendered_image(image)
            for image in self._environment.studio.read_texture_array(self.file_index, self.path_id)
        ]

    def read_audio_payload(self) -> AudioPayload:
        if self.class_id != 83:
            raise UnsupportedUnityObjectError(f"object class {self.type.name} is not an AudioClip")
        value = self._environment.studio.read_audio_clip(self.file_index, self.path_id)
        return AudioPayload(value.name, value.extension, value.payload_kind, value.data)


class _Unread:
    pass


_UNREAD = _Unread()


@dataclass(slots=True)
class _TextAsset:
    name: str
    script: bytes
    path_id: int

    @property
    def m_Name(self) -> str:
        return self.name

    @property
    def m_Script(self) -> str:
        return self.script.decode("utf-8", "surrogateescape")


@dataclass(slots=True)
class _TextureAsset:
    image: RenderedImage


@dataclass(slots=True)
class _SpriteAsset:
    image: RenderedImage


@dataclass(slots=True)
class _TextureArrayAsset:
    images: list[RenderedImage]


@dataclass(slots=True)
class _AudioClipAsset:
    name: str
    extension: str
    payload_kind: str
    data: bytes

    @property
    def samples(self) -> dict[str, bytes]:
        extension = self.extension
        if extension and not extension.startswith("."):
            extension = f".{extension}"
        filename = self.name
        if extension and not filename.lower().endswith(extension.lower()):
            filename += extension
        return {filename: self.data}


def load_bundle(
    path_or_bytes: str | Path | bytes,
    unity_version: str | None,
) -> UnityRsEnvironment:
    """Load one Unity bundle with an explicit project Unity version."""

    if not isinstance(unity_version, str) or not unity_version.strip():
        raise UnityRsLoadError("unity-rs bundle loading requires UNITY_VERSION")
    try:
        if isinstance(path_or_bytes, bytes):
            studio = unity_rs.UnityRs.from_bytes(path_or_bytes, unity_version=unity_version)
        else:
            studio = unity_rs.UnityRs(path_or_bytes, unity_version=unity_version)
        return UnityRsEnvironment(studio)
    except Exception as exc:
        raise UnityRsLoadError(f"failed to load Unity bundle {path_or_bytes!s}: {exc}") from exc


def iter_container_items(
    environment: UnityRsEnvironment,
) -> Iterator[tuple[str, UnityRsObject]]:
    """Yield container entries in stable object-table order."""

    yield from environment.container.items()


def read_type_tree(entry: UnityRsObject) -> dict[str, Any]:
    return entry.read_typetree()


def read_text_bytes(entry: UnityRsObject) -> bytes:
    if entry.class_id != 49:
        raise UnsupportedUnityObjectError(f"object class {entry.type.name} is not a TextAsset")
    return entry._environment.studio.read_text(entry.file_index, entry.path_id)


def read_image(entry: UnityRsObject) -> RenderedImage:
    return entry.read_image()


def read_texture_array_images(entry: UnityRsObject) -> list[RenderedImage]:
    return entry.read_texture_array_images()


def read_audio_clip(entry: UnityRsObject) -> AudioPayload:
    return entry.read_audio_payload()


__all__ = [
    "AudioPayload",
    "CLASS_ID_NAMES",
    "MissingContainerError",
    "RenderedImage",
    "UnsupportedReferenceError",
    "UnsupportedUnityObjectError",
    "UnityObjectEntry",
    "UnityRsAdapterError",
    "UnityRsEnvironment",
    "UnityRsLoadError",
    "UnityRsObject",
    "iter_container_items",
    "load_bundle",
    "read_audio_clip",
    "read_image",
    "read_text_bytes",
    "read_texture_array_images",
    "read_type_tree",
]
