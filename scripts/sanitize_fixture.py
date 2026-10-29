"""Turn a saved portal page into a safe, offline test fixture.

    uv run python scripts/sanitize_fixture.py <in.html> <out.html> [--replace OLD=NEW ...]

Removes scripts, external stylesheets/preloads, hidden input values, CSRF and
analytics meta tags, and replaces account identifiers you name.
"""

import argparse
import re
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("src")
ap.add_argument("dst")
ap.add_argument("--replace", action="append", default=[], metavar="OLD=NEW")
a = ap.parse_args()

s = Path(a.src).read_text(encoding="utf-8")
s = re.sub(r"<script\b.*?</script>", "", s, flags=re.DOTALL | re.IGNORECASE)
s = re.sub(r"<link\b[^>]*>", "", s, flags=re.IGNORECASE)
s = re.sub(r'(<input\b[^>]*type="hidden"[^>]*\bvalue=")[^"]*(")', r"\1REDACTED\2", s, flags=re.IGNORECASE)
s = re.sub(r'(<input\b[^>]*\bvalue=")[^"]*("[^>]*type="hidden")', r"\1REDACTED\2", s, flags=re.IGNORECASE)
s = re.sub(r'<meta\b[^>]*name="(?:csrf-token|request-id|html-safe-nonce|visitor-[\w-]+|'
           r'octolytics-[\w-]+|analytics-[\w-]+|user-login|fetch-nonce|release)"[^>]*>', "", s,
           flags=re.IGNORECASE)
s = re.sub(r'\s(?:src|srcset)="https://(?:avatars|private-avatars)[^"]*"', "", s)
s = re.sub(r'<meta\b[^>]*name="[\w:-]*(?:nonce|token|hmac|csrf|session|payload)[\w:-]*"[^>]*>', "",
           s, flags=re.IGNORECASE)
s = re.sub(r"(avatars\.githubusercontent\.com/u/)\d+", r"\g<1>0", s)
s = re.sub(r"\b((?:repository|user|organization):)\d+", r"\g<1>0", s)
s = re.sub(r'(\bdata-[\w-]*(?:repository|user|owner|org)[\w-]*-id=")\d+(")', r"\g<1>0\2", s,
           flags=re.IGNORECASE)
for pair in a.replace:
    old, new = pair.split("=", 1)
    s = re.sub(re.escape(old), new, s, flags=re.IGNORECASE)
Path(a.dst).write_text(s, encoding="utf-8")
print(f"{a.dst}: {len(s)} bytes")
