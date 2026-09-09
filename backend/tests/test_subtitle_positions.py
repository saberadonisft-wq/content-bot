import pytest
from pydantic import ValidationError

from app.schemas import SubtitleDocumentV2
from app.services.subtitle_timing import transform_project_cues
from app.services.subtitle_track import subtitles_to_matroska
from app.services.subtitles import subtitles_to_ass


def document():
    return SubtitleDocumentV2.model_validate(
        {
            "segments": [
                {
                    "id": "first",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "text": "First",
                    "layout": {"x": 20, "y": 30},
                },
                {
                    "id": "second",
                    "start_ms": 1000,
                    "end_ms": 2000,
                    "text": "Second",
                    "layout": {"x": 70, "y": 80},
                },
                {"id": "legacy", "start_ms": 2000, "end_ms": 3000, "text": "Legacy"},
            ]
        }
    )


def test_positions_survive_validation_preview_and_precision_export():
    cues = document().model_dump()["segments"]
    options = {"position": "custom", "pos_x": 50, "pos_y": 78, "animation": "none"}
    ass = subtitles_to_ass(cues, options, play_res_x=1000, play_res_y=1000)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    for event, expected in zip(
        events, [r"\pos(200,300)", r"\pos(700,800)", r"\pos(500,780)"]
    ):
        assert expected in event
    transformed = transform_project_cues(cues, trim_start_ms=500, video_speed=2)
    assert transformed[0]["layout"] == {"x": 20, "y": 30}
    assert transformed[1]["layout"] == {"x": 70, "y": 80}
    track = subtitles_to_matroska(cues, options, play_res_x=1000, play_res_y=1000)
    assert b"\\pos(200,300)" in track
    assert b"\\pos(700,800)" in track


@pytest.mark.parametrize(
    "layout", [{"x": -1, "y": 50}, {"x": 50, "y": 101}, {"x": float("nan"), "y": 50}]
)
def test_rejects_invalid_positions(layout):
    payload = document().model_dump()
    payload["segments"][0]["layout"] = layout
    with pytest.raises(ValidationError):
        SubtitleDocumentV2.model_validate(payload)
