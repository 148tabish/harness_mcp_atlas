"""Anthropic /v1/messages <-> Amazon Bedrock Converse translation.

Pure functions: no I/O. The client side is the shape zbridge presents, so the
harness's runner sees the same blocks on both GLM routes: signed thinking
blocks, text, tool_use, Anthropic usage.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

IMAGE_NOTE = "[image omitted: this model cannot view images]"
EMPTY_TEXT = "(empty)"

UNSUPPORTED_FIELDS = ("n", "logprobs", "seed", "service_tier")

# Bedrock maps `reasoning_effort` for zai models to low|medium|high|xhigh|max and
# refuses to switch reasoning off, so a request without thinking sends nothing
# and still gets reasoning blocks back.
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "guardrail_intervened": "end_turn",
    "content_filtered": "end_turn",
    "model_context_window_exceeded": "max_tokens",
}


class TranslationError(Exception):
    def __init__(self, message: str, error_type: str = "invalid_request_error"):
        super().__init__(message)
        self.error_type = error_type


# ---------------------------------------------------------------------------
# Request: Anthropic -> Converse
# ---------------------------------------------------------------------------

def map_reasoning_effort(budget_tokens: int) -> str:
    """Anthropic budget_tokens -> Bedrock reasoning_effort for GLM.

    LiteLLM sends 1024 / 2048 / 4096 for reasoning_effort low / medium / high, so
    the harness's `--effort high` lands on "high" here.
    """
    if budget_tokens < 2048:
        return "low"
    if budget_tokens < 4096:
        return "medium"
    if budget_tokens < 8192:
        return "high"
    if budget_tokens < 16384:
        return "xhigh"
    return "max"


def anthropic_to_converse_request(
    body: dict[str, Any],
    reasoning_effort_override: str | None = None,
) -> dict[str, Any]:
    """Build the Converse JSON body. The model ARN lives in the URL, not the body."""
    for field in UNSUPPORTED_FIELDS:
        if field in body:
            raise TranslationError(f"unsupported field: {field}")
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_schema":
        raise TranslationError("unsupported field: response_format=json_schema")
    if "max_tokens" not in body:
        raise TranslationError("max_tokens is required")

    tools = body.get("tools") or []
    has_tools = bool(tools)
    out: dict[str, Any] = {
        "messages": _build_messages(body.get("messages") or [], has_tools),
    }

    system = _system_text(body.get("system"))
    if system:
        out["system"] = [{"text": system}]

    inf: dict[str, Any] = {"maxTokens": int(body["max_tokens"])}
    if "temperature" in body and body["temperature"] is not None:
        inf["temperature"] = float(body["temperature"])
    if "top_p" in body and body["top_p"] is not None:
        inf["topP"] = max(0.01, min(1.0, float(body["top_p"])))
    stops = body.get("stop_sequences")
    if isinstance(stops, list) and stops:
        inf["stopSequences"] = [str(s) for s in stops][:4]
    out["inferenceConfig"] = inf

    if has_tools:
        tool_cfg: dict[str, Any] = {"tools": _translate_tools(tools)}
        choice = _translate_tool_choice(body.get("tool_choice"))
        if choice is not None:
            tool_cfg["toolChoice"] = choice
        out["toolConfig"] = tool_cfg

    effort = None
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        effort = map_reasoning_effort(int(thinking.get("budget_tokens", 4096) or 4096))
    if reasoning_effort_override in REASONING_EFFORTS:
        effort = reasoning_effort_override
    if effort:
        out["additionalModelRequestFields"] = {"reasoning_effort": effort}

    return out


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system.strip()
    if isinstance(system, list):
        parts = [b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"]
        return "\n\n".join(p for p in parts if p).strip()
    return ""


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if "input_schema" not in t:
            raise TranslationError(f"tool {t.get('name')!r} missing input_schema")
        schema = t["input_schema"] if isinstance(t["input_schema"], dict) else {"type": "object"}
        if "type" not in schema:
            schema = {"type": "object", **schema}
        out.append({"toolSpec": {
            "name": t["name"],
            "description": (t.get("description") or t["name"]),   # Bedrock needs a non-empty description
            "inputSchema": {"json": schema},
        }})
    return out


def _translate_tool_choice(choice: Any) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        choice = {"type": choice}
    if not isinstance(choice, dict):
        return None
    t = choice.get("type")
    if t == "auto":
        return {"auto": {}}
    if t == "any":
        return {"any": {}}
    if t == "tool" and choice.get("name"):
        return {"tool": {"name": choice["name"]}}
    if t == "none":
        return None
    return None


def _build_messages(messages: list[Any], has_tools: bool) -> list[dict[str, Any]]:
    """Converse wants strictly alternating user/assistant turns, each with at least
    one non-blank block. Adjacent same-role messages are merged; blank blocks drop."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        blocks = (_user_blocks(msg.get("content"), has_tools) if role == "user"
                  else _assistant_blocks(msg.get("content"), has_tools))
        if not blocks:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    if not out:
        out.append({"role": "user", "content": [{"text": EMPTY_TEXT}]})
    if out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"text": "(continue)"}]})
    # Bedrock refuses a toolResult block that sits after a text block in the same turn.
    for m in out:
        if m["role"] == "user":
            m["content"].sort(key=lambda b: 0 if "toolResult" in b else 1)
    return out


