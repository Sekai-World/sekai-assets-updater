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
from typing import Any, Callable, Iterator, Sequence

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
    128: "Font",
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


class InvalidImageDimensions(UnsupportedUnityObjectError):
    """An image reports dimensions that cannot produce a valid output file."""


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
class CollectionFileIdentity:
    """Portable identity of one serialized file inside a loaded collection.

    ``bundle_name`` is the caller-chosen input name (the plan's bundle
    identity) and ``cab_name`` is the serialized file's own ``CAB-…`` name
    read from the binding.  Both survive reordering of the inputs, unlike
    the positional ``file_index``.
    """

    file_index: int
    bundle_name: str
    cab_name: str
    unity_version: str | None


class CollectionExternalReference:
    """Outcome of mapping one PPtr ``m_FileID`` through the real externals."""

    __slots__ = ("kind", "target_file_index", "dependency_name")

    def __init__(
        self,
        kind: str,
        target_file_index: int | None = None,
        dependency_name: str | None = None,
    ) -> None:
        self.kind = kind
        self.target_file_index = target_file_index
        self.dependency_name = dependency_name


SAME_FILE_REFERENCE = "same_file"
RESOLVED_EXTERNAL_REFERENCE = "resolved_external"
UNKNOWN_EXTERNAL_REFERENCE = "unknown_external"


class CollectionFileTable:
    """Actual file identity table of a loaded multi-file collection.

    PPtr ``m_FileID`` values index the *source file's* external list.  The
    AssetBundle record of each input exposes that list as ordered
    ``dependencies`` (bundle or CAB names), so a non-zero file ID maps to a
    loaded file by identity — never by position or filename guess.
    """

    def __init__(self, environment: UnityRsEnvironment) -> None:
        file_meta = self._native_file_metadata(environment)
        identities: list[CollectionFileIdentity] = []
        by_bundle_name: dict[str, int] = {}
        by_cab: dict[str, int] = {}
        externals: dict[int, tuple[str, ...]] = {}
        asset_bundles: dict[int, UnityRsObject] = {}
        for obj in environment.objects:
            if obj.class_id == 142 and obj.file_index not in asset_bundles:
                asset_bundles[obj.file_index] = obj
        for obj in environment.objects:
            if obj.file_index in externals:
                continue
            source = getattr(obj._info, "source_path", "") or ""
            cab = source.split("::", 1)[1] if "::" in source else source
            meta = file_meta.get(obj.file_index)
            bundle_name = (
                str(getattr(meta, "path", "")).split("::", 1)[0] if meta is not None else cab
            )
            version = getattr(meta, "unity_version", None) if meta is not None else None
            identity = CollectionFileIdentity(
                file_index=obj.file_index,
                bundle_name=bundle_name,
                cab_name=cab,
                unity_version=str(version) if version else None,
            )
            identities.append(identity)
            by_bundle_name.setdefault(bundle_name, identity.file_index)
            by_cab.setdefault(cab, identity.file_index)
            externals[obj.file_index] = self._externals_of(
                environment, asset_bundles.get(obj.file_index)
            )
        self.identities: tuple[CollectionFileIdentity, ...] = tuple(
            sorted(identities, key=lambda value: value.file_index)
        )
        self._by_bundle_name = by_bundle_name
        self._by_cab = by_cab
        self._externals = externals

    @staticmethod
    def _native_file_metadata(environment: UnityRsEnvironment) -> dict[int, Any]:
        files = getattr(environment.studio, "files", None)
        if not callable(files):
            return {}
        try:
            return {int(info.index): info for info in files()}
        except Exception:  # NOSONAR - metadata is an enhancement, never required
            return {}

    @staticmethod
    def _externals_of(
        environment: UnityRsEnvironment, record: UnityRsObject | None
    ) -> tuple[str, ...]:
        if record is None:
            return ()
        try:
            native = environment.studio.read_asset_bundle(record.file_index, record.path_id)
            return tuple(str(name) for name in (getattr(native, "dependencies", None) or ()))
        except Exception:  # NOSONAR - externals stay unknown; such refs report unresolved
            return ()

    def identity_for(self, file_index: int) -> CollectionFileIdentity | None:
        for identity in self.identities:
            if identity.file_index == file_index:
                return identity
        return None

    def externals_of(self, file_index: int) -> tuple[str, ...]:
        return self._externals.get(file_index, ())

    def resolve_file_id(self, source_file_index: int, file_id: int) -> CollectionExternalReference:
        """Map one non-zero PPtr file ID through the source file's externals."""
        externals = self._externals.get(source_file_index, ())
        position = file_id - 1
        if position < 0 or position >= len(externals):
            return CollectionExternalReference(UNKNOWN_EXTERNAL_REFERENCE)
        dependency_name = externals[position]
        target = self._by_bundle_name.get(dependency_name)
        if target is None:
            target = self._by_cab.get(dependency_name)
        if target is None:
            return CollectionExternalReference(
                UNKNOWN_EXTERNAL_REFERENCE, dependency_name=dependency_name
            )
        return CollectionExternalReference(
            RESOLVED_EXTERNAL_REFERENCE,
            target_file_index=target,
            dependency_name=dependency_name,
        )


