"""Thin async wrapper around python-kasa for a handful of Tapo plugs."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from kasa import Credentials, Device, Discover, Module

from .servers import ServerConfig, load_servers

log = logging.getLogger("tapo.plugs")


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


@dataclass
class Job:
    """A long-running action on a plug (currently: graceful server shutdown)."""

    action: str
    message: str
    started_at: float = field(default_factory=time.monotonic)
    failed: bool = False
    task: asyncio.Task | None = None

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "message": self.message,
            "failed": self.failed,
            "elapsed": round(time.monotonic() - self.started_at),
            "running": self.task is not None and not self.task.done(),
        }


async def _port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """True if we can complete a TCP handshake — our proxy for 'still alive'."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


class PlugManager:
    """Keeps one long-lived connection per plug, so toggles don't re-handshake."""

    def __init__(self) -> None:
        self.username = os.getenv("TAPO_USERNAME", "")
        self.password = os.getenv("TAPO_PASSWORD", "")
        self.discovery = os.getenv("TAPO_DISCOVERY", "true").lower() != "false"
        self.servers: dict[str, ServerConfig] = load_servers()
        self.hosts = _env_list("TAPO_HOSTS")
        # A plug listed as a server is implicitly a plug we manage.
        for host in self.servers:
            if host not in self.hosts:
                self.hosts.append(host)
        self._devices: dict[str, Device] = {}
        self._jobs: dict[str, Job] = {}

    # ---------------------------------------------------------------- devices

    @property
    def _credentials(self) -> Credentials:
        return Credentials(self.username, self.password)

    async def _connect(self, host: str) -> Device:
        dev = await Discover.discover_single(host, credentials=self._credentials)
        if dev is None:
            raise ConnectionError(f"no Tapo device answered at {host}")
        await dev.update()
        return dev

    async def _device(self, host: str) -> Device:
        dev = self._devices.get(host)
        if dev is None:
            dev = await self._connect(host)
            self._devices[host] = dev
        return dev

    async def discover(self) -> list[str]:
        """Broadcast-discover plugs on the LAN and remember any new ones."""
        if not self.discovery:
            return []
        found = await Discover.discover(credentials=self._credentials, discovery_timeout=5)
        new = []
        for host, dev in found.items():
            if host in self.hosts:
                continue
            try:
                await dev.update()
            except Exception as exc:  # pragma: no cover - network dependent
                log.warning("discovered %s but could not query it: %s", host, exc)
                continue
            self.hosts.append(host)
            self._devices[host] = dev
            new.append(host)
        return new

    # ---------------------------------------------------------------- status

    async def _status(self, host: str) -> dict:
        job = self._jobs.get(host)
        try:
            dev = await self._device(host)
            await dev.update()
        except Exception as exc:
            # Drop the cached connection so the next poll reconnects cleanly.
            self._devices.pop(host, None)
            log.warning("could not reach %s: %s", host, exc)
            return {
                "host": host,
                "online": False,
                "error": str(exc),
                "is_server": host in self.servers,
                "job": job.as_dict() if job else None,
            }

        entry = {
            "host": host,
            "online": True,
            "name": dev.alias or host,
            "model": dev.model,
            "is_on": bool(dev.is_on),
            "power_w": None,
            "today_kwh": None,
            "is_server": host in self.servers,
            "job": job.as_dict() if job else None,
        }
        energy = dev.modules.get(Module.Energy)
        if energy is not None:
            entry["power_w"] = energy.current_consumption
            entry["today_kwh"] = energy.consumption_today
        if host in self.servers:
            entry["ssh_host"] = self.servers[host].ssh_host
        return entry

    async def status(self, host: str) -> dict:
        return await self._status(host)

    async def statuses(self) -> list[dict]:
        results = await asyncio.gather(*(self._status(h) for h in self.hosts))
        return sorted(results, key=lambda d: (not d["online"], d.get("name", d["host"])))

    async def _power_w(self, host: str) -> float | None:
        try:
            dev = await self._device(host)
            await dev.update()
        except Exception:
            return None
        energy = dev.modules.get(Module.Energy)
        return energy.current_consumption if energy else None

    # ------------------------------------------------------------ switching

    async def _switch(self, host: str, on: bool) -> None:
        """Flip the relay, retrying once on a fresh connection."""
        dev = await self._device(host)
        try:
            await (dev.turn_on() if on else dev.turn_off())
        except Exception:
            # Sessions expire after idle periods; reconnect and try again.
            self._devices.pop(host, None)
            dev = await self._device(host)
            await (dev.turn_on() if on else dev.turn_off())

    async def set_state(self, host: str, on: bool, force: bool = False) -> dict:
        """Turn a plug on/off. Server plugs get a graceful shutdown first."""
        if host not in self.hosts:
            raise KeyError(host)

        if on:
            # Turning on cancels any shutdown in flight, then powers the plug.
            self.cancel_job(host)
            self._jobs.pop(host, None)
            await self._switch(host, True)
            return await self._status(host)

        cfg = self.servers.get(host)
        if cfg and not force:
            return await self.begin_shutdown(host, cfg)

        self._jobs.pop(host, None)
        await self._switch(host, False)
        return await self._status(host)

    async def toggle(self, host: str) -> dict:
        job = self._jobs.get(host)
        if job and job.task and not job.task.done():
            # A tap during a pending shutdown means "never mind".
            self.cancel_job(host)
            self._jobs.pop(host, None)
            return await self._status(host)

        status = await self._status(host)
        if not status["online"]:
            return status
        return await self.set_state(host, not status["is_on"])

    # ------------------------------------------------- graceful server power

    def cancel_job(self, host: str) -> bool:
        job = self._jobs.get(host)
        if job and job.task and not job.task.done():
            job.task.cancel()
            log.info("cancelled %s job for %s", job.action, host)
            return True
        return False

    async def begin_shutdown(self, host: str, cfg: ServerConfig) -> dict:
        """Kick off shutdown-then-cut-power in the background, return at once."""
        existing = self._jobs.get(host)
        if existing and existing.task and not existing.task.done():
            return await self._status(host)

        job = Job(action="shutdown", message=f"Sending shutdown to {cfg.ssh_host}…")
        self._jobs[host] = job
        job.task = asyncio.create_task(self._shutdown_then_off(host, cfg, job))
        return await self._status(host)

    async def _run_ssh(self, cfg: ServerConfig) -> tuple[int, str]:
        args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={cfg.ssh_connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(cfg.ssh_port),
        ]
        known_hosts = os.getenv("SSH_KNOWN_HOSTS")
        if known_hosts:
            args += ["-o", f"UserKnownHostsFile={known_hosts}"]
        if cfg.ssh_key:
            args += ["-i", cfg.ssh_key]
        for opt in cfg.ssh_options:
            args += ["-o", opt]
        args += [f"{cfg.ssh_user}@{cfg.ssh_host}", cfg.shutdown_command]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=cfg.ssh_connect_timeout + 20
            )
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "ssh timed out"
        return proc.returncode or 0, stderr.decode(errors="replace").strip()

    async def _wait_until_down(self, host: str, cfg: ServerConfig, job: Job, deadline: float) -> bool:
        """Poll until the machine stops answering on its SSH port (and, if the
        plug meters power, until draw drops below the configured threshold)."""
        confirmations = 0
        while time.monotonic() < deadline:
            alive = await _port_open(cfg.ssh_host, cfg.ssh_port)
            if alive:
                confirmations = 0
            else:
                confirmations += 1
                if confirmations >= cfg.down_confirmations:
                    if cfg.power_threshold_w is not None:
                        watts = await self._power_w(host)
                        if watts is not None and watts > cfg.power_threshold_w:
                            job.message = (
                                f"{cfg.ssh_host} is unreachable but still drawing "
                                f"{watts:.1f} W — waiting…"
                            )
                            confirmations = 0
                            await asyncio.sleep(cfg.poll_interval)
                            continue
                    return True

            remaining = max(0, round(deadline - time.monotonic()))
            job.message = (
                f"Waiting for {cfg.ssh_host} to power down… ({remaining}s left)"
                if alive
                else f"{cfg.ssh_host} stopped responding, confirming… "
                     f"({confirmations}/{cfg.down_confirmations})"
            )
            await asyncio.sleep(cfg.poll_interval)
        return False

    async def _shutdown_then_off(self, host: str, cfg: ServerConfig, job: Job) -> None:
        try:
            rc, stderr = await self._run_ssh(cfg)
            # A dropped connection mid-shutdown is normal, so a non-zero exit is
            # not fatal on its own — whether the machine goes down is the real
            # test. But shorten the wait so an auth failure surfaces quickly.
            timeout = cfg.wait_timeout
            if rc != 0:
                timeout = min(cfg.wait_timeout, 45)
                log.warning("ssh to %s exited %s: %s", cfg.ssh_host, rc, stderr)
                job.message = f"ssh exited {rc}; checking whether it went down anyway…"

            deadline = time.monotonic() + timeout
            went_down = await self._wait_until_down(host, cfg, job, deadline)

            if not went_down:
                detail = f" (ssh exited {rc}: {stderr})" if rc != 0 else ""
                if cfg.force_off_on_timeout:
                    job.message = f"Timed out waiting for {cfg.ssh_host}; cutting power anyway"
                    log.warning("forcing plug %s off after timeout", host)
                    await self._switch(host, False)
                    self._jobs.pop(host, None)
                    return
                job.failed = True
                job.message = (
                    f"{cfg.ssh_host} did not shut down in time — plug left ON{detail}"
                )
                log.error("shutdown of %s failed; leaving plug %s on", cfg.ssh_host, host)
                return

            if cfg.grace_seconds:
                for remaining in range(cfg.grace_seconds, 0, -1):
                    job.message = f"{cfg.ssh_host} is down — cutting power in {remaining}s"
                    await asyncio.sleep(1)

            await self._switch(host, False)
            log.info("%s is down; plug %s switched off", cfg.ssh_host, host)
            self._jobs.pop(host, None)

        except asyncio.CancelledError:
            log.info("shutdown job for %s cancelled", host)
            self._jobs.pop(host, None)
            raise
        except Exception as exc:
            job.failed = True
            job.message = f"Shutdown failed: {exc} — plug left ON"
            log.exception("shutdown job for %s failed", host)

    async def close(self) -> None:
        for host in list(self._jobs):
            self.cancel_job(host)
        for dev in self._devices.values():
            try:
                await dev.disconnect()
            except Exception:
                pass
        self._devices.clear()
