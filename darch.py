#!/usr/bin/env python3
"""
darch - Declarative Arch Linux image builder

Creates bootable disk images with:
- tmpfs root (ephemeral, rebuilt each boot)
- Immutable generations in btrfs subvolumes
- Persistent /var and /home
- Atomic updates via symlink switching
"""

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
from typing import Set, Dict, Tuple, Literal
import argparse
import json
import os
import shutil
import subprocess
import sys


# =============================================================================
# Configuration
# =============================================================================

FileEntry = Tuple[Literal['file'], str, int | None]  # ('file', content, mode)
SymlinkEntry = Tuple[Literal['symlink'], str]         # ('symlink', target)

@dataclass
class Config:
    """
    Declarative system configuration.

    Build up with helper methods, then pass to build functions.
    """
    name: str

    # System settings
    hostname: str = ""
    timezone: str = "UTC"
    locale: str = "en_US.UTF-8"
    keymap: str = "us"

    # All packages (passed to pacstrap)
    packages: Set[str] = field(default_factory=lambda: {
        "base", "linux", "btrfs-progs", "grub", "efibootmgr", "pacman-contrib"
    })

    # Files and symlinks: path -> ('file', content) | ('symlink', target)
    files: Dict[str, FileEntry | SymlinkEntry] = field(default_factory=dict)

    # Kernel modules for initramfs
    initramfs_modules: Set[str] = field(default_factory=lambda: {
        "btrfs", "ata_piix", "ahci", "sd_mod", "virtio_blk", "virtio_pci"
    })

    def __post_init__(self):
        if not self.hostname:
            self.hostname = self.name

    # -------------------------------------------------------------------------
    # Builder methods
    # -------------------------------------------------------------------------

    def add_packages(self, *names: str) -> "Config":
        """Add packages to install."""
        self.packages.update(names)
        return self

    def add_file(self, path: str, content: str, mode: int | None = None) -> "Config":
        """Add a file with content and optional mode."""
        self.files[path] = ('file', content, mode)
        return self

    def add_symlink(self, path: str, target: str) -> "Config":
        """Add a symlink."""
        self.files[path] = ('symlink', target)
        return self

    def enable_service(self, name: str) -> "Config":
        """Enable a systemd service (creates symlink)."""
        if not name.endswith(('.service', '.socket', '.timer', '.path', '.mount')):
            name = f"{name}.service"
        return self.add_symlink(
            f"/etc/systemd/system/multi-user.target.wants/{name}",
            f"/usr/lib/systemd/system/{name}"
        )

    def mask_service(self, name: str) -> "Config":
        """Mask a systemd service (symlink to /dev/null)."""
        if not name.endswith(('.service', '.socket', '.timer', '.path', '.mount')):
            name = f"{name}.service"
        return self.add_symlink(f"/etc/systemd/system/{name}", "/dev/null")

    def set_timezone(self, tz: str) -> "Config":
        """Set system timezone."""
        self.timezone = tz
        return self.add_symlink("/etc/localtime", f"/usr/share/zoneinfo/{tz}")

    def set_locale(self, locale: str) -> "Config":
        """Set system locale."""
        self.locale = locale
        self.add_file("/etc/locale.gen", f"{locale} UTF-8\n")
        return self.add_file("/etc/locale.conf", f"LANG={locale}\n")

    def set_keymap(self, keymap: str) -> "Config":
        """Set console keymap."""
        self.keymap = keymap
        return self.add_file("/etc/vconsole.conf", f"KEYMAP={keymap}\n")

    def set_hostname(self, hostname: str) -> "Config":
        """Set hostname and generate /etc/hosts."""
        self.hostname = hostname
        self.add_file("/etc/hostname", f"{hostname}\n")
        hosts_content = f"""127.0.0.1   localhost
::1         localhost
127.0.1.1   {hostname}.localdomain {hostname}
"""
        return self.add_file("/etc/hosts", hosts_content)

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize config to a dict (for JSON storage)."""
        # Convert files dict: tuples to lists for JSON
        files_serialized = {}
        for path, entry in self.files.items():
            files_serialized[path] = list(entry)
        return {
            "name": self.name,
            "hostname": self.hostname,
            "timezone": self.timezone,
            "locale": self.locale,
            "keymap": self.keymap,
            "packages": sorted(self.packages),
            "files": files_serialized,
            "initramfs_modules": sorted(self.initramfs_modules),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Deserialize config from a dict."""
        config = cls(name=data["name"])
        config.hostname = data.get("hostname", config.name)
        config.timezone = data.get("timezone", "UTC")
        config.locale = data.get("locale", "en_US.UTF-8")
        config.keymap = data.get("keymap", "us")
        config.packages = set(data.get("packages", []))
        config.initramfs_modules = set(data.get("initramfs_modules", []))
        # Convert files: lists back to tuples
        for path, entry in data.get("files", {}).items():
            config.files[path] = tuple(entry)
        return config

    def to_json(self) -> str:
        """Serialize config to JSON string."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "Config":
        """Deserialize config from JSON string."""
        return cls.from_dict(json.loads(s))


@dataclass
class GenerationInfo:
    """Info about a generation."""
    gen: int
    path: str
    created_at: str


# =============================================================================
# File content generators (pure functions)
# =============================================================================

def generate_mkinitcpio_conf(modules: Set[str]) -> str:
    """Generate mkinitcpio.conf content."""
    modules_str = " ".join(sorted(modules))
    return f"""MODULES=({modules_str})
