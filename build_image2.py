#!/usr/bin/env python3
"""
Build a bootable Arch Linux disk image with atomic/immutable root.

Architecture:
- Root is tmpfs (ephemeral, rebuilt each boot)
- /current symlink points to active generation
- /usr, /etc, /boot are symlinks to /current/...
- /images contains read-only generation subvolumes
- /var, /home are persistent btrfs subvolumes

Disk layout:
- ESP (FAT32) at /efi - contains GRUB only
- btrfs partition with subvolumes:
  - @images (read-only generations)
  - @images/gen-N (each generation)
  - @var (persistent)
  - @home (persistent)

Boot: GRUB loads kernel from btrfs, initramfs sets up tmpfs root.
"""

import os
import shutil
import subprocess
import sys
import time

def run(cmd, check=True, capture_output=False):
    """Run a command and optionally capture output."""
    print(f"Running: {' '.join(cmd)}")
    if capture_output:
        result = subprocess.run(cmd, check=check, capture_output=True, text=True)
        return result.stdout.strip()
    else:
        subprocess.run(cmd, check=check)

def main():
    if os.geteuid() != 0:
        print("Error: This script must be run as root")
        sys.exit(1)

    image_name = "arch-minimal.img"
    mount_root = "/mnt/arch-build"
    efi_mount = f"{mount_root}/efi"

    print("=== Cleaning up any previous build mounts ===")
    # Unmount any leftover mounts from previous builds
    run(["umount", "-R", mount_root], check=False)

    # Also detach any old loop devices associated with our image
    # to prevent fstab pollution
    if os.path.exists(image_name):
        # Find loop devices associated with this image
        try:
            loops = run(["losetup", "-j", image_name], capture_output=True, check=False)
            for line in loops.split('\n'):
                if line:
                    loop_dev = line.split(':')[0]
                    print(f"Detaching old loop device: {loop_dev}")
                    run(["losetup", "-d", loop_dev], check=False)
        except:
            pass  # No old loops to clean up

    print("=== Creating disk image ===")
    # Create a 10GB sparse file
    run(["truncate", "-s", "10G", image_name])

    print("\n=== Partitioning disk ===")
    # Create GPT partition table with:
    # - 512MB EFI System Partition (ESP)
    # - Rest for root filesystem
    run(["sgdisk", "-Z", image_name])  # Zap existing partition table
    run(["sgdisk", "-n", "1:0:+512M", "-t", "1:ef00", image_name])  # ESP
    run(["sgdisk", "-n", "2:0:0", "-t", "2:8300", image_name])       # Linux filesystem

    print("\n=== Setting up loop device ===")
    loop_device = run(["losetup", "-Pf", "--show", image_name], capture_output=True)
    print(f"Loop device: {loop_device}")

    try:
        # Give kernel time to create partition devices
        time.sleep(1)

        esp_part = f"{loop_device}p1"
        root_part = f"{loop_device}p2"

        print(f"\n=== Formatting partitions ===")
        print(f"ESP: {esp_part}")
        print(f"Root: {root_part}")

        # Format ESP as FAT32
        run(["mkfs.fat", "-F32", esp_part])

        # Format root as btrfs
        run(["mkfs.btrfs", "-f", root_part])

        print("\n=== Creating btrfs subvolumes ===")
        os.makedirs(mount_root, exist_ok=True)
        run(["mount", root_part, mount_root])

        # Create subvolume structure
        run(["btrfs", "subvol", "create", f"{mount_root}/@images"])
        run(["btrfs", "subvol", "create", f"{mount_root}/@var"])
        run(["btrfs", "subvol", "create", f"{mount_root}/@home"])
        run(["btrfs", "subvol", "create", f"{mount_root}/@images/gen-1"])

        # Create root's home directory on @home subvolume (700 - only root)
        os.makedirs(f"{mount_root}/@home/root", mode=0o700, exist_ok=True)

        # Create directories for user management files (will set permissions after pacstrap)
        os.makedirs(f"{mount_root}/@var/lib/users", exist_ok=True)
        os.makedirs(f"{mount_root}/@var/lib/machines", exist_ok=True)

        # Unmount btrfs root
        run(["umount", mount_root])

        print("\n=== Mounting filesystems ===")
        # Mount gen-1 subvolume as root for installation
        run(["mount", "-o", "subvol=@images/gen-1", root_part, mount_root])

        # Mount @var at /var so pacstrap populates it properly
        var_mount = f"{mount_root}/var"
        os.makedirs(var_mount, exist_ok=True)
        run(["mount", "-o", "subvol=@var", root_part, var_mount])

        # Mount ESP at /efi (kernel+initramfs stay in /boot on btrfs)
        os.makedirs(efi_mount, exist_ok=True)
        run(["mount", esp_part, efi_mount])

        print("\n=== Installing base system with pacstrap ===")
        # Install base packages (following installation guide)
        base_packages = [
            "base",
            "linux",
            #"linux-firmware",
            "btrfs-progs",
            "grub",
            "efibootmgr",
            "vim",
            "strace"
        ]
        run(["pacstrap", "-K", mount_root] + base_packages)

        print("\n=== Generating fstab ===")
        # Get UUIDs of our partitions
        esp_uuid = run(["blkid", "-s", "UUID", "-o", "value", esp_part], capture_output=True)
        root_uuid = run(["blkid", "-s", "UUID", "-o", "value", root_part], capture_output=True)

        # Write fstab - root and subvolumes are mounted by initramfs, only need /efi here
        fstab = f"""# /etc/fstab: static file system information
# Root is tmpfs, @images/@var/@home mounted by initramfs
#
# <file system>             <mount point>  <type>  <options>  <dump> <pass>

# EFI System Partition
UUID={esp_uuid}             /efi           vfat    rw,relatime,fmask=0022,dmask=0022,codepage=437,iocharset=ascii,shortname=mixed,utf8,errors=remount-ro  0 2
"""

        with open(f"{mount_root}/etc/fstab", "w") as f:
            f.write(fstab)

        print("\n=== Installing custom darch initcpio hook ===")
        # Create initcpio directories
        hooks_dir = f"{mount_root}/usr/lib/initcpio"
        os.makedirs(f"{hooks_dir}/hooks", exist_ok=True)
        os.makedirs(f"{hooks_dir}/install", exist_ok=True)

        # Runtime hook (runs during boot)
        hook_runtime = r'''#!/usr/bin/ash
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

        # Install hook (determines what goes into initramfs)
        hook_install = r'''#!/usr/bin/bash
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

        with open(f"{hooks_dir}/hooks/darch", "w") as f:
            f.write(hook_runtime)
        os.chmod(f"{hooks_dir}/hooks/darch", 0o755)

        with open(f"{hooks_dir}/install/darch", "w") as f:
            f.write(hook_install)
        os.chmod(f"{hooks_dir}/install/darch", 0o755)

        print("Installed darch hook to initcpio")

        print("\n=== Configuring system (in chroot) ===")

        # Create a script to run inside chroot
        # NOTE: Can't use /tmp because arch-chroot mounts a separate tmpfs over it
        # Use /root instead which is part of the actual filesystem
        chroot_script = f"""#!/bin/bash
set -e

echo "=== Setting timezone ==="
ln -sf /usr/share/zoneinfo/UTC /etc/localtime
hwclock --systohc

echo "=== Setting up locale ==="
echo "en_US.UTF-8 UTF-8" > /etc/locale.gen
locale-gen
echo "LANG=en_US.UTF-8" > /etc/locale.conf

echo "=== Setting hostname ==="
echo "archvm" > /etc/hostname

echo "=== Creating /etc/hosts ==="
cat > /etc/hosts <<EOF
127.0.0.1   localhost
::1         localhost
127.0.1.1   archvm.localdomain archvm
EOF

echo "=== Setting root password (empty for testing) ==="
passwd -d root

echo "=== Creating /etc/vconsole.conf ==="
echo "KEYMAP=us" > /etc/vconsole.conf

echo "=== Writing /etc/mkinitcpio.conf ==="
cat > /etc/mkinitcpio.conf <<'MKINITEOF'
# mkinitcpio configuration
# MODULES: explicitly include btrfs and disk drivers for QEMU
# ata_piix: Intel PIIX/ICH PATA/SATA controller (QEMU default IDE)
# ahci: AHCI SATA controller
# sd_mod: SCSI disk support (required for SATA)
# virtio_blk: virtio block device driver (for virtio QEMU disks)
# virtio_pci: virtio PCI bus driver (required for virtio devices)
MODULES=(btrfs ata_piix ahci sd_mod virtio_blk virtio_pci)

BINARIES=()
FILES=()

# HOOKS: using traditional busybox-based initramfs (not systemd)
# - base: basic initramfs structure with busybox
# - udev: device manager for auto-loading modules
# - autodetect: detect needed modules (but we force btrfs above)
# - microcode: CPU microcode
# - modconf: load modules from /etc/modprobe.d
# - block: block device support
# - darch: our custom hook (currently just prints a message)
# - filesystems: filesystem drivers (includes btrfs from MODULES)
# - fsck: filesystem check
HOOKS=(base udev autodetect microcode modconf block darch filesystems fsck)

# Use zstd compression (default for modern kernels)
COMPRESSION="zstd"
MKINITEOF

echo "=== Regenerating initramfs ==="
mkinitcpio -P

echo "=== Installing GRUB to ESP ==="
grub-install --target=x86_64-efi --efi-directory=/efi --boot-directory=/efi --bootloader-id=GRUB --removable

echo "=== Enabling serial console for QEMU ==="
systemctl enable serial-getty@ttyS0.service

echo "=== Masking services incompatible with read-only /etc ==="
# userdb needs writable /etc/userdb, we use traditional passwd files
systemctl mask systemd-userdbd.service systemd-userdbd.socket

echo "=== Creating tmpfiles overrides for darch layout ==="
mkdir -p /etc/tmpfiles.d

# Override etc.conf: change L+ (force) to L (create only if missing) for mtab
# Since we already create /etc/mtab during build, this becomes a no-op
sed 's|^L+ /etc/mtab|L /etc/mtab|' /usr/lib/tmpfiles.d/etc.conf > /etc/tmpfiles.d/etc.conf

# Override provision.conf: remove /root entries since we use a symlink to /home/root
grep -v '^[df].*[[:space:]]/root' /usr/lib/tmpfiles.d/provision.conf > /etc/tmpfiles.d/provision.conf

echo "=== Chroot configuration complete ==="
"""

        chroot_script_path = f"{mount_root}/root/setup.sh"
        with open(chroot_script_path, "w") as f:
            f.write(chroot_script)
        os.chmod(chroot_script_path, 0o755)

        # Execute the script in chroot
        run(["arch-chroot", mount_root, "/root/setup.sh"])

        print("\n=== Setting up persistent /etc files ===")
        # @var is already mounted at /var, so access it directly
        var_path = f"{mount_root}/var"

        # Fix directory permissions (pacstrap/systemd may have changed them)
        # These need to be traversable by non-root users
        os.chmod(f"{var_path}/lib/users", 0o755)
        os.chmod(f"{var_path}/lib/machines", 0o755)

        # Move user management files to @var and create symlinks
        user_files = ["passwd", "shadow", "group", "gshadow"]
        for f in user_files:
            src = f"{mount_root}/etc/{f}"
            dst = f"{var_path}/lib/users/{f}"
            if os.path.exists(src):
                # Copy to @var (preserving permissions)
                shutil.copy2(src, dst)
                # Replace with symlink
                os.remove(src)
                os.symlink(f"/var/lib/users/{f}", src)

        # machine-id: leave the one generated during pacstrap in place
        # It's read-only in the generation, which is fine - machine-id shouldn't change
        # (If we want consistent machine-id across generations, we can copy it during rebuild)

        # resolv.conf: symlink to /run (systemd-resolved or NetworkManager will manage)
        resolv_path = f"{mount_root}/etc/resolv.conf"
        if os.path.exists(resolv_path) or os.path.islink(resolv_path):
            os.remove(resolv_path)
        os.symlink("/run/systemd/resolve/stub-resolv.conf", resolv_path)

        # mtab: symlink to /proc/mounts (standard)
        mtab_path = f"{mount_root}/etc/mtab"
        if os.path.exists(mtab_path) or os.path.islink(mtab_path):
            os.remove(mtab_path)
        os.symlink("/proc/mounts", mtab_path)

        print("\n=== Writing custom GRUB config ===")
        # Write GRUB config that loads kernel directly from btrfs
        grub_cfg = f"""# Custom GRUB config for darch
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

menuentry "Arch Linux (gen-1)" {{
    linux /@images/gen-1/boot/vmlinuz-linux \\
        root=UUID={root_uuid} \\
        darch.gen=1 \\
        console=tty0 console=ttyS0,115200 \\
        systemd.gpt_auto=0 rw
    initrd /@images/gen-1/boot/initramfs-linux.img
}}

menuentry "Arch Linux (gen-1) - Rescue" {{
    linux /@images/gen-1/boot/vmlinuz-linux \\
        root=UUID={root_uuid} \\
        darch.gen=1 \\
        console=tty0 console=ttyS0,115200 \\
        systemd.gpt_auto=0 systemd.unit=rescue.target rw
    initrd /@images/gen-1/boot/initramfs-linux.img
}}
"""
        grub_cfg_path = f"{efi_mount}/grub/grub.cfg"
        os.makedirs(os.path.dirname(grub_cfg_path), exist_ok=True)
        with open(grub_cfg_path, "w") as f:
            f.write(grub_cfg)
        print(f"Wrote GRUB config to {grub_cfg_path}")

        print("\n=== Cleaning up mounts ===")
        run(["sync"])

        # Unmount in reverse order: efi, var, root
        run(["umount", efi_mount], check=False)
        run(["umount", f"{mount_root}/var"], check=False)

        # Wait a moment and use lazy unmount for root if needed
        time.sleep(0.5)
        # Try normal unmount first
        result = subprocess.run(["umount", mount_root], capture_output=True)
        if result.returncode != 0:
            print("Normal unmount failed, trying lazy unmount...")
            run(["umount", "-l", mount_root])

    finally:
        print("\n=== Detaching loop device ===")
        run(["losetup", "-d", loop_device], check=False)

    print(f"\n=== SUCCESS ===")
    print(f"Image created: {image_name}")
    print(f"\nTo test with QEMU:")
    print(f"  qemu-system-x86_64 \\")
    print(f"    -enable-kvm \\")
    print(f"    -m 2G \\")
    print(f"    -drive file={image_name},format=raw \\")
    print(f"    -bios /usr/share/ovmf/x64/OVMF.fd \\")
    print(f"    -nographic")
    print(f"\nOr save console output:")
    print(f"  qemu-system-x86_64 \\")
    print(f"    -enable-kvm \\")
    print(f"    -m 2G \\")
    print(f"    -drive file={image_name},format=raw \\")
    print(f"    -bios /usr/share/ovmf/x64/OVMF.fd \\")
    print(f"    -nographic \\")
    print(f"    -serial file:qemu-console2.log")

if __name__ == "__main__":
    main()
