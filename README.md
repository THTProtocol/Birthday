# Ringparken Birthday Scavenger Hunt v2

Interactive web-app version with QR codes, real-time scoring, decoy traps.

## Quick Start

```bash
cd Birthday
./start.sh            # just localhost
./start.sh --public   # localhost + public trycloudflare tunnel
```

Or manually:
```bash
python3 server.py
# In another terminal for public site:
#   /home/kasparov/cloudflared tunnel --url http://localhost:8080
```

Then visit:
- Players: http://localhost:8080/   (or the public URL)
- Admin: http://localhost:8080/admin (password: cakeoclock2026)
- Editor (missions): /editor

LAN (recommended for the hunt - same WiFi): http://192.168.8.101:8080
(Print the qr-codes-v2/ that now point to this; players' phones join the WiFi/hotspot and scan.)

Public tunnel (ephemeral, for testing or split locations): run `./start.sh --public` or cloudflared yourself, then `python3 generate_qr.py <that-url>` to update QRs.

**Live instance (current tunnel):** https://nail-truly-journalist-rid.trycloudflare.com/
(Quick tunnels rotate names on restart of cloudflared; after restart run `python generate_qr.py YOUR_NEW_URL` and reprint QRs.)

Localhost: http://localhost:8080  (or your LAN IP e.g. http://192.168.8.101:8080 )

## Deployment

See DEPLOY_GUIDE.txt for full instructions (local WiFi, Render, or ngrok).

## Regenerate QR codes

```bash
python3 generate_qr.py YOUR_SERVER_URL
# Example: python3 generate_qr.py http://192.168.1.42:8080
```

QR codes output to qr-codes-v2/ folder. Print on A4 paper, ~8x8 cm each.

## Mission list

| # | Mission | Location |
|---|---------|----------|
| 1 | Birthday Goose Awakens | Ringparken entrance |
| 2 | Tree Hugger Challenge | Big trees |
| 3 | The Worm Whisperer | Grassy field |
| 4 | Nature's Birthday Crown | Flower area |
| 5 | The Danish Duck Opera | Bird zone |
| 6 | Cement Factory Selfie | Park edge |
| 7 | Skate or Pretend | Rabalder Park |
| 8 | Street Art Mimic | Mural walls |
| 9 | Rockstar Moment | Ragnarock |
| 10 | The Birthday Sacrifice | finale |
