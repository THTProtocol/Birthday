# Production Deployment on Hetzner VPS + Stable Subdomain (Recommended for Stable QRs)

This setup gives you a **permanent, always-on** public URL (e.g. https://hunt.yourdomain.com) so your QR codes never need to be regenerated.

## Prerequisites
- Hetzner account + a small VPS (CX11 or CPX11 is plenty, ~€3-5/month)
- A domain you control (you already have one on Vercel)
- SSH access to the VPS

## 1. Set up the VPS (one-time)

SSH into your Hetzner VPS as root:

```bash
apt update && apt upgrade -y
apt install python3-pip python3-venv python3-dev nginx certbot python3-certbot-nginx git ufw -y

# Create user
useradd -m -s /bin/bash www-data || true
usermod -aG sudo www-data || true

# Firewall (optional but recommended)
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
```

## 2. Deploy the app

```bash
# As root or with sudo
mkdir -p /opt
cd /opt
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git birthday-hunt   # or scp/rsync your local folder
cd birthday-hunt

# Create virtualenv and install
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install flask gunicorn   # add any other deps if you have a requirements.txt

# Copy your current code if you didn't git clone
# (scp -r /local/path/to/Birthday root@YOUR_IP:/opt/birthday-hunt )

# Create directories
mkdir -p uploads qr-codes-v2 static
chmod 755 uploads

# Set a strong secret
echo "SECRET_KEY=put-a-very-long-random-string-here" >> .env
echo "ADMIN_PASSWORD=your-new-admin-password" >> .env
```

## 3. Systemd service (keeps the app running)

Copy the provided `birthday-hunt.service` to the VPS:

```bash
# On your local machine
scp birthday-hunt.service root@YOUR_HETZNER_IP:/etc/systemd/system/
```

On the VPS:

```bash
systemctl daemon-reload
systemctl enable birthday-hunt
systemctl start birthday-hunt
systemctl status birthday-hunt   # should be active
```

## 4. Nginx + HTTPS (stable subdomain)

Copy the nginx config:

```bash
scp birthday-hunt.nginx root@YOUR_HETZNER_IP:/etc/nginx/sites-available/birthday-hunt
```

On VPS:

```bash
ln -s /etc/nginx/sites-available/birthday-hunt /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Edit the server_name
nano /etc/nginx/sites-available/birthday-hunt
# Change "hunt.yourdomain.com" to your real subdomain

nginx -t
systemctl reload nginx
```

## 5. Get HTTPS (Let's Encrypt)

```bash
certbot --nginx -d hunt.yourdomain.com
# Follow the prompts. It will automatically update the nginx config for HTTPS.
```

Your site is now live at **https://hunt.yourdomain.com**

## 6. Add the subdomain in Vercel (you have to do this yourself)

**Important:** I cannot create or modify anything in your Vercel account. You (or someone with access to the Vercel project) must do this in the dashboard.

### Steps in Vercel:
1. Go to [vercel.com](https://vercel.com) and log in.
2. Select the project that owns your main domain.
3. Go to **Settings → Domains**.
4. In the "Add Domain" box, type your desired subdomain, for example:
   - `hunt.yourdomain.com`
   - `klaudusia-hunt.yourdomain.com`
   - `scavenger.yourdomain.com`
5. Click **Add**.
6. Vercel will show you the required DNS records.

### How to fill the exact DNS form you showed (Vercel):

- **Name** (this is the "subdomain" field):  
  `hunt`  
  (Replace with whatever prefix you want. This creates `hunt.yourdomain.com`)

- **Type**:  
  `A` (already selected)

- **Value**:  
  The **public IPv4 address of your Hetzner server**  
  (Go to Hetzner Console → Servers → cpx32 → look at the top or Networking section for the public IPv4, e.g. `65.108.123.45`. The 178.105.76.81 you used is likely your home IP, not the server's. Hetzner Cloud public IPv4s are typically in 65.108.x.x, 95.217.x.x, 88.99.x.x etc. ranges.)  
  Replace the current value with the real one from the console.

- **TTL**:  
  `60` (you already have this set — perfect for testing)

- **Priority**:  
  Leave blank (A records do not use Priority)

- **Comment**:  
  `Points to Hetzner VPS for the scavenger hunt app` (optional but useful)

Click **Add** (or Save).

### After saving the record:
Wait a few minutes (TTL 60 makes it fast).  
Test with:
```bash
dig hunt.yourdomain.com +short
```
It should return your Hetzner IP.

Once DNS resolves, go to your Hetzner server and run:
```bash
sudo certbot --nginx -d hunt.yourdomain.com
```
(replace with your actual subdomain)

Then generate the QR codes with the stable URL (run this on any computer with the code):
```bash
cd /home/kasparov/Birthday
python3 generate_qr.py https://hunt.yourdomain.com
```
- **A record**: 
  - Name/Host: `hunt` (or the subdomain prefix you chose)
  - Value: Your Hetzner VPS public IPv4 address
- TTL: 300 (or the lowest available) for faster propagation

If Vercel manages your nameservers, you can add the record directly in the Vercel DNS tab.

Propagation usually takes 5–60 minutes (sometimes longer). You can check with:
```bash
dig hunt.yourdomain.com
```

Once the DNS is pointing correctly to your Hetzner IP, continue with the next steps (nginx + certbot on the VPS).

## 7. Generate stable QR codes (once)

On any machine that has the code:

```bash
cd /path/to/Birthday
python3 generate_qr.py https://hunt.yourdomain.com
```

All QR codes in `qr-codes-v2/` now permanently point to your stable domain. Print them and you're done — they will never change.

## 8. Useful commands on the VPS

```bash
# View logs
journalctl -u birthday-hunt -f

# Restart app
systemctl restart birthday-hunt

# Restart nginx
systemctl reload nginx

# Renew SSL (certbot does this automatically via cron)
certbot renew --dry-run
```

## Tips for Vercel + Hetzner

- You don't need to "buy" the subdomain on Vercel — just add an A record at your registrar or in Vercel's DNS settings pointing the subdomain to the Hetzner IP.
- If you want to manage everything inside Vercel, you can point a CNAME if Hetzner supports it, but A record is simplest and most reliable.
- Keep your VPS firewall tight (only allow 80/443 from anywhere, 22 from your IP if possible).

This setup gives you:
- Permanent domain (QR codes stable forever)
- Always-on (Hetzner VPS doesn't sleep)
- Proper HTTPS (required for camera/scanner on modern phones)
- Cheap (~€4-6/month)

Once set up, you only ever need to regenerate QRs if you decide to change the subdomain.

Good luck with the hunt! Let me know if you hit any snag during the Hetzner setup.