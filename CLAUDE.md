# arch-atomic: Declarative Arch Linux System Manager

WARNING: this file should be used as a reference but not taken as gospel for anything, because it certainly has many mistakes

## Overview

arch-atomic is a tool for declaratively managing Arch Linux systems with atomic updates, instant rollback, and live generation switching. It combines NixOS-style declarative configuration with standard Arch packages.

### Core Philosophy

- **Declarative**: System state defined in Python config, not accumulated through manual changes
- **Atomic**: Generations are complete, immutable snapshots; switch is all-or-nothing
- **Upstream-compatible**: Uses standard Arch packages from official repos and AUR
- **Simple**: Minimal caching logic; current generation is the cache

### Key Differentiators from NixOS

| Aspect | NixOS | arch-atomic |
|--------|-------|-------------|
| Packages | Nix store, symlink farms | Standard pacman packages |
| Reproducibility | Bit-for-bit (in theory) | "Good enough" (pinned repos) |
| Build from source | Central to design | Not required |
| Learning curve | Steep (Nix language) | Shallow (Python) |
| Upstream compatibility | Often painful | Native Arch packages |

---

## Architecture

### Filesystem Layout

```
/                           (tmpfs, created fresh each boot)
├── current → /images/gen-42    (symlink to active generation)
├── usr → /current/usr          (symlink)
├── etc → /current/etc          (symlink)
├── bin → usr/bin               (standard symlink)
├── lib → usr/lib               (standard symlink)
├── sbin → usr/bin              (standard symlink)
├── boot → /current/boot        (symlink to generation's kernel)
├── efi/                        (ESP mount, FAT32, for GRUB)
├── images/                     (btrfs mount point)
├── var/                        (btrfs subvol mount, persistent)
├── home/                       (btrfs subvol mount, persistent)
├── tmp/                        (directory on tmpfs, mode 1777)
├── root → /home/root           (symlink to persistent root home)
├── mnt/                        (empty directory)
├── dev/                        (devtmpfs mount)
├── proc/                       (procfs mount)
├── sys/                        (sysfs mount)
└── run/                        (tmpfs mount)

/images/                    (btrfs subvolume container)
├── gen-41/                 (previous generation, ro)
│   ├── usr/
│   ├── etc/
│   ├── boot/
│   │   ├── vmlinuz-linux
│   │   └── initramfs-linux.img
│   └── manifest.json
└── gen-42/                 (current generation, ro)
    └── ...

/var/                       (@var btrfs subvol, persistent, rw)
├── cache/pacman/pkg/       (package cache)
├── lib/                    (databases, state)
├── log/
└── lib/users/              (passwd, shadow, group - see Users section)

/home/                      (@home btrfs subvol, persistent, rw)
```

### Why tmpfs Root with Symlinks

1. **Live switching**: Update `/current` symlink = instant switch to new generation
2. **Immutability**: Generation subvolumes stay read-only; only symlinks change (only the root tmpfs is written to when changing generation)
3. **Clean separation**: Stateless scaffolding (tmpfs) vs persistent data (btrfs subvols)
4. **No drift**: tmpfs rebuilt each boot from declared state

### Btrfs Subvolume Structure

```
@images     (contains all generations)
@var        (persistent variable data)
@home       (user data)
```

---

## Configuration DSL

### Config Class

```python
# arch_atomic/config.py

from dataclasses import dataclass, field
from typing import Dict, Set
import hashlib

@dataclass
class Config:
    name: str
    base_packages: Set[str] = field(default_factory=set)
    packages: Set[str] = field(default_factory=set)
    files: Dict[str, str] = field(default_factory=dict)      # path -> content
    symlinks: Dict[str, str] = field(default_factory=dict)   # path -> target
    
    def add_base_packages(self, *names: str):
        """Packages installed via pacstrap (base, linux, etc.)"""
        self.base_packages.update(names)
    
    def add_packages(self, *names: str):
        """Packages installed via pacman -S after base"""
        self.packages.update(names)
    
    def add_file(self, path: str, content: str):
        """Add a file with given content"""
        self.files[path] = content
    
    def add_symlink(self, path: str, target: str):
        """Add a symbolic link"""
        self.symlinks[path] = target
    
    def add_service(self, name: str):
        """Enable a systemd service (creates symlink)"""
        self.add_symlink(
            f"/etc/systemd/system/multi-user.target.wants/{name}.service",
            f"/usr/lib/systemd/system/{name}.service"
        )
    
    def manifest(self) -> dict:
        """Generate manifest for this configuration"""
        return {
            "name": self.name,
            "base_packages": sorted(self.base_packages),
            "packages": sorted(self.packages),
            "files": dict(self.files),  # store full content
            "symlinks": dict(self.symlinks),
        }
```

