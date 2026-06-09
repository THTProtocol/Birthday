# Ringparken Birthday Scavenger Hunt v2

Interactive web-app version with QR codes, real-time scoring, decoy traps.

## Quick Start

```bash
cd Birthday
python3 server.py
# Server running at http://localhost:8080
```

Then visit:
- Players: http://localhost:8080/
- Admin: http://localhost:8080/admin (password: cakeoclock2026)

**Live instance (current tunnel):** https://geek-advisor-nut-longitude.trycloudflare.com/
(Quick tunnels rotate names on restart; run `python generate_qr.py YOUR_URL` after each new tunnel.)

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