BINARIES=()
FILES=()
HOOKS=(base udev autodetect microcode modconf block darch filesystems fsck)
COMPRESSION="zstd"
"""


def generate_darch_hook_runtime() -> str:
    """Generate the darch initcpio runtime hook."""
    return r'''#!/usr/bin/ash
# darch initcpio runtime hook
# Sets up tmpfs root with symlinks to generation

run_hook() {
    echo "========================================"
    echo ":: DARCH HOOK - tmpfs root with symlinks"
    echo "========================================"

    # Override the default mount handler
    mount_handler="darch_mount_handler"
}

darch_mount_handler() {
    local newroot="$1"

    echo ":: darch: Parsing kernel cmdline..."

    # Parse kernel command line
    local root_uuid="" gen=""
    for param in $(cat /proc/cmdline); do
        case "$param" in
            root=UUID=*)
                root_uuid="${param#root=UUID=}"
                ;;
            darch.gen=*)
                gen="${param#darch.gen=}"
                ;;
        esac
    done

    echo ":: darch: root_uuid=$root_uuid"
    echo ":: darch: gen=$gen"

    if [ -z "$root_uuid" ]; then
        echo ":: darch: ERROR - no root UUID found!"
        return 1
    fi

    if [ -z "$gen" ]; then
        echo ":: darch: ERROR - no generation specified (darch.gen=N)!"
        return 1
    fi

    # Wait for device
    local device="/dev/disk/by-uuid/$root_uuid"
    echo ":: darch: Waiting for $device..."

    local timeout=10
    while [ ! -b "$device" ] && [ $timeout -gt 0 ]; do
        sleep 1
        timeout=$((timeout - 1))
    done

    if [ ! -b "$device" ]; then
        echo ":: darch: ERROR - device not found!"
        return 1
    fi
    echo ":: darch: Found device"

    # Create tmpfs root
    echo ":: darch: Creating tmpfs root..."
    mount -t tmpfs -o size=512M,mode=0755 tmpfs "$newroot"

    # Create directory structure
    echo ":: darch: Creating directory structure..."
    mkdir -p "$newroot/dev"
    mkdir -p "$newroot/proc"
    mkdir -p "$newroot/sys"
    mkdir -p "$newroot/run"
    mkdir -p "$newroot/tmp"
    mkdir -p "$newroot/mnt"
    mkdir -p "$newroot/efi"
    mkdir -p "$newroot/images"
    mkdir -p "$newroot/var"
    mkdir -p "$newroot/home"
    chmod 1777 "$newroot/tmp"

    # Mount btrfs subvolumes
    echo ":: darch: Mounting btrfs subvolumes..."
    mount -t btrfs -o subvol=@images,ro "$device" "$newroot/images"
    mount -t btrfs -o subvol=@var "$device" "$newroot/var"
    mount -t btrfs -o subvol=@home "$device" "$newroot/home"

    # Verify generation exists
    if [ ! -d "$newroot/images/gen-$gen" ]; then
        echo ":: darch: ERROR - generation $gen not found!"
        return 1
    fi

    # Create symlinks to generation (relative paths so they work before switch_root)
    echo ":: darch: Creating symlinks to gen-$gen..."
    ln -s "images/gen-$gen" "$newroot/current"
    ln -s current/usr "$newroot/usr"
    ln -s current/etc "$newroot/etc"
    ln -s current/boot "$newroot/boot"

    # Standard symlinks (relative)
    ln -s usr/bin "$newroot/bin"
    ln -s usr/lib "$newroot/lib"
    ln -s usr/lib "$newroot/lib64"
    ln -s usr/bin "$newroot/sbin"

    # Root home - symlink to persistent home (relative)
    ln -s home/root "$newroot/root"

    # /init - point to systemd (relative path through symlink chain)
    ln -s usr/lib/systemd/systemd "$newroot/init"

    echo ":: darch: tmpfs root setup complete!"
}
'''


def generate_darch_hook_install() -> str:
    """Generate the darch initcpio install hook."""
    return r'''#!/usr/bin/bash
# darch initcpio install hook

build() {
    add_runscript
}

help() {
    cat <<HELPEOF
darch hook - sets up the arch-atomic root filesystem
HELPEOF
}
'''


def generate_fstab(esp_uuid: str) -> str:
    """Generate /etc/fstab content."""
    return f"""# /etc/fstab: static file system information