### Example Configuration

```python
# config.py

from arch_atomic import Config

def configure(c: Config):
    # Common to all machines
    c.add_base_packages("base", "linux", "linux-firmware", "btrfs-progs")
    c.add_packages("networkmanager", "vim", "git", "htop")
    c.add_service("NetworkManager")
    
    if c.name == "desktop":
        c.add_packages("nvidia", "sway", "foot", "firefox", "steam")
        c.add_file("/etc/hostname", "desktop")
        c.add_symlink("/etc/localtime", "/usr/share/zoneinfo/Europe/Oslo")
        c.add_file("/etc/vconsole.conf", "KEYMAP=no")
        c.add_file("/etc/locale.conf", "LANG=en_US.UTF-8")
        enable_sway(c)
    
    elif c.name == "laptop":
        c.add_packages("intel-ucode", "sway", "foot", "firefox", "tlp")
        c.add_file("/etc/hostname", "laptop")
        c.add_service("tlp")
        enable_sway(c)
    
    elif c.name == "server":
        c.add_base_packages("linux-lts")  # LTS kernel for server
        c.add_packages("docker", "nginx", "certbot")
        c.add_file("/etc/hostname", "server")
        c.add_service("docker")
        c.add_service("nginx")
    
    elif c.name == "installer":
        c.add_packages(
            "arch-install-scripts", 
            "parted", 
            "grub", 
            "efibootmgr",
            "btrfs-progs"
        )
        c.add_file("/root/install.sh", INSTALL_SCRIPT)
        c.add_file("/root/config.py", open("config.py").read())
        # Auto-login for installer
        c.add_file(
            "/etc/systemd/system/getty@tty1.service.d/autologin.conf",
            "[Service]\nExecStart=\nExecStart=-/usr/bin/agetty --autologin root --noclear %I $TERM\n"
        )

# Helper functions
def enable_sway(c: Config):
    c.add_packages("sway", "swaylock", "swayidle", "foot", "fuzzel", "waybar")
    c.add_file("/etc/sway/config", SWAY_CONFIG)

def set_timezone(c: Config, tz: str):
    c.add_symlink("/etc/localtime", f"/usr/share/zoneinfo/{tz}")

def set_locale(c: Config, locale: str):
    c.add_file("/etc/locale.conf", f"LANG={locale}")
    c.add_file("/etc/locale.gen", f"{locale} UTF-8\n")
```

---

## Build Process

### Two-Phase Build

**Phase 1: Outside (setup)**
- Create or snapshot btrfs subvolume (depending on whether first generation or not)
- Mount as working directory
- Run pacstrap for base packages (if fresh build)
- Write manifest.json into the build
- Call arch-chroot with inner tool

**Phase 2: Inside chroot**
- Install/remove packages to match manifest
- Run pacman -Syu
- Clean up pacnew/pacsave files
- Write declared config files and symlinks

### Apply (Incremental Update)

