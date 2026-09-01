# Notes

Things that are not derivable from the code, kept for whoever picks this up
next. Deployment-specific values live with the deployment, not here.

## Maturity

Verified against real hardware: image build, plug communication (P115), live
power/energy readings, `servers.json` loading, SSH key auth from inside the
container, passwordless sudo, and one full graceful shutdown followed by an
automatic boot on power restore.

Observed during that shutdown, which is worth knowing when picking
`power_threshold_w`:

    t=0s    96.5W   ssh shutdown sent
    t=4s   115.7W   spike — disks flushing during shutdown
    t=7s   107.5W   port 22 stopped answering (1/3)
    t=13s   12.9W   machine soft-off, draw collapsed (2/3)
    t=20s   11.5W   confirmed down, grace countdown
    t=39s      —    plug switched off

Running draw was ~96W and soft-off idle ~12W, so a threshold of 20W sat in a
wide gap. Measure your own machine's idle draw with it powered off; the gap is
usually large, but a threshold below standby draw will hang until timeout.

**There is no test suite.** The failure paths have only ever been exercised
against stubs: machine-never-goes-down, ssh auth failure,
`force_off_on_timeout`, and cancel-mid-wait. The happy path is proven on real
hardware; the safety net is not.

The split server/plug controls and the power cycle are in the same position:
exercised against stubs, never watched on real hardware. `power_cycle_seconds:
10` is a guess at "long enough for the PSU to drain" — if a cycle leaves the
machine dark, raise that first.

## Read this before debugging "the plugs stopped working"

**Tapo firmware 1.4.0 breaks local control by default.** It switched plugs to a
new encryption scheme, `TPAP`, and turned third-party access off. Every plug
then reports as offline with:

    Unsupported device ... with encrypt_scheme ... encrypt_type='TPAP'

`python-kasa` does not implement TPAP in any released version — 0.10.2 was the
latest at time of writing and has no TPAP support at all, so bumping the pin
does not help. Tracked upstream as python-kasa issue #1590 (open); PR #1706 is
stalled with the crypto unimplemented.

The fix is not in this repo — it is a per-device toggle in the Tapo phone app:

> Tapo app → the plug → Device Settings → **Third-Party Compatibility** → enable

Expect it to recur after firmware updates, and to need doing on every plug
individually.

## `power_threshold_w` is what makes a crash visible

It started as a second confirmation that a shutdown had finished. It now also
carries the only signal separating **crashed** from **cleanly off**: both are
"plug on, SSH not answering", and the draw is the only difference. Without it a
hung machine shows as merely off.

A hung machine usually draws close to its idle-running figure, so the same wide
gap works. One that crashes into a low-power state still reads as cleanly off,
and no threshold-tuning fixes that — the plug switch is the fallback.

## Design invariant: a failed shutdown leaves the plug ON

When the SSH shutdown fails or times out, the plug is **deliberately left on**
and the failure surfaced in the UI. This is intentional — do not "fix" it into
cutting power on failure.

Cutting power to a machine still writing to disk risks filesystem corruption. A
server left powered on costs a few watts; the alternative can cost the
filesystem. The same reasoning drives the other conservative defaults: requiring
consecutive down-confirmations rather than a single probe, the optional
power-draw threshold as a second independent signal, and the grace period after
the machine stops answering.

Escape hatches for the opposite behaviour already exist: `force_off_on_timeout`
per server in `servers.json`, `POST /api/plugs/{ip}/off?force=true` per call,
and the **Tapo plug** switch in the UI. That switch overrides everything,
including a power cycle in flight; it is the control of last resort and must not
learn to second-guess.

If someone reports "it didn't turn off", check for a failed job / red card
first — that is most likely this feature working, not a bug. The answer is to
expand the card, not to loosen the invariant.

## Why "turn the server on" cuts power first

The server switch's On position power-cycles rather than just switching on,
because the plug is usually already on. `server/off` leaves power up, so the
machine sits in soft-off behind a live plug, and only an AC loss boots it from
there (see the `Restore on AC Power Loss` trap below). Switching an already-on
plug on again is a no-op, so the cycle is the entire mechanism.

- A power cycle is **not cancellable**. Abandoning one leaves the plug off and
  the machine dark, worse than either end state.
- `close()` restores power to a plug caught mid-cycle rather than awaiting the
  job, so a container restart cannot strand the server without power. It is the
  one job that does not follow the "jobs live in memory" rule below.
- Jobs are one-per-plug and cancellation is asynchronous, so a replaced job runs
  its cleanup *after* its replacement is installed. `_clear_job` lets a finishing
  job clear the slot only if it still owns it; a plain `pop` reintroduces a race
  that silently loses the new job.

## Traps that look like app bugs

1. **BIOS "Restore on AC Power Loss" must be `Power On`.** Not `Last State` —
   and `Last State` is the trap, because this app deliberately reaches a clean
   soft-off *before* cutting AC. The recorded last state is therefore always
   "off", so `Last State` restores "off" and the machine never boots. It fails
   100% of the time, not intermittently. On boards that expose it, also disable
   **ErP Ready / Deep Sleep**, which cuts +5VSB in soft-off and defeats
   auto-power-on.
2. **`.local` hostnames do not resolve inside the container.** They work in a
   host shell via nss-mdns, but `python:3.12-slim` ships no `libnss-mdns`, so
   `ssh_host: "something.local"` fails only once containerised. Use IP
   addresses in `servers.json`.
3. **SSH key paths in `servers.json` are container paths.** The key is mounted
   at `/ssh`, so use `/ssh/id_...`, never `~/.ssh/id_...` — `~` resolves to
   the container's `/root`.
4. **Passwordless sudo is required** for the shutdown command, or `sudo` waits
   for a password and the job times out. Note that a tight sudoers rule granting
   only `shutdown`/`poweroff` will make unrelated `sudo -n` probes fail; that is
   correct, not a misconfiguration.
5. **Give the plugs and the server DHCP reservations.** `ssh_host` is hardcoded
   in `servers.json`; if that lease moves, the shutdown SSH goes nowhere and the
   plug is left on with a red card, indistinguishable from the genuine failure
   above.

## Networking

Broadcast discovery needs the container on the host network. Behind any kind of
network namespace — a Tailscale sidecar, a bridge — outbound unicast to the
plugs and to the server's SSH port routes fine, but UDP broadcast does not. In
that setup set `TAPO_DISCOVERY=false` and list every plug in `TAPO_HOSTS`; the
"Scan network" button will find nothing.

`APP_PORT` moves the listening port and is honoured by both the Dockerfile `CMD`
and its `HEALTHCHECK`. It exists because host networking puts the app straight
into the host's port space, where 8080 is often already taken.

## Shutdown jobs live in memory

Restarting the container mid-shutdown loses the job, leaving the plug on and the
machine off. Switch the plug off again once it settles. (Power cycles are the
exception — see above.)
