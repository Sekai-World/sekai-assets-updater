from __future__ import annotations

from types import SimpleNamespace

from updater.extract.playable import extract_playable


class _Object:
    def __init__(self, path_id: int, object_type: str, data: dict) -> None:
        self.path_id = path_id
        self.type = SimpleNamespace(name=object_type)
        self._data = data

    def read_typetree(self) -> dict:
        return self._data


def _pointer(path_id: int) -> dict:
    return {"m_FileID": 0, "m_PathID": path_id}


def _script(path_id: int, class_name: str) -> _Object:
    return _Object(
        path_id,
        "MonoScript",
        {"m_ClassName": class_name, "m_Namespace": "Sekai.Test"},
    )


def test_streaming_live_and_virtual_live_mc_tracks_are_extracted() -> None:
    root = _Object(
        100,
        "MonoBehaviour",
        {
            "m_Script": _pointer(101),
            "m_Name": "timeline",
            "m_Tracks": [_pointer(200), _pointer(400)],
        },
    )
    streaming_track = _Object(
        200,
        "MonoBehaviour",
        {
            "m_Script": _pointer(201),
            "m_Name": "GlobalSettings",
            "m_Clips": [
                {
                    "m_Start": 1.25,
                    "m_ClipIn": 0.1,
                    "m_Duration": 2.5,
                    "m_TimeScale": 1.0,
                    "m_DisplayName": "RenderGlobalSettingsClip",
                    "m_Asset": _pointer(300),
                }
            ],
            "isApplyDefault": 1,
            "defaultValue": 0.5,
        },
    )
    streaming_asset = _Object(
        300,
        "MonoBehaviour",
        {
            "m_Script": _pointer(301),
            "m_Name": "RenderGlobalSettingsClip(Clone)",
            "data": {"globalIntensity": 0.75},
        },
    )
    mc_track = _Object(
        400,
        "MonoBehaviour",
        {
            "m_Script": _pointer(401),
            "m_Name": "Akito",
            "CharacterId": 7,
            "m_Clips": [
                {
                    "m_Start": 4.0,
                    "m_Duration": 1.0,
                    "m_Asset": _pointer(500),
                }
            ],
        },
    )
    mc_asset = _Object(
        500,
        "MonoBehaviour",
        {
            "m_Script": _pointer(501),
            "motionKey": "act_01",
            "facialKey": "smile",
        },
    )

    objects = [
        root,
        streaming_track,
        streaming_asset,
        mc_track,
        mc_asset,
        _script(101, "TimelineAsset"),
        _script(201, "RenderGlobalSettingsTrack"),
        _script(301, "RenderGlobalSettingsClip"),
        _script(401, "MCTimelineCharacterMotionTrack"),
        _script(501, "CharacterMotionClip"),
    ]
    environment = SimpleNamespace(
        objects=objects,
        container={"timeline.playable": root},
    )

    result = extract_playable(environment, "timeline.playable")

    timeline = result["__timelineParse"]
    assert timeline["meta"]["totalEvents"] == 2
    assert timeline["meta"]["trackEventCounts"] == {
        "RenderGlobalSettingsTrack": 1,
        "MCTimelineCharacterMotionTrack": 1,
    }

    streaming_event = next(
        event for event in timeline["events"] if event["type"] == "streamingLiveClip"
    )
    assert streaming_event["trackType"] == "RenderGlobalSettingsTrack"
    assert streaming_event["clipType"] == "RenderGlobalSettingsClip"
    assert streaming_event["end"] == 3.75
    assert streaming_event["trackData"] == {"isApplyDefault": 1, "defaultValue": 0.5}
    assert streaming_event["assetData"] == {"data": {"globalIntensity": 0.75}}

    mc_event = next(event for event in timeline["events"] if event["type"] == "motion")
    assert mc_event["character"] == "Akito"
    assert mc_event["motionKey"] == "act_01"
