from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from updater import unity_rs_adapter


class _Info:
    def __init__(
        self,
        *,
        file_index: int,
        object_index: int,
        path_id: int,
        class_id: int,
        name: str | None = None,
        container: str | None = None,
        source_path: str = "bundle",
    ) -> None:
        self.file_index = file_index
        self.object_index = object_index
        self.path_id = path_id
        self.class_id = class_id
        self.name = name
        self.container = container
        self.source_path = source_path


class _Studio:
    def __init__(self, objects: list[_Info]) -> None:
        self._objects = objects

    def objects(self):
        return iter(self._objects)

    def read_type_tree_json(self, _file_index, _path_id):
        return json.dumps({"m_Name": "fixture", "m_Asset": {"m_FileID": 0, "m_PathID": 7}})

    def read_text(self, _file_index, _path_id):
        return b"fixture\xff"


def _external_reference_environment():
    objects = [
        _Info(file_index=0, object_index=0, path_id=1, class_id=142, source_path="CAB-a"),
        _Info(file_index=0, object_index=1, path_id=42, class_id=114, source_path="CAB-a"),
        _Info(file_index=1, object_index=0, path_id=1, class_id=142, source_path="CAB-b"),
        _Info(file_index=1, object_index=1, path_id=42, class_id=48, source_path="CAB-b"),
        _Info(file_index=2, object_index=0, path_id=1, class_id=142, source_path="CAB-c"),
        _Info(file_index=2, object_index=1, path_id=42, class_id=28, source_path="CAB-c"),
    ]

    class _ExternalStudio(_Studio):
        def read_asset_bundle(self, file_index, _path_id):
            dependencies = {0: ["CAB-c", "CAB-b"], 1: [], 2: []}
            return SimpleNamespace(container=[], dependencies=dependencies[file_index])

    return unity_rs_adapter.UnityRsEnvironment(_ExternalStudio(objects))


def _native_external_environment(
    external_rows,
    *,
    include_file_metadata=True,
    fail_at=None,
    file_metadata_paths=None,
):
    objects = [
        _Info(file_index=0, object_index=0, path_id=1, class_id=142, source_path="source::CAB-a"),
        _Info(file_index=0, object_index=1, path_id=42, class_id=114, source_path="source::CAB-a"),
        _Info(file_index=1, object_index=0, path_id=1, class_id=142, source_path="target::CAB-b"),
        _Info(file_index=1, object_index=1, path_id=42, class_id=48, source_path="target::CAB-b"),
    ]

    class _NativeExternalStudio(_Studio):
        def __init__(self):
            super().__init__(objects)
            self.page_calls = []

        def files(self):
            if not include_file_metadata:
                return []
            paths = (
                file_metadata_paths
                if file_metadata_paths is not None
                else ("source::CAB-a", "target::CAB-b")
            )
            return [
                SimpleNamespace(index=index, path=path, unity_version="2022.3")
                for index, path in enumerate(paths)
            ]

        def file_external_page(self, file_index, *, offset=0, limit=4096):
            self.page_calls.append((file_index, offset, limit))
            if fail_at == (file_index, offset):
                raise RuntimeError("native page failure")
            rows = external_rows.get(file_index, ())
            return rows[offset : offset + limit]

    studio = _NativeExternalStudio()
    return unity_rs_adapter.UnityRsEnvironment(studio), studio


def _native_external_row(file_id: int, path: str, *, guid=None):
    return SimpleNamespace(file_id=file_id, path=path, guid=guid, kind=None, prefix=None)


class _FontStudio(_Studio):
    def __init__(self, objects: list[_Info], font):
        super().__init__(objects)
        self.font = font

    def read_font(self, _file_index, _path_id):
        return self.font


def test_environment_sorts_objects_and_exposes_unique_container_entries() -> None:
    infos = [
        _Info(file_index=1, object_index=2, path_id=20, class_id=49, container="second"),
        _Info(file_index=0, object_index=4, path_id=10, class_id=49, container="first"),
        _Info(file_index=1, object_index=1, path_id=19, class_id=49, container="second"),
    ]

    environment = unity_rs_adapter.UnityRsEnvironment(_Studio(infos))

    assert [(obj.file_index, obj.object_index) for obj in environment.objects] == [
        (0, 4),
        (1, 1),
        (1, 2),
    ]
    assert list(environment.container) == ["first", "second"]
    assert environment.container["second"].object_index == 1


