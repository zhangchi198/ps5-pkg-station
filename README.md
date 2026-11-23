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
| Dashboard | the current run first (how far along, about how long it will take, each build's stage), run now (after a summary), stop (after the current games, or now), cancel a single game, pause the schedule, rescan; what is being prepared and what is next; the last run's result when idle; setup checklist; free space; why a scan or run failed |
| Library | every game found, the copy that will be used and why (each copy with its path, version and what became of it), whether it runs on your console's firmware, the backport decision, files that could not be identified; per-game options: copy to use, keep the backport or the original, extra files folder, never build, build just this one |
| History | **Runs**: each run (when, how it started, how long, what it built, failed, cancelled) opening to its games; **Packages**: every package with its size, times, checks and logs, searchable and sortable, with Rebuild, Forget and the replaced packages |
| Settings | in plain language, with a description and the default of every setting: console firmware, performance (games at once, threads, compression level, priority, free space), cleanup patterns with a test against your last scan, schedule, notifications, folders; mistakes are shown next to the field and nothing is saved until all of it is valid |
| Logs | a list grouped by what it is and by game, and a viewer with search, download, earlier lines, "only warnings and errors", collapsed progress lines, and follow |

### History

Every job a run finishes is written to `state/history.jsonl` as it happens: built, copied, failed, cancelled, and waiting for space,
with the run it belongs to. The file is only ever appended to, so nothing is lost when the next run comes; the ledger stays what the
planner reads (one entry per game, the latest package).

- **Runs** lists the runs newest first: when, scheduled or manual, how long, the counts. Opening one lists its games with the result,
  what happened (a failure in the words of the failure explanations), the time it took and a link to the build log. A run that was cut
  short says it did not finish. Runs from before this version aren't in it; their packages are under Packages.
- **Packages** has one row per game: size, "prepared in 12 min · built in 60 min · compression level 4 · 40 checks passed", where the
  package is (with a copy button), the source it came from, and **Build log**, **Validation report** (the log, opened at the validator's
  output) and **Copy SHA-256** (the whole hash). Entries written by any earlier version of the app are read as they are: what an entry
  does not say is shown as "not recorded", never guessed. Search by title, ID or file name; filter by built, PS4, file missing, rebuild
  requested, failed; sort by date, size, time or name; the heading has the total size and the time the library took.
- **Rebuild** marks a game for the next run (`state/rebuild.json`): it is planned as "rebuild requested" without deleting anything, and
  its package is replaced only after the new one is complete and checked. **Forget** removes the game's entry from the ledger (the
  package file stays) and can also set "never build this game"; it waits for a run in progress to end. Both ask first.
- **Replaced packages**: when a game is built again, its older package stays in the output folder as `... (replaced YYYYmmddHHMM).pkg`.
  They are listed with size and date and can be deleted one by one; Settings > Cleanup keeps all, the newest one, or none.

### Logs

