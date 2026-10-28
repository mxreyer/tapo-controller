# Tapo Plug Controller

A tiny web app for switching TP-Link Tapo P115 smart plugs on and off from your
home server. Talks to the plugs **locally** over your LAN (via
[python-kasa](https://github.com/python-kasa/python-kasa), which implements
Tapo's KLAP protocol) — no cloud round-trip for the actual switching.

Shows on/off state plus live power draw and today's energy for each plug, since
the P115 has energy monitoring.

Plugs that power a **server** get special handling: switching the toggle off
SSHes in, shuts the machine down, waits until it is confirmed down, and only
then cuts the plug. Switching it back on restores power, and the machine boots
itself.

Tapping a server card expands it into separate switches for the machine and the
plug — see [Expanded controls](#expanded-controls-the-server-and-the-plug-separately).

## Setup

1. Give each plug a **static IP / DHCP reservation** in your router. Plugs that
   move around after a reboot are the main source of "it stopped working".
2. Copy the config and fill it in:

   ```sh
   cp compose.example.yaml compose.yaml
   cp .env.example .env
   $EDITOR .env
   ```

   | Variable | Meaning |
   | --- | --- |
   | `TAPO_USERNAME` / `TAPO_PASSWORD` | Your TP-Link/Tapo account. The plugs authenticate locally against these credentials — they're never sent to TP-Link by this app. |
   | `TAPO_HOSTS` | Comma-separated plug IPs. Recommended. |
   | `TAPO_DISCOVERY` | `true` (default) enables broadcast discovery on startup and the "Scan network" button. Needs host networking. |
   | `TAPO_SERVERS_FILE` | Path to the server config. Defaults to `/config/servers.json`. |
   | `SSH_KNOWN_HOSTS` | `known_hosts` for the shutdown SSH connection. |
   | `WEB_USERNAME` / `WEB_PASSWORD` | Optional HTTP basic auth for the UI. Blank = no auth. |
   | `APP_PORT` | Listening port. Defaults to `8080`. |

   > **Firmware note.** Tapo firmware 1.4.0 turns off third-party access by
   > default and the plugs will all report as offline. Enable
   > *Device Settings → Third-Party Compatibility* in the Tapo app, per plug.
   > See [`NOTES.md`](NOTES.md).

3. If a plug powers a server, configure it (see
   [Server plugs](#server-plugs) below):

   ```sh
   cp servers.example.json servers.json
   mkdir -p ssh && ssh-keygen -t ed25519 -f ssh/id_ed25519 -N "" -C tapo-controller
   ssh-keyscan -H 192.168.1.20 >> ssh/known_hosts
   cat ssh/id_ed25519.pub
   ```

   Then set up a `tapo` user on the server that can only shut the machine down,
   using the public key printed by the last command — see
   [Setting up the server](#setting-up-the-server).

   Otherwise create an empty one so the mount succeeds: `echo '{}' > servers.json`

4. Run it:

   ```sh
   docker compose up -d
   ```

   Then open `http://<your-server>:8080`.

## Server plugs

`servers.json` maps a **plug IP** to the machine it powers:

```json
{
  "192.168.1.50": {
    "ssh_host": "192.168.1.20",
    "ssh_user": "tapo",
    "ssh_key": "/ssh/id_ed25519",
    "shutdown_command": "sudo -n /sbin/shutdown -h now",
    "wait_timeout": 300,
    "grace_seconds": 20,
    "power_threshold_w": 8,
    "power_cycle_seconds": 10,
    "force_off_on_timeout": false
  }
}
```

### What happens when you switch it off

1. Runs `shutdown_command` over SSH.
2. Polls the machine's SSH port until it stops answering, and requires
   `down_confirmations` (default 3) consecutive failures so a brief network
   blip isn't mistaken for "down".
3. If `power_threshold_w` is set, also waits for the plug's own power reading to
   fall below it — a genuinely independent confirmation that the machine has
   stopped, which is worth setting. Check the plug's idle wattage once the
   machine is off and pick something a little above it.
4. Waits `grace_seconds` more so the OS can finish flushing disks and cut power
   to the PSU.
5. Switches the plug off.

The UI shows progress the whole time and polls every 2s while a shutdown is in
flight. **Tapping the switch again cancels** — the machine is already going
down, but power is left on so it comes straight back up.

### If the machine doesn't go down

By default the plug is **left on** and the card turns red with the reason (bad
SSH key, `sudo` prompting for a password, a hung shutdown). That is the safe
failure: cutting power to a machine that is still writing to disk risks
corrupting the filesystem. An SSH failure gives up after ~45s rather than
waiting out the full `wait_timeout`.

Set `force_off_on_timeout: true` to cut power anyway after `wait_timeout`. Only
do this if you understand the risk. There is also an escape hatch that skips the
whole sequence:

```sh
curl -X POST 'http://homeserver:8080/api/plugs/192.168.1.50/off?force=true'
```

### Expanded controls: the server and the plug, separately

Tap a server card to expand it and drive the two independently.

**Server** — the machine, never the relay.

| | What it does |
| --- | --- |
| Off | The shutdown sequence above, but it stops once the machine is down and **leaves the plug powered**. |
| On | **Power-cycles the plug**: off, `power_cycle_seconds` (default 10), on. A machine in soft-off behind a live plug needs a real AC loss before it boots. |

**Tapo plug** — the relay only, marked with a warning. No SSH, no waiting, no
checks; off cuts power immediately. It overrides anything in progress. Cutting
power to a machine that is still answering asks for confirmation first.

### When the machine crashes

A hard crash leaves the machine unreachable over SSH while the plug still reads
on and draws running-level power. The combined toggle refuses to act on that, by
design — it cannot tell a hung machine from a busy one.

With `power_threshold_w` set, the expanded view compares SSH reachability against
the power reading and shows the server as **unreachable — crashed**; its switch
then power-cycles instead of attempting an SSH shutdown that cannot succeed.
Without the threshold there is no second signal and a crashed machine reads as
merely off, so set it.

A power cycle cannot be cancelled part-way, and the container restores power if
stopped mid-cycle. The plug switch overrides it.

### Setting up the server

The app logs in over SSH for one thing only: running `shutdown_command`. Its
up/down checks just probe the SSH port and never log in. So it gets its own
user, `tapo`, whose key can shut the machine down and do nothing else. Run
these **on the server**.

1. **Create the user.**

   ```sh
   sudo useradd -m -s /bin/sh tapo
   ```

   `-m` creates `/home/tapo`, where sshd looks for the key. The shell must be a
   real one: sshd runs the forced command below through it, so `nologin` would
   break shutdowns. No password is set, so the key is the only way in.

2. **Install the key, locked to the shutdown command.** Paste the public key
   printed during [Setup](#setup) in place of `ssh-ed25519 AAAA...`:

   ```sh
   sudo install -d -m 700 -o tapo -g tapo /home/tapo/.ssh
   echo 'restrict,command="sudo -n /sbin/shutdown -h now" ssh-ed25519 AAAA... tapo-controller' | \
     sudo tee /home/tapo/.ssh/authorized_keys
   sudo chown tapo:tapo /home/tapo/.ssh/authorized_keys
   sudo chmod 600 /home/tapo/.ssh/authorized_keys
   ```

   - `command="..."` makes the server run this command whatever the client
     asks for: a shell, `scp`, `sftp` or any other command gets the shutdown
     instead. Keep `shutdown_command` in `servers.json` set to the same thing
     so the config describes what really runs.
   - `restrict` turns off terminal sessions, port/agent/X11 forwarding and
     `~/.ssh/rc`.

   A stolen key can then only shut the machine down. To also tie it to one
   source address, add `from="192.168.1.10"` to the options: the Docker host's
   LAN IP under `network_mode: host`, or the sidecar's address behind
   Tailscale/WireGuard. It is optional, and breaks shutdowns if that address
   changes.

3. **Allow passwordless shutdown.** `tapo` must run the shutdown without a
   password prompt, or `sudo -n` fails and the job errors out. Grant exactly
   that command, arguments included, and nothing more:

   ```sh
   echo 'tapo ALL=(root) NOPASSWD: /sbin/shutdown -h now' | \
     sudo tee /etc/sudoers.d/tapo-shutdown
   sudo chmod 440 /etc/sudoers.d/tapo-shutdown
   sudo visudo -cf /etc/sudoers.d/tapo-shutdown
   ```

   The path must match the one in the forced command exactly — check it with
   `command -v shutdown` (often `/usr/sbin/shutdown`) and use the same path in
   all three places: here, `authorized_keys` and `shutdown_command`.

4. **Test it** from the Docker host. Asking for anything else should shut the
   machine down rather than print the file:

   ```sh
   ssh -i ssh/id_ed25519 tapo@192.168.1.20 'cat /etc/passwd'
   ```

   If it prints `Permission denied (publickey)` instead, check the ownership
   and modes from step 2; sshd ignores keys that other users can write to.
   (On an sshd running without PAM, a passwordless account can also be refused
   outright — `sudo usermod -p '*' tapo` fixes that and still allows no
   password login.)

One more thing to set on the server itself:

- **Boot on power restore.** Switching the plug back on only restores power —
  the machine boots itself only if its BIOS/UEFI is set to power on after AC
  loss. Look for "Restore on AC Power Loss" / "After Power Failure" and set it
  to **Power On** (not "Last State", which will keep it off). Without this, the
  On toggle powers the plug and nothing else happens.

## Notes

- **Networking.** `network_mode: host` lets UDP broadcast discovery work and
  puts the container on the same L2 segment as the plugs. Behind any network
  namespace instead — a bridge, or a Tailscale/WireGuard sidecar — outbound
  unicast to the plugs and to the server's SSH port still routes fine, but
  **broadcast discovery does not**. In that case set `TAPO_DISCOVERY=false` and
  list every plug in `TAPO_HOSTS`. Give the plugs DHCP reservations either way,
  so their addresses stay put.
- **Shutdown jobs live in memory.** Restarting the container mid-shutdown loses
  the job, leaving the plug on and the machine off. Switch the plug off again
  once it settles.
- **Password changes.** If you change your TP-Link account password, the plugs
  re-key; update `.env` and restart the container.
- **Auth.** Basic auth over plain HTTP is only meaningful on a trusted LAN. If
  you expose this beyond your network, put it behind a reverse proxy with TLS
  (Caddy/Traefik/nginx) — or better, don't expose it and use a VPN.

## API

Handy for scripting or hooking into Home Assistant / shortcuts:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/plugs` | Status of every known plug |
| `POST` | `/api/plugs/{ip}/on` | Turn on |
| `POST` | `/api/plugs/{ip}/off` | Turn off — graceful shutdown first for server plugs |
| `POST` | `/api/plugs/{ip}/off?force=true` | Cut power immediately, skipping shutdown |
| `POST` | `/api/plugs/{ip}/toggle` | Toggle |
| `POST` | `/api/plugs/{ip}/cancel` | Abort a shutdown in progress |
| `POST` | `/api/plugs/{ip}/server/off` | Shut the machine down over SSH, **leave the plug on** |
| `POST` | `/api/plugs/{ip}/server/on` | Power-cycle the plug so the machine boots |
| `POST` | `/api/plugs/{ip}/plug/on` | Relay on — no SSH, no checks |
| `POST` | `/api/plugs/{ip}/plug/off` | Relay off — no SSH, no checks |
| `POST` | `/api/plugs/{ip}/plug/cycle` | Relay off, pause, back on |
| `POST` | `/api/discover` | Re-scan the LAN |
| `GET` | `/healthz` | Health check (never requires auth) |

`GET /api/plugs` reports `server_state` for server plugs: `up`, `down` (machine
off, plug still powered), `no_power` (plug off), or `unreachable` (drawing power
but not answering — crashed).

```sh
curl -X POST http://homeserver:8080/api/plugs/192.168.1.50/off

# Reboot without cutting power:
curl -X POST http://homeserver:8080/api/plugs/192.168.1.50/server/off
curl -X POST http://homeserver:8080/api/plugs/192.168.1.50/server/on

# Revive a crashed machine:
curl -X POST http://homeserver:8080/api/plugs/192.168.1.50/plug/cycle
```

## Testing local changes

`compose.yaml` runs the published image, so `docker compose up -d` ignores local
edits. To build the working tree instead, add an override file next to it:

```yaml
# compose.local.yaml
services:
  tapo-controller:
    build: /path/to/this/repo
    # A separate tag, so the local build cannot overwrite the pulled image.
    image: tapo-controller:local
    pull_policy: build
```

Compose auto-loads only `compose.override.yaml`, so this name keeps local builds
opt-in:

```sh
docker compose -f compose.yaml -f compose.local.yaml up -d --build
docker compose up -d    # back to the published image
```

## Running without Docker

Needs the `ssh` client on `PATH` for server plugs.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a && . ./.env && set +a
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```
