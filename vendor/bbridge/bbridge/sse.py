"""Replay a complete Anthropic message as the standard SSE event sequence.

The bridge serves streaming requests in buffered mode (like zbridge's default):
it calls non-streaming Converse, keeps the client alive with ping frames, then
emits message_start, one content block at a time, message_delta, message_stop.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

PING_FRAME = b"event: ping\ndata: {\"type\":\"ping\"}\n\n"


def event(name: str, data: dict[str, Any]) -> bytes:
    return (f"event: {name}\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")


def error_frame(err_type: str, message: str) -> bytes:
    return event("error", {"type": "error", "error": {"type": err_type, "message": message}})


def replay_message(message: dict[str, Any]) -> Iterator[bytes]:
    usage = dict(message.get("usage") or {})
    start_usage = {k: v for k, v in usage.items() if k != "output_tokens"}
    start_usage["output_tokens"] = 0
    yield event("message_start", {
        "type": "message_start",
        "message": {
            "id": message.get("id", ""),
            "type": "message",
            "role": "assistant",
            "model": message.get("model", ""),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": start_usage,
        },
    })

    for index, blk in enumerate(message.get("content") or []):
        t = blk.get("type")
        if t == "thinking":
            yield event("content_block_start", {"type": "content_block_start", "index": index,
                                                "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            yield event("content_block_delta", {"type": "content_block_delta", "index": index,
                                                "delta": {"type": "thinking_delta", "thinking": blk.get("thinking", "")}})
            yield event("content_block_delta", {"type": "content_block_delta", "index": index,
                                                "delta": {"type": "signature_delta", "signature": blk.get("signature", "")}})
        elif t == "text":
            yield event("content_block_start", {"type": "content_block_start", "index": index,
                                                "content_block": {"type": "text", "text": ""}})
            yield event("content_block_delta", {"type": "content_block_delta", "index": index,
                                                "delta": {"type": "text_delta", "text": blk.get("text", "")}})
        elif t == "tool_use":
            yield event("content_block_start", {"type": "content_block_start", "index": index,
                                                "content_block": {"type": "tool_use", "id": blk.get("id", ""),
                                                                  "name": blk.get("name", ""), "input": {}}})
            yield event("content_block_delta", {"type": "content_block_delta", "index": index,
                                                "delta": {"type": "input_json_delta",
                                                          "partial_json": json.dumps(blk.get("input") or {}, ensure_ascii=False)}})
        else:
            continue
        yield event("content_block_stop", {"type": "content_block_stop", "index": index})

    yield event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message.get("stop_reason", "end_turn"), "stop_sequence": None},
        "usage": usage,
    })
    yield event("message_stop", {"type": "message_stop"})
