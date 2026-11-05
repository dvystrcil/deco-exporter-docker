"""Exporter configuration, from environment variables only.

The password comes from DECO_PASSWORD, which the Deployment fills from a Secret
materialised by an InfisicalSecret. It is never a CLI argument: argv is
readable by any process on the node through /proc/<pid>/cmdline.
"""

from dataclasses import dataclass, field

# AC8 (homelab#1931): admin/device?form=device_list is the call reported to
# briefly interrupt wireless backhaul on some Deco firmware. Even when it is
# turned on, it may only run every few minutes.
MIN_MESH_INTERVAL = 300
MIN_POLL_INTERVAL = 15


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    host: str
    password: str = field(repr=False)
    username: str
    poll_interval: int
    timeout: int
    mesh_enabled: bool
    mesh_interval: int
    port: int


def _int(env, key, default):
    raw = env.get(key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None


def _bool(env, key):
    return env.get(key, "").strip().lower() in ("1", "true", "yes", "on")


def load_config(env):
    password = env.get("DECO_PASSWORD", "")
    if not password:
        raise ConfigError("DECO_PASSWORD is required (mount it from a Secret, never argv)")

    poll = _int(env, "DECO_POLL_INTERVAL_SECONDS", 60)
    if poll < MIN_POLL_INTERVAL:
        raise ConfigError(f"DECO_POLL_INTERVAL_SECONDS must be >= {MIN_POLL_INTERVAL}, got {poll}")

    mesh_interval = _int(env, "DECO_MESH_INTERVAL_SECONDS", 600)
    if mesh_interval < MIN_MESH_INTERVAL:
        raise ConfigError(
            f"DECO_MESH_INTERVAL_SECONDS must be >= {MIN_MESH_INTERVAL}, got {mesh_interval}: "
            "device_list is the call reported to interrupt wireless backhaul")

    return Config(
        host=env.get("DECO_HOST", "http://192.168.86.1"),
        password=password,
        username=env.get("DECO_USERNAME", "admin"),
        poll_interval=poll,
        timeout=_int(env, "DECO_TIMEOUT_SECONDS", 10),
        mesh_enabled=_bool(env, "DECO_MESH_STATS_ENABLED"),
        mesh_interval=mesh_interval,
        port=_int(env, "LISTEN_PORT", 9850),
    )