@dataclass(frozen=True, slots=True)
class AudioPayload:
    """One bounded payload returned for a Unity ``AudioClip``."""

    name: str
    extension: str
    payload_kind: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ModelFilePayload:
    file_name: str
    data: bytes


@dataclass(frozen=True, slots=True)
class FbxPayload:
    fbx: bytes
    textures: tuple[ModelFilePayload, ...]
    skipped: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.fbx, bytes) or not self.fbx:
            raise UnsupportedUnityObjectError("FBX payload must be non-empty bytes")
        seen: set[str] = set()
        for texture in self.textures:
            if (
                not isinstance(texture.file_name, str)
                or not texture.file_name
                or texture.file_name in {".", ".."}
                or any(char in texture.file_name for char in "\x00/\\")
                or not isinstance(texture.data, bytes)
            ):
                raise UnsupportedUnityObjectError("invalid FBX texture payload")
            key = texture.file_name.casefold()
            if key in seen:
                raise UnsupportedUnityObjectError("duplicate FBX texture filename")
            seen.add(key)
        if any(not isinstance(item, str) for item in self.skipped):
            raise UnsupportedUnityObjectError("invalid skipped FBX texture entry")


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
    if not isinstance(width, int) or not isinstance(height, int):
        raise UnsupportedUnityObjectError("unity-rs image reader did not return width and height")
    if width <= 0 or height <= 0:
        raise InvalidImageDimensions(f"unity-rs image has invalid dimensions {width}x{height}")
    if not isinstance(pixels, bytes):
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
    if width <= 0 or height <= 0:
        raise InvalidImageDimensions(f"unity-rs image has invalid dimensions {width}x{height}")
    # Pixel-buffer validation is deferred: pulling ``value.rgba`` here would
    # copy the whole frame across the FFI boundary even when the image is
    # encoded natively and the bytes are never needed on the Python side.
    # ``to_pil`` still validates through ``_rgba_image``.
    return RenderedImage(native=value, width=width, height=height)


def _is_empty_image_error(exc: NotImplementedError) -> bool:
    message = str(exc).lower()
    return ("texture2d 0x0 carries no image data" in message) or (
        "sprite 0x0 carries no image data" in message
    )


def _read_native_image(reader: Callable[[], Any]) -> RenderedImage:
    try:
        return _rendered_image(reader())
    except NotImplementedError as exc:
        if _is_empty_image_error(exc):
            raise InvalidImageDimensions(str(exc)) from exc
        raise


