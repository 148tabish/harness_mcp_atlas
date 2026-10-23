"""Harbor's openhands-sdk agent with a runner whose ATIF trajectory the task
verifiers can grade.

Use it with `harbor run -a openhands_bridge_agent:OpenHandsSDKBridge ...`
(this directory on PYTHONPATH; run-openhands.sh does both).

It is the built-in agent unchanged except for the runner script it uploads,
which is the built-in runner with these edits:

  1. every tool result is attached to the step holding its own tool call, and
     results accumulate instead of replacing each other, so a turn with several
     tool calls keeps every response;
  2. the "[Tool 'x' executed.]" line OpenHands puts in front of an MCP result is
     dropped, so the recorded response is the server's own JSON;
  3. the message of the closing `finish` tool call is also recorded as an agent
     message, so the trajectory's final message is the agent's answer;
  4. MCP tool arguments, which OpenHands wraps as {"data": {...}}, are recorded
     unwrapped, so they read as the arguments the server received;
  5. the GLM models are told they can see images, are kept off the string
     serializer that strips image parts, and are given a real output limit;
  6. the model's own reasoning is carried onto each step as `thought`, which
     harbor otherwise drops (nothing is ever synthesised: empty stays empty);
  7. GLM token prices are registered with LiteLLM, which knows no rate for them
     and would report every run as costing 0.0.
"""
from __future__ import annotations

from pathlib import Path

from harbor.agents.installed import openhands_sdk as _builtin
from harbor.agents.installed.openhands_sdk import OpenHandsSDK
from harbor.environments.base import BaseEnvironment

