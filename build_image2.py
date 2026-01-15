#!/usr/bin/env python3
"""
Build a minimal bootable Arch Linux disk image following the official installation guide.
This is a simplified version that creates a standard Arch installation with:
- EFI partition (FAT32) mounted at /boot
- Root filesystem (btrfs)
- GRUB bootloader
- Minimal base system

Once this boots successfully, we can add custom features.
"""

import os
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
    boot_mount = f"{mount_root}/boot"

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

        print("\n=== Mounting filesystems ===")
        os.makedirs(mount_root, exist_ok=True)
        run(["mount", root_part, mount_root])

        # Mount ESP at /boot (standard Arch setup)
        os.makedirs(boot_mount, exist_ok=True)
        run(["mount", esp_part, boot_mount])

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
        ]
        run(["pacstrap", "-K", mount_root] + base_packages)

        print("\n=== Generating fstab ===")
        # Get UUIDs of our partitions
        esp_uuid = run(["blkid", "-s", "UUID", "-o", "value", esp_part], capture_output=True)
        root_uuid = run(["blkid", "-s", "UUID", "-o", "value", root_part], capture_output=True)

        # Write fstab manually to avoid pollution from other mounted loop devices
        fstab = f"""# /etc/fstab: static file system information
#
# <file system>             <mount point>  <type>  <options>  <dump> <pass>

# Root filesystem
UUID={root_uuid}            /              btrfs   rw,relatime  0 1

# EFI System Partition
UUID={esp_uuid}             /boot          vfat    rw,relatime,fmask=0022,dmask=0022,codepage=437,iocharset=ascii,shortname=mixed,utf8,errors=remount-ro  0 2
"""

        with open(f"{mount_root}/etc/fstab", "w") as f:
            f.write(fstab)

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
# - filesystems: filesystem drivers (includes btrfs from MODULES)
# - fsck: filesystem check
HOOKS=(base udev autodetect microcode modconf block filesystems fsck)

# Use zstd compression (default for modern kernels)
COMPRESSION="zstd"
MKINITEOF

echo "=== Configuring GRUB for serial console ==="
cat >> /etc/default/grub <<'GRUBEOF'
GRUB_TERMINAL_INPUT="console serial"
GRUB_TERMINAL_OUTPUT="console serial"
GRUB_SERIAL_COMMAND="serial --unit=0 --speed=115200"
GRUB_CMDLINE_LINUX_DEFAULT="loglevel=7 console=tty0 console=ttyS0,115200"
GRUBEOF

echo "=== Regenerating initramfs ==="
mkinitcpio -P

echo "=== Installing GRUB to ESP ==="
grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB --removable

echo "=== Generating GRUB config ==="
grub-mkconfig -o /boot/grub/grub.cfg

echo "=== Enabling serial console for QEMU ==="
systemctl enable serial-getty@ttyS0.service

echo "=== Chroot configuration complete ==="
"""

        chroot_script_path = f"{mount_root}/root/setup.sh"
        with open(chroot_script_path, "w") as f:
            f.write(chroot_script)
        os.chmod(chroot_script_path, 0o755)

        # Execute the script in chroot
        run(["arch-chroot", mount_root, "/root/setup.sh"])

        print("\n=== Cleaning up mounts ===")
        run(["sync"])

        # Unmount boot first
        run(["umount", boot_mount], check=False)

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