def test_environment_uses_asset_bundle_container_targets() -> None:
    root = _Info(file_index=0, object_index=0, path_id=42, class_id=114, container="wrong")
    asset_bundle = _Info(file_index=0, object_index=1, path_id=1, class_id=142)

    class _AssetBundleStudio(_Studio):
        def read_asset_bundle(self, _file_index, _path_id):
            return SimpleNamespace(container=[("actual.playable", 0, 1, (0, 42))])

    environment = unity_rs_adapter.UnityRsEnvironment(_AssetBundleStudio([root, asset_bundle]))

    assert environment.container["actual.playable"].path_id == 42
    assert environment.container["actual.playable"].container == "actual.playable"


def test_asset_bundle_container_file_zero_reference_uses_owner_file_identity() -> None:
    path = "actual.playable"
    collision = _Info(file_index=0, object_index=0, path_id=42, class_id=114)
    target = _Info(file_index=1, object_index=0, path_id=42, class_id=114)
    bundles = [
        _Info(file_index=0, object_index=1, path_id=1, class_id=142),
        _Info(file_index=1, object_index=1, path_id=2, class_id=142),
    ]

    class _AssetBundleStudio(_Studio):
        def read_asset_bundle(self, file_index, _path_id):
            container = [(path, 0, 1, (0, 42))] if file_index == 1 else []
            return SimpleNamespace(container=container)

    environment = unity_rs_adapter.UnityRsEnvironment(
        _AssetBundleStudio([collision, target, *bundles])
    )

    assert environment.container[path] is environment.object_by_identity(1, 42)
    assert environment.object_by_identity(1, 42).container == path
    assert environment.object_by_identity(0, 42).container is None


def test_asset_bundle_container_does_not_guess_missing_owner_target() -> None:
    collision = _Info(file_index=0, object_index=0, path_id=42, class_id=114)
    asset_bundle = _Info(file_index=1, object_index=0, path_id=1, class_id=142)

    class _AssetBundleStudio(_Studio):
        def read_asset_bundle(self, _file_index, _path_id):
            return SimpleNamespace(container=[("wrong.playable", 0, 1, (0, 42))])

    environment = unity_rs_adapter.UnityRsEnvironment(_AssetBundleStudio([collision, asset_bundle]))

    assert "wrong.playable" not in environment.container
    assert environment.object_by_identity(0, 42).container is None


@pytest.mark.parametrize(
    "reference",
    [(1, 42), (-1, 42), (0, 0), ("0", 42), (0, "42"), (0, 42, 43)],
)
def test_asset_bundle_container_ignores_unsupported_or_malformed_references(reference) -> None:
    target = _Info(file_index=0, object_index=0, path_id=42, class_id=114)
    collision = _Info(file_index=1, object_index=0, path_id=42, class_id=114)
    asset_bundle = _Info(file_index=0, object_index=1, path_id=1, class_id=142)

    class _AssetBundleStudio(_Studio):
        def read_asset_bundle(self, _file_index, _path_id):
            return SimpleNamespace(container=[("unresolved.playable", 0, 1, reference)])

    environment = unity_rs_adapter.UnityRsEnvironment(
        _AssetBundleStudio([target, collision, asset_bundle])
    )

    assert "unresolved.playable" not in environment.container
    assert environment.object_by_identity(0, 42).container is None
    assert environment.object_by_identity(1, 42).container is None


@pytest.mark.parametrize("order", [(28, 213), (213, 28)])
def test_asset_bundle_container_keeps_texture_over_same_path_sprite(order) -> None:
    # A Sprite-imported PNG lists its Texture2D and Sprite under one path, as in
    # bonds_honor/character/chr_sd_21_01; the Sprite would export trimmed.
    path = "assets/sekai/assetbundle/resources/startapp/bonds_honor/character/chr_sd_21_01.png"
    texture = _Info(file_index=0, object_index=0, path_id=1, class_id=28)
    sprite = _Info(file_index=0, object_index=1, path_id=2, class_id=213)
    asset_bundle = _Info(file_index=0, object_index=2, path_id=3, class_id=142)
    path_ids = {28: 1, 213: 2}

    class _AssetBundleStudio(_Studio):
        def read_asset_bundle(self, _file_index, _path_id):
            return SimpleNamespace(
                container=[(path, 0, 1, (0, path_ids[class_id])) for class_id in order]
            )

    environment = unity_rs_adapter.UnityRsEnvironment(
        _AssetBundleStudio([texture, sprite, asset_bundle])
    )

    assert environment.container[path].class_id == 28
    # The Sprite still knows its path; it is only not the exported entry.
    assert environment.objects[1].container == path