# Root is tmpfs, @images/@var/@home mounted by initramfs
#
# <file system>             <mount point>  <type>  <options>  <dump> <pass>

# EFI System Partition
UUID={esp_uuid}             /efi           vfat    rw,relatime,fmask=0022,dmask=0022,codepage=437,iocharset=ascii,shortname=mixed,utf8,errors=remount-ro  0 2
"""


def generate_grub_config(root_uuid: str, generations: list[GenerationInfo]) -> str:
    """Generate GRUB config for all generations (newest first)."""
    header = f"""# Custom GRUB config for darch
# Loads kernel directly from btrfs subvolume

set timeout=5
set default=0

# Serial console for QEMU
serial --unit=0 --speed=115200
terminal_input serial console
terminal_output serial console

# Load btrfs module
insmod btrfs

# Find the btrfs partition by UUID
search --set=root --fs-uuid {root_uuid}
"""
    entries = []
    # Newest first
    for g in sorted(generations, key=lambda x: x.gen, reverse=True):
        entries.append(f"""
menuentry "Arch Linux (gen-{g.gen}, {g.created_at})" {{
    linux /@images/gen-{g.gen}/boot/vmlinuz-linux \\
        root=UUID={root_uuid} \\
        darch.gen={g.gen} \\
        console=tty0 console=ttyS0,115200 \\
        systemd.gpt_auto=0 rw
    initrd /@images/gen-{g.gen}/boot/initramfs-linux.img
}}""")
    return header + "".join(entries) + "\n"


# =============================================================================
# Build context and functions
# =============================================================================

@dataclass
class BuildContext:
    """Runtime context for a build - paths and UUIDs discovered during setup."""
    mount_root: str      # Where generation is mounted (e.g., /mnt/arch-build)
    efi_mount: str       # Where ESP is mounted (e.g., /mnt/arch-build/efi)
    var_path: str        # Where @var is mounted (e.g., /mnt/arch-build/var)
    esp_uuid: str        # UUID of ESP partition
    root_uuid: str       # UUID of btrfs partition
    gen: int = 1         # Generation number
    fresh_install: bool = True  # False for rebuild of existing system
    upgrade: bool = False       # Run pacman -Syu


def run(cmd, check=True, capture_output=False):
    """Run a command and optionally capture output."""
    print(f"Running: {' '.join(cmd)}")
    if capture_output:
        result = subprocess.run(cmd, check=check, capture_output=True, text=True)
        return result.stdout.strip()
    else:
        subprocess.run(cmd, check=check)


def build_generation(config: Config, ctx: BuildContext):
    """
    Build a generation from config into the mounted filesystem.

    Expects:
    - ctx.mount_root: generation subvolume mounted here
    - ctx.var_path: @var subvolume mounted here
    - ctx.efi_mount: ESP mounted here
    - Partitions already formatted, subvolumes already created
    """
    print("\n=== Installing base system with pacstrap ===")
    run(["pacstrap", "-K", ctx.mount_root] + list(config.packages))

    # Apply config.files (locale.gen, mkinitcpio.conf, hooks needed by chroot commands)
    print("\n=== Applying config files ===")
    for path, entry in config.files.items():
        full_path = f"{ctx.mount_root}{path}"
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        if os.path.exists(full_path) or os.path.islink(full_path):
            os.remove(full_path)
        if entry[0] == 'file':
            content, mode = entry[1], entry[2]
            with open(full_path, 'w') as f:
                f.write(content)
            if mode is not None:
                os.chmod(full_path, mode)
        elif entry[0] == 'symlink':
            os.symlink(entry[1], full_path)
        print(f"  {entry[0]}: {path}")

    print("\n=== Configuring system (in chroot) ===")
    # Chroot script runs commands that can't be done declaratively
    chroot_script = """#!/bin/bash
