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
from typing import Set, Dict, Tuple, Literal, Iterator
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
    files: Dict[str, FileEntry | SymlinkEntry] = field(default_factory=dict)

    def _validate_path(self, path: str) -> None:
        """Validate a user file path."""
        if not path.startswith("~"):
            raise ValueError(f"User file path must start with '~': {path}")
        if ".." in path:
            raise ValueError(f"User file path cannot contain '..': {path}")

    def add_groups(self, *names: str) -> "User":
        """Add groups to the user."""
        self.groups.update(names)
        return self

    def add_file(self, path: str, content: str, mode: int | None = None) -> User:
        """Add a file to the user's home directory. Path must start with ~."""
        self._validate_path(path)
        self.files[path] = ('file', content, mode)
        return self

    def add_symlink(self, path: str, target: str) -> User:
        """Add a symlink in the user's home directory. Path must start with ~."""
        self._validate_path(path)
        self.files[path] = ('symlink', target)
        return self

    def to_dict(self) -> dict:
        """Serialize user to a dict."""
        return {
            "name": self.name,
            "uid": self.uid,
            "shell": self.shell,
            "password_hash": self.password_hash,
            "groups": sorted(self.groups),
            "files": {k: list(v) for k, v in self.files.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "User":
        """Deserialize user from a dict."""
        user = cls(name=data["name"])
        user.uid = data.get("uid", 1000)
        user.shell = data.get("shell", "/bin/bash")
        user.password_hash = data.get("password_hash")
        user.groups = set(data.get("groups", []))
        user.files = {k: tuple(v) for k, v in data.get("files", {}).items()}
        return user


@dataclass
class ConfigDiff:
    """Differences between two configs."""
    packages_to_install: Set[str]
    packages_to_remove: Set[str]
    files_to_add: Dict[str, FileEntry | SymlinkEntry]
    files_to_remove: Dict[str, FileEntry | SymlinkEntry]
    files_to_update: Dict[str, FileEntry | SymlinkEntry]
    users_changed: bool

    @classmethod
    def compute(cls, old: Config, new: Config) -> ConfigDiff:
        """Compare two configs and return the differences."""
        old_users = json.dumps([u.to_dict() for u in old.users], sort_keys=True)
        new_users = json.dumps([u.to_dict() for u in new.users], sort_keys=True)
        return cls(
            packages_to_install=new.packages - old.packages,
            packages_to_remove=old.packages - new.packages,
            files_to_add={p: e for p, e in new.files.items() if p not in old.files},
            files_to_remove={p: e for p, e in old.files.items() if p not in new.files},
            files_to_update={p: e for p, e in new.files.items()
                             if p in old.files and old.files[p] != e},
            users_changed=old_users != new_users,
        )

    def has_changes(self) -> bool:
        """Check if this diff has any changes."""
        return bool(
            self.packages_to_install or
            self.packages_to_remove or
            self.files_to_add or
            self.files_to_remove or
            self.files_to_update or
            self.users_changed
        )

    def print_summary(self):
        """Print a human-readable summary of changes."""
        if not self.has_changes():
            print("No configuration changes.")
            return

        if self.packages_to_install:
            print(f"\nPackages to install ({len(self.packages_to_install)}):")
            for pkg in sorted(self.packages_to_install):
                print(f"  + {pkg}")

        if self.packages_to_remove:
            print(f"\nPackages to remove ({len(self.packages_to_remove)}):")
            for pkg in sorted(self.packages_to_remove):
                print(f"  - {pkg}")

        if self.files_to_add:
            print(f"\nFiles to add ({len(self.files_to_add)}):")
            for path in sorted(self.files_to_add):
                print(f"  + {path}")

        if self.files_to_update:
            print(f"\nFiles to update ({len(self.files_to_update)}):")
            for path in sorted(self.files_to_update):
                print(f"  ~ {path}")

        if self.files_to_remove:
            print(f"\nFiles to remove ({len(self.files_to_remove)}):")
            for path in sorted(self.files_to_remove):
                print(f"  - {path}")

        if self.users_changed:
            print("\nUser configuration changed.")


@dataclass
class Config:
    """
    Declarative system configuration.

    Build up with helper methods, then pass to build functions.
    """

    # All packages (passed to pacstrap)
    packages: Set[str] = field(default_factory=lambda: {
        # these packages are needed for darch to function
        "base",
        "btrfs-progs",
        "grub",
        "efibootmgr",
        "python",
        "pacman-contrib",
        "arch-install-scripts"
    })

    # Files and symlinks: path -> ('file', content) | ('symlink', target)
    files: Dict[str, FileEntry | SymlinkEntry] = field(default_factory=dict)

    # Kernel modules for initramfs
    initramfs_modules: Set[str] = field(default_factory=lambda: {
        "btrfs", "ata_piix", "ahci", "sd_mod", "virtio_blk", "virtio_pci"
    })

    # Users
    users: list[User] = field(default_factory=list)

    # -------------------------------------------------------------------------
    # Builder methods
    # -------------------------------------------------------------------------

    def add_packages(self, *names: str) -> Config:
        """Add packages to install."""
        self.packages.update(names)
        return self

    def enable_qemu_testing(self):
        """Add packages necessary for darch to create images, and to run them with QEMU."""
        self.add_packages("edk2-ovmf", "qemu", "gptfdisk", "dosfstools")

    def add_file(self, path: str, content: str, mode: int | None = None) -> Config:
        """Add a file with content and optional mode."""
        self.files[path] = ('file', content, mode)
        return self

    def add_symlink(self, path: str, target: str) -> Config:
        """Add a symlink."""
        self.files[path] = ('symlink', target)
        return self

    def enable_service(self, name: str, target: str = "multi-user.target") -> Config:
        """Enable a systemd service (creates symlink in target.wants)."""
        if not name.endswith(('.service', '.socket', '.timer', '.path', '.mount')):
            name = f"{name}.service"
        return self.add_symlink(
            f"/etc/systemd/system/{target}.wants/{name}",
            f"/usr/lib/systemd/system/{name}"
        )

    def mask_service(self, name: str) -> Config:
        """Mask a systemd service (symlink to /dev/null)."""
        if not name.endswith(('.service', '.socket', '.timer', '.path', '.mount')):
            name = f"{name}.service"
        return self.add_symlink(f"/etc/systemd/system/{name}", "/dev/null")

    def set_timezone(self, tz: str) -> Config:
        """Set system timezone."""
        return self.add_symlink("/etc/localtime", f"/usr/share/zoneinfo/{tz}")

    def set_locales(self, locale: str, *extra_gen: list[str]) -> Config:
        """Set system locale."""
        gen_lines = [f"{locale} UTF-8"]
        for extra in extra_gen:
            gen_lines.append(f"{extra} UTF-8")
        self.add_file("/etc/locale.gen", '\n'.join(gen_lines))
        return self.add_file("/etc/locale.conf", f"LANG={locale}\n")

    def set_keymap(self, keymap: str) -> Config:
        """Set console keymap."""
        return self.add_file("/etc/vconsole.conf", f"KEYMAP={keymap}\n")

    def set_hostname(self, hostname: str) -> Config:
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
            "users": [u.to_dict() for u in self.users],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        """Deserialize config from a dict."""
        config = cls()
        config.packages = set(data.get("packages", []))
        config.initramfs_modules = set(data.get("initramfs_modules", []))
        # Convert files: lists back to tuples
        for path, entry in data.get("files", {}).items():
            config.files[path] = tuple(entry)
        # Deserialize users
        config.users = [User.from_dict(u) for u in data.get("users", [])]
        return config

    def to_json(self) -> str:
        """Serialize config to JSON string."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> Config:
        """Deserialize config from JSON string."""
        return cls.from_dict(json.loads(s))


@dataclass
class BuildInfo:
    """Metadata about how a generation was built."""
    fresh: bool                 # True if full rebuild, False if incremental
    package_count: int = 0      # Total packages in generation

    def to_dict(self) -> dict:
        return {"fresh": self.fresh, "package_count": self.package_count}

    @classmethod
    def from_dict(cls, data: dict) -> "BuildInfo":
        return cls(fresh=data.get("fresh", True), package_count=data.get("package_count", 0))

    def summary(self) -> str:
        """Compact summary like 'rebuild, 150 pkgs' or 'incremental, 150 pkgs'."""
        build_type = "rebuild" if self.fresh else "incremental"
        return f"{build_type}, {self.package_count} pkgs"


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
    build_info: BuildInfo | None = None


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


def generate_fstab(esp_uuid: str, root_uuid: str) -> str:
    """Generate /etc/fstab content."""
    return f"""# /etc/fstab: static file system information
# Root is tmpfs, @images/@var/@home mounted by initramfs
#
# <file system>             <mount point>  <type>  <options>  <dump> <pass>

# EFI System Partition
UUID={esp_uuid}             /efi           vfat    rw,relatime,fmask=0022,dmask=0022,codepage=437,iocharset=ascii,shortname=mixed,utf8,errors=remount-ro  0 2

# @var (mounted by initramfs)
UUID={root_uuid}            /var           btrfs   rw,subvol=@var,x-initrd.mount,nofail  0 0
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
        if not g.complete or g.created_at is None:
            continue
        created_str = datetime.fromtimestamp(g.created_at).strftime("%Y-%m-%d %H:%M")
        build_str = f", {g.build_info.summary()}" if g.build_info else ""
        entries.append(f"""
menuentry "Arch Linux (gen-{g.gen}, {created_str}{build_str})" {{
    linux /@images/gen-{g.gen}/boot/vmlinuz-linux \\
        root=UUID={root_uuid} \\
        darch.gen={g.gen} \\
        console=tty0 console=ttyS0,115200 \\
        systemd.gpt_auto=0 rw
    initrd /@images/gen-{g.gen}/boot/initramfs-linux.img
}}""")
    return header + "".join(entries) + "\n"


def configure_users(users: list[User], gen_root: Path, home_path: Path):
    """
    Configure declarative users in the generation.

    Reads base system users from generation's /etc, adds/updates declarative users,
    writes back. Also creates home directories in @home.
    """
    if not users:
        return

    etc = gen_root / "etc"
    user_names = {u.name for u in users}

    # Read base system files, filtering out declarative users
    passwd_lines = [l for l in (etc / "passwd").read_text().splitlines()
                    if l and l.split(":")[0] not in user_names]
    shadow_lines = [l for l in (etc / "shadow").read_text().splitlines()
                    if l and l.split(":")[0] not in user_names]
    group_lines = [l for l in (etc / "group").read_text().splitlines()
                   if l and l.split(":")[0] not in user_names]
    gshadow_lines = [l for l in (etc / "gshadow").read_text().splitlines()
                     if l and l.split(":")[0] not in user_names]

    # Add user entries
    for user in users:
        passwd_lines.append(f"{user.name}:x:{user.uid}:{user.uid}::/home/{user.name}:{user.shell}")
        pw_hash = user.password_hash if user.password_hash else "!"
        shadow_lines.append(f"{user.name}:{pw_hash}:19000:0:99999:7:::")
        group_lines.append(f"{user.name}:x:{user.uid}:")
        gshadow_lines.append(f"{user.name}:!::")

    # Find existing group names and max GID for creating new groups
    existing_groups = set()
    max_gid = 999  # Start user groups at 1000+
    for line in group_lines:
        parts = line.split(":")
        if len(parts) >= 3:
            existing_groups.add(parts[0])
            try:
                gid = int(parts[2])
                if gid > max_gid:
                    max_gid = gid
            except ValueError:
                pass

    # Collect all needed groups from all users
    all_groups = set()
    for user in users:
        all_groups.update(user.groups)

    # Create missing groups
    for group_name in sorted(all_groups):
        if group_name not in existing_groups:
            max_gid += 1
            group_lines.append(f"{group_name}:x:{max_gid}:")
            gshadow_lines.append(f"{group_name}:!::")
            existing_groups.add(group_name)
            print(f"  Created group: {group_name} (gid={max_gid})")

    # Add users to their supplementary groups
    new_group_lines = []
    for line in group_lines:
        parts = line.split(":")
        if len(parts) >= 4:
            group_name = parts[0]
            members = [m for m in parts[3].split(",") if m]
            # Add each user who should be in this group
            for user in users:
                if group_name in user.groups and user.name not in members:
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

    # Create home directories in @home
    for user in users:
        user_home = home_path / user.name
        if not user_home.exists():
            user_home.mkdir(parents=True, exist_ok=True)
            user_home.chmod(0o700)
            os.chown(user_home, user.uid, user.uid)
            print(f"  Created home directory: {user_home}")

    # Write user files to home directories
    for user in users:
        if not user.files:
            continue
        user_home = home_path / user.name
        for file_path, entry in user.files.items():
            # Expand ~ to user's home directory
            rel_path = file_path[2:] if file_path.startswith("~/") else file_path[1:]
            path = user_home / rel_path

            # Create parent directories with user ownership
            if not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                # Walk up and chown all created directories
                for parent in [path.parent] + list(path.parent.parents):
                    if parent == user_home or not str(parent).startswith(str(user_home)):
                        break
                    os.chown(parent, user.uid, user.uid)

            if entry[0] == 'file':
                content, mode = entry[1], entry[2]
                path.write_text(content)
                if mode is not None:
                    path.chmod(mode)
                os.chown(path, user.uid, user.uid)
                print(f"  {user.name}: {file_path}")
            elif entry[0] == 'symlink':
                target = entry[1]
                # Expand ~ in symlink target
                if target.startswith("~/"):
                    target = str(user_home / target[2:])
                elif target.startswith("~"):
                    target = str(user_home / target[1:])
                if path.exists() or path.is_symlink():
                    path.unlink()
                path.symlink_to(target)
                os.lchown(path, user.uid, user.uid)
                print(f"  {user.name}: {file_path} -> {target}")


# =============================================================================
# Build context and functions
# =============================================================================

@dataclass
class BuildContext:
    """Runtime context for a build - paths and UUIDs discovered during setup."""
    mount_root: Path     # Where generation is mounted
    efi_mount: Path      # Where ESP is mounted
    var_path: Path       # Where @var is mounted (or will be mounted)
    btrfs_dev: Path      # Btrfs device path (for mounting @var)
    esp_uuid: str        # UUID of ESP partition
    root_uuid: str       # UUID of btrfs partition
    gen: int = 1         # Generation number
    fresh_install: bool = True  # False for rebuild of existing system
    upgrade: bool = False       # Run pacman -Syu


def run(cmd, check=True, capture_output=False) -> str:
    """Run a command and optionally capture output."""
    # Clean environment: remove locale vars to prevent host locale leaking into chroot
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("LC_") and k not in ("LANGUAGE",)}
    env["LC_ALL"] = "C"

    cmd_str = ' '.join(str(c) for c in cmd)
    print(f"Running: {cmd_str}")
    try:
        if capture_output:
            result = subprocess.run(cmd, check=check, capture_output=True, text=True, env=env)
            return result.stdout.strip()
        subprocess.run(cmd, check=check, env=env)
        return ""
    except subprocess.CalledProcessError as e:
        print(f"\nError: Command failed: {cmd_str}")
        print(f"Exit code: {e.returncode}")
        if e.stderr:
            print(f"stderr:\n{e.stderr}")
        raise


def chroot_run(root: Path, *cmd, check=True, capture_output=False) -> str | None:
    """Run a command inside a chroot."""
    return run(["arch-chroot", root] + list(cmd), check=check, capture_output=capture_output)


def fix_owner(path: Path):
    """Fix ownership of a file to the invoking user (when run via sudo)."""
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and gid:
        os.chown(path, int(uid), int(gid))


def write_config_files(root: Path, files: Dict[str, FileEntry | SymlinkEntry]) -> Set[str]:
    """Write config files to filesystem, returning paths that were changed."""
    changed = set()
    for config_path, entry in files.items():
        path = root / config_path[1:]  # strip leading /
        path.parent.mkdir(parents=True, exist_ok=True)

        # Check if file already matches
        if entry[0] == 'file':
            content, mode = entry[1], entry[2]
            if path.is_file() and not path.is_symlink():
                if path.read_text() == content and (mode is None or path.stat().st_mode & 0o777 == mode):
                    continue
            if path.exists() or path.is_symlink():
                path.unlink()
            path.write_text(content)
            if mode is not None:
                path.chmod(mode)
            print(f"  file: {config_path}")
        elif entry[0] == 'symlink':
            target = entry[1]
            if path.is_symlink() and os.readlink(path) == target:
                continue
            if path.exists() or path.is_symlink():
                path.unlink()
            path.symlink_to(target)
            print(f"  symlink: {config_path}")

        changed.add(config_path)
    return changed


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


def get_available_upgrades(mount_root: Path) -> list[str]:
    """Get list of available package upgrades. Returns empty list if none."""
    try:
        output = chroot_run(mount_root, "checkupdates", capture_output=True)
        return output.strip().split('\n') if output.strip() else []
    except subprocess.CalledProcessError as e:
        if e.returncode == 2:
            return []  # No updates available
        # Exit code 1 = unknown failure
        print(f"Warning: checkupdates failed (exit code {e.returncode})")
        if e.stderr:
            print(f"  {e.stderr.strip()}")
        raise


def check_upgrades_available(mount_root: Path) -> bool:
    """Check if package upgrades are available."""
    return len(get_available_upgrades(mount_root)) > 0


def count_packages(mount_root: Path) -> int:
    """Count installed packages in a generation."""
    output = chroot_run(mount_root, "pacman", "--dbpath", "/pacman", "-Q", capture_output=True)
    return len(output.strip().split('\n')) if output.strip() else 0


def build_generation(config: Config, ctx: BuildContext):
    """
    Build a generation from config into the mounted filesystem.

    Expects:
    - ctx.mount_root: generation subvolume mounted here
    - ctx.efi_mount: ESP mounted here
    - @var NOT mounted (we mount it after moving pacman db)
    - Partitions already formatted, subvolumes already created
    """
    # Write config files before pacstrap so they exist for package install hooks
    print("\n=== Writing config files (pre-pacstrap) ===")
    write_config_files(ctx.mount_root, config.files)

    print("\n=== Installing base system with pacstrap ===")
    # Bind-mount host's package cache so pacstrap reads/writes there.
    # On non-darch host: uses host cache. On darch system: uses @var cache.
    gen_cache = ctx.mount_root / "var" / "cache" / "pacman" / "pkg"
    gen_cache.mkdir(parents=True, exist_ok=True)
    with mount(Path("/var/cache/pacman/pkg"), gen_cache, bind=True):
        run(["pacstrap", "-K", ctx.mount_root] + sorted(config.packages))

    print("\n=== Relocating pacman state to /pacman ===")
    pacman_src = ctx.mount_root / "var" / "lib" / "pacman"
    pacman_dst = ctx.mount_root / "pacman"
    if pacman_src.exists():
        shutil.move(pacman_src, pacman_dst)
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
        write_config_files(ctx.mount_root, config.files)

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
        # Package changes - sync database first if installing or upgrading
        if diff.packages_to_install or ctx.upgrade:
            print("\n=== Syncing package database ===")
            chroot_run(ctx.mount_root, "pacman", "-Sy")

        if diff.packages_to_remove:
            print(f"\n=== Removing packages: {diff.packages_to_remove} ===")
            chroot_run(ctx.mount_root, "pacman", "-Rns", "--noconfirm", *sorted(diff.packages_to_remove))

        if diff.packages_to_install:
            print(f"\n=== Installing packages: {diff.packages_to_install} ===")
            chroot_run(ctx.mount_root, "pacman", "-S", "--noconfirm", *sorted(diff.packages_to_install))

        if ctx.upgrade:
            print("\n=== Upgrading system packages ===")
            chroot_run(ctx.mount_root, "pacman", "-Su", "--noconfirm")

        # Apply changed files
        files_to_write = {**diff.files_to_add, **diff.files_to_update}
        print("\n=== Applying config files ===")
        changed_files = write_config_files(ctx.mount_root, files_to_write)

        # Remove deleted files
        for path in diff.files_to_remove:
            full_path = ctx.mount_root / path[1:]
            if full_path.exists() or full_path.is_symlink():
                full_path.unlink()
                print(f"  removed: {path}")

        # Regenerate locale if locale.gen changed
        if "/etc/locale.gen" in changed_files:
            print("\n=== Regenerating locales ===")
            chroot_run(ctx.mount_root, "locale-gen")

        # Check if initramfs needs regeneration
        initramfs_paths = {"/etc/mkinitcpio.conf", "/usr/lib/initcpio/hooks/darch",
                           "/usr/lib/initcpio/install/darch"}
        needs_initramfs = bool(changed_files & initramfs_paths)

        if needs_initramfs:
            print("\n=== Regenerating initramfs ===")
            chroot_run(ctx.mount_root, "mkinitcpio", "-P")


def detect_darch_system() -> tuple[Path, Path] | None:
    """
    Detect if running on a darch system and return (btrfs_dev, esp_dev).

    Returns None if not running on darch.
    """
    current = Path("/current")
    if not current.is_symlink():
        return None

    target = os.readlink(current)
    if not target.startswith("images/gen-"):
        return None

    # Parse /proc/mounts to find devices
    btrfs_dev = None
    esp_dev = None
    with open("/proc/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3:
                device, mountpoint, fstype = parts[0], parts[1], parts[2]
                if mountpoint == "/images" and fstype == "btrfs":
                    btrfs_dev = Path(device)
                elif mountpoint == "/efi" and fstype == "vfat":
                    esp_dev = Path(device)

    if btrfs_dev and esp_dev:
        return btrfs_dev, esp_dev
    return None


def switch_generation(new_gen: int):
    """Switch the running system to a new generation by updating /current symlink."""
    current = Path("/current")
    new_target = f"images/gen-{new_gen}"

    # Atomically replace symlink
    tmp_link = Path("/current.new")
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(new_target)
    tmp_link.rename(current)
    print(f"Switched /current -> {new_target}")


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


def cleanup_stale_loops(image_path: Path):
    """Detach any stale loop devices associated with this image (including deleted)."""
    image_name = image_path.name
    result = run(["losetup", "-a"], capture_output=True)
    mounts = run(["mount"], capture_output=True)

    # Find all loop devices for this image
    loop_devs = []
    for line in result.strip().split('\n'):
        if line and image_name in line:
            loop_devs.append(line.split(':')[0])

    # Collect all mounts from these loop devices
    devices_to_unmount = []
    for mount_line in mounts.split('\n'):
        for loop_dev in loop_devs:
            if mount_line.startswith(loop_dev):
                devices_to_unmount.append(mount_line.split(' on ')[0])

    # Unmount in reverse order (last mounted first) - mount output is in mount order
    for device in reversed(devices_to_unmount):
        print(f"  Unmounting: {device}")
        run(["umount", "-l", device], check=False)

    # Detach loop devices
    for loop_dev in loop_devs:
        print(f"  Detaching stale loop device: {loop_dev}")
        run(["losetup", "-d", loop_dev], check=False)


@contextmanager
def loop_device(image_path: Path) -> Iterator[Tuple[Path, Path]]:
    """Loop-mount a disk image, yield (esp_part, btrfs_part)."""
    cleanup_stale_loops(image_path)
    loop = run(["losetup", "-Pf", "--show", image_path], capture_output=True)
    run(["udevadm", "settle"])
    try:
        yield Path(f"{loop}p1"), Path(f"{loop}p2")  # esp, btrfs
    finally:
        run(["sync"])
        run(["losetup", "-d", loop], check=False)


@contextmanager
def mount(device: Path, mount_point: Path, options: str | None = None, bind: bool = False) -> Iterator[Path]:
    """Mount a filesystem, yield mount point as Path."""
    mount_point.mkdir(parents=True, exist_ok=True)
    # Ensure not already mounted from a previous failed run
    run(["umount", mount_point], check=False)
    cmd: list[str | Path] = ["mount"]
    if bind:
        cmd.append("--bind")
    if options:
        cmd.extend(["-o", options])
    cmd.extend([device, mount_point])
    run(cmd)
    try:
        yield mount_point
    finally:
        # Sync to flush writes, then unmount properly
        run(["sync"])
        run(["umount", mount_point], check=False)


@contextmanager
def image_file(image_path: Path, size: str = "10G") -> Iterator[Tuple[Path, Path]]:
    """Create a blank disk image with ESP and btrfs partitions + subvolumes."""

    if image_path.exists():
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
            run(["mount", root_part, mount_point])

            run(["btrfs", "subvol", "create", mount_point / "@images"])
            run(["btrfs", "subvol", "create", mount_point / "@var"])
            run(["btrfs", "subvol", "create", mount_point / "@home"])

            (mount_point / "@home/root").mkdir(mode=0o700, parents=True, exist_ok=True)
            (mount_point / "@var/lib/machines").mkdir(parents=True, exist_ok=True)

            run(["umount", mount_point])
            fix_owner(image_path)
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
        build_info = None
        if config_path.exists():
            created_at = config_path.stat().st_ctime
            complete = True
            # Load build info if present
            build_info_path = p / "build-info.json"
            if build_info_path.exists():
                try:
                    build_info = BuildInfo.from_dict(json.loads(build_info_path.read_text()))
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"Warning: Failed to parse {build_info_path}: {e}")
        else:
            created_at = None
            complete = False
        result.append(GenerationInfo(gen=gen, path=p, complete=complete, created_at=created_at, build_info=build_info))
    return sorted(result, key=lambda g: g.gen)


def garbage_collect_generations(images: Path):
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
    gens = get_generations(images)

    # First pass: delete all incomplete generations
    for g in gens:
        if not g.complete:
            print(f"Deleting incomplete gen-{g.gen}")
            run(["btrfs", "subvol", "delete", g.path])

    # Second pass: GC old complete generations
    complete = [g for g in gens if g.complete]
    if len(complete) <= GC_KEEP_MIN:
        return

    # Sort by gen number (oldest first) for deletion candidates
    complete_sorted = sorted(complete, key=lambda g: g.gen)
    complete_deleted = 0

    for g in complete_sorted:
        if not g.complete or g.created_at is None:
            continue
        remaining = len(complete) - complete_deleted

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
            run(["btrfs", "subvol", "delete", g.path])
            complete_deleted += 1
            continue

        # Delete if over max count
        if GC_KEEP_MAX > 0 and remaining > GC_KEEP_MAX:
            print(f"Deleting excess gen-{g.gen} (count: {remaining} > {GC_KEEP_MAX})")
            run(["btrfs", "subvol", "delete", g.path])
            complete_deleted += 1


def create_gen_subvol(images: Path, gen: int, snapshot_from: int | None = None):
    """Create a generation subvolume in mounted @images."""
    target = images / f"gen-{gen}"
    # Delete existing subvolume if present (e.g., from failed build)
    if target.exists():
        print(f"Deleting existing gen-{gen}")
        run(["btrfs", "subvol", "delete", target])
    if snapshot_from is not None:
        source = images / f"gen-{snapshot_from}"
        print(f"Creating gen-{gen} as snapshot of gen-{snapshot_from}")
        run(["btrfs", "subvol", "snapshot", source, target])
    else:
        print(f"Creating gen-{gen}")
        run(["btrfs", "subvol", "create", target])


def load_gen_config(gen_path: Path) -> Config | None:
    """Load config.json from mounted generation, or None if not found."""
    config_file = gen_path / "config.json"
    if not config_file.exists():
        return None
    return Config.from_json(config_file.read_text())


def load_config_module(config_path: Path) -> Config | None:
    """Load config.py and call configure()."""
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        return None

    spec = importlib.util.spec_from_file_location("config", config_path)
    if spec is None or spec.loader is None:
        print(f"Error: Could not load config module: {config_path}")
        return None

    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"Error: Failed to execute config module: {e}")
        return None

    if not hasattr(module, "configure"):
        print(f"Error: Config module missing configure() function: {config_path}")
        return None

    config = module.configure()

    # Add darch-specific files
    config.add_file("/etc/mkinitcpio.conf", generate_mkinitcpio_conf(config.initramfs_modules))
    config.add_file("/usr/lib/initcpio/hooks/darch", generate_darch_hook_runtime(), mode=0o755)
    config.add_file("/usr/lib/initcpio/install/darch", generate_darch_hook_install(), mode=0o755)
    return config


