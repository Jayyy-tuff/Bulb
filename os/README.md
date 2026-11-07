# Bulb OS

Bulb OS is the cloud half of Bulb, set up the way umbrelOS is. You install it on a
virtual machine (or a spare PC), then manage everything from a browser at
**https://bulb.local**:

- create the owner account, then invite family and friends
- upload PC games, retro ROMs (NES → 3DS) and HTML5 games
- see storage, signed-in devices, and restart or shut down the box

The **Bulb app** on Android is the bridge. You sign in to bulb.local, pick a game,
and the app downloads it and runs it on the phone with Wine. Saves sync back to the
cloud. Retro and HTML5 games also play right in the browser.

## Install in VirtualBox on Windows

1. Get **BulbOS.iso**: Actions → *Build Bulb OS ISO* → latest run → *Artifacts*
   (or from a Release).
2. Install [VirtualBox](https://www.virtualbox.org/) and click **New**:
   - Name: `Bulb OS`. ISO image: **BulbOS.iso**. Type: Linux. Version: Debian (64-bit).
   - Tick **Skip Unattended Installation**. Bulb has its own installer.
   - Memory: **4096 MB** (2048 works). CPUs: 2.
   - Hard disk: **100 GB or more**, *dynamically allocated*. Games live here, so
     give it room. It only uses real space as you fill it.
3. Before starting the VM, open **Settings**:
   - **System → Motherboard:** tick **Enable EFI**.
   - **Network → Adapter 1:** set *Attached to:* **Bridged Adapter**, and pick
     your Wi-Fi card. NAT would hide the VM from your phone.
4. **Start** the VM. At the Bulb menu, press **Enter** on *Install Bulb OS*.
   This erases the VM's virtual disk only, not your Windows drive.
5. Wait 10–20 minutes. It downloads packages from Debian, installs, and
   reboots on its own. If it boots back into the installer menu, close the VM,
   remove the ISO under Settings → Storage, and start it again.
6. When the console shows **Bulb OS — open https://bulb.local**, open that address
   on your phone or laptop. Accept the certificate warning once, then create
   your account.

If `bulb.local` doesn't open, use the IP shown on the VM's console
(`https://192.168.x.x`). Android browsers often can't resolve `.local` names.
The Bulb app finds the cloud by itself.

**VMware Workstation Player** works too: EFI firmware, bridged network, and the
same steps. Hyper-V isn't tested.

The cloud is only online while the laptop and the VM are running. Games you've
already downloaded keep working in the app offline.

## Console

Log in on the VM's screen as `bulb` / `bulb` (change it with `passwd`). Commands:

```
bulbctl status          # running? address? storage?
bulbctl logs            # follow the log
bulbctl restart
bulbctl url
bulbctl fingerprint     # compare with what the Bulb app shows on first connect
bulbctl reset-password <user> <new-password>
bulbctl update          # pull the latest Bulb OS from GitHub (main branch)
```

## How the ISO is made

`os/iso/build-iso.sh` takes the official Debian netinst ISO and adds three things:
an automatic installer answer file (`os/iso/preseed.cfg`), the Bulb files, and a
boot menu. GitHub Actions builds it, then installs it in a QEMU VM and checks that
Bulb answers. That's the *test-install* job.

The installed system is Debian with:
- Python
- Avahi (`bulb.local` and the `_bulb._tcp` service the app looks for)
- the `bulb` systemd service, running as the `bulbos` user, with data in
  `/var/lib/bulb`
