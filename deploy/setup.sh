#!/usr/bin/env bash
# Pasang GroupTask Tracker di Ubuntu (22.04/24.04): nginx + gunicorn + systemd.
# Jalankan dari dalam folder proyek:  sudo bash deploy/setup.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Jalankan dengan sudo."; exit 1; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/grouptask
WEB_DIR=/var/www/grouptask

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip nginx

# Backend
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/backend"
cp -r "$SRC/backend" "$APP_DIR/backend"
rm -rf "$APP_DIR/backend/venv"
python3 -m venv "$APP_DIR/backend/venv"
"$APP_DIR/backend/venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"
chown -R www-data:www-data "$APP_DIR"

# Frontend (API lewat nginx di alamat yang sama, tanpa CORS/port)
mkdir -p "$WEB_DIR"
cp "$SRC/frontend/index.html" "$WEB_DIR/index.html"
echo 'window.API_BASE = "/api";' > "$WEB_DIR/config.js"

# Konfigurasi rahasia (dibuat sekali; tidak ditimpa jika sudah ada)
# Untuk banyak instance, beri SECRET_KEY yang SAMA:
#   sudo SECRET_KEY=xxxx APP_URL=http://dns-alb bash deploy/setup.sh
ENV_FILE=/etc/grouptask.env
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOT
SECRET_KEY=${SECRET_KEY:-$(python3 -c 'import secrets;print(secrets.token_hex(32))')}
APP_URL=${APP_URL:-http://localhost}
# Email OTP dan undangan (kosongkan SMTP_HOST untuk mode dev tanpa email):
#SMTP_HOST=smtp.gmail.com
#SMTP_PORT=587
#SMTP_USER=emailanda@gmail.com
#SMTP_PASSWORD=app-password-16-karakter
#SMTP_FROM=emailanda@gmail.com
# Login Google (butuh domain HTTPS):
#GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
EOT
  chown root:www-data "$ENV_FILE"; chmod 640 "$ENV_FILE"
fi

# Service + nginx
cp "$SRC/deploy/grouptask.service" /etc/systemd/system/grouptask.service
systemctl daemon-reload
systemctl enable --now grouptask
systemctl restart grouptask

cp "$SRC/deploy/nginx.conf" /etc/nginx/sites-available/grouptask
ln -sf /etc/nginx/sites-available/grouptask /etc/nginx/sites-enabled/grouptask
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx

echo "Selesai. Buka http://<public-ip-instance> dan cek /api/health"
echo "Atur APP_URL/SMTP/GOOGLE_CLIENT_ID di $ENV_FILE lalu: sudo systemctl restart grouptask"
