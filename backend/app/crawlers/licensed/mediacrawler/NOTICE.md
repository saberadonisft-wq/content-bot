# MediaCrawler reuse notice

This directory is the only approved location for source-derived MediaCrawler
implementation in Content Bot.

- Upstream snapshot: `NanmiCoder/MediaCrawler`
- Snapshot commit: `071c8c0acaece3e82f2532cffb19faeddc9ec1c3`
- Snapshot date: 2026-08-05
- License: `NON-COMMERCIAL LEARNING LICENSE 1.1`
- Copyright notice: `Copyright (c) [2024] [relakkes@gmail.com]`
- Project use: non-commercial learning/research only

The bundled `LICENSE` is part of every source-derived copy.  `SOURCE_MAP.json`
records the upstream path, local path, commit and adaptation boundary for each
copied or materially modified file.  A module is not allowed to enter the
normal CBCE registry merely because it exists here: the provider must be
explicitly selected, pass the runtime policy guard, and use the CBCE worker,
budget, cancellation, privacy and persistence boundaries.

The following upstream responsibilities remain excluded from this directory:
the MediaCrawler CLI/WebUI/API, global mutable configuration, persistence and
export sinks, proxy rotation, fingerprint/anti-detection, challenge solving,
unbounded crawling, and profile/process ownership.  Those responsibilities
belong to Content Bot or are disabled by policy.

Commercial deployment, redistribution or a change of purpose requires a new
license review and written permission from the copyright owner before the
derived code is enabled.

