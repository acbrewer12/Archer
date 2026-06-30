#!/bin/bash
# Run this on the Pi to connect it to Archer's terminal
# Usage: bash pi_connect.sh
#
# Required environment variables (set in ~/.bashrc or ~/.profile on the Pi):
#   ARCHER_URL       — e.g. https://aydencatman-archer.hf.space
#   ARCHER_PI_TOKEN  — must match ARCHER_PI_TOKEN set on the server

if [ -z "$ARCHER_URL" ]; then
    echo "[PI] ERROR: ARCHER_URL is not set. Export it before running this script."
    echo "       e.g.  export ARCHER_URL=https://aydencatman-archer.hf.space"
    exit 1
fi

if [ -z "$ARCHER_PI_TOKEN" ]; then
    echo "[PI] ERROR: ARCHER_PI_TOKEN is not set. Export it before running this script."
    echo "       e.g.  export ARCHER_PI_TOKEN=<your-token>"
    exit 1
fi

# Temp cookie jar — holds archer_sid so CSRF tokens remain valid
COOKIE_JAR="$(mktemp /tmp/archer_pi_cookies.XXXXXX)"
trap 'rm -f "$COOKIE_JAR"' EXIT

# Fetch a CSRF token from the server and store the session cookie
pi_csrf_token() {
    curl -s -c "$COOKIE_JAR" -b "$COOKIE_JAR" \
        "$ARCHER_URL/csrf_token" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null
}

# POST to an Archer endpoint with CSRF + token auth
pi_post() {
    local endpoint="$1"
    local body="$2"
    local csrf
    csrf=$(pi_csrf_token)
    if [ -z "$csrf" ]; then
        echo "[PI] WARNING: Could not fetch CSRF token — server may be unreachable"
        return 1
    fi
    curl -s -c "$COOKIE_JAR" -b "$COOKIE_JAR" \
        -X POST "$ARCHER_URL$endpoint" \
        -H "Content-Type: application/json" \
        -H "X-CSRF-Token: $csrf" \
        -d "$body"
}

echo "[PI] Starting Archer Pi terminal connection..."

# Install ttyd if not present
if ! command -v ttyd &> /dev/null; then
    echo "[PI] Installing ttyd..."
    sudo apt-get install -y ttyd
fi

# Start ttyd on port 7682 (local Pi terminal)
ttyd -p 7682 -t fontSize=13 -t theme='{"background":"#0a0a0a","foreground":"#ff3333"}' bash &
TTYD_PID=$!
echo "[PI] ttyd started on port 7682 (PID $TTYD_PID)"

# Install ngrok if not present
if ! command -v ngrok &> /dev/null; then
    echo "[PI] Installing ngrok..."
    curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
    echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
    sudo apt-get update && sudo apt-get install -y ngrok
fi

# Start ngrok tunnel on port 7682
echo "[PI] Starting ngrok tunnel..."
ngrok http 7682 --log=stdout &
NGROK_PID=$!
sleep 4

# Get tunnel URL
TUNNEL_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])" 2>/dev/null)

if [ -z "$TUNNEL_URL" ]; then
    echo "[PI] ERROR: Could not get ngrok tunnel URL"
    exit 1
fi

echo "[PI] Tunnel URL: $TUNNEL_URL"

# Register with Archer (CSRF + token auth)
echo "[PI] Registering with Archer..."
pi_post "/terminal/pi_register" "{\"token\":\"$ARCHER_PI_TOKEN\",\"url\":\"$TUNNEL_URL\"}"

echo ""
echo "[PI] Connected. Terminal available at $ARCHER_URL/terminal"
echo "[PI] Press Ctrl+C to disconnect"

# Keep alive — re-register every 5 minutes in case tunnel URL changes
cleanup() {
    echo "[PI] Disconnecting..."
    pi_post "/terminal/pi_disconnect" "{\"token\":\"$ARCHER_PI_TOKEN\"}"
    kill $TTYD_PID $NGROK_PID 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

while true; do
    sleep 300
    TUNNEL_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])" 2>/dev/null)
    if [ -n "$TUNNEL_URL" ]; then
        pi_post "/terminal/pi_register" "{\"token\":\"$ARCHER_PI_TOKEN\",\"url\":\"$TUNNEL_URL\"}" > /dev/null
        echo "[PI] Re-registered tunnel: $TUNNEL_URL"
    fi
done