def apply_configuration(
    config_path: Path,
    image_path: Path | None,
    image_size: str,
    btrfs_dev: Path | None,
    esp_dev: Path | None,
    upgrade: bool,
    rebuild: bool,
    switch: bool,
) -> int:
    """Applies the provided config to the system found in 'image' or 'btrfs'/'esp'"""
    with ExitStack() as stack:
        stack.enter_context(lockfile())
        config = load_config_module(config_path)
        if config is None:
            print("Error: Could not load configuration.")
            return 1

        on_darch = False
        if image_path:
            if btrfs_dev is not None or esp_dev is not None:
                print("Error: --btrfs and --esp not supported in combination with --image")
                return 1
            esp_dev, btrfs_dev = stack.enter_context(image_file(image_path, image_size))
        elif btrfs_dev is None and esp_dev is None:
            # Try auto-detection on darch system
            detected = detect_darch_system()
            if detected:
                btrfs_dev, esp_dev = detected
                on_darch = True
                print(f"Detected darch system: btrfs={btrfs_dev}, esp={esp_dev}")
            else:
                print("Error: --image or (--btrfs and --esp) required")
                print("       (or run on a booted darch system for auto-detection)")
                return 1
        elif btrfs_dev is None or esp_dev is None:
            print("Error: --btrfs and --esp must both be specified")
            return 1

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
        config.add_file("/etc/fstab", generate_fstab(esp_uuid, root_uuid))

        fresh = current is None or rebuild
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
                    if not diff.has_changes() and not (upgrade and check_upgrades_available(old_gen)):
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
            upgrade=upgrade,
        )

        if fresh:
            build_generation(config, ctx)
        else:
            # For incremental builds, invalidate inherited config.json so a failed
            # build is clearly incomplete. Rename to .prev for debugging.
            old_config_file = mount_root / "config.json"
            if old_config_file.exists():
                old_config_file.rename(mount_root / "config.json.prev")

            assert diff is not None
            build_incremental(diff, ctx)

        # Configure declarative users
        if config.users:
            print(f"\n=== Configuring users: {[u.name for u in config.users]} ===")
            home_mount = ctx.mount_root / "home"
            home_mount.mkdir(exist_ok=True)
            with mount(ctx.btrfs_dev, home_mount, "subvol=@home"):
                configure_users(config.users, ctx.mount_root, home_mount)

        # Save config and build info
        print("\n=== Saving config ===")
        (ctx.mount_root / "config.json").write_text(config.to_json())
        build_info = BuildInfo(fresh=fresh, package_count=count_packages(ctx.mount_root))
        (ctx.mount_root / "build-info.json").write_text(json.dumps(build_info.to_dict()))

        # Write GRUB config with all complete generations
        print("\n=== Writing GRUB config ===")
        complete_gens = [g for g in get_generations(images) if g.complete]
        grub_cfg = ctx.efi_mount / "grub" / "grub.cfg"
        grub_cfg.parent.mkdir(parents=True, exist_ok=True)
        grub_cfg.write_text(generate_grub_config(ctx.root_uuid, complete_gens))

        print(f"\n=== SUCCESS: Built gen-{new_gen} ===")

        # Live switch if requested and running on darch
        if switch:
            if on_darch:
                print("\n=== Switching to new generation ===")
                switch_generation(new_gen)
                print("Note: Kernel/initramfs changes require reboot")
            else:
                print("Note: --switch ignored (not running on darch system)")

    return 0