@pytest.mark.parametrize("order", [(28, 213), (213, 28)])
def test_object_containers_keep_texture_over_same_path_sprite(order) -> None:
    infos = [
        _Info(
            file_index=0,
            object_index=index,
            path_id=index + 1,
            class_id=class_id,
            container="a.png",
        )
        for index, class_id in enumerate(order)
    ]

    environment = unity_rs_adapter.UnityRsEnvironment(_Studio(infos))

    assert environment.container["a.png"].class_id == 28


def test_type_tree_and_text_asset_keep_json_and_raw_bytes() -> None:
    info = _Info(file_index=0, object_index=0, path_id=7, class_id=49, name="fixture")
    obj = unity_rs_adapter.UnityRsEnvironment(_Studio([info])).objects[0]

    assert obj.read_typetree()["m_Name"] == "fixture"
    assert unity_rs_adapter.read_text_bytes(obj) == b"fixture\xff"
    assert obj.read().m_Script.encode("utf-8", "surrogateescape") == b"fixture\xff"


@pytest.mark.parametrize(
    "font", [b"OTTOfont-data", SimpleNamespace(data=b"\x00\x01\x00\x00font-data")]
)
def test_font_bytes_use_native_reader_without_typetree(font) -> None:
    info = _Info(file_index=0, object_index=0, path_id=8, class_id=128, name="font.otf")
    studio = _FontStudio([info], font)
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    obj = environment.objects[0]

    assert unity_rs_adapter.read_font_bytes(obj) == (font if isinstance(font, bytes) else font.data)


def test_pptr_values_remain_mapping_compatible_and_resolvable() -> None:
    info = _Info(file_index=0, object_index=0, path_id=1, class_id=114)
    environment = unity_rs_adapter.UnityRsEnvironment(_Studio([info]))
    value = environment.objects[0].read()

    pointer = value["m_Asset"]
    assert pointer["m_FileID"] == 0
    assert pointer.m_PathID == 7
    dereferenced = pointer.deref()
    assert dereferenced is None


def test_nonzero_pptr_does_not_silently_resolve_to_a_wrong_file() -> None:
    info = _Info(file_index=0, object_index=0, path_id=1, class_id=114)

    class _CrossFileStudio(_Studio):
        def read_type_tree_json(self, _file_index, _path_id):
            return json.dumps({"m_Asset": {"m_FileID": 2, "m_PathID": 7}})

    obj = unity_rs_adapter.UnityRsEnvironment(_CrossFileStudio([info])).objects[0]
    pointer = obj.read()["m_Asset"]
    with pytest.raises(unity_rs_adapter.UnsupportedReferenceError):
        pointer.deref()


def test_external_resolution_without_serialized_table_never_uses_dependencies() -> None:
    environment = _external_reference_environment()

    table = unity_rs_adapter.build_collection_file_table(environment)
    resolution = table.resolve_file_id(0, 1)

    assert table.externals_of(0) == ()
    assert resolution.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert resolution.target_file_index is None
    assert resolution.dependency_name is None


