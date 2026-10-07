"""GLB Phase 3 tests: collection loading, cross-file PPtrs, L2 validation."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import orjson
import pytest

from updater import unity_rs_adapter
from updater.extract.glb import (
    DIAG_MISSING_TARGET,
    DIAG_NULL_REFERENCE,
    DIAG_ROOT_OBJECT_MISSING,
    DIAG_TYPE_MISMATCH,
    DIAG_UNRESOLVED_EXTERNAL,
    DIAG_UNSUPPORTED_REFERENCE,
    SCENE3D_PURPOSE,
    BundleRef,
    ExtractionPlan,
    PackageNotPublishable,
    RootRef,
    build_package_manifest,
    input_bundle_checksums,
    iter_pptr_locations,
    load_package_collection,
    load_package_manifest,
    persist_package_manifest,
    resolve_pptr,
    validate_collection_reachability,
)
from updater.extract.sync_worker import extract_collection_sync

STAGE_BUNDLE = "stage/root_stage"
SHADER_BUNDLE = "shader/common_pack"
STAGE_CAB = "CAB-a"
SHADER_CAB = "CAB-b"
ROOT_CONTAINER = "stage/root_stage#root_go"


def _plan(
    roots: tuple[RootRef, ...] | RootRef | None = None,
    *,
    with_shader_bundle: bool = True,
) -> ExtractionPlan:
    if roots is None:
        roots = (
            RootRef(
                root_id="stage_root",
                kind="stage",
                bundle_name=STAGE_BUNDLE,
                container_path=ROOT_CONTAINER,
            ),
        )
    elif isinstance(roots, RootRef):
        roots = (roots,)
    bundles = [BundleRef(bundle_name=STAGE_BUNDLE)]
    if with_shader_bundle:
        bundles.append(BundleRef(bundle_name=SHADER_BUNDLE))
    return ExtractionPlan(
        package_id="streaming_live/0006_lon_vbs_01/stage",
        purpose=SCENE3D_PURPOSE,
        profile="scene3d",
        roots=roots,
        bundles=tuple(bundles),
    )


class _Info:
    def __init__(
        self,
        *,
        file_index: int,
        path_id: int,
        class_id: int,
        name: str | None = None,
        container: str | None = None,
        object_index: int = 0,
        source_path: str = "",
    ) -> None:
        self.file_index = file_index
        self.object_index = object_index
        self.path_id = path_id
        self.class_id = class_id
        self.name = name
        self.container = container
        self.source_path = source_path


class _CollectionStudio:
    """Fake binding studio shaped like the from_memory_files collection.

    Typetrees and AssetBundle records are declared per (file_index, path_id);
    the file metadata mirrors ``FileInfo`` (``path = "<input>::<CAB>"``).
    """

    def __init__(
        self,
        *,
        files: list[tuple[str, str]],
        objects: list[_Info],
        typetrees: dict[tuple[int, int], dict],
        dependencies: dict[int, list[str]],
        containers: dict[int, list[tuple]],
    ) -> None:
        self._files = [
            SimpleNamespace(index=index, path=f"{name}::{cab}", unity_version="2022.3.21f1")
            for index, (name, cab) in enumerate(files)
        ]
        self._objects = objects
        self._typetrees = typetrees
        self._dependencies = dependencies
        self._containers = containers

    def files(self):
        return list(self._files)

    def objects(self):
        return iter(self._objects)

    def read_asset_bundle(self, file_index: int, _path_id: int):
        return SimpleNamespace(
            container=self._containers.get(file_index, []),
            dependencies=self._dependencies.get(file_index, []),
        )

    def read_type_tree_json(self, file_index: int, path_id: int):
        tree = self._typetrees[(file_index, path_id)]
        return orjson.dumps(tree)


def _studio(*, include_shader_pack: bool = True) -> _CollectionStudio:
    files = [(STAGE_BUNDLE, STAGE_CAB)]
    objects = [
        _Info(
            file_index=0,
            path_id=1,
            class_id=142,
            name=STAGE_BUNDLE,
            container=ROOT_CONTAINER,
            source_path=f"{STAGE_BUNDLE}::{STAGE_CAB}",
        ),
        _Info(
            file_index=0,
            path_id=100,
            class_id=1,
            name="root_go",
            container=ROOT_CONTAINER,
            source_path=f"{STAGE_BUNDLE}::{STAGE_CAB}",
        ),
        _Info(file_index=0, path_id=101, class_id=4, source_path=f"{STAGE_BUNDLE}::{STAGE_CAB}"),
        _Info(
            file_index=0,
            path_id=102,
            class_id=21,
            name="mat_shared",
            source_path=f"{STAGE_BUNDLE}::{STAGE_CAB}",
        ),
        _Info(file_index=0, path_id=103, class_id=23, source_path=f"{STAGE_BUNDLE}::{STAGE_CAB}"),
    ]
    typetrees = {
        (0, 1): {"m_Name": STAGE_BUNDLE},
        (0, 100): {
            "m_Name": "root_go",
            "m_Component": [
                {"component": {"m_FileID": 0, "m_PathID": 101}},
                {"component": {"m_FileID": 0, "m_PathID": 103}},
            ],
        },
        (0, 101): {
            "m_GameObject": {"m_FileID": 0, "m_PathID": 100},
            "m_Father": {"m_FileID": 0, "m_PathID": 0},
        },
        (0, 103): {
            "m_GameObject": {"m_FileID": 0, "m_PathID": 100},
            "m_Materials": [{"element": {"m_FileID": 0, "m_PathID": 102}}],
        },
        (0, 102): {
            "m_Name": "mat_shared",
            "m_Shader": {"m_FileID": 1, "m_PathID": 200},
        },
    }
    dependencies = {0: [SHADER_BUNDLE]}
    containers = {0: [(ROOT_CONTAINER, 0, 1, (0, 100))]}
    if include_shader_pack:
        files.append((SHADER_BUNDLE, SHADER_CAB))
        objects.append(
            _Info(
                file_index=1,
                path_id=1,
                class_id=142,
                name=SHADER_BUNDLE,
                source_path=f"{SHADER_BUNDLE}::{SHADER_CAB}",
            )
        )
        objects.append(
            _Info(
                file_index=1,
                path_id=200,
                class_id=48,
                name="shader_lit",
                source_path=f"{SHADER_BUNDLE}::{SHADER_CAB}",
            )
        )
        typetrees[(1, 1)] = {"m_Name": SHADER_BUNDLE}
        typetrees[(1, 200)] = {"m_ParsedForm": {"m_PropInfo": []}}
        dependencies[1] = []
        containers[1] = []
    return _CollectionStudio(
        files=files,
        objects=objects,
        typetrees=typetrees,
        dependencies=dependencies,
        containers=containers,
    )


def _environment(include_shader_pack: bool = True):
    return unity_rs_adapter.UnityRsEnvironment(_studio(include_shader_pack=include_shader_pack))


def _table(environment):
    return unity_rs_adapter.build_collection_file_table(environment)


def test_iter_pptr_locations_walks_nested_trees_deterministically() -> None:
    tree = {
        "m_Component": [{"component": {"m_FileID": 0, "m_PathID": 7}}],
        "m_ZEnd": {"m_Ptr": {"m_FileID": 2, "m_PathID": 9, "m_Extra": 1}},
        "m_Null": {"m_FileID": 0, "m_PathID": 0},
        "m_NotAPtr": {"m_FileID": 0, "m_PathID": 0, "m_Vtable": []},
    }

    locations = list(iter_pptr_locations(tree))

    assert [(loc.field_path, loc.file_id, loc.path_id) for loc in locations] == [
        ("m_Component[0].component", 0, 7),
        ("m_Null", 0, 0),
    ]


def test_collection_file_table_builds_portable_identities_and_externals() -> None:
    environment = _environment()
    table = _table(environment)

    assert [(i.bundle_name, i.cab_name) for i in table.identities] == [
        (STAGE_BUNDLE, STAGE_CAB),
        (SHADER_BUNDLE, SHADER_CAB),
    ]
    assert table.externals_of(0) == (SHADER_BUNDLE,)
    assert table.externals_of(1) == ()

    resolved = table.resolve_file_id(0, 1)
    assert resolved.kind == "resolved_external"
    assert resolved.target_file_index == 1
    assert resolved.dependency_name == SHADER_BUNDLE

    unknown = table.resolve_file_id(1, 1)
    assert unknown.kind == "unknown_external"
    assert unknown.dependency_name is None


def test_resolve_pptr_covers_same_file_null_and_external_cases() -> None:
    environment = _environment()
    table = _table(environment)

    internal = resolve_pptr(environment, table, 0, 0, 100)
    assert internal.status == "same_file"
    assert internal.object.path_id == 100

    null = resolve_pptr(environment, table, 0, 0, 0)
    assert null.status == "same_file"
    assert null.object is None

    external = resolve_pptr(environment, table, 0, 1, 200)
    assert external.status == "resolved_external"
    assert external.object.class_id == 48
    assert external.target_file_index == 1

    unresolved = resolve_pptr(environment, table, 0, 5, 200)
    assert unresolved.status == "unknown_external"


def test_combined_collection_resolves_required_cross_file_references() -> None:
    environment = _environment(include_shader_pack=True)
    table = _table(environment)
    plan = _plan()

    report = validate_collection_reachability(environment, table, plan.roots)

    assert report.publishable
    assert (
        report.visited_objects == 5
    )  # AssetBundle, GO, Transform, Renderer, Material, Shader - 142 not visited
    codes = {finding.code for finding in report.findings}
    assert codes == {DIAG_NULL_REFERENCE}
    shader = [finding for finding in report.findings if finding.field_path.endswith("m_Shader")]
    assert not shader


def test_stage_alone_fixture_reports_unresolved_external_with_identities() -> None:
    environment = _environment(include_shader_pack=False)
    table = _table(environment)
    plan = _plan()

    report = validate_collection_reachability(environment, table, plan.roots)

    assert not report.publishable
    external = [f for f in report.findings if f.code == DIAG_UNRESOLVED_EXTERNAL]
    assert len(external) == 1
    finding = external[0]
    assert finding.dependency_name == SHADER_BUNDLE
    assert (finding.source_file_index, finding.source_path_id) == (0, 102)
    assert finding.severity == "error"


def test_missing_target_reports_dep_file_and_path_id() -> None:
    studio = _studio(include_shader_pack=True)
    studio._typetrees[(0, 102)] = {
        "m_Name": "mat_shared",
        "m_Shader": {"m_FileID": 1, "m_PathID": 999},
    }
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    table = _table(environment)

    report = validate_collection_reachability(environment, table, _plan().roots)

    missing = [f for f in report.findings if f.code == DIAG_MISSING_TARGET]
    assert len(missing) == 1
    assert (missing[0].target_file_index, missing[0].target_path_id) == (1, 999)
    assert not report.publishable


def test_optional_unresolved_external_is_structured_warning() -> None:
    environment = _environment(include_shader_pack=False)
    table = _table(environment)

    report = validate_collection_reachability(
        environment,
        table,
        _plan().roots,
        optional_dependencies=frozenset({SHADER_BUNDLE}),
    )

    external = [f for f in report.findings if f.code == DIAG_UNRESOLVED_EXTERNAL]
    assert [f.severity for f in external] == ["warning"]
    assert report.publishable


def test_expected_class_map_reports_type_mismatch() -> None:
    environment = _environment()
    table = _table(environment)

    report = validate_collection_reachability(
        environment,
        table,
        _plan().roots,
        expected_classes={"m_Shader": frozenset({28})},  # expects Texture2D, gets Shader
    )

    mismatch = [f for f in report.findings if f.code == DIAG_TYPE_MISMATCH]
    assert len(mismatch) == 1
    assert mismatch[0].target_class == "Shader"
    assert not report.publishable


def test_missing_root_object_is_reported_with_bundle_identity() -> None:
    environment = _environment()
    table = _table(environment)
    root = RootRef(
        root_id="ghost",
        kind="stage",
        bundle_name=STAGE_BUNDLE,
        container_path="stage/absent",
    )

    report = validate_collection_reachability(environment, table, (root,))

    missing = [f for f in report.findings if f.code == DIAG_ROOT_OBJECT_MISSING]
    assert len(missing) == 1
    assert missing[0].dependency_name == STAGE_BUNDLE
    assert not report.publishable


def test_unreadable_typetree_is_structured_warning() -> None:
    studio = _studio()
    original = studio.read_type_tree_json

    def read_type_tree_json(file_index: int, path_id: int):
        if (file_index, path_id) == (0, 100):
            raise RuntimeError("corrupt header")
        return original(file_index, path_id)

    studio.read_type_tree_json = read_type_tree_json
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    table = _table(environment)

    report = validate_collection_reachability(environment, table, _plan().roots)

    broken = [f for f in report.findings if f.code == DIAG_UNSUPPORTED_REFERENCE]
    assert len(broken) == 1
    assert broken[0].severity == "warning"


def test_load_package_collection_rejects_payloads_outside_plan() -> None:
    plan = _plan()
    payloads = [
        (STAGE_BUNDLE, b"stage"),
        (SHADER_BUNDLE, b"shader"),
        ("unrelated/bundle", b"extra"),
    ]

    with pytest.raises(ValueError, match="outside the plan"):
        load_package_collection(plan, payloads, "2022.3.21f1")

    with pytest.raises(ValueError, match="missing payloads"):
        load_package_collection(plan, [(STAGE_BUNDLE, b"stage")], "2022.3.21f1")


def test_load_collection_validates_and_sorts_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_from_memory_files(files, *, unity_version=None, **_kwargs):
        captured["files"] = files
        captured["unity_version"] = unity_version
        return _studio()

    monkeypatch.setattr(
        unity_rs_adapter.unity_rs.UnityRs,
        "from_memory_files",
        staticmethod(_fake_from_memory_files),
    )

    environment = unity_rs_adapter.load_collection(
        [(SHADER_BUNDLE, b"b"), (STAGE_BUNDLE, b"a")], "2022.3.21f1"
    )
    assert captured["files"] == [(SHADER_BUNDLE, b"b"), (STAGE_BUNDLE, b"a")]
    assert captured["unity_version"] == "2022.3.21f1"
    assert environment is not None

    for bad in (
        [],
        [("", b"x")],
        [("dup", b"a"), ("dup", b"b")],
        [("name", "not-bytes")],
    ):
        with pytest.raises(unity_rs_adapter.UnityRsLoadError):
            unity_rs_adapter.load_collection(bad, "2022.3.21f1")

    with pytest.raises(unity_rs_adapter.UnityRsLoadError):
        unity_rs_adapter.load_collection([(STAGE_BUNDLE, b"a")], None)

    def _boom(files, **_kwargs):
        raise ValueError("native failure")

    monkeypatch.setattr(unity_rs_adapter.unity_rs.UnityRs, "from_memory_files", staticmethod(_boom))
    with pytest.raises(unity_rs_adapter.UnityRsLoadError, match="native failure"):
        unity_rs_adapter.load_collection([(STAGE_BUNDLE, b"a")], "2022.3.21f1")


def test_input_bundle_checksums_are_deterministic() -> None:
    payloads = [(SHADER_BUNDLE, b"shader-bytes"), (STAGE_BUNDLE, b"stage-bytes")]

    checksums = input_bundle_checksums(payloads)

    assert checksums == {
        STAGE_BUNDLE: hashlib.sha256(b"stage-bytes").hexdigest(),
        SHADER_BUNDLE: hashlib.sha256(b"shader-bytes").hexdigest(),
    }


def test_manifest_round_trip_and_rejects_wrong_shape(tmp_path) -> None:
    environment = _environment()
    table = _table(environment)
    plan = _plan()
    report = validate_collection_reachability(environment, table, plan.roots)
    payloads = [(STAGE_BUNDLE, b"stage"), (SHADER_BUNDLE, b"shader")]

    manifest = build_package_manifest(plan, payloads, "2022.3.21f1", report, environment, table)
    assert manifest["manifest_version"] == 1
    assert manifest["package_id"] == plan.package_id
    assert manifest["collection_id"] == plan.collection_id()
    assert manifest["unity_version"] == "2022.3.21f1"
    assert manifest["adapter_version"]
    assert manifest["inputs"][STAGE_BUNDLE] == hashlib.sha256(b"stage").hexdigest()
    assert [f["bundle_name"] for f in manifest["files"]] == [STAGE_BUNDLE, SHADER_BUNDLE]
    assert manifest["l2"]["publishable"] is True
    assert manifest["l0"]["count"] == 0

    target = tmp_path / "manifests" / "stage.json"
    target.parent.mkdir(parents=True)
    persist_package_manifest(target, manifest)
    assert load_package_manifest(target) == manifest

    broken = dict(manifest)
    broken.pop("l0")
    with pytest.raises(ValueError):
        persist_package_manifest(target, broken)
    target.write_text("{not json")
    assert load_package_manifest(target) is None
    assert load_package_manifest(tmp_path / "absent.json") is None


def test_extract_collection_sync_publishes_manifest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from updater.extract.glb import collection as glb_collection

    plan = _plan()
    payloads = [(STAGE_BUNDLE, b"stage"), (SHADER_BUNDLE, b"shader")]
    manifest_path = tmp_path / "packages" / "stage.json"
    monkeypatch.setattr(
        glb_collection,
        "load_collection",
        lambda _payloads, _version: _environment(include_shader_pack=True),
    )

    result = extract_collection_sync(plan, payloads, "2022.3.21f1", str(manifest_path))

    assert result.report.publishable
    assert load_package_manifest(manifest_path)["collection_id"] == plan.collection_id()


def test_extract_collection_sync_refuses_non_publishable_package(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from updater.extract.glb import collection as glb_collection

    # Stage-alone plan: the closure never included the shader pack, and the
    # material's cross-file reference stays unresolved at collection level.
    plan = _plan(with_shader_bundle=False)
    payloads = [(STAGE_BUNDLE, b"stage")]
    manifest_path = tmp_path / "packages" / "stage.json"
    monkeypatch.setattr(
        glb_collection,
        "load_collection",
        lambda _payloads, _version: _environment(include_shader_pack=False),
    )

    with pytest.raises(PackageNotPublishable) as exc_info:
        extract_collection_sync(plan, payloads, "2022.3.21f1", str(manifest_path))

    external = [f for f in exc_info.value.report.findings if f.code == DIAG_UNRESOLVED_EXTERNAL]
    assert len(external) == 1
    assert external[0].dependency_name == SHADER_BUNDLE
    assert (external[0].source_file_index, external[0].source_path_id) == (0, 102)
    assert SHADER_BUNDLE in str(exc_info.value)
    assert not manifest_path.exists()


def test_package_not_publishable_message_identifies_source_and_target() -> None:
    environment = _environment(include_shader_pack=False)
    table = _table(environment)
    report = validate_collection_reachability(environment, table, _plan().roots)

    with pytest.raises(PackageNotPublishable) as exc_info:
        raise PackageNotPublishable(report)

    message = str(exc_info.value)
    assert "unresolved_external" in message
    assert "(0,102)" in message
    assert SHADER_BUNDLE in message


def _real_collection_inputs() -> list[tuple[str, bytes]]:
    import os
    from pathlib import Path as StdPath

    spec = os.environ.get("SEKAI_GLB_COLLECTION", "")
    if not spec:
        return []
    inputs: list[tuple[str, bytes]] = []
    for entry in spec.split(";"):
        name, _, path = entry.partition("=")
        payload = StdPath(path)
        if not payload.is_file():
            return []
        inputs.append((name, payload.read_bytes()))
    return inputs


def test_real_collection_loads_and_resolves_through_identity_table() -> None:
    """Integration: a real streaming-live collection (SEKAI_GLB_COLLECTION).

    The variable lists ``bundle-name=path`` entries separated by ``;``.  The
    externals of every loaded file must resolve through the AssetBundle
    dependency table — never by positional guesswork.
    """

    import json
    import os
    from pathlib import Path as StdPath

    inputs = _real_collection_inputs()
    if not inputs:
        pytest.skip("SEKAI_GLB_COLLECTION not set or sample bundles missing")

    environment = unity_rs_adapter.load_collection(inputs, "2022.3.21f1")
    table = _table(environment)

    assert [i.bundle_name for i in table.identities] == sorted(name for name, _ in inputs)
    assert all(i.cab_name.startswith("CAB-") for i in table.identities)

    metadata_path = os.environ.get("SEKAI_GLB_METADATA", "cache/jp/json/asset_bundle_info.json")
    metadata = json.loads(StdPath(metadata_path).read_text(encoding="utf-8"))
    for identity in table.identities:
        record = metadata["bundles"].get(identity.bundle_name)
        if record is None:
            continue
        externals = table.externals_of(identity.file_index)
        for external in externals:
            assert external in record.get("dependencies", [])