def check_configuration(
    config_path: Path,
    image_path: Path | None,
    btrfs_dev: Path | None,
    esp_dev: Path | None,
    upgrade: bool,
) -> int:
    """Check what would change without building (dry-run mode)."""
    with ExitStack() as stack:
        config = load_config_module(config_path)
        if config is None:
            print("Error: Could not load configuration.")
            return 1

        if image_path:
            if btrfs_dev is not None or esp_dev is not None:
                print("Error: --btrfs and --esp not supported in combination with --image")
                return 1
            esp_dev, btrfs_dev = stack.enter_context(loop_device(image_path))
        elif btrfs_dev is None and esp_dev is None:
            detected = detect_darch_system()
            if detected:
                btrfs_dev, esp_dev = detected
            else:
                print("Error: --image or (--btrfs and --esp) required")
                return 1
        elif btrfs_dev is None or esp_dev is None:
            print("Error: --btrfs and --esp must both be specified")
            return 1

        images = stack.enter_context(mount(btrfs_dev, Path("/mnt/darch-images"), "subvol=@images"))

        # Find current complete generation
        gens = get_generations(images)
        complete_gens = [g for g in gens if g.complete]

        if not complete_gens:
            print("No existing generations. A fresh build would be performed.")
            print(f"\nPackages to install ({len(config.packages)}):")
            for pkg in sorted(config.packages):
                print(f"  + {pkg}")
            print(f"\nFiles to create ({len(config.files)}):")
            for path in sorted(config.files):
                print(f"  + {path}")
            return 0

        current = complete_gens[-1]

        # Add runtime-dependent files before diffing
        esp_uuid = run(["blkid", "-s", "UUID", "-o", "value", esp_dev], capture_output=True)
        root_uuid = run(["blkid", "-s", "UUID", "-o", "value", btrfs_dev], capture_output=True)
        config.add_file("/etc/fstab", generate_fstab(esp_uuid, root_uuid))

        # Load old config and compute diff
        with mount(btrfs_dev, Path("/mnt/darch-old"), f"subvol=@images/gen-{current.gen}") as old_gen:
            old_config = load_gen_config(old_gen)
            if old_config is None:
                print(f"gen-{current.gen} has no config.json. A fresh build would be performed.")
                return 0

            diff = ConfigDiff.compute(old_config, config)
            diff.print_summary()

            # Check for upgrades if requested
            if upgrade:
                upgrades = get_available_upgrades(old_gen)
                if upgrades:
                    print(f"\nPackage upgrades available ({len(upgrades)}):")
                    for line in upgrades:
                        print(f"  ^ {line}")
                else:
                    print("\nNo package upgrades available.")

            if not diff.has_changes() and not (upgrade and upgrades):
                print("\nAlready up to date. No build needed.")
            else:
                print(f"\nA new generation (gen-{current.gen + 1}) would be built.")

    return 0