```python
def apply(config: Config):
    new_manifest = config.manifest()
    gen_num = next_generation_number()
    
    if has_current_generation():
        old_manifest = read_manifest("/images/current/manifest.json")
        
        # Clone current as starting point
        run(["btrfs", "subvol", "snapshot", 
             "/images/current", f"/images/gen-{gen_num}"])
    else:
        old_manifest = None
        run(["btrfs", "subvol", "create", f"/images/gen-{gen_num}"])
    
    # Mount working directory
    run(["mount", f"/images/gen-{gen_num}", "/mnt/build"])
    
    if old_manifest is None:
        # Fresh build: pacstrap base packages
        run(["pacstrap", "/mnt/build"] + list(config.base_packages))
    
    # Write manifest for inner phase
    write_json("/mnt/build/manifest.json", new_manifest)
    
    # Run inner phase in chroot
    old_manifest_arg = json.dumps(old_manifest) if old_manifest else ""
    run(["arch-chroot", "/mnt/build", 
         "arch-atomic-chroot", "--updateFrom", old_manifest_arg])
    
    # Finalize
    run(["umount", "/mnt/build"])
    update_grub(gen_num)
    live_switch(gen_num)
```

### Chroot Phase

```python
def chroot_main(update_from: Optional[str]):
    new_manifest = read_json("/manifest.json")
    old_manifest = json.loads(update_from) if update_from else None

    if old_manifest:
        # Incremental: handle base_packages and packages
        old_base = set(old_manifest["base_packages"])
        new_base = set(new_manifest["base_packages"])
        old_pkgs = set(old_manifest["packages"])
        new_pkgs = set(new_manifest["packages"])

        # Combine all package changes
        to_add = (new_base | new_pkgs) - (old_base | old_pkgs)
        to_remove = (old_base | old_pkgs) - (new_base | new_pkgs)

        if to_add:
            run(["pacman", "-S", "--needed", "--noconfirm"] + list(to_add))
        if to_remove:
            run(["pacman", "-Rns", "--noconfirm"] + list(to_remove))
    else:
        # Fresh: install all packages (excluding base_packages already installed by pacstrap)
        all_pkgs = set(new_manifest["packages"]) - set(new_manifest["base_packages"])
        if all_pkgs:
            run(["pacman", "-S", "--needed", "--noconfirm"] + list(all_pkgs))

    # Always update
    run(["pacman", "-Syu", "--noconfirm"])

    # Clean up pacman noise
    for pattern in ["*.pacnew", "*.pacsave"]:
        for f in glob(f"/etc/**/{pattern}", recursive=True):
            os.remove(f)

    # Write declared files (atomically)
    for path, content in new_manifest["files"].items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(content)
        os.rename(tmp_path, path)

    # Create declared symlinks
    for path, target in new_manifest["symlinks"].items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.lexists(path):
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        os.symlink(target, path)

    # Generate locales if locale.gen exists
    if os.path.exists("/etc/locale.gen"):
        run(["locale-gen"])
```

### Rebuild (From Scratch)

```python
def rebuild(config: Config):
    """Full rebuild, no incremental - eliminates any drift"""
    gen_num = next_generation_number()
    
    run(["btrfs", "subvol", "create", f"/images/gen-{gen_num}"])
    run(["mount", f"/images/gen-{gen_num}", "/mnt/build"])
    
    # Full pacstrap
    run(["pacstrap", "/mnt/build"] + list(config.base_packages))
    
    # Write manifest
    write_json("/mnt/build/manifest.json", config.manifest())
    
    # Chroot with no previous manifest
    run(["arch-chroot", "/mnt/build", "arch-atomic-chroot"])
    
    run(["umount", "/mnt/build"])
    update_grub(gen_num)
    live_switch(gen_num)
```

---

## Boot Process

### GRUB Configuration

GRUB on ESP reads kernels directly from btrfs subvolumes:

```bash
# /efi/grub/grub.cfg

set timeout=5
set default=0

insmod btrfs
search --set=root --fs-uuid <BTRFS-UUID>

menuentry "Arch Linux (gen-42)" {
    linux /@images/gen-42/boot/vmlinuz-linux \
        btrfs_uuid=<BTRFS-UUID> \
        gen=42 \
        ro
    initrd /@images/gen-42/boot/initramfs-linux.img
}

menuentry "Arch Linux (gen-41) [rollback]" {
    linux /@images/gen-41/boot/vmlinuz-linux \
        btrfs_uuid=<BTRFS-UUID> \
        gen=41 \
        ro
    initrd /@images/gen-41/boot/initramfs-linux.img
}
```

### Custom Initramfs Hook

