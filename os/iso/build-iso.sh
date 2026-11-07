#!/usr/bin/env bash
# Builds BulbOS.iso: the official Debian netinst ISO + an automatic installer (preseed)
# that installs Bulb OS. Runs on Ubuntu/Debian with xorriso, curl, cpio and gzip.
#   os/iso/build-iso.sh [output.iso]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OS="$(dirname "$HERE")"
OUT="${1:-$PWD/BulbOS.iso}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
MIRROR="${DEBIAN_CD:-https://cdimage.debian.org/debian-cd/current/amd64/iso-cd}"

echo "▸ Finding the current Debian netinst ISO"
curl -fsSL "$MIRROR/SHA256SUMS" -o "$WORK/SHA256SUMS"
ISO_NAME="$(grep -o 'debian-[0-9.]*-amd64-netinst\.iso' "$WORK/SHA256SUMS" | head -1)"
[ -n "$ISO_NAME" ] || { echo "Couldn't find a netinst ISO in $MIRROR"; exit 1; }
echo "  $ISO_NAME"
curl -fL --retry 3 -o "$WORK/$ISO_NAME" "$MIRROR/$ISO_NAME"
( cd "$WORK" && grep " $ISO_NAME\$" SHA256SUMS | sha256sum -c - )

echo "▸ Preparing the Bulb payload"
mkdir -p "$WORK/bulb"
cp -r "$OS/server" "$OS/web" "$OS/system" "$WORK/bulb/"
VERSION="$(grep -o 'VERSION = "[^"]*"' "$OS/server/bulb.py" | cut -d'"' -f2)"
echo "$VERSION" > "$WORK/bulb/VERSION"
find "$WORK/bulb" -name "__pycache__" -prune -exec rm -rf {} +

echo "▸ Writing boot menus (BIOS + UEFI)"
xorriso -osirrox on -indev "$WORK/$ISO_NAME" -extract /boot/grub/grub.cfg "$WORK/grub.orig.cfg" \
        -extract /isolinux/isolinux.cfg "$WORK/isolinux.orig.cfg" >/dev/null 2>&1
chmod u+w "$WORK"/*.cfg
ARGS="auto=true priority=critical preseed/file=/cdrom/preseed.cfg locale=en_US.UTF-8 keymap=us hostname=bulb domain=local"

cat > "$WORK/grub.cfg" <<G
set default=0
set timeout=-1
menuentry "Install Bulb OS  (erases this disk)" {
    linux    /install.amd/vmlinuz $ARGS vga=788 --- quiet
    initrd   /install.amd/initrd.gz
}
menuentry "Install Bulb OS  (show the installer's questions)" {
    linux    /install.amd/vmlinuz preseed/file=/cdrom/preseed.cfg hostname=bulb domain=local --- quiet
    initrd   /install.amd/initrd.gz
}
menuentry "Debian installer menu" {
    configfile /boot/grub/grub.debian.cfg
}
G
cat > "$WORK/isolinux.cfg" <<I
default bulb
prompt 1
timeout 0
say
say  Bulb OS installer. Press Enter to install (this ERASES the disk), or type 'manual'.
say
label bulb
    kernel /install.amd/vmlinuz
    append $ARGS vga=788 initrd=/install.amd/initrd.gz --- quiet
label manual
    kernel /install.amd/vmlinuz
    append preseed/file=/cdrom/preseed.cfg hostname=bulb domain=local initrd=/install.amd/initrd.gz --- quiet
I

echo "▸ Building $OUT"
rm -f "$OUT"
xorriso -indev "$WORK/$ISO_NAME" -outdev "$OUT" \
  -map "$HERE/preseed.cfg" /preseed.cfg \
  -map "$WORK/bulb" /bulb \
  -map "$WORK/grub.orig.cfg" /boot/grub/grub.debian.cfg \
  -map "$WORK/grub.cfg" /boot/grub/grub.cfg \
  -map "$WORK/isolinux.cfg" /isolinux/isolinux.cfg \
  -boot_image any replay
ls -lh "$OUT"
sha256sum "$OUT" | tee "$OUT.sha256"
echo "✓ Done: Bulb OS $VERSION on $ISO_NAME"
