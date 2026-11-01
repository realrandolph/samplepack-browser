#!/usr/bin/env python3
"""Run the samplepack MCP server over the standard local stdio transport."""

from __future__ import annotations

import json
import sys

import catalog_db
import mcp_protocol


def main() -> int:
    catalog_db.migrate_legacy_files()
    catalog_db.generate_indexes()
    for raw in sys.stdin.buffer:
        try:
            message = json.loads(raw.decode("utf-8"))
            response = mcp_protocol.handle_message(message)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse or dispatch error: {exc}"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