def test_native_external_pages_preserve_ids_order_empty_and_duplicate_paths_without_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_NATIVE_EXTERNAL_PAGE_SIZE", 2)
    environment, studio = _native_external_environment(
        {
            0: [
                _native_external_row(1, "CAB-b"),
                _native_external_row(2, ""),
                _native_external_row(3, "CAB-b"),
                _native_external_row(4, "Resources/unity_builtin_extra"),
            ]
        }
    )

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == (
        "CAB-b",
        "",
        "CAB-b",
        "Resources/unity_builtin_extra",
    )
    first_reference = table.resolve_file_id(0, 1)
    assert first_reference.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert first_reference.target_file_index is None
    assert first_reference.dependency_name is None
    empty_reference = table.resolve_file_id(0, 2)
    assert empty_reference.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert empty_reference.dependency_name is None
    duplicate_reference = table.resolve_file_id(0, 3)
    assert duplicate_reference.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert duplicate_reference.target_file_index is None
    assert duplicate_reference.dependency_name is None
    builtin_reference = table.resolve_file_id(0, 4)
    assert builtin_reference.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert builtin_reference.dependency_name is None
    assert [call for call in studio.page_calls if call[0] == 0] == [
        (0, 0, 2),
        (0, 2, 2),
        (0, 4, 2),
    ]


def test_native_external_pages_fail_closed_after_late_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_NATIVE_EXTERNAL_PAGE_SIZE", 1)
    environment, _studio = _native_external_environment(
        {0: [_native_external_row(1, "CAB-b"), _native_external_row(2, "CAB-b")]},
        fail_at=(0, 1),
    )

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ()
    resolution = table.resolve_file_id(0, 1)
    assert resolution.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert resolution.target_file_index is None
    assert resolution.dependency_name is None


