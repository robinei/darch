# darch: Declarative Arch Linux Image Builder

## Overview

darch builds bootable Arch Linux disk images with:
- Declarative configuration in Python
- Immutable generations in btrfs subvolumes
- tmpfs root rebuilt fresh each boot
- Persistent /var and /home
- Incremental builds with config diffing

## Filesystem Layout

### At Runtime (booted system)

```
/                           tmpfs (ephemeral, rebuilt each boot)
├── current -> images/gen-N     symlink to active generation
├── usr -> current/usr
├── etc -> current/etc
├── bin -> usr/bin
├── lib -> usr/lib
├── sbin -> usr/bin
├── boot -> current/boot
├── root -> home/root           persistent root home
├── images/                     @images subvol (ro), contains generations
├── var/                        @var subvol (rw), persistent
├── home/                       @home subvol (rw), persistent
└── efi/                        ESP mount
```

### Generation Contents

Each generation (`/images/gen-N/`) contains:
```
gen-N/
├── usr/                    installed packages
├── etc/                    system configuration
├── boot/                   kernel + initramfs
├── pacman/                 full pacman state (local + sync DBs)
├── current -> .            self-reference for build-time compatibility
└── config.json             serialized Config for diffing
```

### Btrfs Subvolumes

```
@images     contains gen-1/, gen-2/, etc.
@var        persistent /var (logs, state, caches)
@home       persistent /home
```

## Key Mechanisms

### The /current Symlink Trick

Generations contain a `current -> .` symlink pointing to themselves. This enables a single symlink path to work in two contexts:

**At build time (chroot):** The generation is mounted at `/`, so `/current` resolves to `/./` = `/`. Paths like `/current/pacman` reach `/pacman` in the generation.

**At runtime:** The tmpfs root has `/current -> /images/gen-N`, shadowing the generation's self-referential symlink. The same paths now traverse through the tmpfs symlink to reach the generation.

### Pacman State in Generations

The entire `/var/lib/pacman` directory (both `local/` and `sync/`) lives in the generation as `/pacman`. This means:
- Each generation is a snapshot of repo state at build time
- No "reinstalling" confusion from stale databases
- Generations are more self-contained

The persistent @var has a symlink: `/var/lib/pacman -> ../../../current/pacman`

This symlink exits @var (3 levels up), reaches the tmpfs root, follows `/current` to the generation, and finds `/pacman`.

### Package Cache Sharing

During pacstrap, the host's `/var/cache/pacman/pkg` is bind-mounted into the generation. This means:
- On a regular host: packages cache to the host's /var
- On a darch system: packages cache to @var (since that's what /var is)
- Packages only download once across builds

### Incremental Builds

Builds compare the new config against the previous generation's `config.json`:
- Package additions/removals are applied with pacman
- File changes are written directly
- No changes = "Already up to date"

New generations are created as btrfs snapshots of the previous, making incremental builds fast.

### User Management

User files (`/etc/passwd`, `/etc/shadow`, etc.) are symlinks to `/var/lib/users/`. On first build, the files are copied to @var. This makes users persistent across generations without declaring them in config.

### Initramfs Hook

A custom mkinitcpio hook (`darch`) overrides the mount handler to:
1. Create tmpfs at new root
2. Mount @images (ro), @var, @home
3. Create symlinks to the generation specified by `darch.gen=N` kernel parameter
4. Hand off to systemd

## Build Modes

### Fresh Build (`--rebuild`)
1. Create new btrfs subvolume
2. Bind-mount package cache
3. Run pacstrap
4. Move /var/lib/pacman to /pacman
5. Create /current -> .
6. Remove /var from generation
7. Mount @var, create pacman symlink
8. Run chroot configuration
9. Save config.json

### Incremental Build
1. Snapshot previous generation
2. Mount @var (symlink already exists)
3. Apply package diff with pacman
4. Apply file changes
5. Regenerate initramfs if needed
6. Save config.json

## Configuration

`config.py` exports a `configure()` function returning a `Config` object:

```python
def configure() -> Config:
    config = Config(name="myvm")
    config.add_packages("htop", "vim")
    config.set_hostname("myvm")
    config.set_timezone("UTC")
    config.enable_service("sshd")
    return config
```

darch adds its own files (mkinitcpio.conf, initramfs hooks, fstab) before building.

## GRUB

GRUB config lists all generations with creation timestamps, newest first. Each entry loads the kernel directly from the btrfs subvolume with the generation number as a kernel parameter.