def _text_block(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, str) or not text.strip():
        return None
    return {"text": text}


def _tool_result_text(content: Any) -> str:
    """The text a tool returned. Images become a note (text-only model)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") == "image":
                    parts.append(IMAGE_NOTE)
                else:
                    parts.append(str(blk.get("text", "")))
            else:
                parts.append(str(blk))
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _user_blocks(content: Any, has_tools: bool) -> list[dict[str, Any]]:
    if isinstance(content, str):
        b = _text_block(content)
        return [b] if b else []
    if not isinstance(content, list):
        b = _text_block(str(content))
        return [b] if b else []
    out: list[dict[str, Any]] = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        t = blk.get("type")
        if t == "text":
            b = _text_block(blk.get("text"))
            if b:
                out.append(b)
        elif t == "image":
            out.append({"text": IMAGE_NOTE})
        elif t == "tool_result":
            text = _tool_result_text(blk.get("content")) or EMPTY_TEXT
            if has_tools:
                tr: dict[str, Any] = {"toolUseId": blk.get("tool_use_id", ""),
                                      "content": [{"text": text}]}
                tr["status"] = "error" if blk.get("is_error") else "success"
                out.append({"toolResult": tr})
            else:
                # no toolConfig in this request: Bedrock would reject a toolResult block
                out.append({"text": f"[result of tool call {blk.get('tool_use_id', '')}]\n{text}"})
    return out


def _assistant_blocks(content: Any, has_tools: bool) -> list[dict[str, Any]]:
    if isinstance(content, str):
        b = _text_block(content)
        return [b] if b else []
    if not isinstance(content, list):
        b = _text_block(str(content))
        return [b] if b else []
    out: list[dict[str, Any]] = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        t = blk.get("type")
        if t == "text":
            b = _text_block(blk.get("text"))
            if b:
                out.append(b)
        elif t == "thinking":
            think = blk.get("thinking", "")
            if isinstance(think, str) and think.strip():
                out.append({"reasoningContent": {"reasoningText": {"text": think}}})
        elif t == "tool_use":
            inp = blk.get("input")
            if not isinstance(inp, dict):
                inp = {}
            if has_tools:
                out.append({"toolUse": {"toolUseId": blk.get("id", ""), "name": blk.get("name", ""), "input": inp}})
            else:
                out.append({"text": f"[tool call {blk.get('name', '')}({json.dumps(inp, ensure_ascii=False)})]"})
    return out


# ---------------------------------------------------------------------------
# Response: Converse -> Anthropic Message
# ---------------------------------------------------------------------------

def sign_thinking(text: str, key: bytes | None) -> str:
    if not key:
        return ""
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def map_usage(usage_in: dict[str, Any] | None) -> dict[str, Any]:
    """Converse usage -> Anthropic usage. Bedrock reports inputTokens without the
    cached portion (Anthropic semantics), so the fields map one to one."""
    u = usage_in or {}

    def n(k: str) -> int:
        try:
            return max(0, int(u.get(k) or 0))
        except (TypeError, ValueError):
            return 0

    usage: dict[str, Any] = {"input_tokens": n("inputTokens"), "output_tokens": n("outputTokens")}
    if "cacheReadInputTokens" in u or "cacheWriteInputTokens" in u:
        usage["cache_read_input_tokens"] = n("cacheReadInputTokens")
        usage["cache_creation_input_tokens"] = n("cacheWriteInputTokens")
    return usage


def converse_to_anthropic_response(
    body: dict[str, Any],
    model: str,
    thinking_sig_key: bytes | None = None,
) -> dict[str, Any]:
    msg = ((body.get("output") or {}).get("message") or {})
    blocks: list[dict[str, Any]] = []
    for blk in msg.get("content") or []:
        if not isinstance(blk, dict):
            continue
        if "reasoningContent" in blk:
            rc = blk["reasoningContent"] or {}
            text = ((rc.get("reasoningText") or {}).get("text") or "")
            if text:
                blocks.append({"type": "thinking", "thinking": text,
                               "signature": sign_thinking(text, thinking_sig_key)})
        elif "text" in blk:
            if blk["text"]:
                blocks.append({"type": "text", "text": blk["text"]})
        elif "toolUse" in blk:
            tu = blk["toolUse"] or {}
            inp = tu.get("input")
            if not isinstance(inp, dict):
                inp = {}
            blocks.append({"type": "tool_use", "id": tu.get("toolUseId", "") or f"toolu_{secrets.token_hex(8)}",
                           "name": tu.get("name", ""), "input": inp})

    stop = STOP_REASON_MAP.get(str(body.get("stopReason") or "end_turn"), "end_turn")
    if any(b["type"] == "tool_use" for b in blocks) and stop == "end_turn":
        stop = "tool_use"

    return {
        "id": f"msg_{secrets.token_hex(12)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": map_usage(body.get("usage")),
    }
