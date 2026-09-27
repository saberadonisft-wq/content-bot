from unittest.mock import MagicMock, patch

import pytest

from app.schemas import SubtitleOcrRegion
from app.services.video_masking import (
    VideoMaskingError,
    apply_hardsub_mask,
    generate_hardsub_mask_filter,
)


def test_generate_hardsub_mask_filter_blur():
    region = SubtitleOcrRegion(x=10.0, y=75.0, width=80.0, height=15.0)
    filter_str = generate_hardsub_mask_filter(region, mode="blur", orig_w=1280, orig_h=720, blur_power=20)

    # 10% of 1280 = 128; 75% of 720 = 540; 80% of 1280 = 1024; 15% of 720 = 108
    assert "crop=1024:108:128:540" in filter_str
    assert "boxblur=20:3" in filter_str
    assert "overlay=128:540" in filter_str


def test_generate_hardsub_mask_filter_patch():
    region = SubtitleOcrRegion(x=5.0, y=70.0, width=90.0, height=20.0)
    filter_str = generate_hardsub_mask_filter(region, mode="patch", orig_w=1920, orig_h=1080, patch_color="black@0.8")

    assert "drawbox=x=96:y=756:w=1728:h=216:color=black@0.8:t=fill" in filter_str


def test_apply_hardsub_mask_calls_ffmpeg(tmp_path):
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"dummy video")
    output_video = tmp_path / "output.mp4"
    region = SubtitleOcrRegion(x=10, y=75, width=80, height=15)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = apply_hardsub_mask(input_video, output_video, region, mode="blur")
        assert result == output_video
        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert "ffmpeg" in cmd[0]
        assert str(input_video) in cmd
        assert str(output_video) in cmd


def test_apply_hardsub_mask_missing_file(tmp_path):
    missing_video = tmp_path / "non_existent.mp4"
    output_video = tmp_path / "out.mp4"
    region = SubtitleOcrRegion(x=10, y=75, width=80, height=15)

    with pytest.raises(VideoMaskingError, match="Input video not found"):
        apply_hardsub_mask(missing_video, output_video, region)
