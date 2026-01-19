from darch import Config


def configure() -> Config:
    config = Config(name="archvm")

    # Packages
    config.add_packages("strace", "htop", "helix", "btop")

    # System settings
    config.set_hostname("archvm")
    config.set_timezone("UTC")
    config.set_locale("en_US.UTF-8")
    config.set_keymap("us")

    # Services
    config.enable_service("serial-getty@ttyS0")

    # Mask problematic services for darch
    config.mask_service("systemd-userdbd.service")
    config.mask_service("systemd-userdbd.socket")

    return config
