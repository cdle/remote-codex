#!/usr/bin/env python3
"""TLS split proxy for chatgpt.com / api.openai.com (Codex remote control).

Terminate TLS with our own cert, then route per request on a keep-alive
connection (loop until EOF):

  GET  /backend-api/codex/models      -> local free-model catalogue
  POST .../codex/responses, /v1/responses
                                       -> local bridge if model slug starts "cn:"
                                          else real upstream
  everything else                     -> real upstream (raw splice)

Blocking sockets + threads. Official remote-control websocket and token
refresh pass through untouched, so the heartbeat is not affected.
"""
import socket
import ssl
import threading
import json
import os
import sys
import time

import httpx

LISTEN = ("127.0.0.1", 443)
BRIDGE = ("127.0.0.1", 7863)   # WorkBuddy gateway with native Responses API (no relay needed)
UPSTREAM = ("104.18.32.47", 443)
CERT = "/opt/chatgpt-mitm/server.crt"
KEY = "/opt/chatgpt-mitm/server.key"
CAFILE = "/etc/ssl/certs/ca-certificates.crt"

RESPONSES_PATHS = ("/backend-api/codex/responses", "/v1/responses")
MODELS_PATH = "/backend-api/codex/models"

server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
server_ctx.load_cert_chain(CERT, KEY)
up_ctx = ssl.create_default_context(cafile=CAFILE)

http = httpx.Client(verify=CAFILE, timeout=httpx.Timeout(20.0, connect=10.0))

_CACHE = {"ts": 0.0, "body": None}
_CACHE_TTL = 300.0


def log(msg):
    import datetime
    sys.stderr.write("[split] %s %s\n" % (datetime.datetime.utcnow().strftime("%H:%M:%S.%f")[:-3], msg))
    sys.stderr.flush()


def recv_head(sock, carry=b""):
    """Read one HTTP request head; returns (head, leftover) or (None, None)."""
    buf = carry
    while b"\r\n\r\n" not in buf:
        d = sock.recv(65536)
        if not d:
            return None, None
        buf += d
        if len(buf) > (1 << 20):
            return None, None
    i = buf.find(b"\r\n\r\n") + 4
    return buf[:i], buf[i:]


def read_exact(sock, n, extra=b""):
    body = extra[:n]
    while len(body) < n:
        d = sock.recv(65536)
        if not d:
            break
        body += d
    return body[:n], extra[n:]


def parse_head(head):
    lines = head.split(b"\r\n")
    parts = lines[0].decode("latin1").split(" ", 2)
    if len(parts) < 2:
        return None
    method, path = parts[0], parts[1]
    headers = {}
    for l in lines[1:]:
        if b":" in l:
            k, v = l.split(b":", 1)
            headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
    return method, path, headers, lines


# Official models hidden from the picker per user request.
# To re-enable, restore the slug list and the injection block in local_catalogue().
OFFICIAL_MODELS = []