def _texture_supersedes(texture: UnityRsObject, other: UnityRsObject) -> bool:
    """Whether a Texture2D keeps its container path against a same-path Sprite.

    A PNG imported as a Sprite is listed twice under one path: the Texture2D
    and a Sprite made from it. The Sprite renders only its tight texture rect,
    which drops the transparent margin and the image's offset in its canvas
    (``bonds_honor/character/chr_sd_*`` came out 140x110 instead of 160x136).
    RawImage consumers draw the whole texture, so export the texture.
    """
    return texture.class_id == 28 and other.class_id == 213


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
            self._load_asset_bundle_containers(read_asset_bundle)

        # Some serialized files do not carry an AssetBundle container table.
        # Preserve the native ObjectInfo hint as a fallback for those inputs.
        if not self.container:
            self._load_object_containers()

    def _register_container_item(self, item) -> None:
        if len(item) != 4:
            return
        container_path, _preload_index, _preload_size, identity = item
        if not isinstance(container_path, str) or not isinstance(identity, tuple):
            return
        target = self._by_identity.get(identity)
        if target is None:
            return
        current = self.container.get(container_path)
        if current is None or not _texture_supersedes(current, target):
            self.container[container_path] = target
        self._container_paths[identity] = container_path

    def _load_asset_bundle_containers(self, read_asset_bundle: Callable) -> None:
        for obj in self.objects:
            if obj.class_id != 142:
                continue
            for item in read_asset_bundle(obj.file_index, obj.path_id).container:
                self._register_container_item(item)

    def _load_object_containers(self) -> None:
        for obj in self.objects:
            if obj.container is None:
                continue
            current = self.container.get(obj.container)
            if current is None or _texture_supersedes(obj, current):
                self.container[obj.container] = obj

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
                _read_native_image(lambda: studio.read_texture(self.file_index, self.path_id))
            )
        elif self.class_id == 213:
            value = _SpriteAsset(
                _read_native_image(lambda: studio.read_sprite(self.file_index, self.path_id))
            )
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
            return _read_native_image(
                lambda: self._environment.studio.read_texture(self.file_index, self.path_id)
            )
        if self.class_id == 213:
            return _read_native_image(
                lambda: self._environment.studio.read_sprite(self.file_index, self.path_id)
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
    def m_Name(self) -> str:  # NOSONAR - Unity serialized field compatibility
        return self.name

    @property
    def m_Script(self) -> str:  # NOSONAR - Unity serialized field compatibility
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


def load_collection(
    named_bundles: Sequence[tuple[str, bytes]],
    unity_version: str | None,
) -> UnityRsEnvironment:
    """Load named bundles as one cross-reference-capable collection.

    Inputs are sorted by name before being handed to the binding so the
    positional ``file_index`` values are deterministic for a given input set.
    Names must be non-empty and unique; they become the portable bundle
    identities exposed through :class:`CollectionFileTable`.
    """

    if not isinstance(unity_version, str) or not unity_version.strip():
        raise UnityRsLoadError("unity-rs collection loading requires UNITY_VERSION")
    prepared: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for name, payload in named_bundles:
        if not isinstance(name, str) or not name.strip():
            raise UnityRsLoadError("collection input names must be non-empty strings")
        if name in seen:
            raise UnityRsLoadError(f"duplicate collection input name: {name}")
        if not isinstance(payload, (bytes, bytearray)):
            raise UnityRsLoadError(f"collection input {name!r} must be bytes")
        seen.add(name)
        prepared.append((name, bytes(payload)))
    if not prepared:
        raise UnityRsLoadError("collection loading requires at least one input bundle")
    prepared.sort(key=lambda item: item[0])
    try:
        studio = unity_rs.UnityRs.from_memory_files(prepared, unity_version=unity_version)
    except Exception as exc:
        names = [name for name, _ in prepared]
        raise UnityRsLoadError(
            f"failed to load Unity collection from {len(names)} input(s): {exc}"
        ) from exc
    return UnityRsEnvironment(studio)


def build_collection_file_table(environment: UnityRsEnvironment) -> CollectionFileTable:
    """Read the loaded collection's actual per-file identity table."""

    return CollectionFileTable(environment)


def adapter_version() -> str | None:
    """Version of the native unity-rs binding backing this adapter."""

    return getattr(unity_rs, "__version__", None)


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


def read_font_bytes(entry: UnityRsObject) -> bytes:
    """Read embedded Font bytes without consulting its serialized TypeTree."""
    if entry.class_id != 128:
        raise UnsupportedUnityObjectError(f"object class {entry.type.name} is not a Font")
    native = entry._environment.studio.read_font(entry.file_index, entry.path_id)
    data = native if isinstance(native, bytes) else getattr(native, "data", None)
    if not isinstance(data, bytes):
        raise UnsupportedUnityObjectError("unity-rs font reader did not return binary data")
    return data


def read_image(entry: UnityRsObject) -> RenderedImage:
    return entry.read_image()


def read_texture_array_images(entry: UnityRsObject) -> list[RenderedImage]:
    return entry.read_texture_array_images()


def read_audio_clip(entry: UnityRsObject) -> AudioPayload:
    return entry.read_audio_payload()


def has_mesh_scene(environment: UnityRsEnvironment) -> bool:
    """Return whether the loaded bundle contains a scene node with a mesh."""
    return any(getattr(node, "mesh", None) is not None for node in environment.studio.scene())


def read_fbx_with_textures(
    environment: UnityRsEnvironment, texture_format: str = "png"
) -> FbxPayload:
    native = environment.studio.read_fbx_with_textures(texture_format=texture_format)
    textures = tuple(ModelFilePayload(item.file_name, item.data) for item in native.textures)
    skipped = tuple(native.skipped)
    return FbxPayload(native.fbx, textures, skipped)


__all__ = [
    "AudioPayload",
    "CollectionExternalReference",
    "CollectionFileIdentity",
    "CollectionFileTable",
    "ModelFilePayload",
    "FbxPayload",
    "CLASS_ID_NAMES",
    "RESOLVED_EXTERNAL_REFERENCE",
    "SAME_FILE_REFERENCE",
    "UNKNOWN_EXTERNAL_REFERENCE",
    "InvalidImageDimensions",
    "MissingContainerError",
    "RenderedImage",
    "UnsupportedReferenceError",
    "UnsupportedUnityObjectError",
    "UnityObjectEntry",
    "UnityRsAdapterError",
    "UnityRsEnvironment",
    "UnityRsLoadError",
    "UnityRsObject",
    "adapter_version",
    "build_collection_file_table",
    "iter_container_items",
    "load_bundle",
    "load_collection",
    "has_mesh_scene",
    "read_fbx_with_textures",
    "read_audio_clip",
    "read_image",
    "read_text_bytes",
    "read_font_bytes",
    "read_texture_array_images",
    "read_type_tree",
]
