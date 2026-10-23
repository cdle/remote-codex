---
name: remote-codex
description: Control a Codex CLI running on a headless Linux host from the ChatGPT mobile app, and route its models through an OpenAI-compatible gateway. Covers the split-proxy architecture, app-server systemd setup, SSH-mode alternative, public exposure via frp, and the specific failure modes (WebSocket inference, zstd bodies, keep-alive routing, pong timeouts).
metadata:
  short-description: Headless Codex remote control + model routing
---

# Remote Codex on a headless Linux host

Two independent goals, solved by two independent mechanisms. Read the one that
matches the request; they compose but neither requires the other.

## Pick the right mechanism

| Goal | Mechanism |
|---|---|
| Let the ChatGPT app control a Codex CLI on a headless Linux box | **A: app-server + pairing**, or **B: SSH mode** (no official account needed) |
| Make that Codex use models from your own gateway (not OpenAI) | **C: TLS split proxy** |

**B is strictly simpler than A when the app can reach the host by SSH.** It
needs no ChatGPT account on the host, no TLS interception, and works on
networks where OpenAI is unreachable. Use A only when you specifically need
the official remote-control features or public internet access through the
official relay.

**C is orthogonal.** It is required for A (the official relay forces the
`openai` provider and official endpoints). For B it is optional — B reads the
host's `config.toml`, which can point at a gateway directly.

## A. app-server + pairing (official remote control)

```bash
BIN=/root/.codex/packages/app-server-daemon/releases/0.163.0-aarch64-unknown-linux-musl/bin/codex
$BIN app-server --remote-control --listen unix:// --analytics-default-enabled
```

Run it under systemd (`scripts/codex-app-server.service`). Pair with:

```bash
$BIN remote-control pair --json
# {"manualPairingCode":"XXXX-XXXX","environmentId":"env_...","expiresAt":...}
```

Codes are one-shot, short-lived, and per-device. Multiple devices (iPad +
phone) can pair against the same environment; nothing is invalidated.

Operational notes that cost hours to learn:
- The app-server must run **without** `OPENAI_BASE_URL` or any endpoint
  override in its environment. Overrides redirect the heartbeat and produce
  `pong timeout` within seconds.
- `codex app-server proxy` is how an SSH-launched session talks to the
  app-server; it is not a pairing entry point.
- Logs: `~/.codex/logs_2.sqlite` (query with python's sqlite3, there is no
  `sqlite3` CLI in slim containers). Filter by `process_uuid LIKE 'pid:<PID>%'`.

## B. SSH mode (no pairing, no official dependency)

The ChatGPT app has an **SSH** connection type in Remote Control. It SSHes to
the host and runs codex there, reading the host's own `~/.codex/config.toml`.

```
host = <lan-ip or tailscale name>
port = 2222
user = root
auth = private key (PEM)
```

Key format gotcha: the app rejects OpenSSH-format keys. It wants
**PKCS#8** (`BEGIN PRIVATE KEY`) or **SEC1** (`BEGIN EC PRIVATE KEY`).
Generate ECDSA P-256 and export SEC1 for maximum compatibility:

```bash
ssh-keygen -t ed25519 -f /tmp/k -N "" -C ipad-codex-ssh
# convert with cryptography: private_bytes(Encoding.PEM, PrivateFormat.PKCS8)
```

SSH mode reads the host config, so a gateway-backed `config.toml` is all you
need for custom models — no TLS interception at all.

Public exposure: run `frpc` on the host (OpenWrt: `apk add frpc luci-app-frpc`)
against an `frps` on a VPS. For "same address inside and outside the LAN", add
a hairpin NAT rule on the LAN router:

```
iptables -t nat -A PREROUTING -s <lan>/24 -d <vps-ip> -p tcp --dport 2222 \
  -j DNAT --to-destination <host-lan-ip>:2222
iptables -t nat -A POSTROUTING -s <lan>/24 -d <host-lan-ip> -p tcp --dport 2222 \
  -j MASQUERADE
```

## C. TLS split proxy (model routing for the official path)

`scripts/split-proxy.py` terminates TLS on 127.0.0.1:443 with a self-signed
cert and routes per request:

| Path | Action |
|---|---|
| `/v1/models`, `/backend-api/codex/models` | fetch the gateway catalogue, optional prefix rename, drop official models |
| `/v1/responses`, `/backend-api/codex/responses` | route by model slug: gateway slug -> local gateway; other slug -> real upstream (bytes spliced) |
| everything else | raw byte splice to the real upstream (keeps remote-control and token refresh intact) |

Required companion pieces:
- `scripts/gen-cert.sh` — CA + server cert (SAN must include
  `chatgpt.com`, `*.chatgpt.com`, **and `api.openai.com`**) + trust install +
  hosts entries.
- hosts: `127.0.0.1 chatgpt.com`, `127.0.0.1 api.openai.com`.
- The gateway key is injected by the proxy (the client's ChatGPT bearer token
  is useless to the gateway).
- Loop over requests per connection (keep-alive) and support streaming.

`scripts/diagnose.sh` checks every layer in order. Run it before deep-diving
logs.

## Failure modes worth memorising

Read `references/troubleshooting.md` — it is a catalogue of the exact
symptoms (WebSocket-first inference, zstd bodies, keep-alive bypass, pong
timeouts, "cannot open this task", SSH hangs) and what actually fixed each.

## Do not

- Do not put endpoint overrides in the app-server environment (breaks the
  heartbeat; see troubleshooting #7).
- Do not run long-lived daemons with `nohup` over a non-interactive SSH
  session (hangs the session; use systemd).
- Do not trust `stream: false` tests to validate the inference path — the app
  always sends `stream: true`.
