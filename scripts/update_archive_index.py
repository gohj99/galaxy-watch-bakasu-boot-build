#!/usr/bin/env python3
"""Refresh the repository's compact Archive.org Samsung source index."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from archive_index import INDEX_PATH, build_index
from find_source_links import Client
from common import write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=INDEX_PATH)
    args = parser.parse_args()
    index = build_index(Client(), datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    temporary = args.output.with_suffix(args.output.suffix + '.tmp')
    write_json(temporary, index)
    temporary.replace(args.output)
    print(f"Wrote {index['item_count']} Archive.org items to {args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