```bash
# /etc/initcpio/install/arch-atomic

#!/bin/bash
build() {
    add_runscript
}

help() {
    cat <<EOF
Sets up tmpfs root with symlinks to generation.
EOF
}
```

```bash
# /etc/initcpio/hooks/arch-atomic

#!/bin/bash
run_hook() {
    # Get parameters from kernel cmdline
    local gen btrfs_uuid
    for param in $(cat /proc/cmdline); do
        case "$param" in
            gen=*) gen="${param#gen=}" ;;
            btrfs_uuid=*) btrfs_uuid="${param#btrfs_uuid=}" ;;
        esac
    done

    # Mount btrfs root temporarily
    mkdir -p /mnt/btrfs
    mount -t btrfs "UUID=${btrfs_uuid}" /mnt/btrfs

    # Setup tmpfs root
    mount -t tmpfs -o size=2G tmpfs /new_root

    # Create directory structure
    mkdir -p /new_root/{dev,proc,sys,run,tmp,mnt,root}
    mkdir -p /new_root/{images,var,home,efi}
    chmod 1777 /new_root/tmp

    # Bind mount subvolumes to new root
    mount --bind /mnt/btrfs/@images /new_root/images
    mount --bind /mnt/btrfs/@var /new_root/var
    mount --bind /mnt/btrfs/@home /new_root/home

    # Symlinks to generation
    ln -s /images/gen-${gen} /new_root/current
    ln -s /current/usr /new_root/usr
    ln -s /current/etc /new_root/etc
    ln -s /current/boot /new_root/boot
    ln -s usr/bin /new_root/bin
    ln -s usr/lib /new_root/lib
    ln -s usr/bin /new_root/sbin
    ln -s /home/root /new_root/root

    # Mount ESP
    mount UUID=<ESP-UUID> /new_root/efi

    # Unmount temporary btrfs mount (subvolumes already bind-mounted)
    umount /mnt/btrfs
}
```

```bash
# /etc/mkinitcpio.conf
HOOKS=(base udev autodetect microcode modconf block btrfs arch-atomic filesystems keyboard)
```

---

## Live Switching

### How It Works

Both generations are read-only. Running processes keep handles to old generation files. New processes use new generation via updated symlinks.

```python
def update_grub(gen_num: int):
    """Regenerate GRUB config with new generation as default"""
    btrfs_uuid = run_output(["findmnt", "-no", "UUID", "/images"]).strip()
    esp_uuid = run_output(["findmnt", "-no", "UUID", "/efi"]).strip()

    # List all generations (sorted descending)
    generations = sorted(
        [int(d.name.split("-")[1]) for d in Path("/images").iterdir()
         if d.is_dir() and d.name.startswith("gen-")],
        reverse=True
    )

    grub_cfg = f"""# Generated by arch-atomic
set timeout=5
set default=0

insmod btrfs
search --set=root --fs-uuid {btrfs_uuid}

"""
    for i, gen in enumerate(generations):
        label = "" if i == 0 else " [rollback]"
        grub_cfg += f"""menuentry "Arch Linux (gen-{gen}){label}" {{
    linux /@images/gen-{gen}/boot/vmlinuz-linux \\
        btrfs_uuid={btrfs_uuid} \\
        gen={gen} \\
        ro
    initrd /@images/gen-{gen}/boot/initramfs-linux.img
}}

"""

    # Write atomically
    tmp_path = "/efi/grub/grub.cfg.new"
    with open(tmp_path, "w") as f:
        f.write(grub_cfg)
    os.rename(tmp_path, "/efi/grub/grub.cfg")


def live_switch(gen_num: int):
    """Switch to new generation without reboot"""

    # Atomic symlink update
    tmp_link = "/current.new"
    os.symlink(f"/images/gen-{gen_num}", tmp_link)
    os.rename(tmp_link, "/current")  # atomic

    print(f"Switched to gen-{gen_num}")
    print("Running processes use old generation until restarted")
    print("Reboot for kernel/initramfs changes")
```

### What Survives

| Component | Behavior |
|-----------|----------|
| Running processes | Keep using old generation files |
| New process spawns | Use new generation |
| Open file descriptors | Still valid (old inodes) |
| mmap'd libraries | Old versions until process restart |
| Kernel | Old until reboot |
| systemd (PID 1) | Old until reboot |