`logs/` holds the engine's own log (`pkg_sync.log`: each scan, the plan, every game's steps; it is renamed `pkg_sync.<date>.log` when it
passes 16 MiB), the last scan, the raw output of each run (`run_<time>.stdout.log`, which repeats the activity log's lines) and one
UltraPack log per build attempt, `pkg/<time>_<TITLE ID>.log`. The Logs page lists them by kind, with the game's title, date, size and
outcome for builds, and finds them by title or ID. **Library** rows link to a game's logs (`#logs?game=<TITLE ID>`), History and the
Dashboard link to the log of a build (`#logs/pkg/<file>`), so a log has an address you can bookmark or send.

The viewer starts at the end (a log is read from its end, so a huge one opens at once), loads earlier lines on request or the whole file,
searches with highlights and next / previous, can show only warnings and errors, collapses runs of progress lines, wraps or not, and
**Download** gives the file exactly as it is on disk. **Follow** asks only for what was written since (`GET /api/logs/<name>?offset=N`
returns the bytes after N; `before=N` the lines before N; the headers `X-Log-Size`, `X-Log-Start`, `X-Log-Next` say where you are), so
the text you scrolled to stays where it is; it only follows the end if you are at the end.

Retention (Settings > Cleanup): build logs, run output and rotated activity logs older than *N* days are removed when a run starts, and
the oldest are removed while they take more than *N* GB (both 0 by default: nothing is removed). The log of a game that is still failing,
the engine's current log and the last scan are never removed, and nothing written to in the last hour. The Logs page shows how much room
the logs take and has a "Remove old logs now" button that says what it will remove first.

### Notifications

Settings > Notifications sends messages to an [ntfy](https://ntfy.sh) topic or to anything that takes a webhook. **Send test** posts a
test message with what is on the form now (saved or not) and shows the answer beside the field: "Delivered (HTTP 200)", or what went wrong
("HTTP 403 Forbidden", "Connection refused", "timed out").

| Setting | Default | Sends |
|---|---|---|
| When a run finishes | on | one summary: `Run finished: 4 built, 1 failed (Example Game: ...), 2 cancelled. 3 h 12 min. 1.3 TiB free in the output folder.` A run with nothing to do says nothing. |
| When something needs attention | on | a game that **failed for a reason it had not failed for before** (an unchanged failure that is tried again is not announced again), a game **waiting for room**, an **output folder or work drive nearly full** (said once, and again only after it has been fixed and is low again), a run that **could not start** (library not mounted, a failing setup check, another run holding the lock), a scheduled run **skipped** because another was active, a scheduled run that **ended badly**, a **settings file that can't be read**. High priority. |
| For every finished game | off | a message each time a game is built or copied. An install that already had an address before these settings existed keeps this on. |

The failure texts are the explanations of the Dashboard's failure rows. A **Link back** address (where this page is) is added to ntfy
messages so a tap opens the Dashboard, and sent as `url` to a webhook.

**ntfy** (the default) is a plain POST: the body is the message (UTF-8), the headers are `Title`, `Priority` (`high` for attention and
failures), `Tags` and, with a link back, `Click`. A title outside ASCII is sent as RFC 2047 (`=?UTF-8?B?...?=`), which ntfy decodes.

**Webhook** posts `Content-Type: application/json; charset=utf-8`, the same document for every event:

```json
{
  "schema": 1,
  "event": "summary",
  "title": "PS5 pkg: run finished",
  "message": "Run finished: 4 built, 1 failed (Example Game: ...). 3 h 12 min. 1.3 TiB free in the output folder.",
  "priority": "default",
  "url": "http://nas.local:8765",
  "time": "2026-11-22T06:12:44+0000",
  "games": [{"title_id": "PPSA01234", "title": "Example Game", "name": "Example Game [PPSA01234] [v01.004.000] [fw9.60].pkg",
             "status": "built", "output": "/output/Example Game ... .pkg", "bytes": 44250112000, "seconds": 3630}],
  "run": {"trigger": "scheduled", "seconds": 11520, "counts": {"built": 4, "copied": 0, "failed": 1, "cancelled": 0, "deferred": 0}, "free_bytes": 1428000000000}
}
```

`event` is one of `summary`, `built`, `copied`, `failed`, `attention`, `test`. `url` is `null` without a link back. `games` lists the
games the message is about (a game that failed, was built, ...; for a summary all of the run's) with `status` one of `built`, `copied`,
`failed`, `cancelled`, `deferred`, and, where they apply, `reason` (a failure in words), `output`, `bytes`, `seconds`. `run` is only in a
summary. Fields may be added in later versions; `schema` changes only if one is removed or changes meaning.

What became of the last message is kept (`state/notify_last.json`) and shown under the address ("Last delivery: ok, 2 min ago" / "failed:
HTTP 403 Forbidden"), every delivery is a line in `pkg_sync.log`, and a destination that does not work is a row in **Needs attention**. The
address is never written to the log, to an error message or to the status (it may hold a private topic or a token: errors are scrubbed of
it and of anything that looks like one), and "Export settings" can leave it out as before.

### Controlling a run

- **Run now** first shows what it would do: "Build 5 games (1.2 TiB of sources, about 6 h)", and how much room the work drive
  has. The time is the pipeline's estimate from earlier builds on this machine (sizes only until there has been one).
  "Don't ask me again" is kept in the browser only; a link on the Dashboard turns the question back on. **Build only this**
  (Library) asks the same way.
- **Stop…** offers two choices. *Finish current games, then stop* starts nothing new and lets what is running finish (the
  dialog says about how long), and **Resume** takes it back until the last game ends. *Stop now* asks once more, saying what is
  lost ("2 builds in progress will be discarded; 1 prepared copy is kept for next time"), then ends every UltraPack and
  7-Zip process the run started and removes their work folders.
- **Cancel** on a game that is being built or prepared ends just that game (the other builds go on); **Remove from this run**
  takes a queued game out. A cancelled game is recorded as `cancelled` in the run's results, never as a failure: no failure is
  remembered, no notification is sent, and the next run builds it again. A game that was already prepared keeps its prepared
  copy for the next run.
- **Pause schedule** is one click: no run starts by itself until you press **Resume schedule**, or until the optional "until"
  time (on the server's clock). The header says "schedule paused until Mon 08:00". A run that comes due while the schedule is
  paused is skipped (the log says so), not made up later.
- A button that can't be pressed says why ("A run is active", "A scan is running", the failing Setup items).

What a cancel removes: only the game's own work folder (under the work folder) and its folder or half-copied file in
`<output>/.staging`, through the same guarded delete as everything else. Source folders are only ever read, and a finished
package in the output folder is never touched: an older package of the same name is moved aside only when the new one is
complete, so a build that is cancelled, or fails, while it is copied leaves the older one where it was.

The web page and the engine talk through small files in the state folder: `stop.request` (`{"mode": "after_current" | "now"}`),
`cancel/<TITLE ID>.request`, and `schedule_pause.json`. The API: `POST /api/stop` (`{"mode": ...}`; `DELETE /api/stop` takes an
`after_current` request back), `POST /api/builds/<TITLE ID>/cancel`, `POST` / `DELETE /api/schedule/pause`.

### Progress and time estimates

The Dashboard leads with the run: when it started and whether it was scheduled or started by hand, how many games it
covers, how many are done or failed, a bar for the whole run and, when it can be said, "about 1 h 20 min left". Below
it: each build with its stage in plain words (UltraPack's own text is in the tooltip), elapsed time and time left;
what is being prepared or copied (bytes, percentage, rate); what is next; and the results. When nothing runs it shows
the last run with its date and result, and the next scheduled time ("today 14:00" / "tomorrow 03:00", on the server's
clock). The counts and the Library follow the run: a game that has been built since the scan is "done" at once. Folders are one line ("Library 40 games · 5 to build · Folders ok"); it turns red and moves to the top only when a folder is missing, read-only, or too small for the biggest game still to build (the rule of the Setup card, not a percentage).

The estimates are not guesses about speed. A build's time left comes from its own UltraPack log (its progress and
rate so far) and, for the stages after the current one, from how long each stage took relative to the others in
earlier builds on this machine (plain defaults until there is such history: the first stage, compressing, is most of a
build; the same shares weigh the bar). It says "estimating…" for the first two minutes and whenever there is no rate yet, is always an "about",
and moves smoothly rather than jumping. The time for the whole run adds what is still queued, at the pace this run
has shown (or earlier builds), with the same limits on parallel builds as the engine (`max_builds`,
`max_compress`). `status.json` carries the data (start time, trigger, totals, per build the stage timings); each
finished build adds its stage times, source size and finish time to `ledger.json`.

There is no login unless you set a password. Keep port 8765 on your LAN or behind a VPN such as Tailscale, and
don't forward it to the internet. See [Keeping it private](#keeping-it-private).

### The Library: why this copy

Each game has a **Why this copy · Options** panel. It lists every copy found with its full path, kind, version and
the firmware it asks for, and says what became of each: used (and why: the newest version, the faster kind, your
choice), an older version, an archive that checks the image, a packed copy that is passed over, or not used because you
chose another. Under the title you see the firmware the game needs next to your console's (Settings), and a red tag when
it is higher. If an image and its original archive ask for different firmware (a backport) the panel says which will be
used for your console and who decided; the files are compared when the build starts, and what it did is shown afterwards
(Library and History). Files a scan looked at but could not identify are listed under "Not recognised", with the error.

Saving options plans again by itself (a second or so with the scan cache warm); during a run it says the change
applies from the next run and the list updates when the run ends. Typing in the filter does not reload anything, and an
open panel or an unsaved edit stays. **Build only this** asks first, with the size and, once a build has finished on this
machine, about how long it takes.

### Packed copies (LZ4 / Zstd)

Some dumps store the game's AMPR assets compressed (LZ4 or Zstd packs). UltraPack builds from unpacked files, so the app
passes over such a copy when another copy of the game exists, and a game that only has packed copies is listed as
**skipped**. To build it, use an unpacked copy of the game (an image or a folder from the original dump), then scan again.

### On a phone, with a keyboard, with a screen reader

The page is made for a phone as much as for a desk.

- **Phones (under 720 px)**: the tabs are a bar at the bottom of the screen (icon and name), the header is one line, and every table becomes a list of
  cards with the column names above the values. Long names wrap, and a game's ID and version stay together on one line. Every button, field, checkbox and
  link you press is at least 44 x 44 px (also on a tablet held upright, and on any touch screen).
- **Colours**: the page follows the phone's or computer's light/dark setting. The button in the header (half-filled circle) switches between "follow the system",
  light and dark; the choice is remembered in that browser only. All text is at least 4.5:1 against its background and the edges of fields and buttons at least 3:1
  in both schemes. A status is never told by colour alone: each tag starts with its own mark (a tick, a cross, ...).
- **Links to a place**: the address names the tab, so a reload stays on it and Back goes to the tab you came from:
  `#dashboard`, `#library`, `#library/PPSA01234` (opens that game's row), `#history`, `#history/packages`, `#settings`, `#logs`, `#logs/pkg/<file>`,
  `#logs?game=PPSA01234`. Leaving Settings with unsaved changes still asks first, whichever way you leave.
- **Keyboard**: Tab reaches "Skip to the content" first. On the tab bar the arrow keys, Home and End move between the tabs, and Enter or Space opens the one in focus.
  The questions the page asks (start a run, stop now, cancel a game) are dialogs that keep the focus inside, leave the page behind them unreachable, start on the safe
  answer when something would be lost, and give the focus back to the button you pressed when they close. Escape closes them.
- **Screen readers**: the tabs are announced as tabs with the selected one marked; the run's state (Idle, Running, Stopping) and the messages ("Run started", "Scanning...")
  are announced when they change; every progress bar has a name ("Build progress: <game>") and its value; every field has a label; times show the exact moment when
  pointed at (they are on the server's clock, like the schedule).
- **Selecting text**: the Dashboard refreshes every 5 seconds by changing only what changed, so text you have selected, a button in focus and a section you opened
  stay as they are.
- **On the home screen**: the page has a web app manifest and icons, so a phone can add it to the home screen and open it like an app (it still needs the network and
  the same login, if there is one).

Contrast, as measured (WCAG ratios; 4.5 is the target for text, 3 for the edge of a control):

| | before | after |
|---|---|---|
| primary button, dark scheme (white on light blue, now dark text) | 2.54 | 7.36 |
| danger button, dark scheme | 2.77 | 6.95 |
| primary button, light scheme | 5.17 | 6.70 |
| secondary text on the selected log row, light scheme | 3.96 | 5.32 |
| "built" tag, light scheme | 4.11 | 6.49 |
| edge of a button or field, light / dark | 1.24 / 1.23 | 4.83 / 3.94 |
| a disabled "Run now", light / dark | 2.42 / 1.63 | 5.29 / 6.10 |

The colour pairs are written as tokens at the top of `app/web/static/index.html`, and `tests/test_server_a11y.py` measures every pair of both schemes from them.

### Keeping it private

The page and the API are open to anyone who can reach the port, so the app does a few things to make "on my LAN"
mean what it says:

- **Other websites can't use it.** A page you have open in your browser can't start, stop or change anything:
  every request that changes something (POST, PUT, DELETE) must come from this page (its `Origin`, or else its
  `Referer`, must be the address you opened it at) and must be sent as `Content-Type: application/json`. No CORS
  headers are sent. Opening the app at an IP address, `localhost`, `http://<host>.local:8765`, a single-word name
  or a Tailscale name works as it is.
- **Only your own host names are answered.** The `Host` header must be an IP address, `localhost`, a name without a
  dot, or end in `.local`, `.lan`, `.home.arpa`, `.ts.net` or `.internal`. This stops DNS rebinding (a website whose
  name is pointed at your NAS). A request for another name gets a page saying so. To use a name of your own,
  for example behind a reverse proxy, list it in the `PS5PKG_ALLOWED_HOSTS` environment variable
  (comma separated; `*.example.com` allows any subdomain).
- **Scripts need one header.** `curl -X POST -H 'Content-Type: application/json' http://<host>:8765/api/scan` works;
  without the header the request is refused with 415.
- **Text from game files is never trusted.** A game's title ID must be `PPSA` or `CUSA` and five digits, or the
  source is not recognised (it is logged and left out). Everything the page shows from game files, file names, log
  lines and messages is escaped, there is no inline script or event handler, and a Content-Security-Policy allows
  scripts from the app itself only. The page can't be put in a frame.
- **Folders and addresses.** A game's patch folder must be inside `<config>/patches` (`/config/patches` in the
  container), because its files are copied into the game that is built; a value already in `config.json` keeps
  working. The notification URL must start with `http://` or `https://`.
- **A warning when it looks open.** If a request comes from a public address, or carries the headers of a reverse
  proxy (`X-Forwarded-For`, `Forwarded`, ...), and there is no password, the page shows a red banner. If something
  in front of the app already asks for a login, set `PS5PKG_EXPOSURE_WARNING=0` to hide it. The footer of every page
  says there is no login.
- **An optional password.** Set `PS5PKG_PASSWORD` in the container's environment and the page asks for it; the
  session is a signed cookie (`HttpOnly`, `SameSite=Strict`, `Secure` over HTTPS, 30 days). Everything under `/api/`
  except `/api/health` (the Docker health check) then needs it. Wrong passwords are slowed down and limited. Changing
  the password ends every session. The password is sent as typed, so use it with HTTPS or a VPN if the network is not
  yours; on a home LAN it keeps out the neighbours and the guests, not a determined attacker.

```yaml
    environment:
      PS5PKG_PASSWORD: "something long"          # optional: ask for a password
      PS5PKG_ALLOWED_HOSTS: ps5.example.com      # optional: names of your own (reverse proxy)
```

Behind a reverse proxy, pass the original `Host` header (`proxy_set_header Host $host;` in nginx; Caddy and
Traefik do it by default). If your proxy rewrites it, list the public name in `PS5PKG_ALLOWED_HOSTS`: an `Origin`
from a listed name is accepted too.

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
- `compression_level`: UltraPack's Kraken level, 1 (fastest) to 9 (smallest). 4 is fast; 7 gives somewhat smaller packages.
- `max_builds` / `max_compress` / `cpu_count`: how many games are in progress at once, how many of them compress
  at once (never more than `max_builds`), and how many threads each one uses. Settings shows how many threads the
  machine has and warns when games compressing at once times threads per game is more; the warning can be saved
  anyway.
- `junk_file_patterns` / `junk_title_patterns`: regexes for release-group watermarks. Matching files are
  deleted from the work copy, and matching text is cut out of the title in `param.json` and the file
  name. Both are empty by default. On the Settings page they are one pattern per line (so a pattern can contain a
  comma, like `\d{1,3}`), a pattern that is not a valid regex is refused with the compile error and its line, and
  **Test against the last scan** lists the titles, or the file names inside the archives the scans listed, that the
  patterns would match.

### When a change applies

Build settings (compression level, parallel builds, threads, priority, free space, console firmware, schedule,
notifications) are read when a run starts: a change applies **from the next run**, not to the games already in
progress. Saving during a run says so ("Saved. Applies to the next run.").

A few settings decide what the **Library** shows, so they need a rescan: the library, downloads and output folders,
`ps4_mode`, `skip_download_prefixes` and the two junk-pattern lists. The scan records the values it used next to the
plan (`state/plan_settings.json`); when they differ from the saved settings, Settings and the Library say which
changed, with a **Rescan now** button. A plan made by an older version of the app (one without that record) is shown
as "rescan to refresh" and its firmware and backport fields are not judged until it is refreshed.
- `overrides`: per title ID, `source`, `patch` (a folder, for example under `/config/patches`, whose files are
  copied over the game; it must be inside `/config/patches`), `modified_exec` (`image` or `original`), `skip_verify` (don't check the copy against the
  original archive: "keep this copy anyway"), `skip`. Usually set from the Library page. A patch that contains
  `sce_sys/param.json` (the usual way to lower `requiredSystemSoftwareVersion` for an older console) names the package:
  the `[fwNN.NN]` tag, the version and the title come from that file, in the Library, the file name, the history and
  the notification. A package built before that, under the old name, still counts as done and is not renamed.

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
converted. With `ps4_mode` = `copy` (the default, under Settings > Console) the planner shows the title as
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
| `history.jsonl` | what every run did, job by job | the Runs list starts empty again; packages and the planner are not affected |
| `../patches/` | the patch folders your overrides point to (if you use any) | the overlay files are gone |

`ledger.json` and `failures.json` are **state**, not settings: an export doesn't contain them. The full backup (.zip)
includes `ledger.json`; `failures.json` only if you tick the box for it.

**What is safe to delete:** `ident_cache.json` and `listing_cache/` (they are rebuilt by the next scan, which is
then slower), `plan.json` and `plan_settings.json` (the Library then asks for a rescan), `schedule_pause.json` (deleting it resumes the schedule), `status.json`, `last_run.json`, the `logs` folder, and `backups/` (the automatic
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
Already-finished packages are kept, so wait for the Dashboard to go idle first, or press **Stop now** (Dashboard > Stop…) and let it
end the builds cleanly: a `docker stop` leaves the build's work folder for the next run to clear.

## Credits and license

The package building is all [PS5 UltraPack](https://github.com/knutwurst/ps5-ultrapack) by knutwurst and its
contributors, plus the projects it builds on (MkPFS, LibOrbisPkg / LibProsperoPkg, ampr-emu, 7-Zip). See
[NOTICE.md](NOTICE.md).

PS5 PKG Station is licensed under the [GNU GPL v3.0](LICENSE).
