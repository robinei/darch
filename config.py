"""My Arch configuration."""
from darch import Config, User


def enable_sway(config: Config, user: User):
    """Adds sway and associated packages."""
    config.add_packages("sway", "foot", "mesa", "xorg-xwayland")
    config.enable_service("seatd")
    user.add_groups("seat")


def enable_fish(config: Config, user: User):
    """Enable the fish shell."""
    config.add_packages("fish")
    user.shell = "/usr/bin/fish"


def enable_sudo(config: Config, *users: User):
    """Enable sudo for the given users via the wheel group."""
    config.add_packages("sudo")
    config.add_file("/etc/sudoers.d/wheel", "%wheel ALL=(ALL:ALL) ALL\n", mode=0o440)
    for user in users:
        user.add_groups("wheel")


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
        "linux",
        "strace",
        "htop",
        "btop",
        "helix",
    )

    config.enable_qemu_testing()
    enable_sudo(config, user)
    enable_sway(config, user)
    enable_fish(config, user)

    # Services
    config.enable_service("serial-getty@ttyS0")

    # Mask problematic services for darch
    config.mask_service("systemd-userdbd.service")
    config.mask_service("systemd-userdbd.socket")

    return config
