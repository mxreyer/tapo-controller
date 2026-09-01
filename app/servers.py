"""Configuration for plugs that feed a machine needing a graceful shutdown."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("tapo.servers")

DEFAULT_SERVERS_FILE = "/config/servers.json"


@dataclass
class ServerConfig:
    """A machine powered by one plug, shut down over SSH before the plug is cut."""

    ssh_host: str
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_key: str | None = None
    shutdown_command: str = "sudo /sbin/shutdown -h now"
    # How long to wait for the machine to disappear before giving up.
    wait_timeout: int = 300
    poll_interval: int = 5
    # Consecutive failed probes before we believe the machine is really down.
    down_confirmations: int = 3
    # Extra seconds after the machine stops answering, so the OS can finish
    # flushing disks and power off cleanly before we pull the plug.
    grace_seconds: int = 20
    # If the plug reports power draw, also require it to fall below this many
    # watts. A powered-off machine still draws a little for the PSU standby.
    power_threshold_w: float | None = None
    # Default is deliberately safe: on timeout, leave the plug ON rather than
    # risk cutting power to a machine that is still writing to disk.
    force_off_on_timeout: bool = False
    ssh_connect_timeout: int = 10
    ssh_options: list[str] = field(default_factory=list)
    # How long power stays off during a power cycle. Long enough that the PSU
    # drains and the board sees a genuine AC loss on the way back up, which is
    # what triggers "Restore on AC Power Loss = Power On" and boots the machine.
    power_cycle_seconds: int = 10

    @classmethod
    def from_dict(cls, host: str, raw: dict) -> "ServerConfig":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            log.warning("server config for %s: ignoring unknown keys %s", host, sorted(unknown))
        return cls(**{k: v for k, v in raw.items() if k in known})


def load_servers() -> dict[str, ServerConfig]:
    """Load plug-IP -> ServerConfig from $TAPO_SERVERS (JSON) or a JSON file."""
    raw_env = os.getenv("TAPO_SERVERS", "").strip()
    path = Path(os.getenv("TAPO_SERVERS_FILE", DEFAULT_SERVERS_FILE))

    if raw_env:
        source, blob = "$TAPO_SERVERS", raw_env
    elif path.is_file():
        source, blob = str(path), path.read_text()
    else:
        return {}

    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        log.error("could not parse server config from %s: %s", source, exc)
        return {}

    servers: dict[str, ServerConfig] = {}
    for plug_host, cfg in data.items():
        try:
            servers[plug_host] = ServerConfig.from_dict(plug_host, cfg)
        except TypeError as exc:
            log.error("invalid server config for plug %s: %s", plug_host, exc)
    log.info("loaded %d server plug(s) from %s", len(servers), source)
    return servers
