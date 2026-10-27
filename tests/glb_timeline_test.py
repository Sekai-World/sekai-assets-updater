"""GLB Phase 6 tests: Timeline resolution into playable documents (#40)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from updater import unity_rs_adapter
from updater.extract.glb import DIAG_TRACK_UNSUPPORTED, build_timeline_document, find_playable_roots
from updater.extract.glb.timeline import (
    load_timeline_document,
    persist_timeline_document,
    timeline_reference_problems,
    validate_timeline_document,
)

STAGE_BUNDLE = "timeline/mc_bundle"
STAGE_SOURCE = f"{STAGE_BUNDLE}::CAB-a"
PLAYABLE_PATH = "ondemand/virtual_live/mc/timeline/mc_x/mc_x.playable"


def _mb(path_id, script_path_id, extra=None, name=None):
    """MonoBehaviour typetree with Unity boilerplate."""

    data = {
        "m_GameObject": {"m_FileID": 0, "m_PathID": 0},
        "m_Enabled": 1,
        "m_Script": {"m_FileID": 0, "m_PathID": script_path_id},
        "m_Name": name or "",
    }
    data.update(extra or {})
    return data


class _TimelineStudio:
    """TimelineAsset -> GroupTrack -> (spawn track, unknown track) with clips."""

    def __init__(self) -> None:
        # MonoScripts: 10 TimelineAsset, 11 GroupTrack, 12 SpawnTrack,
        # 13 UnknownTrack, 14 CharacterSpawnClip, 15 AnimationPlayableAsset
        self._objects: list[SimpleNamespace] = []
        self._typetrees: dict[tuple[int, int], dict] = {}
        self._container: list[tuple[str, int, int, tuple[int, int]]] = []

        def add(path_id, class_id, typetree, name=None):
            self._objects.append(
                SimpleNamespace(
                    file_index=0,
                    object_index=len(self._objects),
                    path_id=path_id,
                    class_id=class_id,
                    name=name,
                    container=None,
                    source_path=STAGE_SOURCE,
                )
            )
            self._typetrees[(0, path_id)] = typetree

        add(10, 115, {"m_ClassName": "TimelineAsset", "m_Namespace": "UnityEngine.Timeline"})
        add(11, 115, {"m_ClassName": "GroupTrack", "m_Namespace": "UnityEngine.Timeline"})
        add(12, 115, {"m_ClassName": "MCTimelineCharacterSpawnTrack", "m_Namespace": ""})
        add(13, 115, {"m_ClassName": "SekaiBrandNewTrack", "m_Namespace": ""})
        add(14, 115, {"m_ClassName": "CharacterSpawnClip", "m_Namespace": ""})
        add(
            15,
            115,
            {"m_ClassName": "AnimationPlayableAsset", "m_Namespace": "UnityEngine.Timeline"},
        )

        add(
            100,
            114,
            _mb(
                100,
                10,
                {
                    "m_Tracks": [{"m_FileID": 0, "m_PathID": 200}],
                    "m_FixedDuration": 4.5,
                    "m_DurationMode": 1,
                },
                name="mc_x",
            ),
            name="mc_x",
        )
        add(
            200,
            114,
            _mb(
                200,
                11,
                {
                    "m_Children": [
                        {"m_FileID": 0, "m_PathID": 201},
                        {"m_FileID": 0, "m_PathID": 202},
                    ],
                    "m_Clips": [],
                },
                name="Characters",
            ),
        )
        add(
            201,
            114,
            _mb(
                201,
                12,
                {
                    "m_Clips": [
                        {
                            "m_Start": 0.0,
                            "m_Duration": 1.5,
                            "m_ClipIn": 0.0,
                            "m_TimeScale": 1.0,
                            "m_DisplayName": "Enter",
                            "m_Asset": {"m_FileID": 0, "m_PathID": 300},
                        }
                    ]
                },
                name="ミク_入場",
            ),
        )
        add(
            202,
            114,
            _mb(
                202,
                13,
                {
                    "m_Clips": [
                        {
                            "m_Start": 2.0,
                            "m_Duration": 0.5,
                            "m_ClipIn": 0.0,
                            "m_TimeScale": 1.0,
                            "m_DisplayName": "Mystery",
                            "m_Asset": {"m_FileID": 0, "m_PathID": 301},
                        }
                    ]
                },
                name="unknown_track",
            ),
        )
        add(
            300,
            114,
            _mb(
                300,
                14,
                {
                    "CharacterId": 25,
                    "Character3dId": 25,
                    "motionKey": "stand_idle",
                    "facialKey": "smile",
                },
                name="spawn_asset",
            ),
        )
        add(
            301,
            114,
            _mb(
                301,
                15,
                {
                    "m_Clip": {"m_FileID": 0, "m_PathID": 400},
                    "m_Position": {"x": 0, "y": 0, "z": 0},
                },
            ),
        )
        add(400, 74, {"m_Name": "dance_loop", "m_SampleRate": 60.0}, name="dance_loop")

        self._objects.append(
            SimpleNamespace(
                file_index=0,
                object_index=len(self._objects),
                path_id=1,
                class_id=142,
                name=STAGE_BUNDLE,
                container=None,
                source_path=STAGE_SOURCE,
            )
        )
        self._typetrees[(0, 1)] = {"m_Name": STAGE_BUNDLE}
        self._container = [(PLAYABLE_PATH, 0, 1, (0, 100))]

    def add_script(self, path_id, class_name):
        self._objects.append(
            SimpleNamespace(
                file_index=0,
                object_index=len(self._objects),
                path_id=path_id,
                class_id=115,
                name=None,
                container=None,
                source_path=STAGE_SOURCE,
            )
        )
        self._typetrees[(0, path_id)] = {"m_ClassName": class_name, "m_Namespace": ""}

    def add_track(self, path_id, script_path_id, extra=None, name=None):
        self._objects.append(
            SimpleNamespace(
                file_index=0,
                object_index=len(self._objects),
                path_id=path_id,
                class_id=114,
                name=name,
                container=None,
                source_path=STAGE_SOURCE,
            )
        )
        self._typetrees[(0, path_id)] = _mb(path_id, script_path_id, extra, name)

    def files(self):
        return [SimpleNamespace(index=0, path=STAGE_SOURCE, unity_version="2022.3.21f1")]

    def objects(self):
        return iter(self._objects)

    def scene(self, *, limits=None):
        return []

    def read_asset_bundle(self, _file_index: int, _path_id: int):
        return SimpleNamespace(container=self._container, dependencies=[])

    def read_type_tree_json(self, file_index: int, path_id: int):
        import orjson

        return orjson.dumps(self._typetrees[(file_index, path_id)])


def _timeline_environment():
    return unity_rs_adapter.UnityRsEnvironment(_TimelineStudio())


def _small_table(environment):
    # The fake studio models one file with no serialized externals. Keep this
    # declaration independent of AssetBundle.dependencies, which is unrelated.
    return unity_rs_adapter.build_collection_file_table(environment, serialized_externals={0: ()})


def test_find_playable_roots_returns_container_entries() -> None:
    environment = _timeline_environment()

    roots = find_playable_roots(environment)

    assert [(path, (obj.file_index, obj.path_id)) for path, obj in roots] == [
        (PLAYABLE_PATH, (0, 100))
    ]


def test_timeline_document_resolves_tracks_clips_and_references() -> None:
    environment = _timeline_environment()
    table = _small_table(environment)

    document = build_timeline_document(environment, table, "pkg/mc_x", PLAYABLE_PATH)

    assert document["timeline_name"] == "mc_x"
    assert document["bundle_name"] == STAGE_BUNDLE
    assert document["fixed_duration"] == 4.5
    assert document["duration"] == 2.5  # 0 + 1.5 and 2 + 0.5
    classes = [track["track_class"] for track in document["tracks"]]
    # GroupTrack is structural and never appears as a track record.
    assert classes == ["MCTimelineCharacterSpawnTrack", "SekaiBrandNewTrack"]
    supported = [track["supported"] for track in document["tracks"]]
    assert supported == [True, False]
    spawn = document["tracks"][0]
    clip = spawn["clips"][0]
    assert clip["start"] == 0.0 and clip["end"] == 1.5
    assert clip["display_name"] == "Enter"
    assert clip["asset"]["resolved"] is True
    assert clip["asset"]["target_class"] == "CharacterSpawnClip"
    assert clip["asset_data"]["CharacterId"] == 25
    assert clip["asset_data"]["motionKey"] == "stand_idle"
    # Characters collect stable identities, not derived names.
    assert document["characters"] == [
        {"character_id": 25, "character3d_id": 25, "source_track_path_ids": [201]}
    ]
    # The AnimationPlayableAsset's m_Clip resolves into an animation reference.
    assert document["animation_references"] == [
        {
            "bundle_name": STAGE_BUNDLE,
            "file_index": 0,
            "path_id": 400,
            "clip_name": "dance_loop",
            "source_track_path_ids": [202],
        }
    ]
    codes = [diagnostic["code"] for diagnostic in document["diagnostics"]]
    assert codes == [DIAG_TRACK_UNSUPPORTED]
    assert "SekaiBrandNewTrack" in document["diagnostics"][0]["message"]
    assert "1 clips recorded verbatim" in document["diagnostics"][0]["message"]
    # The unknown track's clips stay in the document — nothing is lost.
    assert document["tracks"][1]["clips"][0]["display_name"] == "Mystery"


def test_non_group_track_children_are_resolved_with_parent_clips() -> None:
    studio = _TimelineStudio()
    studio.add_script(16, "AnimationTrack")
    studio.add_track(
        203,
        16,
        {
            "m_Clips": [
                {
                    "m_Start": 0.0,
                    "m_Duration": 1.0,
                    "m_Asset": {"m_FileID": 0, "m_PathID": 301},
                }
            ],
            "m_Children": [{"m_FileID": 0, "m_PathID": 204}],
        },
        name="Animation parent",
    )
    studio.add_track(
        204,
        16,
        {
            "m_Clips": [
                {
                    "m_Start": 1.0,
                    "m_Duration": 1.5,
                    "m_Asset": {"m_FileID": 0, "m_PathID": 301},
                }
            ]
        },
        name="Animation child",
    )
    studio._typetrees[(0, 200)]["m_Children"] = [{"m_FileID": 0, "m_PathID": 203}]
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    table = _small_table(environment)

    document = build_timeline_document(environment, table, "pkg/mc_x", PLAYABLE_PATH)

    assert [(track["path_id"], track["track_class"]) for track in document["tracks"]] == [
        (203, "AnimationTrack"),
        (204, "AnimationTrack"),
    ]
    assert [len(track["clips"]) for track in document["tracks"]] == [1, 1]
    assert document["animation_references"][0]["source_track_path_ids"] == [203, 204]


def test_nested_groups_keep_unknown_tracks_and_their_children() -> None:
    studio = _TimelineStudio()
    studio.add_script(16, "GroupTrack")
    studio.add_track(205, 16, {"m_Children": [{"m_FileID": 0, "m_PathID": 202}]})
    studio._typetrees[(0, 200)]["m_Children"] = [{"m_FileID": 0, "m_PathID": 205}]
    studio._typetrees[(0, 202)]["m_Children"] = [{"m_FileID": 0, "m_PathID": 201}]
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    table = _small_table(environment)

    document = build_timeline_document(environment, table, "pkg/mc_x", PLAYABLE_PATH)

    assert [(track["path_id"], track["supported"]) for track in document["tracks"]] == [
        (201, True),
        (202, False),
    ]
    assert all(track["clips"] for track in document["tracks"])
    assert all(track["track_class"] != "GroupTrack" for track in document["tracks"])
    assert any("SekaiBrandNewTrack" in item["message"] for item in document["diagnostics"])


def test_shared_track_is_deduplicated_and_cycles_are_diagnosed() -> None:
    studio = _TimelineStudio()
    studio.add_script(16, "GroupTrack")
    studio.add_track(205, 16, {"m_Children": [{"m_FileID": 0, "m_PathID": 201}]})
    studio.add_track(
        206,
        16,
        {
            "m_Children": [
                {"m_FileID": 0, "m_PathID": 201},
                {"m_FileID": 0, "m_PathID": 200},
            ]
        },
    )
    studio._typetrees[(0, 200)]["m_Children"] = [
        {"m_FileID": 0, "m_PathID": 205},
        {"m_FileID": 0, "m_PathID": 206},
    ]
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    table = _small_table(environment)

    document = build_timeline_document(environment, table, "pkg/mc_x", PLAYABLE_PATH)

    assert [track["path_id"] for track in document["tracks"]] == [201]
    cycle_diagnostics = [
        item for item in document["diagnostics"] if "forms a child cycle" in item["message"]
    ]
    assert len(cycle_diagnostics) == 1
    assert cycle_diagnostics[0]["track_path_id"] == 200


def test_unresolved_track_reference_is_diagnosed() -> None:
    studio = _TimelineStudio()
    studio._typetrees[(0, 100)]["m_Tracks"] = [{"m_FileID": 0, "m_PathID": 999}]
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    table = _small_table(environment)

    document = build_timeline_document(environment, table, "pkg/mc_x", PLAYABLE_PATH)

    assert document["tracks"] == []
    assert len(document["diagnostics"]) == 1
    assert "did not resolve" in document["diagnostics"][0]["message"]
    assert document["diagnostics"][0]["track_path_id"] == 999


@pytest.mark.parametrize(
    "track_ref",
    [
        pytest.param(None, id="none-reference"),
        pytest.param("not-a-pptr", id="non-mapping-reference"),
        pytest.param({"m_FileID": None, "m_PathID": 1}, id="invalid-file-id"),
        pytest.param({"m_FileID": 0, "m_PathID": "not-an-integer"}, id="invalid-path-id"),
    ],
)
def test_invalid_track_references_are_diagnosed(track_ref) -> None:
    studio = _TimelineStudio()
    studio._typetrees[(0, 100)]["m_Tracks"] = [track_ref]
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    table = _small_table(environment)

    document = build_timeline_document(environment, table, "pkg/mc_x", PLAYABLE_PATH)

    assert document["tracks"] == []
    assert len(document["diagnostics"]) == 1
    assert document["diagnostics"][0]["code"] == DIAG_TRACK_UNSUPPORTED


def test_timeline_document_round_trips_and_rejects_corruption(tmp_path) -> None:
    environment = _timeline_environment()
    table = _small_table(environment)
    document = build_timeline_document(environment, table, "pkg/mc_x", PLAYABLE_PATH)

    path = tmp_path / "timeline.playable.json"
    persist_timeline_document(path, document)
    assert load_timeline_document(path) == document

    assert load_timeline_document(tmp_path / "absent.json") is None
    path.write_text("{broken", encoding="utf-8")
    assert load_timeline_document(path) is None

    broken = dict(document)
    broken.pop("content_hash")
    with pytest.raises(ValueError, match="fields must be exactly"):
        validate_timeline_document(broken)
    wrong_kind = dict(document)
    wrong_kind["kind"] = "other"
    with pytest.raises(ValueError, match="kind"):
        validate_timeline_document(wrong_kind)


def test_timeline_reference_problems_flags_incomplete_references() -> None:
    environment = _timeline_environment()
    table = _small_table(environment)
    document = build_timeline_document(environment, table, "pkg/mc_x", PLAYABLE_PATH)
    assert timeline_reference_problems(document) == []

    missing_bundle = {
        "bundle_name": None,
        "file_index": 0,
        "path_id": 400,
        "clip_name": "dance_loop",
        "source_track_path_ids": [202],
    }
    document["animation_references"].append(missing_bundle)
    problems = timeline_reference_problems(document)
    assert any("no bundle name" in problem for problem in problems)
    document["tracks"].append(dict(document["tracks"][0]))
    problems = timeline_reference_problems(document)
    assert any("duplicate track identities" in problem for problem in problems)


def test_real_timeline_bundle_resolves_when_env_set(tmp_path) -> None:
    """Integration: a real MC timeline (SEKAI_GLB_TIMELINE=bundle=path)."""

    import os
    from pathlib import Path

    spec = os.environ.get("SEKAI_GLB_TIMELINE", "")
    if not spec:
        pytest.skip("SEKAI_GLB_TIMELINE not set or sample bundle missing")
    name, _, raw_path = spec.partition("=")
    bundle = Path(raw_path)
    if not bundle.is_file():
        pytest.skip("SEKAI_GLB_TIMELINE sample bundle missing")

    environment = unity_rs_adapter.load_collection([(name, bundle.read_bytes())], "2022.3.21f1")
    table = unity_rs_adapter.build_collection_file_table(environment)
    roots = find_playable_roots(environment)
    assert roots, "expected at least one .playable container entry"

    for container_path, _root in roots:
        document = build_timeline_document(environment, table, "real/mc", container_path)
        assert document["tracks"], "expected resolved tracks"
        assert document["content_hash"]
        assert timeline_reference_problems(document) == []
        persist_timeline_document(tmp_path / "timeline.playable.json", document)
        assert load_timeline_document(tmp_path / "timeline.playable.json") == document