---

## User Management

Users are **not** managed declaratively. They are persistent state in `/var`.

### Setup

```bash
# Symlinks in /etc point to /var/lib/users/
/etc/passwd → /var/lib/users/passwd
/etc/shadow → /var/lib/users/shadow
/etc/group → /var/lib/users/group
/etc/gshadow → /var/lib/users/gshadow
```

### Initial User Setup

```bash
# One-time, outside the tool
useradd -m robin
passwd robin
usermod -aG wheel,docker robin
```

These files persist across generations because they live in `/var`.

---

## AUR Package Vendoring

### Philosophy

- PKGBUILDs version-controlled for reproducibility
- Local repository for offline builds
- Same interface as official packages

### Directory Structure

```
/aur/
├── PKGBUILDs/
│   ├── yay-bin/
│   │   └── PKGBUILD
│   ├── my-package/
│   │   └── PKGBUILD
│   └── ...
├── packages/
│   ├── yay-bin-12.3.0-1-x86_64.pkg.tar.zst
│   └── my-package-1.0-1-x86_64.pkg.tar.zst
└── repo/
    ├── aur.db.tar.gz
    └── aur.files.tar.gz
```

### Workflow

```bash
# Add new AUR package
aur-vendor add my-package
# Downloads PKGBUILD, you review, git commit

# Update existing
aur-vendor update my-package
# Shows PKGBUILD diff, you review, git commit

# Build all vendored packages
aur-vendor build
# Creates local repo
```

### pacman.conf Integration

```ini
[aur]
SigLevel = Optional TrustAll
Server = file:///var/lib/aur-repo
```

Then in config: `c.add_packages("my-aur-package")` — no special handling.

---

## Installer Image

The installer is defined as another "machine" in the config:

```python
elif c.name == "installer":
    c.add_packages(
        "arch-install-scripts",
        "btrfs-progs", 
        "parted",
        "grub",
        "efibootmgr",
    )
    c.add_file("/root/config.py", open("config.py").read())
    c.add_file("/root/arch-atomic", TOOL_BINARY)
    c.add_file("/root/install.sh", INSTALL_SCRIPT)
```

### Building Install Image

```python
def build_install_image(output: str = "arch-atomic-install.img"):
    # Collect packages from all machines
    all_packages = set()
    for machine in ["desktop", "laptop", "server", "installer"]:
        c = Config(machine)
        configure(c)
        all_packages |= c.base_packages | c.packages
    
    # Create disk image
    run(["truncate", "-s", "30G", output])
    
    # Partition: ESP + btrfs
    run(["sgdisk", output,
         "-n", "1:0:+512M", "-t", "1:ef00",  # ESP
         "-n", "2:0:0", "-t", "2:8300"])      # btrfs
    
    loop = run_output(["losetup", "-Pf", "--show", output]).strip()
    
    try:
        # Format
        run(["mkfs.fat", "-F32", f"{loop}p1"])
        run(["mkfs.btrfs", f"{loop}p2"])
        
        # Create btrfs structure
        run(["mount", f"{loop}p2", "/mnt"])
        run(["btrfs", "subvol", "create", "/mnt/@images"])
        run(["btrfs", "subvol", "create", "/mnt/@var"])
        run(["btrfs", "subvol", "create", "/mnt/@home"])
        
        # Build installer generation
        run(["btrfs", "subvol", "create", "/mnt/@images/gen-1"])
        
        # Configure for installer
        c = Config("installer")
        configure(c)
        
        # Pacstrap
        run(["pacstrap", "/mnt/@images/gen-1"] + list(c.base_packages))
        
        # Write manifest
        write_json("/mnt/@images/gen-1/manifest.json", c.manifest())
        
        # Chroot phase
        run(["arch-chroot", "/mnt/@images/gen-1", "arch-atomic-chroot"])

        # Pre-download all packages for offline install into generation
        os.makedirs("/mnt/@images/gen-1/offline-packages", exist_ok=True)
        cache_dir = "/mnt/@images/gen-1/offline-packages"
        run(["pacman", "-Sy", "--cachedir", cache_dir,
             "-w", "--noconfirm"] + list(all_packages))

        # Install GRUB
        run(["mount", f"{loop}p1", "/mnt/efi"])
        run(["grub-install", "--target=x86_64-efi",
             "--efi-directory=/mnt/efi",
             "--boot-directory=/mnt/@images/gen-1/boot",
             "--removable"])
        write_grub_config("/mnt/efi/grub/grub.cfg", gen=1)
        
    finally:
        run(["umount", "-R", "/mnt"])
        run(["losetup", "-d", loop])
```

