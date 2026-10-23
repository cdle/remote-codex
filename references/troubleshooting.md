# Troubleshooting: Codex remote control on a headless Linux host

Ordered by how often each one bites. Every item below was hit for real while
bringing up this stack.

## 1. `model_providers contains reserved built-in provider IDs: openai`

Codex refuses to let you override the built-in `openai` provider in
`config.toml`. You cannot redefine it to point at a custom gateway this way.
Use a different provider name, or (better) keep the built-in provider and
intercept at the TLS layer with the split proxy.

## 2. Inference does not go through `chatgpt.com`

With a ChatGPT login, Codex sends inference to
`https://api.openai.com/v1/responses`, not to the `chatgpt.com/backend-api`
endpoint. Hijacking only `chatgpt.com` silently leaves inference on the
official path while the model catalogue and remote-control channel go through
the proxy — the exact combination that makes the app show your models and then
fail with `model is not supported when using Codex with a ChatGPT account`.

Fix: hijack **both** hostnames, and cover both in the certificate SAN.

## 3. Inference is a WebSocket, not an HTTP POST

`codex_api::endpoint::responses_websocket` — Codex opens
`wss://chatgpt.com/backend-api/codex/responses` first. An HTTP-level proxy
never sees the request. It retries 5 times with exponential backoff (~7 s),
then falls back to HTTPS POST. Returning `501 Not Implemented` on the upgrade
makes that fallback happen quickly and deterministically. `410 Gone` works
too; TCP RST does not shorten the retry sequence.

## 4. Keep-alive connections bypass per-connection routing

A naive proxy handles the first request on a TLS connection and then splices
raw bytes. The second request on the same connection (very common: models
catalogue, then inference) never reaches the routing logic. Loop over requests
until EOF, carrying leftover bytes between iterations.

## 5. Request bodies are zstd-compressed

App-sent inference bodies arrive with `Content-Encoding: zstd` (magic
`28 b5 2f fd`). If you parse the body to make a routing decision you must
decompress first. Install `zstandard` (and `brotli`/`gzip` for other
clients). When forwarding to a backend that does not advertise the same
encoding, send the **decoded** body and drop `Content-Encoding`, otherwise the
backend answers `422 'utf-8' codec can't decode byte 0xb5`.

`ZstdDecompressor.decompress()` needs `max_output_size=` — without it the
decompression fails silently on frames that do not embed the size.

## 6. Streaming vs non-streaming

Codex sends `stream: true` for real turns. A proxy that reads the backend
response until EOF will hang forever on an SSE stream, and must not add
`Content-Length` to a streaming response (the gateway resets the connection
when it sees it). Route streaming requests to a byte-splicing pump and pass
headers through verbatim.

## 7. `OPENAI_BASE_URL` is not a safe injection point

Injecting `OPENAI_BASE_URL` into the app-server process looks like the obvious
hook, but it also redirects the remote-control heartbeat traffic, producing
`remote control websocket pong timeout` within seconds and an app that shows
offline. Do not touch the app-server environment; intercept at the network
layer instead.

## 8. Model catalogue entries need full metadata

Injecting stub entries (`{"slug": ..., "display_name": ...}`) into the model
list makes Codex silently drop them — the picker never shows them. Clone a
complete entry from the backend catalogue and override only the slug/name.

## 9. App says "cannot open this task" / "session expired"

Usually the host no longer has the thread the client cached. After clearing
`~/.codex/sessions` and `thread_history_1.sqlite`, the app must be force
restarted so it drops its local session list, otherwise every cached thread
fails to resume. New sessions work fine.

## 10. `nohup ... &` over non-interactive SSH hangs the session

The daemon inherits sshd's stdout/stderr pipes and sshd waits for them. The
command already returned but the terminal shows "running" for as long as the
daemon lives. Use `systemd` units for anything long-lived.

## 11. Forced model mapping via environment variable survives CLI flags

`codex-bridge-python` reads `CODEX_BRIDGE_FORCE_DEFAULT_MODEL` from the
environment **and** `--force-default-model` from argv. Removing the flag while
the `Environment=` line stays in the unit file keeps the forced mapping
active. Check both when a model choice appears to be ignored.

## 12. Codex 0.165+ ignores third-party model lists without `model_catalog_url`

Since codex 0.165, `supports_api_key_models()` (model-provider/src/models_endpoint.rs)
only enables model discovery when the provider block sets `model_catalog_url`.
Without it an API-key provider silently falls back to the built-in official
list, no matter how correct `/v1/models` is. The fix belongs in whatever tool
writes `config.toml` (e.g. magpie's `putProvider()`), not in hand-edits:

```toml
[model_providers.magpie]
base_url = "http://127.0.0.1:3425/v1"
wire_api = "responses"
model_catalog_url = "http://127.0.0.1:3425/v1/codex/models"
```

Symptom of the fallback: the app's model picker suddenly shows GPT models
again after an update. Verify with `codex exec` — a missing catalogue shows
`Model metadata not found` warnings.

## 13. Stale `auth.json` tokens flip codex back to ChatGPT mode

Gateways that detect ChatGPT login state (e.g. magpie reads
`tokens.access_token`) will switch the config to "beside sign-in" mode
(`openai_base_url`, no `model_provider`) as long as old tokens exist, undoing
the provider fix on every sync. Clear `tokens` from `/root/.codex/auth.json`
(keep `OPENAI_API_KEY`, set `auth_mode = "apikey"`), back the file up first,
then re-trigger the gateway's config sync. This is why the list "reverts to
official" even after the provider block looks right.

## 14. Building magpie and shipping the binary

Build on an ARM64 box with Go, not the memory-constrained Mac:
`CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -tags nogui -trimpath -o magpie .`
(the main package is in the repo root, there is no `cmd/`). The binary is
~75 MB; pulling it from Singapore direct is ~50 KB/s — route the transfer via
a jump host instead (`ssh -J tlite orc-db`, tens of seconds). Keep a dated
backup of the previous binary before replacing it.