def find_ovmf() -> tuple[Path, Path] | None:
    """Find OVMF firmware files for UEFI boot."""
    ovmf_paths = [
        ("/usr/share/edk2-ovmf/x64/OVMF_CODE.4m.fd", "/usr/share/edk2-ovmf/x64/OVMF_VARS.4m.fd"),
        ("/usr/share/edk2-ovmf/x64/OVMF_CODE.fd", "/usr/share/edk2-ovmf/x64/OVMF_VARS.fd"),
        ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
    ]
    for code_file, vars_file in ovmf_paths:
        if Path(code_file).exists() and Path(vars_file).exists():
            return Path(code_file), Path(vars_file)
    return None


def test_image(
    image_path: Path,
    memory: str,
    cpus: int,
    graphics: bool,
) -> int:
    """Boot an image in QEMU for testing."""
    if not image_path.exists():
        print(f"Error: Image file '{image_path}' not found")
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
    print(f"Starting QEMU with image: {image_path}")
    print(f"OVMF: {ovmf_code}")
    print(f"Mode: {'graphics' if graphics else 'serial console'}")

    # Create a temporary copy of OVMF_VARS (it's writable)
    vars_copy = tempfile.NamedTemporaryFile(delete=False)
    vars_copy.write(ovmf_vars.read_bytes())
    vars_copy.close()

    cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu", "host",
        "-m", memory,
        "-smp", str(cpus),
        "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
        "-drive", f"if=pflash,format=raw,file={vars_copy.name}",
        "-drive", f"file={image_path},format=raw",
        "-netdev", "user,id=net0",
        "-device", "virtio-net-pci,netdev=net0",
        "-usb",
        "-device", "usb-tablet",
    ]

    if graphics:
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

    def path_type(value: str | None) -> Path | None:
        if value is None:
            return None
        return Path(value)

    # apply command
    p_apply = subparsers.add_parser("apply", help="Apply configuration (auto-detects fresh vs incremental)")
    p_apply.add_argument("--config", default="./config.py", help="Path to config.py", type=path_type)
    p_apply.add_argument("--image", help="Path to disk image", type=path_type)
    p_apply.add_argument("--size", default="10G", help="Image size (default: 10G)")
    p_apply.add_argument("--btrfs", help="Btrfs device (e.g., /dev/nvme0n1p2)", type=path_type)
    p_apply.add_argument("--esp", help="ESP device (e.g., /dev/nvme0n1p1)", type=path_type)
    p_apply.add_argument("--upgrade", action="store_true", help="Also upgrade all packages (pacman -Syu)")
    p_apply.add_argument("--rebuild", action="store_true", help="Force fresh build even if generations exist")
    p_apply.add_argument("--switch", action="store_true", help="Switch to new generation after build (on darch systems)")

    # test command
    p_test = subparsers.add_parser("test", help="Boot an image in QEMU for testing")
    p_test.add_argument("image", help="Path to disk image", type=path_type)
    p_test.add_argument("--memory", default="4G", help="VM memory (default: 4G)")
    p_test.add_argument("--cpus", type=int, default=2, help="Number of CPUs (default: 2)")
    p_test.add_argument("--graphics", action="store_true", help="Enable graphical display (virtio-gpu)")

    # check command
    p_check = subparsers.add_parser("check", help="Check what would change without building (dry-run)")
    p_check.add_argument("--config", default="./config.py", help="Path to config.py", type=path_type)
    p_check.add_argument("--image", help="Path to disk image", type=path_type)
    p_check.add_argument("--btrfs", help="Btrfs device (e.g., /dev/nvme0n1p2)", type=path_type)
    p_check.add_argument("--esp", help="ESP device (e.g., /dev/nvme0n1p1)", type=path_type)
    p_check.add_argument("--upgrade", action="store_true", help="Also check for package upgrades")

    args = parser.parse_args()

    if args.command == "test":
        return test_image(
            image_path = args.image,
            memory = args.memory,
            cpus = args.cpus,
            graphics = args.graphics,
        )

    # Commands below require root
    if os.geteuid() != 0:
        print("Error: This command must be run as root")
        return 1

    if args.command == "apply":
        return apply_configuration(
            config_path = args.config,
            image_path = args.image,
            image_size = args.size,
            btrfs_dev = args.btrfs,
            esp_dev = args.esp,
            upgrade = args.upgrade,
            rebuild = args.rebuild,
            switch = args.switch,
        )

    if args.command == "check":
        return check_configuration(
            config_path = args.config,
            image_path = args.image,
            btrfs_dev = args.btrfs,
            esp_dev = args.esp,
            upgrade = args.upgrade,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