@pytest.mark.parametrize(
    "rows",
    [
        [_native_external_row(1, "CAB-b"), _native_external_row(1, "CAB-b")],
        [_native_external_row(1, "CAB-b"), _native_external_row(3, "CAB-b")],
        [SimpleNamespace(file_id=1, path=None)],
    ],
)
def test_native_external_pages_fail_closed_on_duplicate_or_incomplete_rows(
    monkeypatch: pytest.MonkeyPatch, rows
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_NATIVE_EXTERNAL_PAGE_SIZE", 1)
    environment, _studio = _native_external_environment({0: rows})

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ()
    assert table.resolve_file_id(0, 1).kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE


def test_native_external_path_does_not_match_bundle_name_or_basename() -> None:
    for path in ("target", "folder/CAB-b"):
        environment, _studio = _native_external_environment({0: [_native_external_row(1, path)]})

        resolution = unity_rs_adapter.build_collection_file_table(environment).resolve_file_id(0, 1)

        assert resolution.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
        assert resolution.target_file_index is None


def test_native_external_path_without_verified_file_metadata_is_not_mapped() -> None:
    environment, _studio = _native_external_environment(
        {0: [_native_external_row(1, "CAB-b")]}, include_file_metadata=False
    )

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ("CAB-b",)
    resolution = table.resolve_file_id(0, 1)
    assert resolution.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert resolution.dependency_name is None


def test_native_external_exact_cab_match_is_unknown_when_identity_is_ambiguous() -> None:
    objects = [
        _Info(file_index=0, object_index=0, path_id=1, class_id=114),
        _Info(file_index=1, object_index=0, path_id=1, class_id=114),
        _Info(file_index=2, object_index=0, path_id=1, class_id=114),
    ]

    class _AmbiguousCabStudio(_Studio):
        def files(self):
            return [
                SimpleNamespace(index=0, path="source::CAB-a"),
                SimpleNamespace(index=1, path="target-one::CAB-b"),
                SimpleNamespace(index=2, path="target-two::CAB-b"),
            ]

        def file_external_page(self, file_index, *, offset=0, limit=4096):
            rows = [_native_external_row(1, "CAB-b")] if file_index == 0 else []
            return rows[offset : offset + limit]

    environment = unity_rs_adapter.UnityRsEnvironment(_AmbiguousCabStudio(objects))
    resolution = unity_rs_adapter.build_collection_file_table(environment).resolve_file_id(0, 1)

    assert resolution.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert resolution.target_file_index is None
    assert resolution.dependency_name is None


def test_native_external_nonzero_guid_does_not_map_by_path() -> None:
    environment, _studio = _native_external_environment(
        {0: [_native_external_row(1, "CAB-b", guid=b"\x01" * 16)]}
    )

    resolution = unity_rs_adapter.build_collection_file_table(environment).resolve_file_id(0, 1)

    assert resolution.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert resolution.target_file_index is None
    assert resolution.dependency_name is None


@pytest.mark.parametrize(
    ("path", "file_metadata_paths"),
    [
        (
            "Resources/unity_builtin_extra",
            ("source::CAB-a", "target::Resources/unity_builtin_extra"),
        ),
        ("arbitrary-name", ("source::CAB-a", "target::arbitrary-name")),
        ("nested/CAB-b", ("source::CAB-a", "target::nested/CAB-b")),
        ("CAB-b", ("source::CAB-a", "different-label::CAB-b")),
        ("CAB-b", ("different-source::CAB-a", "target::CAB-b")),
    ],
)
def test_native_external_paths_never_map_from_names_or_file_metadata(
    path, file_metadata_paths
) -> None:
    environment, _studio = _native_external_environment(
        {0: [_native_external_row(1, path)]}, file_metadata_paths=file_metadata_paths
    )

    resolution = unity_rs_adapter.build_collection_file_table(environment).resolve_file_id(0, 1)

    assert resolution.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert resolution.target_file_index is None
    assert resolution.dependency_name is None


def test_explicit_serialized_externals_keep_existing_mapping_and_skip_native_reader() -> None:
    environment, studio = _native_external_environment(
        {0: [_native_external_row(1, "ignored-native-path")]}
    )

    table = unity_rs_adapter.build_collection_file_table(
        environment, serialized_externals={0: ("CAB-b",)}
    )

    assert studio.page_calls == []
    resolution = table.resolve_file_id(0, 1)
    assert resolution.kind == unity_rs_adapter.RESOLVED_EXTERNAL_REFERENCE
    assert resolution.target_file_index == 1
    assert resolution.dependency_name == "CAB-b"


def test_native_external_per_file_count_budget_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNALS_PER_FILE", 1)
    environment, _studio = _native_external_environment(
        {0: [_native_external_row(1, "one"), _native_external_row(2, "two")]}
    )

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ()
    assert table.resolve_file_id(0, 1).kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE


def test_native_external_total_count_budget_discards_all_native_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNALS_TOTAL", 1)
    environment, _studio = _native_external_environment(
        {0: [_native_external_row(1, "one")], 1: [_native_external_row(1, "two")]}
    )

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ()
    assert table.externals_of(1) == ()


def test_native_external_total_count_budget_includes_failed_file_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNALS_PER_FILE", 1)
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNALS_TOTAL", 2)
    environment, _studio = _native_external_environment(
        {
            0: [_native_external_row(1, "one"), _native_external_row(2, "two")],
            1: [_native_external_row(1, "three")],
        }
    )

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ()
    assert table.externals_of(1) == ()


def test_native_external_per_file_path_byte_budget_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNAL_PATH_BYTES_PER_FILE", 1)
    environment, _studio = _native_external_environment({0: [_native_external_row(1, "é")]})

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ()
    assert table.externals_of(1) == ()


def test_native_external_per_file_failure_cannot_undercharge_total_path_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNAL_PATH_BYTES_PER_FILE", 1)
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNAL_PATH_BYTES_TOTAL", 4)
    environment, _studio = _native_external_environment(
        {0: [_native_external_row(1, "abcd")], 1: [_native_external_row(1, "x")]}
    )

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ()
    assert table.externals_of(1) == ()


def test_native_external_total_path_byte_budget_includes_failed_file_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNAL_PATH_BYTES_PER_FILE", 1)
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNAL_PATH_BYTES_TOTAL", 3)
    environment, _studio = _native_external_environment(
        {0: [_native_external_row(1, "ab")], 1: [_native_external_row(1, "xy")]}
    )

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ()
    assert table.externals_of(1) == ()


def test_native_external_total_path_byte_budget_discards_all_native_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unity_rs_adapter, "_MAX_NATIVE_EXTERNAL_PATH_BYTES_TOTAL", 5)
    environment, _studio = _native_external_environment(
        {0: [_native_external_row(1, "one")], 1: [_native_external_row(1, "two")]}
    )

    table = unity_rs_adapter.build_collection_file_table(environment)

    assert table.externals_of(0) == ()
    assert table.externals_of(1) == ()