_EDITS = [
    # 1. attach each result to its own call; keep every result
    ('''        elif event_type == "tool_result":
            # Find the previous step and add observation
            if steps and steps[-1].get("source") == "agent":
                steps[-1]["observation"] = {
                    "results": [
                        {
                            "source_call_id": event.get("tool_call_id"),
                            "content": event.get("content", ""),
                        }
                    ]
                }
''',
     '''        elif event_type == "tool_result":
            call_id = event.get("tool_call_id")
            owner = next((s for s in reversed(steps) if s.get("source") == "agent"
                          and any(tc.get("tool_call_id") == call_id for tc in s.get("tool_calls") or [])), None)
            if owner is None and steps and steps[-1].get("source") == "agent":
                owner = steps[-1]
            if owner is not None:
                owner.setdefault("observation", {"results": []})["results"].append(
                    {"source_call_id": call_id, "content": event.get("content", "")})
'''),
    # 2. drop the "[Tool 'x' executed.]" header from MCP results
    ('''                if isinstance(obs_raw, list):
                    obs_content = "\\n".join(
                        getattr(c, "text", str(c))
                        for c in obs_raw
                        if getattr(c, "text", None)
                    )
''',
     '''                if isinstance(obs_raw, list):
                    texts = [getattr(c, "text", str(c)) for c in obs_raw if getattr(c, "text", None)]
                    if len(texts) > 1 and texts[0].startswith("[Tool '") and texts[0].endswith(" executed.]"):
                        texts = texts[1:]
                    obs_content = "\\n".join(texts)
'''),
    # 4. record MCP arguments unwrapped from OpenHands' {"data": {...}}
    ('''                except Exception:
                    pass
            entry = {
                "type": "assistant_message",
''',
     '''                except Exception:
                    pass
            if isinstance(tool_call_args, dict) and list(tool_call_args) == ["data"] and isinstance(tool_call_args["data"], dict):
                tool_call_args = tool_call_args["data"]
            entry = {
                "type": "assistant_message",
'''),
    # 5. tell the SDK which GLM models can see images: the feature registry only knows
    #    the Anthropic and OpenAI names, so file_editor otherwise refuses to display
    #    an image and the picture never reaches the model
    ('''    llm = LLM(**llm_kwargs)
''',
     '''    if any(name in model for name in ("glm-5.3-flash", "glm-4.6v", "glm-4.5v")):
        llm_kwargs["capability_overrides"] = {"supports_vision": True}
        # the registry also forces the string serializer for this model, which
        # collapses every message to text and drops the image parts
        llm_kwargs["force_string_serializer"] = False
    if "glm" in model:
        # without a known output limit LiteLLM falls back to 4096, which truncates
        # a long tool call mid-arguments and leaves the agent looping on it
        llm_kwargs["max_output_tokens"] = 16384
    _register_glm_pricing(model)
    llm = LLM(**llm_kwargs)
'''),
    # 8. price the GLM models. LiteLLM knows no rate for them, so every run reports
    #    cost_usd 0.0 while the token counts are right. Rates are z.ai's published
    #    USD/MTok list prices; a Coding Plan run is a subscription, so the figure is
    #    what the tokens would cost at list price, not what was billed.
    ('''def main():
''',
     '''# (input, cached input, output) in USD per million tokens, from
# https://docs.z.ai/guides/overview/pricing. The cached rate matters: a long
# agent run reads the same context back every step, so at ~98% cache hits the
# input bill is an order of magnitude below the headline rate.
_GLM_PRICES_USD_PER_MTOK = {
    "glm-5.3-flash": (0.15, 0.03, 0.50),
    "glm-5.3-flashx": (0.37, 0.075, 1.25),
    "glm-5.3": (1.4, 0.26, 4.4),
    "glm-5.1": (1.4, 0.26, 4.4),
    "glm-5": (1.0, 0.2, 3.2),
    "glm-4.6v-flash": (0.0, 0.0, 0.0),
    "glm-4.6v-flashx": (0.04, 0.004, 0.40),
    "glm-4.6v": (0.3, 0.05, 0.9),
}


def _register_glm_pricing(model: str) -> None:
    """Teach LiteLLM what a GLM token costs, so cost_usd stops reporting 0.0."""
    price = _GLM_PRICES_USD_PER_MTOK.get(model.split("/")[-1])
    if not price:
        return
    inp, cached, out = price
    try:
        import litellm
        litellm.register_model({
            model: {
                "input_cost_per_token": inp / 1_000_000,
                "cache_read_input_token_cost": cached / 1_000_000,
                "cache_creation_input_token_cost": inp / 1_000_000,
                "output_cost_per_token": out / 1_000_000,
                "litellm_provider": model.split("/")[0] if "/" in model else "anthropic",
                "mode": "chat",
            }
        })
    except Exception as exc:  # pricing is reporting only; never fail a run for it
        print(f"could not register pricing for {model}: {exc}", file=sys.stderr)


def main():
'''),
    # 6. carry the model's own reasoning into the trajectory. ActionEvent holds it as
    #    reasoning_content (litellm's name) or thinking_blocks (Anthropic's); harbor
    #    builds its entry from neither, so every step lands with an empty thought.
    #    Nothing is ever synthesised here: an empty reasoning stays empty.
    ('''            entry = {
                "type": "assistant_message",
                "content": "",
                "timestamp": event.timestamp,
                "tool_calls": [
''',
     '''            reasoning = (getattr(event, "reasoning_content", "") or "").strip()
            if not reasoning:
                reasoning = "\\n".join(
                    (getattr(b, "thinking", "") or "").strip()
                    for b in (getattr(event, "thinking_blocks", None) or [])
                ).strip()
            entry = {
                "type": "assistant_message",
                "content": "",
                "thought": reasoning,
                "timestamp": event.timestamp,
                "tool_calls": [
'''),
    # 7. and record it on the step the trajectory file carries
    ('''                "message": event.get("content", ""),
                "model_name": model_name,
            }
''',
     '''                "message": event.get("content", ""),
                "model_name": model_name,
            }
            if event.get("thought"):
                step["thought"] = event["thought"]
'''),
    # 3. the finish tool's message is the final answer
    ('''            events_list.append(entry)
            last_agent_timestamp = event.timestamp
        elif isinstance(event, ObservationEvent):
''',
     '''            events_list.append(entry)
            last_agent_timestamp = event.timestamp
            if event.tool_name == "finish" and isinstance(tool_call_args.get("message"), str):
                events_list.append({"type": "assistant_message", "content": tool_call_args["message"],
                                    "timestamp": event.timestamp})
        elif isinstance(event, ObservationEvent):
'''),
]


def patched_runner() -> str:
    src = (Path(_builtin.__file__).parent / "openhands_sdk_runner.py").read_text()
    for old, new in _EDITS:
        if src.count(old) != 1:
            raise RuntimeError("harbor's openhands_sdk_runner.py changed; update openhands_bridge_agent._EDITS")
        src = src.replace(old, new)
    return src


class OpenHandsSDKBridge(OpenHandsSDK):
    async def exec_as_root(self, environment: BaseEnvironment, command: str, **kw):
        # harbor's install() runs `chown {user}:{user} /opt/openhands-sdk-venv`. A task
        # that names the agent user as "uid:gid" (the harness tasks carry 10001:10001)
        # would hand chown "10001:10001:10001:10001"; pass the pair through unchanged.
        # Kept here so harbor itself needs no patch.
        user = str(environment.default_user or "root")
        if ":" in user:
            command = command.replace(f"chown {user}:{user} ", f"chown {user} ")
        return await super().exec_as_root(environment, command, **kw)

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        local_copy = self.logs_dir / "run_agent.py"
        local_copy.write_text(patched_runner())
        await environment.upload_file(source_path=local_copy, target_path="/installed-agent/run_agent.py")
        await environment.exec(command="chmod +x /installed-agent/run_agent.py", user="root")
