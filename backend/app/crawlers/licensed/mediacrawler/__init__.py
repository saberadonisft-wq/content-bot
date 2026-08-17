"""MediaCrawler-derived compatibility boundary.

The package is non-commercial-learning-only under the bundled license.  It is
never imported by the core registry implicitly; callers must select an
explicit licensed provider and pass through the policy guard.
"""

from .policy import LicensedReuseError, assert_licensed_reuse_allowed

__all__ = ["LicensedReuseError", "assert_licensed_reuse_allowed"]

