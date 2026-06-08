"""Generate per-team QR codes. QRs link directly to task pages with no passphrase."""
import qrcode
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QR_DIR = os.path.join(BASE_DIR, 'qr-codes-v2')

if len(sys.argv) > 1:
    BASE_URL = sys.argv[1].rstrip('/')
else:
    BASE_URL = os.environ.get('BASE_URL', 'http://localhost:8080')

os.makedirs(QR_DIR, exist_ok=True)

for color in ('red', 'blue'):
    filename = f'missions_{color}.json'
    with open(os.path.join(BASE_DIR, filename)) as f:
        data = json.load(f)

    for m in data['missions']:
        url = f"{BASE_URL}/unlock/{m['id']}?c={color}"

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        path = f"{QR_DIR}/{color}-mission-{m['id']:02d}.png"
        img.save(path)
        print(f"  OK: {os.path.basename(path)} -> {url}")

# 4 decoys (not team-specific)
decoys = [
    (101, f"{BASE_URL}/static/decoy1.html"),
    (102, f"{BASE_URL}/static/decoy2.html"),
    (103, f"{BASE_URL}/static/decoy3.html"),
    (104, f"{BASE_URL}/static/decoy4.html"),
]
for d_id, url in decoys:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    path = f"{QR_DIR}/decoy-{d_id}.png"
    img.save(path)
    print(f"  OK: decoy-{d_id}.png -> {url}")

total = sum(len(json.load(open(os.path.join(BASE_DIR, f'missions_{c}.json')))['missions']) for c in ('red','blue'))
print(f"\nGenerated {total} mission QRs ({total//2} red + {total//2} blue) + 4 decoy QRs")
print(f"Base URL: {BASE_URL}")
print(f"Output:   {QR_DIR}")
print(f"\nRed QRs — hide these for the Red team's hunt:")
for m in json.load(open(os.path.join(BASE_DIR, 'missions_red.json')))['missions']:
    print(f"  Red {m['id']:2d}: {m['location']}")
print(f"\nBlue QRs — hide these for the Blue team's hunt:")
for m in json.load(open(os.path.join(BASE_DIR, 'missions_blue.json')))['missions']:
    print(f"  Blue {m['id']:2d}: {m['location']}")