set -e

echo "=== Setting hardware clock ==="
hwclock --systohc

echo "=== Generating locales ==="
locale-gen

echo "=== Setting root password (empty for testing) ==="
passwd -d root

echo "=== Regenerating initramfs ==="
mkinitcpio -P

echo "=== Installing GRUB to ESP ==="
grub-install --target=x86_64-efi --efi-directory=/efi --boot-directory=/efi --bootloader-id=GRUB --removable

echo "=== Creating tmpfiles overrides for darch layout ==="
mkdir -p /etc/tmpfiles.d
# Override mtab line to not use L+ (force recreate)
sed 's|^L+ /etc/mtab|L /etc/mtab|' /usr/lib/tmpfiles.d/etc.conf > /etc/tmpfiles.d/etc.conf
# Remove /root directory entries (darch has /root as symlink)
grep -v '^[df].*[[:space:]]/root' /usr/lib/tmpfiles.d/provision.conf > /etc/tmpfiles.d/provision.conf

echo "=== Chroot configuration complete ==="
"""

    chroot_script_path = f"{ctx.mount_root}/root/setup.sh"
    with open(chroot_script_path, "w") as f:
        f.write(chroot_script)
    os.chmod(chroot_script_path, 0o755)

    run(["arch-chroot", ctx.mount_root, "/root/setup.sh"])

    print("\n=== Setting up persistent /etc files ===")
    # Fix directory permissions (pacstrap/systemd may have changed them)
    os.chmod(f"{ctx.var_path}/lib/users", 0o755)
    os.chmod(f"{ctx.var_path}/lib/machines", 0o755)

    # User management files: copy to @var on fresh install, just symlink on rebuild
    user_files = ["passwd", "shadow", "group", "gshadow"]
    for f in user_files:
        src = f"{ctx.mount_root}/etc/{f}"
        dst = f"{ctx.var_path}/lib/users/{f}"
        if ctx.fresh_install:
            # Fresh install: copy pacstrap's files to @var
            if os.path.exists(src) and not os.path.islink(src):
                shutil.copy2(src, dst)
        # Always ensure symlink exists (remove file/old symlink first)
        if os.path.exists(src) or os.path.islink(src):
            os.remove(src)
        os.symlink(f"/var/lib/users/{f}", src)

    # resolv.conf: symlink to /run (systemd-resolved or NetworkManager will manage)
    resolv_path = f"{ctx.mount_root}/etc/resolv.conf"
    if os.path.exists(resolv_path) or os.path.islink(resolv_path):
        os.remove(resolv_path)
    os.symlink("/run/systemd/resolve/stub-resolv.conf", resolv_path)

    # mtab: symlink to /proc/mounts (standard)
    mtab_path = f"{ctx.mount_root}/etc/mtab"
    if os.path.exists(mtab_path) or os.path.islink(mtab_path):
        os.remove(mtab_path)
    os.symlink("/proc/mounts", mtab_path)


@dataclass
class ConfigDiff:
    """Differences between two configs."""
    packages_to_install: Set[str]
    packages_to_remove: Set[str]
    files_to_add: Dict[str, FileEntry | SymlinkEntry]
    files_to_remove: Dict[str, FileEntry | SymlinkEntry]
    files_to_update: Dict[str, FileEntry | SymlinkEntry]

    @classmethod
    def compute(cls, old: Config, new: Config) -> "ConfigDiff":
        """Compare two configs and return the differences."""
        return cls(
            packages_to_install=new.packages - old.packages,
            packages_to_remove=old.packages - new.packages,
            files_to_add={p: e for p, e in new.files.items() if p not in old.files},
            files_to_remove={p: e for p, e in old.files.items() if p not in new.files},
            files_to_update={p: e for p, e in new.files.items()
                             if p in old.files and old.files[p] != e},
        )

    def has_changes(self) -> bool:
        """Check if this diff has any changes."""
        return bool(
            self.packages_to_install or
            self.packages_to_remove or
            self.files_to_add or
            self.files_to_remove or
            self.files_to_update
        )


def check_upgrades_available(mount_root: str) -> bool:
    """Check if package upgrades are available."""
    result = subprocess.run(
        ["arch-chroot", mount_root, "checkupdates"],
        capture_output=True
    )
    return result.returncode == 0


def build_incremental(config: Config, ctx: BuildContext, diff: ConfigDiff):
    """
    Build a new generation incrementally from an existing one.

    Expects:
    - ctx.mount_root: NEW generation subvolume mounted here (snapshot of old)
    - ctx.var_path: @var subvolume mounted here
    - ctx.efi_mount: ESP mounted here
    - ctx.upgrade: if True, also run pacman -Syu
    - diff: ConfigDiff between old and new config
    """
    # Package changes
    if diff.packages_to_remove:
        print(f"\n=== Removing packages: {diff.packages_to_remove} ===")
        run(["arch-chroot", ctx.mount_root, "pacman", "-Rns", "--noconfirm"]
            + list(diff.packages_to_remove))

    if diff.packages_to_install:
        print(f"\n=== Installing packages: {diff.packages_to_install} ===")
        run(["arch-chroot", ctx.mount_root, "pacman", "-S", "--noconfirm"]
            + list(diff.packages_to_install))

    if ctx.upgrade:
        print("\n=== Upgrading system packages ===")
        run(["arch-chroot", ctx.mount_root, "pacman", "-Syu", "--noconfirm"])

    # Apply changed files
    all_file_changes = {**diff.files_to_add, **diff.files_to_update}
    if all_file_changes:
        print(f"\n=== Applying {len(all_file_changes)} file changes ===")
        for path, entry in all_file_changes.items():
            full_path = f"{ctx.mount_root}{path}"
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            if os.path.exists(full_path) or os.path.islink(full_path):
                os.remove(full_path)
            if entry[0] == 'file':
                content, mode = entry[1], entry[2]
                with open(full_path, 'w') as f:
                    f.write(content)
                if mode is not None:
                    os.chmod(full_path, mode)
            elif entry[0] == 'symlink':
                os.symlink(entry[1], full_path)
            print(f"  {entry[0]}: {path}")

    # Remove deleted files
    if diff.files_to_remove:
        print(f"\n=== Removing {len(diff.files_to_remove)} files ===")
        for path in diff.files_to_remove:
            full_path = f"{ctx.mount_root}{path}"
            if os.path.exists(full_path) or os.path.islink(full_path):
                os.remove(full_path)
                print(f"  removed: {path}")

    # Check if initramfs needs regeneration
    initramfs_paths = {"/etc/mkinitcpio.conf", "/usr/lib/initcpio/hooks/darch",
                       "/usr/lib/initcpio/install/darch"}
    needs_initramfs = bool(set(all_file_changes.keys()) & initramfs_paths)

    if needs_initramfs:
        print("\n=== Regenerating initramfs ===")
        run(["arch-chroot", ctx.mount_root, "mkinitcpio", "-P"])


# =============================================================================
# Mount context managers
# =============================================================================

@contextmanager
def loop_device(image_path: str):
    """Loop-mount a disk image, yield (esp_part, btrfs_part)."""
    loop = run(["losetup", "-Pf", "--show", image_path], capture_output=True)
    run(["udevadm", "settle"])
    try:
        yield f"{loop}p1", f"{loop}p2"  # esp, btrfs
    finally:
        run(["losetup", "-d", loop], check=False)


@contextmanager
def mount(device: str, mount_point: str, options: str | None = None):
    """Mount a filesystem."""
    os.makedirs(mount_point, exist_ok=True)
    if options:
        run(["mount", "-o", options, device, mount_point])
    else:
        run(["mount", device, mount_point])
    try:
        yield mount_point
    finally:
        run(["umount", mount_point], check=False)


@contextmanager
def image_file(image_path: str, size: str = "10G"):
    """Create a blank disk image with ESP and btrfs partitions + subvolumes."""

    if os.path.exists(image_path):
        print(f"=== Disk image exists: {image_path} ===")
        with loop_device(image_path) as result:
            yield result
    else:
        print(f"=== Creating disk image: {image_path} ({size}) ===")
        run(["truncate", "-s", size, image_path])

        print("\n=== Partitioning disk ===")
        run(["sgdisk", "-Z", image_path])
        run(["sgdisk", "-n", "1:0:+512M", "-t", "1:ef00", image_path])  # ESP
        run(["sgdisk", "-n", "2:0:0", "-t", "2:8300", image_path])       # btrfs

        print("\n=== Setting up loop device ===")
        with loop_device(image_path) as (esp_part, root_part):
            print(f"\n=== Formatting partitions ===")
            run(["mkfs.fat", "-F32", esp_part])
            run(["mkfs.btrfs", "-f", root_part])

            print("\n=== Creating btrfs subvolumes ===")
            mount_point = "/mnt/darch-setup"
            os.makedirs(mount_point, exist_ok=True)
            run(["mount", root_part, mount_point])

            run(["btrfs", "subvol", "create", f"{mount_point}/@images"])
            run(["btrfs", "subvol", "create", f"{mount_point}/@var"])
            run(["btrfs", "subvol", "create", f"{mount_point}/@home"])

            os.makedirs(f"{mount_point}/@home/root", mode=0o700, exist_ok=True)
            os.makedirs(f"{mount_point}/@var/lib/users", exist_ok=True)
            os.makedirs(f"{mount_point}/@var/lib/machines", exist_ok=True)

            run(["umount", mount_point])
            print(f"\n=== Image created successfully ===")
            yield esp_part, root_part


# =============================================================================
# Operations on mounted filesystems
# =============================================================================

def get_generations(images_path: str) -> list[GenerationInfo]:
    """Get all generations from mounted @images, sorted by gen number."""
    from datetime import datetime
    result = []
    for entry in os.listdir(images_path):
        if entry.startswith("gen-"):
            try:
                gen = int(entry[4:])
            except ValueError:
                continue
            gen_path = f"{images_path}/{entry}"
            config_path = f"{gen_path}/config.json"
            try:
                ctime = os.stat(config_path).st_ctime
                created_at = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M")
            except FileNotFoundError:
                created_at = "unknown"
            result.append(GenerationInfo(gen=gen, path=gen_path, created_at=created_at))
    return sorted(result, key=lambda g: g.gen)


def create_gen_subvol(images_path: str, gen: int, snapshot_from: int | None = None):
    """Create a generation subvolume in mounted @images."""
    target = f"{images_path}/gen-{gen}"
    if snapshot_from is not None:
        source = f"{images_path}/gen-{snapshot_from}"
        print(f"Creating gen-{gen} as snapshot of gen-{snapshot_from}")
        run(["btrfs", "subvol", "snapshot", source, target])
    else:
        print(f"Creating gen-{gen}")
        run(["btrfs", "subvol", "create", target])


def load_gen_config(gen_path: str) -> Config:
    """Load config.json from mounted generation."""
    with open(f"{gen_path}/config.json") as f:
        return Config.from_json(f.read())


def load_config_module(config_path: str) -> Config:
    """Load config.py and call configure()."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.configure()

    # Add darch-specific files
    config.add_file("/etc/mkinitcpio.conf", generate_mkinitcpio_conf(config.initramfs_modules))
    config.add_file("/usr/lib/initcpio/hooks/darch", generate_darch_hook_runtime(), mode=0o755)
    config.add_file("/usr/lib/initcpio/install/darch", generate_darch_hook_install(), mode=0o755)
    return config


