# Gateway service (messaging platforms)

The Hermes gateway — which drives the messaging-platform integrations
(WeCom, etc.) — runs as a **systemd user service** named
`hermes-gateway.service`. It is *not* the same as running `hermes` in your
terminal: the service runs under systemd, which does **not** source
`~/.bashrc`, so credentials must live in `~/.hermes/.env` or the unit file,
not only in your shell profile.

Unit file: `~/.config/systemd/user/hermes-gateway.service`
(`ExecStart` → `python -m hermes_cli.main gateway run`, `WorkingDirectory`
`~/.hermes`).

## Convenience scripts

These wrap the `systemctl --user` calls and set `XDG_RUNTIME_DIR` /
`DBUS_SESSION_BUS_ADDRESS` for you, so they work even from a shell that would
otherwise fail with `Failed to connect to bus: No medium found`:

```bash
bash scripts/gateway-start.sh   # start (clears stale "failed" state first)
bash scripts/gateway-stop.sh    # stop  (clears cosmetic "failed" state after)
```

The raw commands below are equivalent.

## Start / stop / restart

> **Always pass `--user`.** This is a systemd *user* service. Without
> `--user`, systemd looks in the system scope, doesn't find the unit, and
> the command fails (e.g. `Failed to stop hermes-gateway.service: Unit
> polkit.service is masked.`). That missing flag is the usual reason "stop
> doesn't work."

> **`Failed to connect to bus: No medium found`?** Your shell is missing
> `XDG_RUNTIME_DIR`, so `systemctl --user` has no socket to reach the user
> systemd instance (common in SSH / `exec`-style shells when the user has
> `Linger=no`). Set these once per shell — or add them to `~/.bashrc`:
>
> ```bash
> export XDG_RUNTIME_DIR=/run/user/$(id -u)
> export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
> ```
>
> Optional, sturdier: `sudo loginctl enable-linger $(id -un)` keeps the user
> manager running across logouts.

```bash
systemctl --user start   hermes-gateway.service   # start
systemctl --user stop    hermes-gateway.service   # stop
systemctl --user restart hermes-gateway.service   # restart (use after editing ~/.hermes/.env or config.yaml)
systemctl --user status  hermes-gateway.service   # status
```

Enable / disable auto-start at login:

```bash
systemctl --user enable  hermes-gateway.service
systemctl --user disable hermes-gateway.service
```

> The unit sets `Restart=always`, so systemd revives the gateway if it
> crashes. An explicit `systemctl --user stop` is honored and will **not**
> be auto-restarted.

### "failed" state after stop is expected

After `systemctl --user stop`, the unit shows **`failed`** (not `inactive`),
even though the gateway *is* stopped (`MainPID=0`). On SIGTERM the gateway
exits with code 1 on purpose — so that a real crash is auto-revived by
`Restart=always` — and systemd records that non-zero exit as `failed`. The
process is genuinely down; the red status is cosmetic. Clear it with:

```bash
systemctl --user reset-failed hermes-gateway.service
```

## Hermes CLI equivalents

The `hermes` CLI can stop and inspect the gateway, but it does **not** start
the background service (that's systemd's job — `hermes gateway run` only runs
it in the foreground):

```bash
hermes gateway status    # show gateway status
hermes gateway stop      # stop the gateway
hermes gateway list      # list profiles and their gateway status
hermes gateway run       # run in the foreground (debugging)
```

## Logs

```bash
journalctl --user -u hermes-gateway.service -f   # live service logs
tail -f ~/.hermes/logs/gateway.log               # gateway log
tail -f ~/.hermes/logs/errors.log                # errors (e.g. provider 401s)
```

## Credentials note

The gateway reads provider credentials from `~/.hermes/.env` (and
`~/.hermes/config.yaml`, whose `${VAR}` references are resolved against the
process environment). Because systemd does not load your shell profile, a key
exported only in `~/.bashrc` will be visible to the local `hermes` CLI but
**missing in the gateway** — causing provider auth failures (e.g. WeCom
replies returning `HTTP 401: The API key format is incorrect`). Put
provider keys in `~/.hermes/.env`, then `systemctl --user restart
hermes-gateway.service`.
