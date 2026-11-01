#!/usr/bin/env python3
"""Small stateless MCP JSON-RPC surface shared by HTTP and stdio transports."""

from __future__ import annotations

import json
from typing import Any

import catalog_db

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

TOOLS = [
    {
        "name": "search_samples",
        "description": (
            "Search the local sample library by words in names, transcripts, "
            "descriptions, tags, roles, and provenance, with optional catalog/state filters. "
            "Returns file paths relative to samplepack/samples/."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to find in searchable sample metadata."},
                "category": {"type": "string"},
                "role": {"type": "string"},
                "use": {"type": "string"},
                "tag": {"type": "string"},
                "reviewed": {"type": "boolean"},
                "flagged": {"type": "boolean"},
                "favorite": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "select_sample",
        "description": (
            "Select one catalogued sample by its file path from search_samples. "
            "Returns its metadata and verified absolute WAV path for use in a project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"file": {"type": "string", "description": "Catalog file path, e.g. kicks/kick_001_tight.wav."}},
            "required": ["file"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}],
        "structuredContent": value if isinstance(value, dict) else {"samples": value},
        "isError": is_error,
    }


def call_tool(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return _tool_result({"error": "tool arguments must be an object"}, is_error=True)
    try:
        if name == "search_samples":
            allowed = {key: arguments[key] for key in (
                "query", "category", "role", "use", "tag", "reviewed", "flagged", "favorite", "limit"
            ) if key in arguments}
            samples = catalog_db.search_samples(**allowed)
            return _tool_result({"count": len(samples), "samples": samples})
        if name == "select_sample":
            file_name = catalog_db.normalize_file(arguments.get("file"))
            sample = catalog_db.get_sample(file_name)
            if sample is None:
                raise ValueError(f"sample not found in catalog: {file_name}")
            full_path = (catalog_db.SAMPLES_DIR / file_name).resolve()
            full_path.relative_to(catalog_db.SAMPLES_DIR.resolve())
            if not full_path.is_file():
                raise ValueError(f"catalog sample has no audio file: {file_name}")
            return _tool_result({
                "file": file_name,
                "relative_path": f"samples/{file_name}",
                "absolute_path": str(full_path),
                "sample": sample,
            })
        return _tool_result({"error": f"unknown tool: {name}"}, is_error=True)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return _tool_result({"error": str(exc)}, is_error=True)


def handle_message(message: Any) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Notifications return None."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
    method = message.get("method")
    request_id = message.get("id")
    if not isinstance(method, str):
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32600, "message": "Missing JSON-RPC method"}}
    if request_id is None:
        return None

    params = message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "samplepack-browser", "version": "1.0.0"},
            "instructions": (
                "Use search_samples to find production-ready audio. Use select_sample to get a "
                "verified absolute WAV path. Catalog paths are relative to samplepack/samples/."
            ),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        if not isinstance(params, dict):
            result = _tool_result({"error": "params must be an object"}, is_error=True)
        else:
            result = call_tool(str(params.get("name", "")), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
