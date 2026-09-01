import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .plugs import PlugManager

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

STATIC_DIR = Path(__file__).parent / "static"
manager = PlugManager()

WEB_USERNAME = os.getenv("WEB_USERNAME", "")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
_basic = HTTPBasic(auto_error=bool(WEB_PASSWORD))


def require_auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    if not WEB_PASSWORD:
        return
    ok = creds is not None and secrets.compare_digest(
        creds.username, WEB_USERNAME
    ) and secrets.compare_digest(creds.password, WEB_PASSWORD)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authorised",
            headers={"WWW-Authenticate": "Basic"},
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if manager.discovery:
        try:
            found = await manager.discover()
            logging.info("discovery found %d new plug(s)", len(found))
        except Exception as exc:
            logging.warning("startup discovery failed: %s", exc)
    yield
    await manager.close()


app = FastAPI(title="Tapo Plugs", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "plugs": len(manager.hosts)}


@app.get("/api/plugs", dependencies=[Depends(require_auth)])
async def list_plugs() -> list[dict]:
    return await manager.statuses()


@app.post("/api/plugs/{host}/server/{action}", dependencies=[Depends(require_auth)])
async def server_act(host: str, action: str) -> dict:
    """Act on the machine rather than the plug feeding it.

    `off` shuts it down over SSH and leaves the plug powered. `on` power-cycles
    the plug, which is what actually boots a machine sitting in soft-off.
    """
    if action not in {"on", "off"}:
        raise HTTPException(404, "unknown action")
    cfg = manager.servers.get(host)
    if cfg is None:
        raise HTTPException(404, f"plug {host} has no server configured")
    try:
        if action == "off":
            return await manager.begin_shutdown(host, cfg, cut_power=False)
        return await manager.begin_power_cycle(host, cfg)
    except Exception as exc:
        raise HTTPException(502, f"plug did not respond: {exc}")


@app.post("/api/plugs/{host}/plug/{action}", dependencies=[Depends(require_auth)])
async def plug_act(host: str, action: str) -> dict:
    """Drive the relay directly — no SSH, no shutdown, no checks.

    The escape hatch for a machine that has stopped answering. `cycle` drops
    power briefly and restores it.
    """
    if action not in {"on", "off", "cycle"}:
        raise HTTPException(404, "unknown action")
    if host not in manager.hosts:
        raise HTTPException(404, f"unknown plug {host}")
    try:
        if action == "cycle":
            return await manager.begin_power_cycle(host, manager.servers.get(host))
        return await manager.set_plug(host, action == "on")
    except Exception as exc:
        raise HTTPException(502, f"plug did not respond: {exc}")


@app.post("/api/plugs/{host}/{action}", dependencies=[Depends(require_auth)])
async def act(host: str, action: str, force: bool = False) -> dict:
    """Switch a plug.

    For a plug backing a server, `off` starts a graceful shutdown in the
    background and returns immediately with a `job` describing progress; the
    plug is only cut once the machine is confirmed down. `force=true` skips
    that and cuts power right away — the emergency escape hatch.
    """
    if action not in {"on", "off", "toggle", "cancel"}:
        raise HTTPException(404, "unknown action")
    if host not in manager.hosts:
        raise HTTPException(404, f"unknown plug {host}")
    try:
        if action == "cancel":
            manager.cancel_job(host)
            return await manager.status(host)
        if action == "toggle":
            return await manager.toggle(host)
        return await manager.set_state(host, action == "on", force=force)
    except KeyError:
        raise HTTPException(404, f"unknown plug {host}")
    except Exception as exc:
        raise HTTPException(502, f"plug did not respond: {exc}")


@app.post("/api/discover", dependencies=[Depends(require_auth)])
async def discover() -> dict:
    return {"new": await manager.discover()}


@app.get("/", dependencies=[Depends(require_auth)])
async def index() -> FileResponse:
    # Without this the response carries only Last-Modified, which browsers cache
    # heuristically (~10% of the file's age) — a UI change then stays invisible
    # for a day. FileResponse ignores If-None-Match, so this costs a ~12 KB
    # re-send per load rather than a 304; on a LAN, not worth avoiding.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
