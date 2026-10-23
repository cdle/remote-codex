#!/bin/bash
# Generate a self-signed CA + server cert covering chatgpt.com / api.openai.com,
# install the CA into the system trust store, and point the hosts file at localhost.
# Usage: gen-cert.sh [--uninstall]
set -euo pipefail

CERT_DIR="${CERT_DIR:-/opt/chatgpt-mitm}"
CA_CRT="$CERT_DIR/ca.crt"
CA_KEY="$CERT_DIR/ca.key"
SERVER_CRT="$CERT_DIR/server.crt"
SERVER_KEY="$CERT_DIR/server.key"
SERVER_CSR="$CERT_DIR/server.csr"
SAN_EXT="$CERT_DIR/san.ext"
CA_NAME="Local Codex MitM CA"
HOSTS_MARKER="# codex-remote-control-hijack"

install_deps() {
    command -v openssl >/dev/null || { echo "openssl required"; exit 1; }
    command -v update-ca-certificates >/dev/null || \
    command -v trust anchor >/dev/null || \
    echo "warning: no CA install helper found (update-ca-certificates / trust)"
}

gen_cert() {
    mkdir -p "$CERT_DIR"
    if [ ! -f "$CA_CRT" ]; then
        openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
            -keyout "$CA_KEY" -out "$CA_CRT" -subj "/CN=$CA_NAME" \
            -addext "basicConstraints=critical,CA:TRUE" \
            -addext "keyUsage=critical,keyCertSign,cRLSign"
        chmod 600 "$CA_KEY"
    fi
    openssl req -newkey rsa:2048 -nodes -keyout "$SERVER_KEY" -out "$SERVER_CSR" \
        -subj "/CN=chatgpt.com"
    chmod 600 "$SERVER_KEY"
    cat > "$SAN_EXT" <<EXT
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = chatgpt.com
DNS.2 = *.chatgpt.com
DNS.3 = api.openai.com
IP.1 = 127.0.0.1
EXT
    openssl x509 -req -in "$SERVER_CSR" -CA "$CA_CRT" -CAkey "$CA_KEY" \
        -CAcreateserial -out "$SERVER_CRT" -days 397 -sha256 -extfile "$SAN_EXT"
}

install_ca() {
    if command -v update-ca-certificates >/dev/null; then
        cp "$CA_CRT" "/usr/local/share/ca-certificates/local-codex-mitm.crt"
        update-ca-certificates
    elif command -v trust >/dev/null; then
        trust anchor "$CA_CRT"
    else
        echo "MANUAL STEP REQUIRED: install $CA_CRT into your trust store"
    fi
}

hijack_hosts() {
    grep -q "$HOSTS_MARKER" /etc/hosts && return 0
    {
        echo "$HOSTS_MARKER"
        echo "127.0.0.1 chatgpt.com"
        echo "127.0.0.1 api.openai.com"
    } >> /etc/hosts
}

uninstall() {
    if command -v update-ca-certificates >/dev/null; then
        rm -f /usr/local/share/ca-certificates/local-codex-mitm.crt
        update-ca-certificates
    fi
    sed -i "/$HOSTS_MARKER/,+3d" /etc/hosts
    rm -rf "$CERT_DIR"
    echo "uninstalled. restart any codex processes."
}

case "${1:-}" in
    --uninstall) uninstall ;;
    *) install_deps; gen_cert; install_ca; hijack_hosts;
       echo "done. certs in $CERT_DIR, hosts hijacked." ;;
esac
