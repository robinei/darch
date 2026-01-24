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
from datetime import datetime
from pathlib import Path
from typing import Set, Dict, Tuple, Literal
import argparse
import fcntl
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


LOCKFILE = Path("/var/lock/darch.lock")

# Garbage collection settings
GC_KEEP_MIN = 3          # Always keep at least this many complete generations
GC_KEEP_MAX = 10         # Keep at most this many generations (0 = unlimited)
GC_MIN_AGE_DAYS = 7      # Never delete generations younger than this
GC_MAX_AGE_DAYS = 30     # Delete generations older than this (0 = keep forever)


# =============================================================================
# Configuration
# =============================================================================

FileEntry = Tuple[Literal['file'], str, int | None]  # ('file', content, mode)
SymlinkEntry = Tuple[Literal['symlink'], str]         # ('symlink', target)


@dataclass
class User:
    """
    Declarative user configuration.

    Created with initial settings, then mutated by helper methods.
    """
    name: str
    uid: int = 1000
    shell: str = "/bin/bash"
    password_hash: str | None = None
    groups: Set[str] = field(default_factory=set)

    def add_groups(self, *names: str) -> "User":
        """Add groups to the user."""
        self.groups.update(names)
        return self

    def to_dict(self) -> dict:
        """Serialize user to a dict."""
        return {
            "uid": self.uid,
            "shell": self.shell,
            "password_hash": self.password_hash,
            "groups": sorted(self.groups),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "User":
        """Deserialize user from a dict."""
        user = cls(name=data["name"])
        user.uid = data.get("uid", 1000)
        user.shell = data.get("shell", "/bin/bash")
        user.password_hash = data.get("password_hash")
        user.groups = set(data.get("groups", []))
        return user


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


@dataclass
class Config:
    """
    Declarative system configuration.

    Build up with helper methods, then pass to build functions.
    """

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

    # User (optional)
    user: User | None = None

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
        return self.add_symlink("/etc/localtime", f"/usr/share/zoneinfo/{tz}")

    def set_locale(self, locale: str) -> "Config":
        """Set system locale."""
        self.add_file("/etc/locale.gen", f"{locale} UTF-8\n")
        return self.add_file("/etc/locale.conf", f"LANG={locale}\n")

    def set_keymap(self, keymap: str) -> "Config":
        """Set console keymap."""
        return self.add_file("/etc/vconsole.conf", f"KEYMAP={keymap}\n")

    def set_hostname(self, hostname: str) -> "Config":
        """Set hostname and generate /etc/hosts."""
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
            "packages": sorted(self.packages),
            "files": files_serialized,
            "initramfs_modules": sorted(self.initramfs_modules),
            "user": self.user.to_dict() if self.user else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Deserialize config from a dict."""
        config = cls()
        config.packages = set(data.get("packages", []))
        config.initramfs_modules = set(data.get("initramfs_modules", []))
        # Convert files: lists back to tuples
        for path, entry in data.get("files", {}).items():
            config.files[path] = tuple(entry)
        # Deserialize user if present
        if data.get("user"):
            config.user = User.from_dict(data["user"])
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
    """Info about a generation.

    A generation is complete only if it has config.json (written at end of build).
    Incomplete generations result from failed/interrupted builds.
    """
    gen: int
    path: Path
    complete: bool              # True if config.json exists
    created_at: float | None    # Unix timestamp (None if incomplete)


@dataclass
class ApplyOptions:
    """Typed options for the apply command."""
    config: str
    image: str | None = None
    size: str = "10G"
    btrfs: str | None = None
    esp: str | None = None
    upgrade: bool = False
    rebuild: bool = False


@dataclass
class TestOptions:
    """Typed options for the test command."""
    image: str
    memory: str = "4G"
    cpus: int = 2
    graphics: bool = False


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
        created_str = datetime.fromtimestamp(g.created_at).strftime("%Y-%m-%d %H:%M")
        entries.append(f"""
menuentry "Arch Linux (gen-{g.gen}, {created_str})" {{
    linux /@images/gen-{g.gen}/boot/vmlinuz-linux \\
        root=UUID={root_uuid} \\
        darch.gen={g.gen} \\
        console=tty0 console=ttyS0,115200 \\
        systemd.gpt_auto=0 rw
    initrd /@images/gen-{g.gen}/boot/initramfs-linux.img
}}""")
    return header + "".join(entries) + "\n"


def configure_user(user: User, gen_root: Path, home_path: Path):
    """
    Configure the declarative user in the generation.

    Reads base system users from generation's /etc, appends declarative user,
    writes back. Also creates home directory in @home.
    """
    etc = gen_root / "etc"

    # Read base system files
    passwd_lines = [l for l in (etc / "passwd").read_text().splitlines()
                    if l and not l.startswith(f"{user.name}:")]
    shadow_lines = [l for l in (etc / "shadow").read_text().splitlines()
                    if l and not l.startswith(f"{user.name}:")]
    group_lines = [l for l in (etc / "group").read_text().splitlines()
                   if l and not l.startswith(f"{user.name}:")]
    gshadow_lines = [l for l in (etc / "gshadow").read_text().splitlines()
                     if l and not l.startswith(f"{user.name}:")]

    # Add user entry
    passwd_lines.append(f"{user.name}:x:{user.uid}:{user.uid}::/home/{user.name}:{user.shell}")
    pw_hash = user.password_hash if user.password_hash else "!"
    shadow_lines.append(f"{user.name}:{pw_hash}:19000:0:99999:7:::")
    group_lines.append(f"{user.name}:x:{user.uid}:")
    gshadow_lines.append(f"{user.name}:!::")

    # Add user to supplementary groups
    new_group_lines = []
    for line in group_lines:
        parts = line.split(":")
        if len(parts) >= 4 and parts[0] in user.groups:
            members = [m for m in parts[3].split(",") if m]
            if user.name not in members:
                members.append(user.name)
            parts[3] = ",".join(members)
        new_group_lines.append(":".join(parts))

    # Write back to generation
    (etc / "passwd").write_text("\n".join(passwd_lines) + "\n")
    (etc / "shadow").write_text("\n".join(shadow_lines) + "\n")
    (etc / "shadow").chmod(0o600)
    (etc / "group").write_text("\n".join(new_group_lines) + "\n")
    (etc / "gshadow").write_text("\n".join(gshadow_lines) + "\n")
    (etc / "gshadow").chmod(0o600)

    # Create home directory in @home if needed
    user_home = home_path / user.name
    if not user_home.exists():
        user_home.mkdir(parents=True, exist_ok=True)
        user_home.chmod(0o700)
        os.chown(user_home, user.uid, user.uid)
        print(f"  Created home directory: {user_home}")


# =============================================================================
# Build context and functions
# =============================================================================

@dataclass
class BuildContext:
    """Runtime context for a build - paths and UUIDs discovered during setup."""
    mount_root: Path     # Where generation is mounted
    efi_mount: Path      # Where ESP is mounted
    var_path: Path       # Where @var is mounted (or will be mounted)
    btrfs_dev: str       # Btrfs device path (for mounting @var)
    esp_uuid: str        # UUID of ESP partition
    root_uuid: str       # UUID of btrfs partition
    gen: int = 1         # Generation number
    fresh_install: bool = True  # False for rebuild of existing system
    upgrade: bool = False       # Run pacman -Syu


def run(cmd, check=True, capture_output=False) -> str | None:
    """Run a command and optionally capture output."""
    print(f"Running: {' '.join(cmd)}")
    try:
        if capture_output:
            result = subprocess.run(cmd, check=check, capture_output=True, text=True)
            return result.stdout.strip()
        else:
            subprocess.run(cmd, check=check)
    except subprocess.CalledProcessError as e:
        print(f"\nError: Command failed: {' '.join(cmd)}")
        print(f"Exit code: {e.returncode}")
        if e.stderr:
            print(f"stderr:\n{e.stderr}")
        raise


def chroot_run(root: Path, *cmd, check=True, capture_output=False) -> str | None:
    """Run a command inside a chroot."""
    return run(["arch-chroot", str(root)] + list(cmd), check=check, capture_output=capture_output)


def fix_owner(path: Path):
    """Fix ownership of a file to the invoking user (when run via sudo)."""
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and gid:
        os.chown(path, int(uid), int(gid))


def write_file_entry(path: Path, entry: FileEntry | SymlinkEntry):
    """Write a file or symlink entry to the filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    if entry[0] == 'file':
        content, mode = entry[1], entry[2]
        path.write_text(content)
        if mode is not None:
            path.chmod(mode)
    elif entry[0] == 'symlink':
        path.symlink_to(entry[1])


def force_symlink(path: Path, target: str):
    """Create a symlink, removing any existing file/symlink first."""
    if path.exists() or path.is_symlink():
        path.unlink()
    path.symlink_to(target)


def setup_var_pacman_symlink(var_path: Path):
    """Create the pacman symlink in @var pointing to generation's /pacman."""
    var_lib = var_path / "lib"
    var_lib.mkdir(parents=True, exist_ok=True)
    pacman_link = var_lib / "pacman"
    # Symlink: /var/lib/pacman -> ../../../current/pacman
    # At runtime: exits @var mount, reaches tmpfs /, follows /current to generation
    # At build time in chroot: /current -> . so resolves to /pacman
    force_symlink(pacman_link, "../../../current/pacman")


def check_upgrades_available(mount_root: Path) -> bool:
    """Check if package upgrades are available."""
    try:
        chroot_run(mount_root, "checkupdates", capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False


def build_generation(config: Config, ctx: BuildContext):
    """
    Build a generation from config into the mounted filesystem.

    Expects:
    - ctx.mount_root: generation subvolume mounted here
    - ctx.efi_mount: ESP mounted here
    - @var NOT mounted (we mount it after moving pacman db)
    - Partitions already formatted, subvolumes already created
    """
    print("\n=== Installing base system with pacstrap ===")
    # Bind-mount host's package cache so pacstrap reads/writes there.
    # On non-darch host: uses host cache. On darch system: uses @var cache.
    gen_cache = ctx.mount_root / "var" / "cache" / "pacman" / "pkg"
    gen_cache.mkdir(parents=True, exist_ok=True)
    with mount("/var/cache/pacman/pkg", gen_cache, bind=True):
        run(["pacstrap", "-K", str(ctx.mount_root)] + sorted(config.packages))

    print("\n=== Relocating pacman state to /pacman ===")
    pacman_src = ctx.mount_root / "var" / "lib" / "pacman"
    pacman_dst = ctx.mount_root / "pacman"
    if pacman_src.exists():
        shutil.move(str(pacman_src), str(pacman_dst))
        print(f"  Moved {pacman_src} -> {pacman_dst}")

    print("\n=== Creating /current -> . symlink (for build-time pacman access) ===")
    force_symlink(ctx.mount_root / "current", ".")

    print("\n=== Removing /var from generation (will be @var mount point) ===")
    var_in_gen = ctx.mount_root / "var"
    if var_in_gen.exists():
        shutil.rmtree(var_in_gen)
        print("  Removed /var from generation")
    var_in_gen.mkdir(exist_ok=True)

    print("\n=== Mounting @var and setting up pacman symlink ===")
    with mount(ctx.btrfs_dev, var_in_gen, "subvol=@var"):
        setup_var_pacman_symlink(var_in_gen)

        # Apply config.files (locale.gen, mkinitcpio.conf, hooks needed by chroot commands)
        print("\n=== Applying config files ===")
        for path, entry in config.files.items():
            write_file_entry(ctx.mount_root / path[1:], entry)  # strip leading /
            print(f"  {entry[0]}: {path}")

        print("\n=== Configuring system ===")
        chroot_run(ctx.mount_root, "hwclock", "--systohc")
        chroot_run(ctx.mount_root, "locale-gen")
        chroot_run(ctx.mount_root, "passwd", "-d", "root")
        chroot_run(ctx.mount_root, "mkinitcpio", "-P")
        chroot_run(ctx.mount_root, "grub-install",
                   "--target=x86_64-efi", "--efi-directory=/efi",
                   "--boot-directory=/efi", "--bootloader-id=GRUB", "--removable")

        # Create tmpfiles.d overrides for darch layout
        tmpfiles_dir = ctx.mount_root / "etc/tmpfiles.d"
        tmpfiles_dir.mkdir(parents=True, exist_ok=True)

        # Override mtab line to not use L+ (force recreate)
        etc_conf = (ctx.mount_root / "usr/lib/tmpfiles.d/etc.conf").read_text()
        etc_conf = etc_conf.replace("L+ /etc/mtab", "L /etc/mtab")
        (tmpfiles_dir / "etc.conf").write_text(etc_conf)

        # Remove /root directory entries (darch has /root as symlink)
        provision_conf = (ctx.mount_root / "usr/lib/tmpfiles.d/provision.conf").read_text()
        provision_conf = re.sub(r'^[df].*\s/root.*\n', '', provision_conf, flags=re.MULTILINE)
        (tmpfiles_dir / "provision.conf").write_text(provision_conf)

        print("\n=== Setting up /etc symlinks ===")
        # Fix directory permissions (pacstrap/systemd may have changed them)
        (ctx.var_path / "lib/machines").chmod(0o755)

        # resolv.conf: symlink to /run (systemd-resolved or NetworkManager will manage)
        force_symlink(ctx.mount_root / "etc/resolv.conf", "/run/systemd/resolve/stub-resolv.conf")

        # mtab: symlink to /proc/mounts (standard)
        force_symlink(ctx.mount_root / "etc/mtab", "/proc/mounts")


def build_incremental(diff: ConfigDiff, ctx: BuildContext):
    """
    Build a new generation incrementally from an existing one.

    Expects:
    - ctx.mount_root: NEW generation subvolume mounted here (snapshot of old)
    - ctx.efi_mount: ESP mounted here
    - @var NOT mounted (we mount it here)
    - ctx.upgrade: if True, also run pacman -Syu
    - diff: ConfigDiff between old and new config
    """
    # Generation already has /pacman_local and /current -> . from previous build
    # Mount @var so pacman can find its database via the symlink
    with mount(ctx.btrfs_dev, ctx.var_path, "subvol=@var"):
        # Package changes
        if diff.packages_to_remove:
            print(f"\n=== Removing packages: {diff.packages_to_remove} ===")
            chroot_run(ctx.mount_root, "pacman", "-Rns", "--noconfirm", *sorted(diff.packages_to_remove))

        if diff.packages_to_install:
            print(f"\n=== Installing packages: {diff.packages_to_install} ===")
            chroot_run(ctx.mount_root, "pacman", "-S", "--noconfirm", *sorted(diff.packages_to_install))

        if ctx.upgrade:
            print("\n=== Upgrading system packages ===")
            chroot_run(ctx.mount_root, "pacman", "-Syu", "--noconfirm")

        # Apply changed files
        all_file_changes = {**diff.files_to_add, **diff.files_to_update}
        if all_file_changes:
            print(f"\n=== Applying {len(all_file_changes)} file changes ===")
            for path, entry in all_file_changes.items():
                write_file_entry(ctx.mount_root / path[1:], entry)
                print(f"  {entry[0]}: {path}")

        # Remove deleted files
        if diff.files_to_remove:
            print(f"\n=== Removing {len(diff.files_to_remove)} files ===")
            for path in diff.files_to_remove:
                full_path = ctx.mount_root / path[1:]
                if full_path.exists() or full_path.is_symlink():
                    full_path.unlink()
                    print(f"  removed: {path}")

        # Check if initramfs needs regeneration
        initramfs_paths = {"/etc/mkinitcpio.conf", "/usr/lib/initcpio/hooks/darch",
                           "/usr/lib/initcpio/install/darch"}
        needs_initramfs = bool(set(all_file_changes.keys()) & initramfs_paths)

        if needs_initramfs:
            print("\n=== Regenerating initramfs ===")
            chroot_run(ctx.mount_root, "mkinitcpio", "-P")


# =============================================================================
# Context managers
# =============================================================================

@contextmanager
def lockfile():
    """Acquire exclusive lock to prevent concurrent darch runs."""
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCKFILE, "w", encoding='utf-8') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Error: Another darch process is running (lockfile: {LOCKFILE})")
            sys.exit(1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def loop_device(image_path: str):
    """Loop-mount a disk image, yield (esp_part, btrfs_part)."""
    loop = run(["losetup", "-Pf", "--show", image_path], capture_output=True)
    run(["udevadm", "settle"])
    try:
        yield f"{loop}p1", f"{loop}p2"  # esp, btrfs
    finally:
        run(["sync"])
        run(["losetup", "-d", loop], check=False)


@contextmanager
def mount(device: str, mount_point: Path, options: str | None = None, bind: bool = False):
    """Mount a filesystem, yield mount point as Path."""
    mount_point.mkdir(parents=True, exist_ok=True)
    # Ensure not already mounted from a previous failed run
    run(["umount", str(mount_point)], check=False)
    cmd = ["mount"]
    if bind:
        cmd.append("--bind")
    if options:
        cmd.extend(["-o", options])
    cmd.extend([device, str(mount_point)])
    run(cmd)
    try:
        yield mount_point
    finally:
        # Sync to flush writes, then unmount properly
        run(["sync"])
        run(["umount", str(mount_point)], check=False)


@contextmanager
def image_file(image_path: str, size: str = "10G"):
    """Create a blank disk image with ESP and btrfs partitions + subvolumes."""

    image = Path(image_path)
    if image.exists():
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
            print("\n=== Formatting partitions ===")
            run(["mkfs.fat", "-F32", esp_part])
            run(["mkfs.btrfs", "-f", root_part])

            print("\n=== Creating btrfs subvolumes ===")
            mount_point = Path("/mnt/darch-setup")
            mount_point.mkdir(parents=True, exist_ok=True)
            run(["mount", root_part, str(mount_point)])

            run(["btrfs", "subvol", "create", str(mount_point / "@images")])
            run(["btrfs", "subvol", "create", str(mount_point / "@var")])
            run(["btrfs", "subvol", "create", str(mount_point / "@home")])

            (mount_point / "@home/root").mkdir(mode=0o700, parents=True, exist_ok=True)
            (mount_point / "@var/lib/machines").mkdir(parents=True, exist_ok=True)

            run(["umount", str(mount_point)])
            fix_owner(image)
            print("\n=== Image created successfully ===")
            yield esp_part, root_part


# =============================================================================
# Operations on mounted filesystems
# =============================================================================

def get_generations(images: Path) -> list[GenerationInfo]:
    """Get all generations from mounted @images, sorted by gen number.

    Returns both complete and incomplete generations. Use the `complete` field
    to filter as needed.
    """
    result = []
    for p in images.glob("gen-*"):
        if not p.name[4:].isdigit():
            continue
        gen = int(p.name[4:])
        config_path = p / "config.json"
        if config_path.exists():
            created_at = config_path.stat().st_ctime
            complete = True
        else:
            created_at = None
            complete = False
        result.append(GenerationInfo(gen=gen, path=p, complete=complete, created_at=created_at))
    return sorted(result, key=lambda g: g.gen)


def garbage_collect_generations(images: Path) -> list[int]:
    """Delete incomplete and old generations based on GC settings.

    Policy:
    - Always delete incomplete generations (failed builds)
    - Never delete generations younger than GC_MIN_AGE_DAYS
    - Always keep at least GC_KEEP_MIN complete generations
    - Delete oldest if count exceeds GC_KEEP_MAX (and old enough)
    - Delete generations older than GC_MAX_AGE_DAYS (if above GC_KEEP_MIN)

    Returns list of deleted generation numbers.
    """
    now = time.time()
    deleted = []
    gens = get_generations(images)

    # First pass: delete all incomplete generations
    for g in gens:
        if not g.complete:
            print(f"Deleting incomplete gen-{g.gen}")
            run(["btrfs", "subvol", "delete", str(g.path)])
            deleted.append(g.gen)

    # Second pass: GC old complete generations
    complete = [g for g in gens if g.complete]
    if len(complete) <= GC_KEEP_MIN:
        return deleted

    # Sort by gen number (oldest first) for deletion candidates
    complete_sorted = sorted(complete, key=lambda g: g.gen)
    complete_deleted = []

    for g in complete_sorted:
        remaining = len(complete) - len(complete_deleted)

        # Stop if we're at minimum
        if remaining <= GC_KEEP_MIN:
            break

        age_days = (now - g.created_at) / 86400

        # Never delete generations younger than min age
        if age_days < GC_MIN_AGE_DAYS:
            continue

        # Delete if over max age
        if GC_MAX_AGE_DAYS > 0 and age_days > GC_MAX_AGE_DAYS:
            print(f"Deleting old gen-{g.gen} (age: {age_days:.0f} days)")
            run(["btrfs", "subvol", "delete", str(g.path)])
            complete_deleted.append(g.gen)
            continue

        # Delete if over max count
        if GC_KEEP_MAX > 0 and remaining > GC_KEEP_MAX:
            print(f"Deleting excess gen-{g.gen} (count: {remaining} > {GC_KEEP_MAX})")
            run(["btrfs", "subvol", "delete", str(g.path)])
            complete_deleted.append(g.gen)

    return deleted + complete_deleted


def create_gen_subvol(images: Path, gen: int, snapshot_from: int | None = None):
    """Create a generation subvolume in mounted @images."""
    target = images / f"gen-{gen}"
    # Delete existing subvolume if present (e.g., from failed build)
    if target.exists():
        print(f"Deleting existing gen-{gen}")
        run(["btrfs", "subvol", "delete", str(target)])
    if snapshot_from is not None:
        source = images / f"gen-{snapshot_from}"
        print(f"Creating gen-{gen} as snapshot of gen-{snapshot_from}")
        run(["btrfs", "subvol", "snapshot", str(source), str(target)])
    else:
        print(f"Creating gen-{gen}")
        run(["btrfs", "subvol", "create", str(target)])


def load_gen_config(gen_path: Path) -> Config | None:
    """Load config.json from mounted generation, or None if not found."""
    config_file = gen_path / "config.json"
    if not config_file.exists():
        return None
    return Config.from_json(config_file.read_text())


def load_config_module(config_path: str) -> Config:
    """Load config.py and call configure()."""
    spec = importlib.util.spec_from_file_location("config", config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.configure()

    # Add darch-specific files
    config.add_file("/etc/mkinitcpio.conf", generate_mkinitcpio_conf(config.initramfs_modules))
    config.add_file("/usr/lib/initcpio/hooks/darch", generate_darch_hook_runtime(), mode=0o755)
    config.add_file("/usr/lib/initcpio/install/darch", generate_darch_hook_install(), mode=0o755)
    return config


def apply_configuration(opts: ApplyOptions) -> str:
    """Applies the provided config to the system found in 'image' or 'btrfs'/'esp'"""
    if not opts.image and not (opts.btrfs and opts.esp):
        print("Error: --image or (--btrfs and --esp) required")
        return 1

    with ExitStack() as stack:
        stack.enter_context(lockfile())
        config = load_config_module(opts.config)

        if opts.image:
            esp_dev, btrfs_dev = stack.enter_context(image_file(opts.image, opts.size))
        else:
            esp_dev, btrfs_dev = (opts.esp, opts.btrfs)

        images = stack.enter_context(mount(btrfs_dev, Path("/mnt/darch-images"), "subvol=@images"))

        # Clean up incomplete generations from failed builds
        garbage_collect_generations(images)

        # Find current complete generation
        gens = get_generations(images)
        complete_gens = [g for g in gens if g.complete]
        current = complete_gens[-1].gen if complete_gens else None
        new_gen = (current or 0) + 1

        # Get UUIDs and add runtime-dependent files before diffing
        esp_uuid = run(["blkid", "-s", "UUID", "-o", "value", esp_dev], capture_output=True)
        root_uuid = run(["blkid", "-s", "UUID", "-o", "value", btrfs_dev], capture_output=True)
        config.add_file("/etc/fstab", generate_fstab(esp_uuid))

        fresh = current is None or opts.rebuild
        diff = None
        if not fresh:
            # Check for changes before creating new generation
            with mount(btrfs_dev, Path("/mnt/darch-old"), f"subvol=@images/gen-{current}") as old_gen:
                old_config = load_gen_config(old_gen)
                if old_config is None:
                    print(f"gen-{current} has no config.json, forcing rebuild")
                    fresh = True
                else:
                    diff = ConfigDiff.compute(old_config, config)
                    if not diff.has_changes() and not (opts.upgrade and check_upgrades_available(old_gen)):
                        print("Already up to date.")
                        return 0

        # Create and mount new generation
        create_gen_subvol(images, new_gen, snapshot_from=None if fresh else current)
        mount_root = Path("/mnt/darch-build")
        stack.enter_context(mount(btrfs_dev, mount_root, f"subvol=@images/gen-{new_gen}"))
        stack.enter_context(mount(esp_dev, mount_root / "efi"))
        # Note: @var is mounted by builder functions, not here

        ctx = BuildContext(
            mount_root=mount_root,
            efi_mount=mount_root / "efi",
            var_path=mount_root / "var",
            btrfs_dev=btrfs_dev,
            esp_uuid=esp_uuid,
            root_uuid=root_uuid,
            gen=new_gen,
            fresh_install=fresh,
            upgrade=opts.upgrade,
        )

        if fresh:
            build_generation(config, ctx)
        else:
            # For incremental builds, invalidate inherited config.json so a failed
            # build is clearly incomplete. Rename to .prev for debugging.
            old_config_file = mount_root / "config.json"
            if old_config_file.exists():
                old_config_file.rename(mount_root / "config.json.prev")

            build_incremental(diff, ctx)

        # Configure declarative user if specified
        if config.user:
            print(f"\n=== Configuring user: {config.user.name} ===")
            home_mount = ctx.mount_root / "home"
            home_mount.mkdir(exist_ok=True)
            with mount(ctx.btrfs_dev, home_mount, "subvol=@home"):
                configure_user(config.user, ctx.mount_root, home_mount)
            print(f"  Groups: {sorted(config.user.groups)}")

        # Save config
        print("\n=== Saving config ===")
        (ctx.mount_root / "config.json").write_text(config.to_json())

        # Write GRUB config with all complete generations
        print("\n=== Writing GRUB config ===")
        complete_gens = [g for g in get_generations(images) if g.complete]
        grub_cfg = ctx.efi_mount / "grub" / "grub.cfg"
        grub_cfg.parent.mkdir(parents=True, exist_ok=True)
        grub_cfg.write_text(generate_grub_config(ctx.root_uuid, complete_gens))

        print(f"\n=== SUCCESS: Built gen-{new_gen} ===")
    return 0


def find_ovmf() -> tuple[Path, Path] | None:
    """Find OVMF firmware files for UEFI boot."""
    ovmf_paths = [
        ("/usr/share/edk2-ovmf/x64/OVMF_CODE.4m.fd", "/usr/share/edk2-ovmf/x64/OVMF_VARS.4m.fd"),
        ("/usr/share/edk2-ovmf/x64/OVMF_CODE.fd", "/usr/share/edk2-ovmf/x64/OVMF_VARS.fd"),
        ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
    ]
    for code, vars in ovmf_paths:
        if Path(code).exists() and Path(vars).exists():
            return Path(code), Path(vars)
    return None


def test_image(opts: TestOptions) -> int:
    """Boot an image in QEMU for testing."""
    image = Path(opts.image)
    if not image.exists():
        print(f"Error: Image file '{opts.image}' not found")
        return 1

    if not shutil.which("qemu-system-x86_64"):
        print("Error: qemu-system-x86_64 not found")
        print("Install with: sudo pacman -S qemu-full")
        return 1

    ovmf = find_ovmf()
    if not ovmf:
        print("Error: OVMF firmware not found")
        print("Install with: sudo pacman -S edk2-ovmf")
        return 1

    ovmf_code, ovmf_vars = ovmf
    print(f"Starting QEMU with image: {opts.image}")
    print(f"OVMF: {ovmf_code}")
    print(f"Mode: {'graphics' if opts.graphics else 'serial console'}")

    # Create a temporary copy of OVMF_VARS (it's writable)
    vars_copy = tempfile.NamedTemporaryFile(delete=False)
    vars_copy.write(ovmf_vars.read_bytes())
    vars_copy.close()

    cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu", "host",
        "-m", opts.memory,
        "-smp", str(opts.cpus),
        "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
        "-drive", f"if=pflash,format=raw,file={vars_copy.name}",
        "-drive", f"file={opts.image},format=raw",
        "-net", "none",
        "-usb",
        "-device", "usb-tablet",
    ]

    if opts.graphics:
        # Virtio GPU with OpenGL acceleration
        cmd += [
            "-device", "virtio-vga",
            "-display", "gtk",
        ]
        print("Close window to exit")
    else:
        # Serial console mode
        logfile = Path("qemu-console.log")
        print(f"Logging console output to: {logfile}")
        print("Exit with: Ctrl-A X")
        cmd += [
            "-nographic",
            "-chardev", f"stdio,mux=on,id=char0,logfile={logfile},signal=off",
            "-serial", "chardev:char0",
            "-mon", "chardev=char0",
        ]

    print()

    try:
        run(cmd)
    finally:
        Path(vars_copy.name).unlink(missing_ok=True)

    return 0


def main() -> int:
    """Darch entry point."""
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
    p_apply.add_argument("--config", default="./config.py", help="Path to config.py")
    p_apply.add_argument("--upgrade", action="store_true", help="Also upgrade all packages (pacman -Syu)")
    p_apply.add_argument("--rebuild", action="store_true", help="Force fresh build even if generations exist")

    # test command
    p_test = subparsers.add_parser("test", help="Boot an image in QEMU for testing")
    p_test.add_argument("image", help="Path to disk image")
    p_test.add_argument("--memory", default="4G", help="VM memory (default: 4G)")
    p_test.add_argument("--cpus", type=int, default=2, help="Number of CPUs (default: 2)")
    p_test.add_argument("--graphics", action="store_true", help="Enable graphical display (virtio-gpu)")

    args = parser.parse_args()

    if args.command == "test":
        return test_image(TestOptions(
            image=args.image,
            memory=args.memory,
            cpus=args.cpus,
            graphics=args.graphics,
        ))

    # Commands below require root
    if os.geteuid() != 0:
        print("Error: This command must be run as root")
        return 1

    if args.command == "apply":
        return apply_configuration(ApplyOptions(
            config=args.config,
            image=args.image,
            size=args.size,
            btrfs=args.btrfs,
            esp=args.esp,
            upgrade=args.upgrade,
            rebuild=args.rebuild,
        ))

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