def main():
    parser = argparse.ArgumentParser(
        description="darch - Declarative Arch Linux image builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # apply command
    p_apply = subparsers.add_parser("apply", help="Apply configuration (auto-detects fresh vs incremental)")
    p_apply.add_argument("--image", help="Path to disk image")
    p_apply.add_argument("--size", default="10G", help="Image size (default: 10G)")
    p_apply.add_argument("--btrfs", help="Btrfs device (e.g., /dev/nvme0n1p2)")
    p_apply.add_argument("--esp", help="ESP device (e.g., /dev/nvme0n1p1)")
    p_apply.add_argument("--config", required=True, help="Path to config.py")
    p_apply.add_argument("--upgrade", action="store_true", help="Also upgrade all packages (pacman -Syu)")
    p_apply.add_argument("--rebuild", action="store_true", help="Force fresh build even if generations exist")

    args = parser.parse_args()

    if os.geteuid() != 0:
        print("Error: This script must be run as root")
        return 1

    if args.command == "apply":
        if not args.image and not (args.btrfs and args.esp):
            print("Error: --image or (--btrfs and --esp) required")
            return 1

        config = load_config_module(args.config)

        with ExitStack() as stack:
            if args.image:
                esp_dev, btrfs_dev = stack.enter_context(image_file(args.image, args.size))
            else:
                esp_dev, btrfs_dev = (args.esp, args.btrfs)

            images = stack.enter_context(mount(btrfs_dev, "/mnt/darch-images", "subvol=@images"))
            gens = get_generations(images)
            current = gens[-1].gen if gens else None
            new_gen = (current or 0) + 1

            # Get UUIDs and add runtime-dependent files before diffing
            esp_uuid = run(["blkid", "-s", "UUID", "-o", "value", esp_dev], capture_output=True)
            root_uuid = run(["blkid", "-s", "UUID", "-o", "value", btrfs_dev], capture_output=True)
            config.add_file("/etc/fstab", generate_fstab(esp_uuid))

            fresh = current is None or args.rebuild
            diff = None
            if not fresh:
                # Check for changes before creating new generation
                old_mount = stack.enter_context(
                    mount(btrfs_dev, "/mnt/darch-old", f"subvol=@images/gen-{current}"))
                old_config = load_gen_config(old_mount)

                diff = ConfigDiff.compute(old_config, config)

                if not diff.has_changes() and not (args.upgrade and check_upgrades_available(old_mount)):
                    print("Already up to date.")
                    return 0

            # Create and mount new generation
            create_gen_subvol(images, new_gen, snapshot_from=None if fresh else current)
            mount_root = "/mnt/darch-build"
            stack.enter_context(mount(btrfs_dev, mount_root, f"subvol=@images/gen-{new_gen}"))
            stack.enter_context(mount(btrfs_dev, f"{mount_root}/var", "subvol=@var"))
            stack.enter_context(mount(esp_dev, f"{mount_root}/efi"))

            ctx = BuildContext(
                mount_root=mount_root,
                efi_mount=f"{mount_root}/efi",
                var_path=f"{mount_root}/var",
                esp_uuid=esp_uuid,
                root_uuid=root_uuid,
                gen=new_gen,
                fresh_install=fresh,
                upgrade=args.upgrade,
            )

            if fresh:
                build_generation(config, ctx)
            else:
                build_incremental(config, ctx, diff)

            # Save config
            print("\n=== Saving config ===")
            with open(f"{ctx.mount_root}/config.json", "w") as f:
                f.write(config.to_json())

            # Write GRUB config with all generations
            print("\n=== Writing GRUB config ===")
            gens = get_generations(images)
            grub_cfg_path = f"{ctx.efi_mount}/grub/grub.cfg"
            os.makedirs(os.path.dirname(grub_cfg_path), exist_ok=True)
            with open(grub_cfg_path, "w") as f:
                f.write(generate_grub_config(ctx.root_uuid, gens))

            print(f"\n=== SUCCESS: Built gen-{new_gen} ===")

    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())