def local_catalogue():
    now = time.time()
    if _CACHE["body"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["body"]
    try:
        r = http.get("http://%s:%d/v1/models" % BRIDGE,
                     headers={"Authorization": "Bearer test_key"}, timeout=10)
        data = r.json() if r.status_code == 200 else {}
    except Exception as e:
        log("gateway models failed: %s" % e)
        data = {}
    models = list(data.get("data") or data.get("models") or [])
    # rename bridge slugs with the provider prefix (wb:) so the picker shows provenance
    for m in models:
        mid = str(m.get("id") or m.get("slug") or "")
        if mid.startswith("cn:"):
            m["id"] = "wb:" + mid[3:]
            m["slug"] = m["id"]
    seen = {str(m.get("id") or m.get("slug")) for m in models}
    if models:
        # clone the full structure of a working local entry so codex accepts it
        template = json.loads(json.dumps(models[0]))
        for slug in OFFICIAL_MODELS:
            if slug in seen:
                continue
            entry = dict(template)
            entry["slug"] = slug
            entry["id"] = slug
            entry["display_name"] = slug
            entry["description"] = "Official OpenAI model (routed to real API)"
            models.append(entry)
            seen.add(slug)
    out = {"object": "list", "data": models, "models": models}
    if models:
        _CACHE["ts"], _CACHE["body"] = now, out
    return out


def bridge_request(method, bridge_path, head_lines, body, drop_headers=()):
    """One-shot request to the local bridge; returns raw response bytes."""
    drop = tuple(h.lower().encode() for h in drop_headers)
    s = socket.create_connection(BRIDGE, timeout=300)
    inject_auth = (BRIDGE[1] == 7863)
    try:
        out = ["%s %s HTTP/1.1" % (method, bridge_path)]
        for l in head_lines[1:]:
            if not l:
                continue
            low = l.lower()
            if low.startswith(b"host:") or low.startswith(b"content-length:") \
               or low.startswith(b"connection:") or low.startswith(b"transfer-encoding:") \
               or low.startswith(drop):
                continue
            out.append(l.decode("latin1"))
        out.append("Host: %s:%d" % BRIDGE)
        if inject_auth:
            out.append("Authorization: Bearer test_key")
        out.append("Content-Length: %d" % len(body))
        out.append("Connection: close")
        log("BRIDGE-REQ body=%s" % body[:200])
        s.sendall(("\r\n".join(out) + "\r\n\r\n").encode() + body)
        s.settimeout(300)
        chunks = []
        while True:
            d = s.recv(65536)
            if not d:
                break
            chunks.append(d)
        return b"".join(chunks)
    finally:
        try:
            s.close()
        except Exception:
            pass


def bridge_stream(method, bridge_path, head_lines, body, client_writer):
    """Stream a request to the gateway and splice the response back verbatim."""
    s = socket.create_connection(BRIDGE, timeout=60)
    inject_auth = (BRIDGE[1] == 7863)
    out = ["%s %s HTTP/1.1" % (method, bridge_path)]
    for l in head_lines[1:]:
        if not l:
            continue
        low = l.lower()
        if low.startswith(b"host:") or low.startswith(b"content-length:") \
           or low.startswith(b"connection:") or low.startswith(b"transfer-encoding:") \
           or low.startswith(b"content-encoding:") or low.startswith(b"authorization:"):
            continue
        out.append(l.decode("latin1"))
    out.append("Host: %s:%d" % BRIDGE)
    if inject_auth:
        out.append("Authorization: Bearer test_key")
    out.append("Content-Length: %d" % len(body))
    out.append("Connection: close")
    s.sendall(("\r\n".join(out) + "\r\n\r\n").encode() + body)
    s.settimeout(600)

    def pump(a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except Exception:
            pass
    t = threading.Thread(target=pump, args=(s, client_writer), daemon=True)
    t.start()
    t.join()
    try:
        s.close()
    except Exception:
        pass


def handle(client):
    carry = b""
    try:
        client.settimeout(900)
        while True:
            head, carry = recv_head(client, carry)
            if head is None:
                return
            parsed = parse_head(head)
            if not parsed:
                return
            method, path, headers, head_lines = parsed
            base = path.split("?")[0]
            log("REQ %s %s" % (method, path[:120]))
            query = path[len(base):]

            # Reject WSS upgrades on the inference endpoint: codex then falls
            # back to HTTPS POST, which we can route by model slug.
            if base in RESPONSES_PATHS and "upgrade" in headers.get("connection", "").lower():
                log("WS-REJECT %s" % base)
                client.sendall(b"HTTP/1.1 501 Not Implemented\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return

            cl = int(headers.get("content-length", "0") or 0)
            body, carry = read_exact(client, cl, carry)

            # ---------- local model catalogue ----------
            if base in (MODELS_PATH, "/v1/models") and method == "GET":
                payload = json.dumps(local_catalogue()).encode()
                resp = ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        "Content-Length: %d\r\n\r\n" % len(payload)).encode() + payload
                client.sendall(resp)
                continue

            # ---------- inference ----------
            if base in RESPONSES_PATHS:
                slug = None
                bridge_slug = None
                raw = body
                enc = (headers.get("content-encoding") or "").lower()
                log("ENC=%r hdrkeys=%s" % (enc, sorted(headers.keys())))
                try:
                    if enc in ("gzip", "x-gzip"):
                        import gzip as _gz
                        raw = _gz.decompress(body)
                    elif enc == "br":
                        try:
                            import brotli as _br
                            raw = _br.decompress(body)
                        except ImportError:
                            raw = body
                    elif enc == "zstd":
                        try:
                            import zstandard as _zs
                            raw = _zs.ZstdDecompressor().decompress(body, max_output_size=8 << 20)
                        except ImportError:
                            raw = body
                    elif enc == "deflate":
                        import zlib as _zl
                        raw = _zl.decompress(body)
                except Exception:
                    raw = body
                try:
                    slug = json.loads(raw).get("model")
                except Exception:
                    slug = None
                log("INFER method=%s model=%s bodylen=%d body=%s" % (method, slug, len(body), body[:150]))
                if isinstance(slug, str) and slug.startswith("wb:"):
                    slug = "cn:" + slug[3:]
                if isinstance(slug, str) and slug.startswith("cn:"):
                    bridge_slug = slug
                if bridge_slug:
                    # rewrite the model slug in the decoded body before forwarding
                    try:
                        obj = json.loads(raw)
                        obj["model"] = bridge_slug
                        is_stream = bool(obj.get("stream"))
                        raw = json.dumps(obj).encode()
                    except Exception as e:
                        log("model rewrite failed: %s" % e)
                        is_stream = False
                    if is_stream:
                        log("STREAM-INFER model=%s" % bridge_slug)
                        bridge_stream(method, "/v1/responses" + query, head_lines, raw, client)
                        return
                    resp = bridge_request(method, "/v1/responses" + query, head_lines, raw,
                                          drop_headers=("content-encoding",))
                    if resp:
                        # strip upstream hop-by-hop headers, keep body intact
                        i = resp.find(b"\r\n\r\n")
                        if i > 0:
                            hpart, bpart = resp[:i], resp[i + 4:]
                            newh = []
                            for l in hpart.split(b"\r\n")[1:]:
                                low = l.lower()
                                if low.startswith((b"connection:", b"transfer-encoding:",
                                                   b"content-length:")):
                                    continue
                                newh.append(l)
                            out = b"\r\n".join([hpart.split(b"\r\n")[0]] + newh)
                            out += b"\r\nContent-Length: %d\r\n\r\n" % len(bpart)
                            resp = out + bpart
                        client.sendall(resp)
                    else:
                        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    continue
                # official slug -> real upstream (streamed)
                upstream = up_ctx.wrap_socket(
                    socket.create_connection(UPSTREAM, timeout=60),
                    server_hostname="chatgpt.com")
                fixed = []
                for l in head_lines[1:]:
                    if not l:
                        continue
                    if l.lower().startswith(b"host:"):
                        fixed.append(b"Host: chatgpt.com")
                    else:
                        fixed.append(l)
                upstream.sendall(b"\r\n".join([head_lines[0]] + fixed) + b"\r\n\r\n" + body)
                threading.Thread(target=pump, args=(upstream, client), daemon=True).start()
                threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
                return

            # ---------- everything else: raw splice ----------
            upstream = up_ctx.wrap_socket(
                socket.create_connection(UPSTREAM, timeout=60),
                server_hostname="chatgpt.com")
            fixed = []
            for l in head_lines[1:]:
                if not l:
                    continue
                if l.lower().startswith(b"host:"):
                    fixed.append(b"Host: chatgpt.com")
                else:
                    fixed.append(l)
            upstream.sendall(b"\r\n".join([head_lines[0]] + fixed) + b"\r\n\r\n")
            if body:
                upstream.sendall(body)
            if carry:
                upstream.sendall(carry)
                carry = b""
            t1 = threading.Thread(target=pump, args=(client, upstream), daemon=True)
            t2 = threading.Thread(target=pump, args=(upstream, client), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            return
    except Exception as e:
        log("handle error: %s" % e)
    finally:
        try:
            client.close()
        except Exception:
            pass


def pump(src, dst):
    try:
        while True:
            d = src.recv(65536)
            if not d:
                break
            dst.sendall(d)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(128)
    log("listening on %s:%d" % LISTEN)
    while True:
        c, _ = srv.accept()
        try:
            tls = server_ctx.wrap_socket(c, server_side=True)
        except Exception:
            try:
                c.close()
            except Exception:
                pass
            continue
        threading.Thread(target=handle, args=(tls,), daemon=True).start()


if __name__ == "__main__":
    main()
