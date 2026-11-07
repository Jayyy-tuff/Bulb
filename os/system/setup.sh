#!/bin/sh
# Runs inside the freshly installed system (from the installer's late_command,
# or by `bulbctl update`). Installs Bulb OS's services and branding.
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"

install -d /opt/bulb
rm -rf /opt/bulb/server /opt/bulb/web
cp -r "$SRC/server" "$SRC/web" /opt/bulb/
install -m 755 "$SRC/system/bulbctl" /usr/local/bin/bulbctl
[ -f "$SRC/VERSION" ] && cp "$SRC/VERSION" /opt/bulb/VERSION

# Service account + data folder
id bulbos >/dev/null 2>&1 || useradd --system --home-dir /var/lib/bulb --shell /usr/sbin/nologin bulbos
install -d -o bulbos -g bulbos -m 700 /var/lib/bulb

# Bulb may restart / shut down the box from Admin (owner only)
cat > /etc/sudoers.d/bulbos <<'S'
bulbos ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
S
chmod 440 /etc/sudoers.d/bulbos

cat > /etc/systemd/system/bulb.service <<'U'
[Unit]
Description=Bulb OS
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
User=bulbos
Group=bulbos
Environment=BULB_HOME=/opt/bulb
Environment=BULB_DATA=/var/lib/bulb
ExecStart=/usr/bin/python3 -u /opt/bulb/server/bulb.py
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
U

# Advertise Bulb on the network (the app finds it with this; browsers use bulb.local)
install -d /etc/avahi/services
cat > /etc/avahi/services/bulb.service <<'A'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Bulb OS on %h</name>
  <service><type>_bulb._tcp</type><port>443</port><txt-record>path=/</txt-record></service>
  <service><type>_https._tcp</type><port>443</port></service>
</service-group>
A

# Console banner, like umbrelOS: shows where to go
cat > /etc/issue <<'I'

  \e{bold}Bulb OS\e{reset}

  Open  https://bulb.local  in a browser on your network
  (or   https://\4 )

  Console login: bulb / bulb  (change it with: passwd)

I
cp /etc/issue /etc/issue.net
cat > /etc/motd <<'M'

  Bulb OS. Manage it from https://bulb.local
  Commands: bulbctl status | logs | restart | url | fingerprint | reset-password | update

M

hostnamectl set-hostname bulb 2>/dev/null || echo bulb > /etc/hostname
grep -q "bulb" /etc/hosts || sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tbulb.local bulb/' /etc/hosts

systemctl enable bulb.service avahi-daemon.service >/dev/null 2>&1 || true
systemctl daemon-reload 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true
systemctl restart bulb 2>/dev/null || true
case "$SRC" in /opt/bulb-installer*) rm -rf /opt/bulb-installer ;; esac
echo "Bulb OS installed."
