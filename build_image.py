#!/usr/bin/env python3
"""
Simple arch-atomic image builder
Creates a bootable disk image with:
- EFI System Partition (ESP)
- btrfs partition with subvolumes
- Minimal Arch Linux installation
"""

import os
import sys
import subprocess
import json
from pathlib import Path

def run(cmd, **kwargs):
    """Run command and check for errors"""
    print(f"→ {' '.join(cmd)}")
    result = subprocess.run(cmd, check=True, **kwargs)
    return result

def run_output(cmd):
    """Run command and return output"""
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout

def create_disk_image(output: str, size: int = 10):
    """Create a disk image with ESP and btrfs partitions"""
    print(f"Creating {size}GB disk image at {output}")

    # Create empty image
    run(["truncate", "-s", f"{size}G", output])

    # Partition with sgdisk
    # Partition 1: 512MB ESP (EFI System Partition)
    # Partition 2: Rest for btrfs
    run([
        "sgdisk", output,
        "-n", "1:0:+512M", "-t", "1:ef00",  # ESP
        "-n", "2:0:0", "-t", "2:8300"        # Linux filesystem
    ])

    # Setup loop device
    loop = run_output(["losetup", "-Pf", "--show", output]).strip()
    print(f"Created loop device: {loop}")

    # Wait for partition devices to appear
    import time
    run(["partprobe", loop])
    time.sleep(0.5)  # Give kernel time to create device nodes

    return loop, f"{loop}p1", f"{loop}p2"


def setup_btrfs_subvolumes(btrfs_part: str, mount_point: str):
    """Create btrfs subvolume structure"""
    print("Setting up btrfs subvolumes...")

    # Mount btrfs root
    os.makedirs(mount_point, exist_ok=True)
    run(["mount", btrfs_part, mount_point])

    # Create subvolumes
    run(["btrfs", "subvol", "create", f"{mount_point}/@images"])
    run(["btrfs", "subvol", "create", f"{mount_point}/@var"])
    run(["btrfs", "subvol", "create", f"{mount_point}/@home"])

    # Create first generation
    run(["btrfs", "subvol", "create", f"{mount_point}/@images/gen-1"])

    # Unmount
    run(["umount", mount_point])


def install_base_system(btrfs_part: str, mount_point: str):
    """Install minimal Arch Linux base system"""
    print("Installing base system...")

    # Get btrfs UUID for fstab
    btrfs_uuid = run_output(["blkid", "-s", "UUID", "-o", "value", btrfs_part]).strip()

    # Mount generation for installation
    os.makedirs(mount_point, exist_ok=True)
    run(["mount", "-o", "subvol=@images/gen-1", btrfs_part, mount_point])

    # Pacstrap minimal base
    packages = ["base", "linux", "btrfs-progs", "grub", "efibootmgr"]
    run(["pacstrap", "-K", mount_point] + packages)

    # Install custom darch initcpio hooks
    print("Installing custom darch initcpio hooks...")
    hooks_dir = f"{mount_point}/usr/lib/initcpio"
    os.makedirs(f"{hooks_dir}/hooks", exist_ok=True)
    os.makedirs(f"{hooks_dir}/install", exist_ok=True)

    # Copy hook files from current directory
    import shutil
    shutil.copy("hook-darch", f"{hooks_dir}/hooks/darch")
    shutil.copy("install-darch", f"{hooks_dir}/install/darch")

    # Make them executable
    os.chmod(f"{hooks_dir}/hooks/darch", 0o755)
    os.chmod(f"{hooks_dir}/install/darch", 0o755)

    # Write custom mkinitcpio.conf with busybox hooks and our darch hook
    print("Writing custom mkinitcpio.conf...")
    mkinitcpio_conf = """# darch (arch-atomic) mkinitcpio configuration
# Use busybox-based hooks for simplicity and transparency

# Modules to include (btrfs is essential for our setup)
MODULES=(btrfs)

# Binaries (empty, our hook adds what's needed)
BINARIES=()

# Files (empty)
FILES=()

# Hooks - order matters!
# base: Essential busybox utilities
# udev: Device management
# autodetect: Only include modules for current hardware
# modconf: Module configuration from /etc/modprobe.d
# block: Block device support
# darch: Our custom hook to setup tmpfs root
# fsck: Filesystem check (though we use ro mounts)
HOOKS=(base udev autodetect modconf block darch fsck)

# Compression
COMPRESSION="zstd"
"""

    with open(f"{mount_point}/etc/mkinitcpio.conf", "w") as f:
        f.write(mkinitcpio_conf)

    # Create vconsole.conf to prevent mkinitcpio errors
    with open(f"{mount_point}/etc/vconsole.conf", "w") as f:
        f.write("KEYMAP=us\n")

    # Create minimal fstab (empty - kernel cmdline handles root mount)
    # fstab only needed for additional mounts like @var, @home in final design
    fstab = """# Generated fstab for arch-atomic gen-1
# Root is mounted via kernel cmdline
"""
    with open(f"{mount_point}/etc/fstab", "w") as f:
        f.write(fstab)

    # Set locale
    with open(f"{mount_point}/etc/locale.gen", "a") as f:
        f.write("en_US.UTF-8 UTF-8\n")

    with open(f"{mount_point}/etc/locale.conf", "w") as f:
        f.write("LANG=en_US.UTF-8\n")

    # Set hostname
    with open(f"{mount_point}/etc/hostname", "w") as f:
        f.write("arch-atomic\n")

    # Return UUID for bootloader config, keep mounted for chroot
    return btrfs_uuid


