"""My Arch configuration."""
from darch import Config, User


def enable_sway(config: Config):
    """Adds sway and associated packages."""
    config.add_packages("sway", "foot", "mesa", "xorg-xwayland")
    config.enable_service("seatd")
    # Add seat group to user if configured
    if config.user:
        config.user.add_groups("seat")


def enable_fish(config: Config):
    """Enable the fish shell."""
    config.add_packages("fish")
    config.user.shell = "/usr/bin/fish"


def configure() -> Config:
    """Configuration entry point."""
    config = Config()

    # System settings
    config.set_hostname("archvm")
    config.set_timezone("UTC")
    config.set_locales("en_US.UTF-8", "nb_NO.UTF-8")
    config.set_keymap("no")

    # User
    config.user = User("robin",
        groups = {"wheel"},
        password_hash = "$6$bxSIgU/AEruP0HSu$UCk/mosb6FkwuJ556RZn.CHQy1Ys4cFmFVikf5a5QvTo4EO8HGXLFvRHLJdE.QMjFptVAqY/EzwVkYjA7vwwX1",
    )

    # Packages
    config.add_packages(
        "strace",
        "htop",
        "btop",
        "helix",
    )

    enable_sway(config)
    enable_fish(config)

    # Services
    config.enable_service("serial-getty@ttyS0")

    # Mask problematic services for darch
    config.mask_service("systemd-userdbd.service")
    config.mask_service("systemd-userdbd.socket")

    return config
