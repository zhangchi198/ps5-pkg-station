# PS5 PKG Station

A small self-hosted web app that turns a folder of PS5 game backups into clean, well-named fake-signed
`.pkg` files, and keeps doing it as new games show up. It runs in Docker (built for NAS boxes) and
does the actual building with [PS5 UltraPack](https://github.com/knutwurst/ps5-ultrapack)'s
command-line builder.

> For backups of games you own, on your own jailbroken console. This project ships no games, keys or
> firmware, and is not affiliated with Sony Interactive Entertainment.

## What it does

- **Finds every game** in a library folder and an optional downloads folder: images (`.exfat`,
  `.ffpfsc`, `.ffpfs`, `.ffpkg`, ...), extracted game folders, and RAR / 7z / zip sets, including sets
  that wrap an image. Copies of the same title are grouped by title ID.
- **Picks one copy per game**: the newest version wins. Copies whose AMPR assets are packed with LZ4 or
  Zstd are passed over, and a game that only has packed copies is left out. When an image and an archive of the same version both
  exist, the image is used and checked file by file against the archive's CRC32s.
- **Handles backports**: when the image's executables and firmware fields differ from the original
  archive, it keeps whichever copy runs on your console's firmware (set it under Settings), or the one
  you choose per game.
- **Builds** with UltraPack (`--stage-in-place`, so the work disk needs room for roughly one copy of
  each game in flight), validates the package, and names it `Title [PPSA12345] [v01.002.003] [fw10.00].pkg`.
- **Remembers** what it built: later runs only build new games and newer versions.
- **Runs on a schedule** (or on demand), several builds in parallel, at low CPU priority, with an
  optional push notification ([ntfy](https://ntfy.sh)-style URL) when each game finishes.

Source folders are only ever read. Mount them read-only, as in the example below.

## The web UI

Open `http://<host>:8765`.

| Page | |
|---|---|
| Dashboard | setup checklist; run now, stop after the current games, rescan; live stages and progress; free space; why a scan or run failed |
| Library | every game found, the copy that will be used and why; per-game options: force a source, keep the backport or the original, overlay a folder of patch files, skip, build just this one |
| History | every finished package with its validation result, size and times |
| Settings | folders, compression level, parallel builds, console firmware, junk-file cleanup patterns, schedule, notifications |
| Logs | the run log and each build's log, live |

There is no login. Keep port 8765 on your LAN or behind a VPN such as Tailscale, and don't forward it
to the internet.

## Quick start

Requirements: an x86-64 Linux Docker host (UltraPack's Linux tools are x64 only), and a fast scratch
disk (SSD/NVMe) with free space a little larger than your biggest game.

1. Copy [`docker-compose.yml`](docker-compose.yml) and change the five paths under `x-paths`, plus
   `user:` (run `id` on the host to see the uid:gid that owns your output folder).
2. `docker compose up -d`
3. Open `http://<host>:8765`. The **Setup** card at the top of the Dashboard checks your folders and tells you
   what to fix (see below). Set your console firmware under **Settings**, then press **Scan**.
   The Library page shows what would be built. Nothing is built until you press **Run**.

### The setup check

The Dashboard opens with a **Setup** card: one row for the game library, the downloads folder, the output
folder, the work drive, `/config`, UltraPack's files, your console firmware and the server's clock, each marked
`ok`, `warn` or `FAIL` with a one-line fix. It's checked when the page loads and when you press **Re-check**
(also under Settings, as **Check setup**), and it shrinks to a single "Setup OK" line once nothing needs doing.

- A folder that must be writable (output, work, `/config`) is tested by creating and deleting a small hidden
  file, so a permission problem shows up now rather than half an hour into the first game. On Synology that's
  usually the `group_add` line in the compose file.
- **Run now** is disabled, with the reason, while a row says FAIL, and a run or scheduled run won't start then.
- `/downloads` is optional: leave that volume out and the scan uses the library only (a note, not an error).
- The work drive is compared with the biggest game in the last scan plus the "keep free" reserve, because a game
  that doesn't fit is deferred on every run. The server's time and time zone are shown because schedule times
  use that clock; set `TZ` in the compose file for your local time.
- If a scan or run stops with an error, the Dashboard says so in red with the end of its log and a link to the
  full log. It stays until the next scan or run.

To build the image yourself instead of pulling it, use `build: .` in the compose file or run
`docker build -t ps5-pkg-station .`. The build downloads UltraPack's release and checks its SHA-256.

### Folders inside the container

| Path | Use | Mode |
|---|---|---|
| `/library` | game images and folders | read-only |
| `/downloads` | optional second source, typically archive sets | read-only |
| `/output` | finished `.pkg` files | read-write |
| `/work` | scratch space for extraction and building | read-write, fast disk |
| `/config` | `state/config.json`, history, caches, logs, patch folders | read-write |

These are fixed by the `volumes:` list in the compose file, so Settings shows them read-only.

### Folders on your NAS

Everything the app shows is a path inside the container (`/output`), which isn't where you'd look for a
finished package. Docker doesn't tell a container where its bind mounts come from, so you tell the app, and it
then shows real locations on the Dashboard (the Folders card) and under Settings > Folders. The example
compose file writes each host folder once, under `x-paths`, and uses that value for both the volume and
a `PS5PKG_HOST_*` variable, so they can't drift:

| Variable | Host folder mounted at |
|---|---|
| `PS5PKG_HOST_LIBRARY` | `/library` |
| `PS5PKG_HOST_DOWNLOADS` | `/downloads` |
| `PS5PKG_HOST_OUTPUT` | `/output` |
| `PS5PKG_HOST_WORK` | `/work` (the app works in `/work/tmp` and adds the `/tmp` itself) |
| `PS5PKG_HOST_CONFIG` | `/config` |

Prefer a `.env` file next to the compose file? Put `GAMES_LIBRARY=/srv/games/ps5` in it and write
`- ${GAMES_LIBRARY}:/library:ro` under `volumes:` and `PS5PKG_HOST_LIBRARY: ${GAMES_LIBRARY}` under
`environment:` (the same for the other folders).

Where a folder's location comes from, first match wins:

1. `PS5PKG_HOST_LIBRARY` and the other `PS5PKG_HOST_*` variables above.
2. `PS5PKG_HOST_PATHS`, the original single JSON variable, still supported unchanged:
   `'{"library": "/srv/games/ps5", "output": "/srv/games/ps5/pkg", "temp": "/srv/scratch/tmp"}'`. Its values are
   the location of the folder exactly as the app shows it (so `temp` is the host path of `/work/tmp`);
   the keys are `library`, `downloads`, `output`, `temp` (or `work`) and `config`.
3. **Settings > Folders > "is shown as"**, saved as `host_paths` in `config.json`, so it survives recreating the
   container. Used only when the variables above don't cover that folder; a folder set by a variable is shown
   read-only as "set in docker-compose".
4. A hint read from the container's mount table (`/proc/self/mountinfo`), shown as `…/games/ps5 (detected,
   volume prefix unknown)`: it knows the end of the host path but not the volume prefix in front of it.

The hint is also compared with what you configured: if the tail doesn't match, the folder gets a note. That
is only a hint, since btrfs subvolumes and overlay set-ups add prefixes. A malformed variable (bad JSON,
an unknown folder name, a relative path) is shown in an amber banner and ignored, not dropped silently. Until
at least one location is set, the Dashboard shows a dismissible reminder.

### Synology DSM (Container Manager)

- DSM folder permissions are ACLs. If the container can read but not write to your shared folder, add
  `group_add: ["101"]` (the `administrators` group). Alternatively, give the uid in `user:` write access to
  that folder in DSM.
- DSM's kernel doesn't support CPU limits (`cpus:` fails). Use `Workers per build` and `CPU priority` in
  Settings instead. Memory limits work.
- Put `/work` on an NVMe or SSD volume if you have one: building is much faster when the scratch disk
  isn't the same array the package is written to.

## Settings worth knowing

All settings live in `/config/state/config.json` and can be changed from the Settings page.

- `console_fw`: your console's firmware, for example `9.60`. Used to choose between an original copy and a
  backport.
- `compression_level`: UltraPack's Kraken level, 1 to 9. 4 is fast; 7 gives somewhat smaller packages.
- `max_builds` / `max_compress` / `cpu_count`: how many games are in flight, how many compress at once,
  and how many workers each one uses.
- `junk_file_patterns` / `junk_title_patterns`: regexes for release-group watermarks. Matching files are
  deleted from the work copy, and matching text is cut out of the title in `param.json` and the file
  name. Both are empty by default.
- `overrides`: per title ID, `source`, `patch` (a folder, for example under `/config/patches`, whose files are
  copied over the game), `modified_exec` (`image` or `original`), `skip`. Usually set from the Library page.

### Editing `config.json` by hand

Hand-editing is supported (with the compose file above, the file is `./config/state/config.json`).
Keys you leave out use their defaults, and keys this version doesn't know are kept when Settings saves.

- **When an edit takes effect.** The Dashboard and the schedule pick it up within seconds, and the
  Settings page reloads it (or asks first, if you have unsaved edits there). A scan or run reads the file
  when it starts, so the next one uses it; a run that's already going keeps the settings it started with.
  Nothing needs a restart.
- **An invalid value** (say `"compression_level": 99`) is listed in an amber banner, and that one setting
  uses its default until you fix it. The file isn't touched.
- **A syntax error** (a missing comma, say) shows a red banner with the line number. The page and the
  schedule carry on with the last working settings, saving from Settings is refused so your edit isn't
  overwritten, and a scan or run won't start (the log says why) until the file is valid again. Deleting the
  file goes back to the defaults.
- Reading never changes the file. It's written only when you save from Settings or the Library page (and
  once with the defaults if it doesn't exist yet). A save rewrites the whole file (indented JSON), through a
  lock, in one step. `config.json.lock` next to it is that lock; leave it.

## Updating

`docker compose pull && docker compose up -d`. Recreating the container stops a run that's in progress.
Already-finished packages are kept, so wait for the Dashboard to go idle first.

## Credits and license

The package building is all [PS5 UltraPack](https://github.com/knutwurst/ps5-ultrapack) by knutwurst and its
contributors, plus the projects it builds on (MkPFS, LibOrbisPkg / LibProsperoPkg, ampr-emu, 7-Zip). See
[NOTICE.md](NOTICE.md).

PS5 PKG Station is licensed under the [GNU GPL v3.0](LICENSE).
