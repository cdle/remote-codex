# remote-codex

Make a Codex CLI on a headless Linux box controllable from the ChatGPT mobile
app, and optionally route its models through any OpenAI-compatible gateway.

Two independent problems, two independent solutions:

```
A. app control     ChatGPT app ──► official relay ──► codex app-server
   (or SSH mode)   ChatGPT app ──► SSH ──► codex on the host   ← simpler

B. model routing   codex ──► split proxy (TLS) ──► your gateway
                                      └─► real OpenAI (untouched)
```

## Quick start (SSH mode, simplest)

1. Generate a key the app accepts (PKCS#8 or SEC1 PEM):
   ```bash
   ssh-keygen -t ed25519 -f /tmp/k -N "" -C ipad-codex-ssh
   # convert to PKCS#8 if the app rejects the OpenSSH format
   ```
2. Append the public key to `/root/.ssh/authorized_keys` on the host.
3. In the ChatGPT app: Remote Control → Add connection → **SSH** →
   host / port / user / private key.
4. The host's `~/.codex/config.toml` decides the models. Point
   `model_provider` at a gateway provider for non-OpenAI models.

That's it — no pairing, no TLS interception, no official account on the host.

## Quick start (official pairing + model routing)

```bash
# on the host
git clone https://github.com/cdle/remote-codex
cd remote-codex
scripts/gen-cert.sh                       # CA + cert + trust + hosts
cp scripts/split-proxy.py /opt/chatgpt-mitm/
cp scripts/chatgpt-split.service /etc/systemd/system/
cp scripts/codex-app-server.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now chatgpt-split codex-app-server
scripts/diagnose.sh                       # PASS/FAIL per layer

# pair a device
/root/.codex/packages/app-server-daemon/releases/*/bin/codex remote-control pair --json
```

Edit the gateway address, key and model prefix at the top of
`scripts/split-proxy.py` before starting it.

## What is in here

| File | Purpose |
|---|---|
| `SKILL.md` | Full operating guide (this is the skill form of the README) |
| `scripts/split-proxy.py` | TLS split proxy: model catalogue rewrite, inference routing, passthrough |
| `scripts/gen-cert.sh` | Self-signed CA + server cert + trust store + hosts hijack (`--uninstall` supported) |
| `scripts/diagnose.sh` | Layered health check with remediation hints |
| `scripts/chatgpt-split.service` | systemd unit for the split proxy |
| `scripts/codex-app-server.service` | systemd unit for `codex app-server --remote-control` |
| `references/troubleshooting.md` | The failure catalogue — read this when something breaks |

## Security posture (read before deploying)

The split proxy is a TLS man-in-the-middle on the host. It:
- listens on `127.0.0.1` only — it is reachable solely through the hosts file
- holds a private CA key that can mint certs for any site on that host
- passes remote-control and token traffic through unmodified

Consequences: anyone with root on the host can already read everything, so the
CA adds no new local exposure. It does mean you should not copy that CA into a
trust store on a machine you do not control. `scripts/gen-cert.sh --uninstall`
removes the CA, the trust entry and the hosts entries.

## Verified on

- Codex CLI 0.163.0 (aarch64, Debian 12 container on ImmortalWrt)
- ChatGPT iOS app, SSH + pairing modes
- A Go-based OpenAI-compatible gateway exposing `/v1/models` and
  `/v1/responses` natively (the proxy also works against plain
  Chat-Completions gateways via a translation bridge, but direct Responses
  support removes a whole component)

## License

MIT
