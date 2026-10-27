import json

import pytest

from bbridge.sse import replay_message
from bbridge.translate import (IMAGE_NOTE, TranslationError, anthropic_to_converse_request,
                               converse_to_anthropic_response, map_reasoning_effort, map_usage)

TOOLS = [{"name": "get_weather", "description": "Get weather",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]


def test_request_basic_shape():
    out = anthropic_to_converse_request({
        "model": "glm-5.3", "max_tokens": 100, "temperature": 0.2, "top_p": 0.9,
        "system": [{"type": "text", "text": "Be terse.", "cache_control": {"type": "ephemeral"}}],
        "stop_sequences": ["###"],
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert out["system"] == [{"text": "Be terse."}]
    assert out["messages"] == [{"role": "user", "content": [{"text": "hi"}]}]
    assert out["inferenceConfig"] == {"maxTokens": 100, "temperature": 0.2, "topP": 0.9, "stopSequences": ["###"]}
    assert out["additionalModelRequestFields"] == {"reasoning_effort": "high"}
    assert "toolConfig" not in out


def test_request_tools_and_round_trip():
    out = anthropic_to_converse_request({
        "model": "glm-5.3", "max_tokens": 100, "tools": TOOLS, "tool_choice": {"type": "auto"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Weather in Pune?"}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "call the tool", "signature": "abc"},
                {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Pune"}}]},
            {"role": "user", "content": [
                {"type": "text", "text": "note after"},
                {"type": "tool_result", "tool_use_id": "call_1", "content": [{"type": "text", "text": "31C"}], "is_error": True}]},
        ],
    })
    assert out["toolConfig"]["tools"][0]["toolSpec"]["name"] == "get_weather"
    assert out["toolConfig"]["toolChoice"] == {"auto": {}}
    a = out["messages"][1]["content"]
    assert a[0] == {"reasoningContent": {"reasoningText": {"text": "call the tool"}}}
    assert a[1] == {"toolUse": {"toolUseId": "call_1", "name": "get_weather", "input": {"city": "Pune"}}}
    u = out["messages"][2]["content"]
    assert "toolResult" in u[0] and u[0]["toolResult"]["status"] == "error"   # toolResult sorted first
    assert u[1] == {"text": "note after"}


def test_request_merges_same_role_and_drops_blank():
    out = anthropic_to_converse_request({"model": "x", "max_tokens": 1, "messages": [
        {"role": "user", "content": "a"}, {"role": "user", "content": "   "}, {"role": "user", "content": "b"},
        {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]},
    ]})
    assert out["messages"] == [{"role": "user", "content": [{"text": "a"}, {"text": "b"}, {"text": IMAGE_NOTE}]}]


def test_request_tool_blocks_without_tools_become_text():
    out = anthropic_to_converse_request({"model": "x", "max_tokens": 1, "messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "f", "input": {"a": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]},
    ]})
    assert "toolConfig" not in out
    assert all("toolUse" not in b and "toolResult" not in b for m in out["messages"] for b in m["content"])


def test_request_rejects():
    with pytest.raises(TranslationError):
        anthropic_to_converse_request({"model": "x", "max_tokens": 1, "n": 2, "messages": []})
    with pytest.raises(TranslationError):
        anthropic_to_converse_request({"model": "x", "messages": []})


def test_reasoning_effort_ladder():
    assert [map_reasoning_effort(b) for b in (1024, 2048, 4096, 8192, 16384)] == ["low", "medium", "high", "xhigh", "max"]


def test_response_blocks_stop_and_usage():
    body = {"output": {"message": {"role": "assistant", "content": [
        {"reasoningContent": {"reasoningText": {"text": "think"}}},
        {"text": "hello"},
        {"toolUse": {"toolUseId": "call_9", "name": "get_weather", "input": {"city": "Pune"}}}]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 10, "outputTokens": 4, "cacheReadInputTokens": 3, "cacheWriteInputTokens": 0}}
    out = converse_to_anthropic_response(body, "glm-5.3", b"k")
    assert out["model"] == "glm-5.3" and out["id"].startswith("msg_")
    assert [b["type"] for b in out["content"]] == ["thinking", "text", "tool_use"]
    assert out["content"][0]["signature"] and len(out["content"][0]["signature"]) == 24
    assert out["content"][2] == {"type": "tool_use", "id": "call_9", "name": "get_weather", "input": {"city": "Pune"}}
    assert out["stop_reason"] == "tool_use"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 0}


def test_usage_without_cache_fields():
    assert map_usage({"inputTokens": 5, "outputTokens": 2}) == {"input_tokens": 5, "output_tokens": 2}


def test_sse_replay_order():
    msg = converse_to_anthropic_response({"output": {"message": {"content": [
        {"reasoningContent": {"reasoningText": {"text": "t"}}}, {"text": "x"},
        {"toolUse": {"toolUseId": "c", "name": "f", "input": {"a": 1}}}]}},
        "stopReason": "tool_use", "usage": {"inputTokens": 1, "outputTokens": 1}}, "glm-5.3", b"k")
    frames = b"".join(replay_message(msg)).decode()
    names = [ln.split("event: ", 1)[1] for ln in frames.splitlines() if ln.startswith("event: ")]
    assert names == ["message_start",
                     "content_block_start", "content_block_delta", "content_block_delta", "content_block_stop",
                     "content_block_start", "content_block_delta", "content_block_stop",
                     "content_block_start", "content_block_delta", "content_block_stop",
                     "message_delta", "message_stop"]
    datas = [json.loads(ln[6:]) for ln in frames.splitlines() if ln.startswith("data: ")]
    tool_delta = [d for d in datas if d.get("delta", {}).get("type") == "input_json_delta"][0]
    assert json.loads(tool_delta["delta"]["partial_json"]) == {"a": 1}
    assert datas[-2]["delta"]["stop_reason"] == "tool_use" and datas[-2]["usage"]["output_tokens"] == 1