### Usage

```bash
# Build installer
arch-atomic build-image --machine installer -o install.img

# Write to USB
dd if=install.img of=/dev/sdX bs=4M status=progress

# Boot USB, run:
./install.sh
# Prompts for machine name and target disk
```

---

## CLI Interface

```bash
# Apply configuration (incremental)
arch-atomic apply --machine desktop

# Full rebuild (from scratch)
arch-atomic rebuild --machine desktop

# Build bootable install image
arch-atomic build-image --machine installer -o install.img

# List generations
arch-atomic list

# Rollback to previous generation
arch-atomic rollback

# Garbage collect old generations (keep last N)
arch-atomic gc --keep 5

# Install to disk (run from installer)
arch-atomic install --machine desktop /dev/nvme0n1
```

---

## Development Environment

### Prerequisites

- Existing Arch Linux system (host)
- QEMU for testing
- btrfs-progs
- Python 3.10+

### Testing Without Affecting Host

Work entirely with image files and QEMU:

```bash
# Create test image
truncate -s 20G test.img

# Setup partitions and btrfs
# ... (as in build_install_image)

# Boot in QEMU
qemu-system-x86_64 \
    -enable-kvm \
    -m 4G \
    -drive file=test.img,format=raw \
    -bios /usr/share/ovmf/x64/OVMF.fd
```

### Project Structure

```
arch-atomic/
├── arch_atomic/
│   ├── __init__.py
│   ├── config.py          # Config class
│   ├── apply.py           # apply/rebuild commands
│   ├── chroot.py          # chroot phase
│   ├── grub.py            # GRUB config generation
│   ├── image.py           # Image building
│   └── cli.py             # Argument parsing, main()
├── initcpio/
│   ├── install/
│   │   └── arch-atomic
│   └── hooks/
│       └── arch-atomic
├── config.py              # User's system configuration
├── tests/
│   ├── test_config.py
│   └── test_in_qemu.py
└── README.md
```

### Development Workflow

1. Edit code on host
2. Build test image
3. Boot in QEMU
4. Test changes
5. Repeat

```bash
# Quick iteration
./dev.sh  # builds image + boots QEMU
```

---

## Error Handling & Recovery

### Failed Build

If build fails mid-way:

```bash
# Delete incomplete generation
btrfs subvol delete /images/gen-43

# Current generation untouched
```

### Failed Boot

Select previous generation from GRUB menu. System boots into known-good state.

### Rollback

```bash
arch-atomic rollback
# Updates /current symlink to previous generation
# Updates GRUB default
# Optionally reboots
```

---

## Future Considerations

### Not in Initial Scope

- Secure Boot (complex with local builds)
- TPM-sealed LUKS (conflicts with arbitrary generations)
- Multi-boot with other OSes
- Network-based image distribution

### Possible Extensions

- Remote machine management
- Declarative secrets management (age/sops)
- Integration with systemd-homed
- Snapshot scheduling / automatic rollback on failure

---

## Summary

| Feature | Implementation |
|---------|----------------|
| Declarative config | Python DSL with Config class |
| Package management | Standard pacman |
| Atomic updates | btrfs subvolumes + symlinks |
| Live switching | Update /current symlink |
| Rollback | Select previous generation |
| Boot | GRUB reads from btrfs directly |
| Immutability | tmpfs root, ro generation mounts |
| User management | Persistent in /var, not declared |
| AUR | Vendored PKGBUILDs, local repo |
| Multi-machine | Single config, dispatch on name |
