# bbridge

Anthropic-shaped local proxy for Amazon Bedrock. A trajectory harness (or any
Anthropic-SDK client) talks `/v1/messages` to it and the bridge calls Bedrock's
native **Converse** API addressed by an application inference profile ARN,
authenticated with a Bedrock API key. Built for `zai.glm-5.3` behind the
`ap-south-1-glm-5-3-yuji-production` profile.

Sibling of [zbridge](../zbridge/) (z.ai GLM Coding Plan). Same shape on the
client side: signed thinking blocks, tool_use blocks, Anthropic usage, SSE.

Why Converse and not Bedrock's OpenAI-compatible endpoint: that endpoint refuses
application inference profiles, and the key's IAM policy denies the foundation
model directly, so the ARN over Converse is the only permitted path.

## Run

```bash
./bbridge.sh start | stop | status | check
```

Reads `.env` (mode 600, written by the top-level `setup.sh`):

| Var | Meaning |
|---|---|
| `BB_BEDROCK_API_KEY` | Bedrock API key (`ABSK...`), sent as `Authorization: Bearer` |
| `BB_MODEL_ARN` | application inference profile ARN |
| `BB_REGION` | `ap-south-1` |
| `BB_BRIDGE_SECRET` | what clients present as `x-api-key` |
| `BB_THINKING_SIG_KEY` | HMAC key for thinking-block signatures |
| `BB_HOST` / `BB_PORT` | default `0.0.0.0` / `8767` |
| `BB_REASONING_EFFORT` | optional override: low, medium, high, xhigh, max |

Streaming requests are served in buffered mode: the bridge calls non-streaming
Converse, sends `ping` frames while waiting, then replays the complete answer
as the standard Anthropic SSE sequence.

## Smoke

```bash
curl -sS http://127.0.0.1:8767/v1/messages \
  -H "content-type: application/json" -H "x-api-key: $BB_BRIDGE_SECRET" \
  -d '{"model":"glm-5.3-bedrock","max_tokens":64,"messages":[{"role":"user","content":"Say hi in one word."}]}'
```
