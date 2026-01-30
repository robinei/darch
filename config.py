"""My Arch configuration."""

# pylint: disable=line-too-long,too-many-lines

from pathlib import Path
from darch import Config, User

def configure() -> Config:
    """Configuration entry point."""
    config = Config()

    # System settings
    config.set_hostname("archvm")
    config.set_timezone("Europe/Oslo")
    config.set_locales("en_US.UTF-8", "nb_NO.UTF-8")
    config.set_keymap("no")

    # Users
    password_hash="$6$bxSIgU/AEruP0HSu$UCk/mosb6FkwuJ556RZn.CHQy1Ys4cFmFVikf5a5QvTo4EO8HGXLFvRHLJdE.QMjFptVAqY/EzwVkYjA7vwwX1"
    user = User("robin", password_hash=password_hash)
    root = User("root", uid=0, password_hash=password_hash)
    config.users = [user, root]

    # Packages
    config.add_packages(
        "base-devel",
        "linux",
        "strace",
        "htop",
        "btop",
        "less",
    )

    config.enable_qemu_testing()
    enable_sudo(config, [user])
    enable_sway(config, [user])
    enable_fish(config, [user])
    enable_helix(config)
    enable_network(config)
    copy_darch_files([user, root])

    # Services
    config.enable_service("serial-getty@ttyS0")

    # Mask problematic services for darch
    config.mask_service("systemd-userdbd.service")
    config.mask_service("systemd-userdbd.socket")

    return config


def enable_helix(config: Config):
    """Enable the Helix editor."""
    config.add_packages("helix")
    config.add_symlink("/usr/local/bin/hx", "/usr/bin/helix")


def enable_sway(config: Config, users: list[User]):
    """Adds sway and associated packages."""
    config.add_packages("sway", "foot", "mesa", "xorg-xwayland")
    config.enable_service("seatd")
    for user in users:
        user.add_groups("seat")
        user.add_file("~/.config/sway/config", """\
set $mod Mod4
set $term foot
bindsym $mod+Return exec $term
bindsym $mod+Shift+q kill
bindsym $mod+d exec wmenu-run
bindsym $mod+Shift+e exec swaynag -t warning -m 'Exit sway?' -B 'Yes' 'swaymsg exit'
""")


def enable_fish(config: Config, users: list[User]):
    """Enable the fish shell."""
    config.add_packages("fish", "pkgfile")
    for user in users:
        user.shell = "/usr/bin/fish"


def enable_sudo(config: Config, users: list[User]):
    """Enable sudo for the given users via the wheel group."""
    config.add_packages("sudo")
    config.add_file("/etc/sudoers.d/wheel", "%wheel ALL=(ALL:ALL) ALL\n", mode=0o440)
    for user in users:
        user.add_groups("wheel")


def enable_network(config: Config):
    """Enable systemd-networkd with DHCP for all ethernet interfaces."""
    config.enable_service("systemd-networkd")
    config.enable_service("systemd-resolved")
    config.add_file("/etc/systemd/network/20-wired.network", """\
[Match]
Type=ether

[Network]
DHCP=yes
""")


def copy_darch_files(users: list[User], darch_dir: Path = Path(__file__).parent):
    """Copy darch .py files to users' home directories for in-image testing."""
    for py_file in darch_dir.glob("*.py"):
        content = py_file.read_text()
        for user in users:
            user.add_file(f"~/darch/{py_file.name}", content)
