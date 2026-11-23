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
- **Copies PS4 packages as they are**: a PS4 title's ready-made `.pkg` files are copied, named from their own
  headers, without conversion (see "PS4 titles" below).
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
   volume prefix unknown)`: it knows the end of the host path but not the volume prefix in front of it (the
   API marks it `partial`, with a leading `…`, so it can't be mistaken for a full path). A btrfs subvolume prefix
   such as Synology's `@syno` is left out, since it isn't part of any path you can open.

Once a folder has a host location (steps 1 to 3; the partial hint in step 4 is never used for this), it is shown
wherever a path appears, each with a **copy** button that copies the host path: the Library (the source of each
game, the choices under Options), History (the package's location and its source), the reasons under "This run",
the Options of a game (the patch folder, with where it is on your NAS, and a path typed in as it is on the NAS is
stored as the container path), push notifications (a built package says which folder it is in), and the Logs tab
(the "show NAS paths" box, on by default; it only changes what is displayed: the log files, and the text
`/api/logs/<name>` returns without `?host=1`, stay exactly as written). A path in a folder with no host location is shown as the container path, tagged "in
container", so it is never mistaken for one on your NAS. The API keeps the container path in the field you know
and adds a `*_host` twin (`source_host`, `output_host`, `why_host`, ...) only when there is one.

The hint is also compared with what you configured: if the tail doesn't match, the folder gets a note. That
is only a hint, since overlay and other set-ups can add prefixes. A malformed variable (bad JSON,
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
  copied over the game), `modified_exec` (`image` or `original`), `skip_verify` (don't check the copy against the
  original archive: "keep this copy anyway"), `skip`. Usually set from the Library page.

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

## PS4 titles

A PS4 game (a `CUSA` title ID) usually comes as an archive that already holds ready-to-install packages: the base
game `.pkg`, often a small update or downgrade patch, sometimes a `DLC.zip` or DLC `.pkg` files. These aren't
converted. With `ps4_mode` = `copy` (the default, under Settings > Conversion) the planner shows the title as
**copy**, and a run copies the `.pkg` files as they are, never starting UltraPack and never changing a byte:

- Each package is named and classified from **its own header** (the content ID, the title and the version in its
  PARAM.SFO; game / patch / DLC), not from its file name. Only the first few MiB of each package are read.
- The folder and the base game's file name use the game's **English title**, like the PS5 side: the package's
  `TITLE_01` (English US), else `TITLE_18` (English UK); if it has none and its `TITLE` has letters of another script
  (an Asian release has a Chinese or Korean `TITLE`, sometimes with Latin words after it), the English title in the base game's file name next to
  the title ID (`<Title>-CUSA12345.v1.0[tag].pkg`), else the name of the folder the release was downloaded into,
  else `TITLE`. The title the package itself carries is shown in the Library ("Title in package: ...").
- A `.pkg` inside a nested archive (`DLC.zip`) is copied too. If it holds no `.pkg`, nothing is copied from it and the
  Library says so. Other files in the archive (`.url` files and the like) are not copied.
- Files go to `<output>/PS4/<title> [CUSA12345]/` through `<output>/.staging` and are renamed into place once 7-Zip
  has extracted them with its CRC check and the sizes match the listing. The base game is named
  `Title [CUSA12345] [v01.00].pkg`; patches and DLC keep their own names.
- Names are cleaned with **your** `junk_title_patterns` (and the text `junk_file_patterns` match), nothing is hard-coded.
- The title is "done" like a built one (same title and version, also after a restart) and is marked **PS4** in
  History; a newer package set is copied again, and nothing already in the folder is deleted (a file it replaces is
  kept as `... (replaced <time>).pkg`).
- A copy counts as **copied**, not built: the run summary says `built N, copied N, ...`, "This run" shows `copied`, and
  the notification is "PS4: copied" with the folder it went to.
- A failed copy is remembered and explained like any failed game (see below), and **Retry** copies it again.

`ps4_mode` = `skip` lists PS4 titles as "PS4: skipped" instead. A per-game **skip** override always wins over both.
PS4 game *folders* (an extracted game) are listed as "PS4 folder: not handled".

## Backing up

Everything lives in the config volume (`./config` in the example compose file). Under Settings, the **Storage &
backup** card says where each file is (the folder on your NAS when it's configured, with a copy button), lists
every per-game override (also for games that are no longer in the library) with a Remove button, and shows the
version of the app and of the bundled UltraPack.

**What to copy** (`config/state/`):

| File | What it is | If you lose it |
|---|---|---|
| `config.json` | your settings and the per-game overrides | back to the defaults; firmware, schedule and every per-game decision are gone |
| `ledger.json` | what was built (the build history) | the app thinks nothing was built and builds the whole library again |
| `failures.json` | games that failed or wait for space, and why | they're tried once more |
| `../patches/` | the patch folders your overrides point to (if you use any) | the overlay files are gone |

`ledger.json` and `failures.json` are **state**, not settings: an export doesn't contain them.

**What is safe to delete:** `ident_cache.json` and `listing_cache/` (they are rebuilt by the next scan, which is
then slower), `plan.json`, `status.json`, `last_run.json`, the `logs` folder, and `backups/` (the automatic
backups, below).

On the Settings page:

- **Export settings** saves the settings and overrides as one JSON file (`{format, app_version, exported_at,
  settings, overrides}`). It holds only what can be imported again: not the paths of UltraPack's files, not keys
  this version doesn't know. The notification URL may be a private topic, so there is a box to leave it out.
- **Import** checks the file with the same rules as the Settings form (a key it doesn't know, a wrong type or an
  out-of-range value is rejected, naming the key, and nothing is written), previews what would change ("3 settings
  change, 2 overrides added, 1 replaced"), and only then applies it, writing atomically. You choose settings,
  overrides or both. It only sets what the file says: settings and overrides that aren't in it, keys the app
  doesn't know and unknown fields of an override all stay. The folders inside the container (library, output, ...)
  are flagged in the preview: import them only if both installs mount the same folders.
- **Download full backup (.zip)** has `config.json` exactly as it is, `ledger.json`, the list of patch folders
  (names only, not their files) and a note on restoring; the failed-game records only if you tick the box. To restore it, stop the
  container, copy the files back into `config/state/` and start it again.
- **Automatic backups**: before the settings file is changed (a save, an import, a restore, removing an override),
  its previous content is copied to `config/state/backups/config-YYYYmmdd-HHMMSS.json`, and the newest 10 are kept.
  Reading the Dashboard or saving without changing anything never makes one. **Restore…** previews what a backup
  would change, then makes it current, after backing up the file it replaces. It also works when the current file
  can't be read.

## When a game fails

A failed game doesn't just vanish from "This run". It is remembered in `/config/state/failures.json` (so it
survives the next run and a container restart) and shown in three places: a **Needs attention** card on the
Dashboard, a red **failed** state in the Library (instead of looking like a game that was never tried), and the
History page (filter: Built / Failed). Each one says in words what happened, what you can do, and has the lines
of its log and a link to the build log:

| What happened | What you can do |
|---|---|
| The copy differs from the original archive (names up to 5 files) | Use the archive copy, keep this copy anyway, view the log |
| A backport and the console firmware isn't set | Set the console firmware, keep the backport, use the original |
| UltraPack stopped (the stage and its error lines are shown) | View the build log, Retry |
| The package failed validation (it stays in `/output/.staging`; its size is shown) | View the report, delete the staged package, Retry |
| The source is another title | Skip this source |
| Not enough room on the work drive | Nothing is wrong: it shows as **waiting for space** with the numbers and is retried automatically |

Preparing a big game copies hundreds of GiB, so a game that failed is **not** tried again on every run. A run
(scheduled or **Run now**) skips it, and says so in the run log (`skipped: failed on <date>, unchanged`), until
something it depends on changes: its source file (or the twin archive) is replaced, the options set for it
change, the console firmware or the junk patterns change, or the app is updated (an image update can fix what
failed). **Retry** always retries it, and so does "Build only this". A failure that repeats exactly the same way
is counted, not announced again: you get one notification per distinct failure.

## Updating

`docker compose pull && docker compose up -d`. Recreating the container stops a run that's in progress.
Already-finished packages are kept, so wait for the Dashboard to go idle first.

## Credits and license

The package building is all [PS5 UltraPack](https://github.com/knutwurst/ps5-ultrapack) by knutwurst and its
contributors, plus the projects it builds on (MkPFS, LibOrbisPkg / LibProsperoPkg, ampr-emu, 7-Zip). See
[NOTICE.md](NOTICE.md).

PS5 PKG Station is licensed under the [GNU GPL v3.0](LICENSE).
