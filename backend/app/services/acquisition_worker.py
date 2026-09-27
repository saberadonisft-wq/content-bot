"""Private JSON protocol for yt-dlp metadata; no media downloads."""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout

from .acquisition import AcquisitionError, AcquisitionManager
from .video_download_worker import guard_public_network


def main() -> None:
    try:
        request = json.load(sys.stdin)
        guard_public_network()
        # Extractor diagnostics must never corrupt the JSON protocol.
        with redirect_stdout(sys.stderr):
            items = AcquisitionManager._extract_with_yt_dlp(request)
        result = {"items": items}
    except AcquisitionError as error:
        result = {"error": str(error), "code": error.code}
    except Exception:
        result = {"error": "Không thể lấy metadata từ nguồn.", "code": "SOURCE_UNAVAILABLE"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