def test_authoritative_serialized_external_order_beats_dependency_order_and_path_collisions() -> (
    None
):
    environment = _external_reference_environment()
    serialized_names = ["CAB-b", "CAB-c"]

    table = unity_rs_adapter.build_collection_file_table(
        environment, serialized_externals={0: serialized_names}
    )
    serialized_names[0] = "CAB-c"  # the table must own a snapshot
    resolution = table.resolve_file_id(0, 1)

    assert table.externals_of(0) == ("CAB-b", "CAB-c")
    assert resolution.kind == unity_rs_adapter.RESOLVED_EXTERNAL_REFERENCE
    assert resolution.target_file_index == 1
    assert environment.object_by_identity(resolution.target_file_index, 42).class_id == 48
    assert resolution.dependency_name == "CAB-b"


def test_authoritative_but_unmatched_external_name_is_preserved_without_guessing() -> None:
    environment = _external_reference_environment()
    table = unity_rs_adapter.build_collection_file_table(
        environment, serialized_externals={0: ("CAB-not-loaded",)}
    )

    resolution = table.resolve_file_id(0, 1)

    assert resolution.kind == unity_rs_adapter.UNKNOWN_EXTERNAL_REFERENCE
    assert resolution.target_file_index is None
    assert resolution.dependency_name == "CAB-not-loaded"


@pytest.mark.parametrize(
    ("serialized_externals", "error"),
    [
        ({"0": ("CAB-b",)}, TypeError),
        ({True: ("CAB-b",)}, TypeError),
        ({9: ("CAB-b",)}, ValueError),
        ({0: "CAB-b"}, TypeError),
        ({0: (None,)}, TypeError),
        ({0: ("",)}, ValueError),
        ({0: ("  ",)}, ValueError),
    ],
)
def test_serialized_external_metadata_rejects_invalid_indices_and_entries(
    serialized_externals, error
) -> None:
    environment = _external_reference_environment()

    with pytest.raises(error):
        unity_rs_adapter.build_collection_file_table(
            environment, serialized_externals=serialized_externals
        )


def test_image_contract_rejects_malformed_native_image() -> None:
    malformed = SimpleNamespace(width=2, height=2, rgba=b"short")
    with pytest.raises(unity_rs_adapter.UnsupportedUnityObjectError):
        unity_rs_adapter._rgba_image(malformed)


@pytest.mark.parametrize("width,height", [(0, 1), (1, 0), (-1, 2), (2, -1)])
def test_image_contract_rejects_zero_or_negative_dimensions(width: int, height: int) -> None:
    image = SimpleNamespace(width=width, height=height, rgba=b"")
    with pytest.raises(unity_rs_adapter.InvalidImageDimensions, match="invalid dimensions"):
        unity_rs_adapter._rendered_image(image)


@pytest.mark.parametrize(
    "message",
    ["Texture2D 0x0 carries no image data", "Sprite 0x0 carries no image data"],
)
def test_native_empty_image_error_becomes_invalid_dimensions(message: str) -> None:
    with pytest.raises(unity_rs_adapter.InvalidImageDimensions, match="no image data"):
        unity_rs_adapter._read_native_image(
            lambda: (_ for _ in ()).throw(NotImplementedError(message))
        )


def test_other_native_not_implemented_image_error_is_preserved() -> None:
    with pytest.raises(NotImplementedError, match="decoder unavailable"):
        unity_rs_adapter._read_native_image(
            lambda: (_ for _ in ()).throw(NotImplementedError("decoder unavailable"))
        )


def test_invalid_image_dimensions_remains_unsupported_error() -> None:
    assert issubclass(
        unity_rs_adapter.InvalidImageDimensions,
        unity_rs_adapter.UnsupportedUnityObjectError,
    )


def test_load_bundle_requires_explicit_unity_version(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(unity_rs_adapter.UnityRsLoadError, match="requires UNITY_VERSION"):
        unity_rs_adapter.load_bundle(b"fixture", None)

    class _FakeUnityRs:
        def __new__(cls, _path, *, unity_version):
            assert unity_version == "2022.3.52f1"
            return _Studio([])

        @staticmethod
        def from_bytes(_data, *, unity_version):
            assert unity_version == "2022.3.52f1"
            return _Studio([])

    monkeypatch.setattr(unity_rs_adapter.unity_rs, "UnityRs", _FakeUnityRs)
    environment = unity_rs_adapter.load_bundle(b"fixture", "2022.3.52f1")
    assert environment.objects == []
