#!/bin/bash
# Start the Birthday Scavenger Hunt server + optional public tunnel
set -e
cd "$(dirname "$0")"

echo "=== Klaudusia's Birthday Scavenger Hunt ==="

# Kill old instances safely (use exact name where possible)
pkill -x cloudflared 2>/dev/null || true
for pidf in /tmp/bday-*.pid; do
  [ -f "$pidf" ] && kill $(cat "$pidf") 2>/dev/null || true
done
sleep 0.6

# Start Flask persistently (nohup + pidfile)
nohup python3 server.py > /tmp/bday-server.log 2>&1 &
SPID=$!
echo $SPID > /tmp/bday-server.pid
echo "Server started (pid $SPID) -> http://localhost:8080"

sleep 1.2
if ! curl -s --max-time 2 http://127.0.0.1:8080/api/state >/dev/null; then
  echo "WARNING: server may not be responding yet"
fi

# Optional public tunnel
if [ "${1:-}" = "--public" ] || [ "${1:-}" = "-p" ]; then
  echo "Launching cloudflared quick tunnel for public website..."
  nohup /home/kasparov/cloudflared tunnel --url http://127.0.0.1:8080 > /tmp/bday-tunnel.log 2>&1 &
  TPID=$!
  echo $TPID > /tmp/bday-tunnel.pid
  echo "Tunnel pid $TPID, waiting for URL..."
  URL=""
  for i in $(seq 1 18); do
    sleep 1
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/bday-tunnel.log 2>/dev/null | tail -1 || true)
    if [ -n "$URL" ]; then
      echo ""
      echo ">>> PUBLIC WEBSITE READY: $URL"
      echo ">>> To update QRs for this URL:  python3 generate_qr.py $URL"
      echo ""
      break
    fi
  done
  if [ -z "$URL" ]; then
    echo "(URL not printed yet - check tail -f /tmp/bday-tunnel.log)"
  fi
fi

echo ""
echo "Access:"
echo "  Players:  http://localhost:8080"
echo "  Admin:    http://localhost:8080/admin   (no password required - easy reset/restart)"
echo "  Editor:   http://localhost:8080/editor   (no password required)"
echo ""
echo "LAN (phone on same wifi): http://192.168.8.101:8080  (run 'hostname -I' to confirm IP)"
echo ""
echo "Stop everything:  pkill -x cloudflared; kill $(cat /tmp/bday-server.pid 2>/dev/null) 2>/dev/null; rm -f /tmp/bday-*.pid"
echo "Logs:  tail -f /tmp/bday-server.log    |    tail -f /tmp/bday-tunnel.log"
echo ""
echo "To start with public site:  ./start.sh --public"
echo ""
echo "For a PERMANENT stable public URL + QR codes (recommended):"
echo "  See hetzner-deploy.md  (Hetzner VPS + your subdomain)"
