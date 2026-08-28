#!/usr/bin/env python
"""Export one OpenAPI document per API version.

    uv run python scripts/export_openapi.py            # writes openapi/v1.json
    uv run python scripts/export_openapi.py --check     # CI: fail on drift

Per-version documents, not one combined file, so a client generated against v1
cannot accidentally call a v2 operation, and so a v2 release does not produce a
spurious diff in the v1 client.

Importing `app.main` builds the app but opens no database connection and loads
no model, so this is safe to run in CI with nothing else up.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from app.api.router import API_VERSIONS  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.openapi import build_version_schema  # noqa: E402
from app.main import app  # noqa: E402


def render(version_name: str) -> tuple[str, str]:
    """Return (filename, serialized document) for one version."""
    settings = get_settings()
    version = next(v for v in API_VERSIONS if v.name == version_name)
    schema = build_version_schema(
        app,
        prefix=f"{settings.api_root_prefix}{version.prefix}",
        title=f"threadline API {version.name}",
        version=version.name,
    )
    return f"{version.name}.json", json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "openapi",
        help="Directory to write per-version schemas into (default: ./openapi).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if any file on disk differs.",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    drifted: list[str] = []
    for version in API_VERSIONS:
        filename, document = render(version.name)
        target = args.output_dir / filename

        if args.check:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != document:
                drifted.append(str(target))
            continue

        target.write_text(document, encoding="utf-8")
        operations = sorted(
            operation["operationId"]
            for path in json.loads(document)["paths"].values()
            for operation in path.values()
            if isinstance(operation, dict) and "operationId" in operation
        )
        print(f"Wrote {target} ({len(operations)} operations)")
        for operation_id in operations:
            print(f"  - {operation_id}")

    if drifted:
        print(
            "OpenAPI schema is out of date:\n  "
            + "\n  ".join(drifted)
            + "\n\nRun `make openapi` and commit the result.",
            file=sys.stderr,
        )
        return 1
    if args.check:
        print("OpenAPI schemas are up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
