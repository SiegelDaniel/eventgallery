# Deploying Event Gallery to eventgallery.siegel.cc

Target: Ubuntu 24.04 VPS, served at `https://eventgallery.siegel.cc`.
Architecture: **gunicorn** (private, localhost:8000) ← **nginx** (public, TLS) ← **Certbot** (Let's Encrypt).

## 0. DNS (done)

GoDaddy A record: `eventgallery` → `<your-VPS-IP>`.
Verify it resolves before continuing:

```bash
dig +short eventgallery.siegel.cc      # should print your VPS IP
```

## 1. Get the code onto the VPS

```bash
sudo mkdir -p /opt/eventgallery
sudo chown $USER:$USER /opt/eventgallery
# copy the project in (git clone, scp, rsync, etc.) so app.py lives at /opt/eventgallery/app.py

cd /opt/eventgallery
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# where uploaded photos are stored (kept out of the app dir so redeploys don't touch them)
sudo mkdir -p /var/lib/eventgallery/uploads
sudo chown -R www-data:www-data /var/lib/eventgallery
```

## 2. Run the app as a service

`/etc/systemd/system/eventgallery.service`:

```ini
[Unit]
Description=Event Gallery
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/eventgallery
Environment=UPLOAD_DIR=/var/lib/eventgallery/uploads
Environment=MAX_CONTENT_MB=50
ExecStart=/opt/eventgallery/.venv/bin/gunicorn -w 3 -b 127.0.0.1:8000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

Note: **no `APPLICATION_ROOT`** — the app serves at the root of its own subdomain.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now eventgallery
systemctl status eventgallery          # should be active (running)
curl -I http://127.0.0.1:8000/          # should return HTTP 200 locally
```

## 3. nginx

```bash
sudo apt update
sudo apt install nginx
sudo ufw allow 'Nginx Full'            # if ufw is active (opens 80 + 443)
```

`/etc/nginx/sites-available/eventgallery.siegel.cc`:

```nginx
server {
    listen 80;
    server_name eventgallery.siegel.cc;

    client_max_body_size 60m;          # allow large photo uploads

    location / {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/eventgallery.siegel.cc /etc/nginx/sites-enabled/
sudo nginx -t                          # syntax check — never skip
sudo systemctl reload nginx
```

Now `http://eventgallery.siegel.cc` should load the app over plain HTTP. Confirm before the next step.

## 4. SSL

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d eventgallery.siegel.cc
```

Certbot validates the domain over port 80, rewrites the nginx file to add the `443`/TLS block + HTTP→HTTPS redirect, and installs an auto-renewal timer (certs last 90 days).

```bash
sudo certbot renew --dry-run           # confirm auto-renewal works
```

Done: **https://eventgallery.siegel.cc** is live and encrypted.

## Updating later

```bash
cd /opt/eventgallery
git pull                               # or re-copy files
.venv/bin/pip install -r requirements.txt
sudo systemctl restart eventgallery
```

## Notes

- **No authentication** — anyone with the link can upload/delete. To gate it, add nginx basic auth:
  `sudo apt install apache2-utils && sudo htpasswd -c /etc/nginx/.htpasswd guest`,
  then inside the `location / { }` block: `auth_basic "Event Gallery"; auth_basic_user_file /etc/nginx/.htpasswd;` and reload nginx.
- **Back up** `/var/lib/eventgallery/uploads` — that's where the photos live.
- `MAX_CONTENT_MB` (app) and `client_max_body_size` (nginx) must both be ≥ your largest upload; keep nginx a bit higher.