def configure_and_install_bootloader(esp_part: str, btrfs_uuid: str, mount_root: str):
    """Configure system and install GRUB bootloader in single chroot"""
    print("Configuring system and installing bootloader...")

    # Mount ESP
    esp_mount = f"{mount_root}/efi"
    os.makedirs(esp_mount, exist_ok=True)
    run(["mount", esp_part, esp_mount])

    # Run all setup commands in single chroot
    setup_commands = " && ".join([
        "locale-gen",
        "ln -sf /usr/share/zoneinfo/UTC /etc/localtime",
        "hwclock --systohc",
        "passwd -d root",
        "mkinitcpio -P -v",  # Rebuild initramfs with our custom darch hook (verbose)
        "systemctl enable serial-getty@ttyS0.service",
        "grub-install --target=x86_64-efi --efi-directory=/efi --boot-directory=/efi --removable"
    ])

    run(["arch-chroot", mount_root, "/bin/bash", "-c", setup_commands])

    # Ensure all writes are flushed
    run(["sync"])

    # Create GRUB config on ESP at /efi/grub/grub.cfg
    grub_cfg = f"""# Simple arch-atomic boot config
set timeout=5
set default=0

serial --unit=0 --speed=115200
terminal_input serial console
terminal_output serial console

insmod btrfs
search --set=root --fs-uuid {btrfs_uuid}

menuentry "Arch Linux (gen-1)" {{
    linux /@images/gen-1/boot/vmlinuz-linux root=UUID={btrfs_uuid} rootflags=subvol=@images/gen-1 rw console=ttyS0,115200 console=tty0 loglevel=7 systemd.log_level=debug rd.debug
    initrd /@images/gen-1/boot/initramfs-linux.img
}}
"""

    grub_cfg_path = f"{esp_mount}/grub/grub.cfg"
    os.makedirs(os.path.dirname(grub_cfg_path), exist_ok=True)
    with open(grub_cfg_path, "w") as f:
        f.write(grub_cfg)

    # Unmount with retry logic
    run(["sync"])
    run(["umount", esp_mount])

    # Use lazy unmount for the root in case something is still holding it
    import time
    time.sleep(0.5)  # Give processes time to exit
    try:
        run(["umount", mount_root])
    except subprocess.CalledProcessError:
        # Try lazy unmount as fallback
        print("  Normal unmount failed, trying lazy unmount...")
        run(["umount", "-l", mount_root])


def cleanup_loop(loop: str):
    """Detach loop device"""
    print(f"Cleaning up loop device {loop}")
    run(["losetup", "-d", loop])


def main():
    import sys

    if os.geteuid() != 0:
        print("Error: This script must be run as root")
        sys.exit(1)

    output = "arch-test.img"
    mount_root = "/mnt/arch-build"

    print("=== Building Arch Linux test image ===")
    print(f"Output: {output}")

    loop = None
    try:
        # Create and partition disk
        loop, esp_part, btrfs_part = create_disk_image(output, size=10)

        # Format partitions
        print("Formatting partitions...")
        run(["mkfs.fat", "-F32", esp_part])
        run(["mkfs.btrfs", "-f", btrfs_part])

        # Setup btrfs subvolumes
        setup_btrfs_subvolumes(btrfs_part, mount_root)

        # Install base system and get UUID
        btrfs_uuid = install_base_system(btrfs_part, mount_root)

        # Configure and install bootloader
        configure_and_install_bootloader(esp_part, btrfs_uuid, mount_root)

        print("\n=== Build complete! ===")
        print(f"Image: {output}")
        print(f"To test with QEMU, run:")
        print(f"  ./test_qemu.sh {output}")

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # Always cleanup loop device
        if loop:
            try:
                cleanup_loop(loop)
            except Exception as e:
                print(f"Warning: Failed to cleanup loop device: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
