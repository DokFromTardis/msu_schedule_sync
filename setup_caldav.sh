#!/usr/bin/env bash
set -euo pipefail

# Quick CalDAV setup script for Ubuntu VPS (Radicale + Nginx + vdirsyncer)
# Defaults target ilabaznikov.ru with a single calendar at /caldav/calendar
# that is auto-synced from a remote .ics timetable (group 104 by default).

DOMAIN=${DOMAIN:-"ilabaznikov.ru"}
GROUP=${GROUP:-"104"}
CALDAV_USER=${CALDAV_USER:-"group${GROUP}"}
CALDAV_PASS=${CALDAV_PASS:-"$(openssl rand -base64 18 | tr -d '=')"}
ICS_URL=${ICS_URL:-"https://example.com/group${GROUP}.ics"} # <-- set to real .ics URL
LE_EMAIL=${LE_EMAIL:-""} # set to request Let's Encrypt automatically

RADICALE_ROOT=/var/lib/radicale
RADICALE_CONF=/etc/radicale/config
RADICALE_USERS=/etc/radicale/users
RADICALE_VENV=/opt/radicale/venv
VDIRSYNCER_CONF=${RADICALE_ROOT}/.vdirsyncer/config

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root." >&2
    exit 1
  fi
}

apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3-venv python3-pip python3-dev build-essential \
    nginx apache2-utils certbot python3-certbot-nginx curl
}

create_user_and_dirs() {
  id radicale >/dev/null 2>&1 || adduser --system --group --home "$RADICALE_ROOT" --disabled-login radicale
  mkdir -p /opt/radicale "$RADICALE_ROOT"/collections /var/log/radicale /etc/radicale "$RADICALE_ROOT"/.vdirsyncer/status
  chown -R radicale:radicale /opt/radicale "$RADICALE_ROOT" /var/log/radicale
}

install_python_apps() {
  python3 -m venv "$RADICALE_VENV"
  "$RADICALE_VENV"/bin/pip install --upgrade pip
  "$RADICALE_VENV"/bin/pip install "radicale>=3,<4" "vdirsyncer>=0.19,<0.20" "passlib[bcrypt]"
}

write_radicale_config() {
  cat >"$RADICALE_CONF" <<'EOF'
[server]
hosts = 127.0.0.1:5232
max_connections = 20
timeout = 30
dns_lookup = False

[auth]
type = htpasswd
htpasswd_filename = /etc/radicale/users
htpasswd_encryption = bcrypt

[rights]
type = owner_only

[storage]
type = filesystem
filesystem_folder = /var/lib/radicale/collections

[logging]
config = /etc/radicale/logging
EOF

  cat > /etc/radicale/logging <<'EOF'
[loggers]
keys = root

[handlers]
keys = file

[formatters]
keys = simple

[logger_root]
level = INFO
handlers = file

[handler_file]
class = FileHandler
level = INFO
formatter = simple
args = ('/var/log/radicale/radicale.log', 'a')

[formatter_simple]
format = %(asctime)s - %(levelname)s - %(message)s
EOF
  chown -R radicale:radicale /etc/radicale /var/log/radicale
}

create_htpasswd() {
  if [ -f "$RADICALE_USERS" ]; then
    htpasswd -bB -C 12 "$RADICALE_USERS" "$CALDAV_USER" "$CALDAV_PASS"
  else
    htpasswd -bBC 12 "$RADICALE_USERS" "$CALDAV_USER" "$CALDAV_PASS"
  fi
  chown radicale:radicale "$RADICALE_USERS"
}

write_systemd_service() {
  cat >/etc/systemd/system/radicale.service <<EOF
[Unit]
Description=Radicale CalDAV/CardDAV server
After=network.target

[Service]
User=radicale
Group=radicale
ExecStart=$RADICALE_VENV/bin/radicale --config $RADICALE_CONF
Restart=on-failure
WorkingDirectory=$RADICALE_ROOT

[Install]
WantedBy=multi-user.target
EOF
}

write_nginx_site() {
  cat >/etc/nginx/sites-available/caldav.conf <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    # allow autodiscovery shortcuts
    location = /.well-known/caldav { return 301 /caldav/; }
    location = /.well-known/carddav { return 301 /caldav/; }

    # fix missing trailing slash
    location = /caldav { return 301 /caldav/; }

    location /caldav/ {
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header Host \$host;
        proxy_redirect off;
        proxy_pass http://127.0.0.1:5232/${CALDAV_USER}/;
    }
}
EOF

  ln -sf /etc/nginx/sites-available/caldav.conf /etc/nginx/sites-enabled/caldav.conf
  nginx -t
  systemctl reload nginx
}

maybe_enable_https() {
  if [ -n "$LE_EMAIL" ]; then
    certbot --nginx --redirect --agree-tos -m "$LE_EMAIL" -d "$DOMAIN" || true
  fi
}

write_vdirsyncer_config() {
  cat >"$VDIRSYNCER_CONF" <<EOF
[general]
status_path = "$RADICALE_ROOT/.vdirsyncer/status/"

[storage timetable_source]
type = "http"
url = "$ICS_URL"
read_only = true

[storage timetable_target]
type = "caldav"
url = "http://localhost:5232/${CALDAV_USER}/calendar/"
username = "$CALDAV_USER"
password = "$CALDAV_PASS"

[pair timetable]
a = "timetable_source"
b = "timetable_target"
collections = ["from a"]
conflict_resolution = "a wins"
metadata = ["displayname", "color"]
EOF
  chown -R radicale:radicale "$RADICALE_ROOT/.vdirsyncer"
}

write_vdirsyncer_unit() {
  cat >/etc/systemd/system/vdirsyncer@radicale.service <<EOF
[Unit]
Description=Vdirsyncer sync for %i
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=%i
Group=%i
ExecStart=$RADICALE_VENV/bin/vdirsyncer -c $VDIRSYNCER_CONF sync
EOF

  cat >/etc/systemd/system/vdirsyncer@radicale.timer <<'EOF'
[Unit]
Description=Run vdirsyncer every 30 minutes for %i

[Timer]
OnCalendar=*:0/30
Persistent=true

[Install]
WantedBy=timers.target
EOF
}

enable_services() {
  systemctl daemon-reload
  systemctl enable --now radicale.service
  sudo -u radicale $RADICALE_VENV/bin/vdirsyncer -c "$VDIRSYNCER_CONF" sync || true
  systemctl enable --now vdirsyncer@radicale.timer
}

show_summary() {
  cat <<EOF
CalDAV ready.

- Base URL: http://$DOMAIN/caldav/
- Calendar URL: http://$DOMAIN/caldav/calendar (proxies to user $CALDAV_USER)
- Username: $CALDAV_USER
- Password: $CALDAV_PASS
- ICS source: $ICS_URL

If HTTPS was not enabled automatically, run:
  LE_EMAIL=you@example.com certbot --nginx --redirect -d $DOMAIN
EOF
}

main() {
  require_root
  apt_install
  create_user_and_dirs
  install_python_apps
  write_radicale_config
  create_htpasswd
  write_systemd_service
  write_nginx_site
  maybe_enable_https
  write_vdirsyncer_config
  write_vdirsyncer_unit
  enable_services
  show_summary
}

main "$@"
