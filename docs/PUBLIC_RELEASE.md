# Publishing the app

How the public build, the hosted backend, the website and the updater fit
together, and the steps that only the operator (you) can do.

## The pieces

| Piece | Where | What it does |
|---|---|---|
| Public app | `electron/` built by electron-builder | Installed app. Never runs a backend; talks to the hosted one. Hides Events and Scheduling and every control that scrapes or rewrites stored runs. |
| Backend access layer | `backend/services/public_access.py` | Requests from this machine (or with the admin token) get everything; everyone else gets the read-only surface only, rate limited. |
| Website | `site/` → GitHub Pages | Landing page with the download button, plus `api.json`, which tells installed apps where the backend lives. |
| Release workflow | `.github/workflows/release.yml` | On a `v*` tag: builds the NSIS installer and publishes it and `latest.yml` to a GitHub Release. |
| Updater | electron-updater in `electron/main.js` | Installed apps check the GitHub Release feed on start and every six hours, download in the background, and prompt to restart. |
| Tunnel | `scripts/install-tunnel.ps1` | Exposes the local backend as `https://api.<your-domain>` through Cloudflare, no port forwarding. |

## One-time setup

1. **GitHub Pages**: repository Settings → Pages → Source: *GitHub Actions*. The
   `Website` workflow deploys `site/` on every push to `main` that touches it.
   The site lives at `https://csfantasy.co.uk/` (custom domain set in Settings → Pages; `site/CNAME` carries it; the github.io address redirects there).
2. **Domain + Cloudflare**: add your domain to Cloudflare (free plan), then run
   `scripts/install-tunnel.ps1 -Hostname api.your-domain.com` in an elevated
   PowerShell. It installs `cloudflared` as a service that forwards the
   hostname to the backend on this machine. When the browser asks which zone
   to authorise, pick the domain that owns the hostname. The script is safe to
   re-run. The service uses `C:\ProgramData\HLTVFantasy\cloudflared\config.yml`
   and logs to `C:\ProgramData\HLTVFantasy\logs\cloudflared.log`; "Registered
   tunnel connection" in that log means it is up. For a quick test without a
   domain, `scripts/quick-tunnel.ps1` prints a temporary `trycloudflare.com` URL.

   Live since 2026-09-16: `https://api.csfantasy.co.uk` (zone csfantasy.co.uk,
   tunnel `hltv-fantasy`).
3. **Point the apps at the backend**: put the public URL in `site/api.json`
   (`apiBase`) and in `electron/public-config.json` (the bundled fallback),
   commit, push. Installed apps read `api.json` at every start, so the backend
   can move later without a new release.
4. **Code signing (strongly recommended before a wide release)**: without a
   certificate Windows shows a SmartScreen notice on first run, and PCs with
   Smart App Control switched on (the default on many new Windows 11 installs,
   including this one) refuse to run the unsigned installer at all, with
   "An Application Control policy has blocked this file". An OV or EV
   code-signing certificate (Azure Trusted Signing is the cheapest route,
   about $10/month) fixes both. To sign, add `win.certificateFile` /
   `certificatePassword` (or the Trusted Signing settings) to the `build`
   block in `electron/package.json` and the secrets to the workflow.

## Releasing a version

1. Bump `version` in `electron/package.json` (`cd electron && npm version 0.1.4 --no-git-tag-version`).
2. Commit and push `main`.

That is all. The `Release` workflow runs on every push to `main`; when the
package version has no release yet it builds `CS-Fantasy-Toolkit-Setup.exe`
and `latest.yml`, creates the `v<version>` tag and the release, and attaches
the files (one release per tag, created by the workflow's upload step; never
create it by hand first). Pushes where the version is already released do
nothing. The download button always points at
`releases/latest/download/CS-Fantasy-Toolkit-Setup.exe`, and installed apps
pick the update up at their next check (every 30 minutes, on window focus,
or 10 s after launch) and offer a restart.

If a tag ever ends up with two releases, the download URL breaks: delete both
releases and push a new version.

To build locally instead: `cd electron && npm run dist` (installer in
`electron/release/`), or `npm run dist:dir` for an unpacked folder. Use Node
22+ (nvm on this PC has v24.13.1); the old 20.18 fails at the blockmap step.

The installer is one-click (no pages, a small progress dialog with the app
icon, installs per user, launches the app when done). Updates install
silently and relaunch the app; the user only ever sees the "Update ready"
prompt.

## Code signing

Windows 11 PCs with Smart App Control on refuse to run an unsigned installer
unless Microsoft's cloud already knows the file, which for a new build it never
does. Any signature from a certificate authority in Microsoft's Trusted Root
Program satisfies it, so releases are signed with a Certum Standard Code Signing
certificate (cloud/SimplySign edition) issued in the operator's own name. The
private key never leaves Certum; the build asks Certum to sign over HTTPS.
Certificates last at most 459 days, so this is renewed yearly; signatures made
while it was valid stay valid because every one carries a timestamp.

**How a build gets signed.** `electron/scripts/sign-windows.cjs` is the
electron-builder signing hook (`win.signtoolOptions.sign` in
`electron/package.json`). electron-builder hands it the app exe, the NSIS
helper `elevate.exe`, the uninstaller and the installer in turn (four signings
per release); it runs `ssign`
(https://github.com/Le-Syl21/ssign) on each, SHA-256 with an RFC 3161 timestamp
from time.certum.pl. Credentials are read from the environment only:

| Variable | Meaning |
| --- | --- |
| `CERTUM_EMAIL` | the SimplySign login (Certum store account e-mail) |
| `CERTUM_OTP` | the TOTP seed behind the SimplySign QR code (`otpauth://...` text or the base32 secret) |
| `CERTUM_TOKEN` | alternative to the seed for a one-off signing by hand: a current 6-digit code |
| `SSIGN_PATH` | optional path to the ssign executable (default: found on PATH) |

Without credentials the build is unsigned and prints
`sign-windows: no Certum credentials...` for each file, so local builds keep
working. With credentials, any signing failure fails the build.

In `.github/workflows/release.yml` the two secrets are exposed as job
environment variables. When `CERTUM_OTP` is set the workflow downloads ssign
v0.1.6 (pinned by SHA-256), builds, then runs `Get-AuthenticodeSignature` on
the installer and refuses to publish unless the status is `Valid` and a
timestamp is present. When the secrets are absent those two steps are skipped.

**Setting it up (once).**

1. Buy Standard Code Signing, cloud edition, at https://shop.certum.eu/code-signing.html
   with a personal e-mail you will keep. Activate it in the store under
   "Data security products": name exactly as on your ID, home address, then
   "automatic identity verification" (ID photo + selfie) and a recent utility
   bill. Allow a few working days.
2. When Certum sends the SimplySign activation QR code, scan it with the
   phone's plain camera first and keep the `otpauth://totp/...` text somewhere
   private: that is the seed. Then pair the SimplySign mobile app with it as
   Certum instructs. Whoever holds the seed and the e-mail can sign as you.
3. GitHub, repository Settings, Secrets and variables, Actions: add
   `CERTUM_EMAIL` and `CERTUM_OTP` (the whole `otpauth://` text).
4. Put the certificate's exact subject name in
   `win.signtoolOptions.publisherName` in `electron/package.json`. electron-updater
   checks downloaded installers against that name, so it must match to the letter.
   Installed builds from before this setting have no name recorded and accept the
   first signed update without it.
5. Bump the version and push. The workflow log shows `sign-windows: signing`
   four times and the signature check prints the signer.

**Signing something by hand** (for example a one-off test build):

    set CERTUM_EMAIL=you@example.com
    set CERTUM_TOKEN=123456        (current code from the SimplySign app)
    ssign "release\CS-Fantasy-Toolkit-Setup.exe"

**If the seed leaks**, re-pair the SimplySign mobile app from your Certum
account (a new QR code invalidates the old seed) and replace the `CERTUM_OTP`
secret. Certum support can revoke the certificate if it was actually misused.

## Running the public build against your own machine

`cd electron && npm run public` starts the checkout in public mode: no local
backend is spawned, the API base comes from `api.json` (or `HLTV_API_BASE`),
and the backend treats it as a public client. `?public` on the Vite dev URL
previews the trimmed renderer.

## Operator access from elsewhere

Public callers reach read-only `GET` data under `/players`, `/teams`,
`/events` and `/assets` (everything the Database tab and the modals read),
the stored Tournament results, and the Top 5 queries. `PUBLIC_GET_DENY` in
`backend/services/public_access.py` keeps the exceptions operator-only: routes
that fetch from HLTV live, model training, page snapshots and job status. New
read-only endpoints under those prefixes are public automatically; anything
that writes or scrapes must be `POST` or go on the deny list.

To use the full API from another machine send `X-Admin-Token: <token>`; the
token is `HLTV_ADMIN_TOKEN` if set, else `.runtime/admin-token.txt` (created
on first start). Limits per public client: heavy queries
`HLTV_PUBLIC_RATE_LIMIT` per minute (default 60) with
`HLTV_PUBLIC_MAX_CONCURRENT` running at once (default 3); everything else
except assets `HLTV_PUBLIC_LIGHT_RATE_LIMIT` per minute (default 600).

## Forcing an update, posting a notice

`GET /public/config` carries `min_client_version` and `message`. To change
either without restarting the backend, write `.runtime/public-config.json`:

```json
{"min_client_version": "0.1.2", "message": "Maintenance tonight 22:00 UK"}
```

An installed app older than `min_client_version` shows an "Update required"
screen (with Check for updates and a website link) instead of the tabs; a
non-empty `message` shows as a notice bar above the tabs. The app probes
`/public/config` every 30 s, so changes take effect within a minute, and the
same probe drives the "Can't reach the server" screen during an outage.
Delete the file to go back to the defaults.

## Which event the public app shows

Public users pick any imported event from the Event dropdown on the
Tournament page (or View on the Events tab); the choice is remembered per
install and defaults to the server's current event. Stored runs (groups,
playoff, Swiss) are keyed by fantasy event, and every read endpoint takes
`?event_id=` to serve another event's run. Runs started from the operator app
always target the active event.

Valuations are frozen once an event starts: the nightly bake refreshes a
groups event only until its first match, and playoff runs only change when
you press Run Combinations. Playoff Top 5 queries are cached and warmed at
startup and after each run.

## The event behaviour contract

One lifecycle for every format, implemented in
`backend/services/event_pipeline.py`; formats plug into it and nothing else
decides when valuations change:

1. **Imported:** baked straight away. Draw or bracket read from the event
   page, every outcome enumerated, roster combinations stored, Top 5 caches
   warmed. If the draw or bracket is not published yet the event is
   *pending* and the nightly run retries it.
2. **Until its first match:** refreshed by the nightly run with that night's
   ratings, rankings, roles and boosters.
3. **From its first match:** frozen. Nothing automatic touches it again.
   An event is *active* until it has finished (its last day plus a day's
   grace); every active event gets steps 1 to 3 each night, finished events
   are archive and never touched.
4. **Stored per event.** Another event's run never replaces it.
5. **Run buttons in the operator app are manual overrides,** allowed at any
   time.
6. **Each public user has their own active event**, chosen with the same Set
   Active button as the operator (Events tab) or the Active event dropdown on
   the Tournament page, remembered on their machine and defaulting to the
   operator's active event. They read exactly the stored run.

Groups and playoff (single and double elimination) events are automated
today. Swiss and Bounty events are registered as *manual*: the nightly run
reports them as such instead of silently skipping, and they are run from the
operator app once per event until their bakers are written. The Events tab's
Valuation column shows each event's state (published and refreshing, frozen,
pending, manual). `POST /groups/bake?event_id=` re-bakes any event by hand.

## Uptime alerts

Two free checks cover the whole chain:

1. **Is the API reachable?** UptimeRobot (free): New Monitor, type *Keyword*,
   URL `https://api.csfantasy.co.uk/health`, keyword `hltv-fantasy`, interval
   5 minutes, alert contact your email. Fires when the tunnel, the backend or
   this PC is down.
2. **Did the nightly run happen and succeed?** healthchecks.io (free): Add
   Check, name "CS Fantasy nightly", period 1 day, grace 3 hours, then copy
   its ping URL into `.runtime/heartbeat.json`:

   ```json
   {"url": "https://hc-ping.com/<your-uuid>"}
   ```

   The scheduler pings `/start` when the batch begins, the URL itself when
   every task finished without an error, and `/fail` otherwise. No restart
   is needed; the file is read at each batch. `HLTV_HEARTBEAT_URL` in the
   environment works too. Test with `POST /schedule/heartbeat-test`.

## Database backup

The last step of every scheduler batch backs up into the folder in
`.runtime/backup.json`, as consistent online-backup snapshots:

* `fantasy_players.db` every night, gzip-compressed (2 GB -> about 180 MB),
  keeping the last `keep_days` copies (default 7, about 1.3 GB).
* `page_snapshots.db` (archived HLTV pages) once a week, stored as-is because
  its pages are already compressed inside the file, keeping `snapshots_keep`
  copies (default 1, about 2 GB).

```json
{"dir": "C:/Users/you/OneDrive/CS Fantasy Backups", "keep_days": 7,
 "snapshots_every_days": 7, "snapshots_keep": 1}
```

OneDrive uploads whatever lands in that folder. The Scheduling tab's run
history shows a `backup` row per night; a failed copy is an error and trips
the heartbeat. Run one by hand with `POST /schedule/run-now {"task": "backup"}`.
Restore = stop the backend, gunzip the file over the database, start it.

## Accounts (sign in with Google)

The public app signs users in with Google; the operator app on this PC never
needs to. Sessions are 90-day bearer tokens stored hashed in the database and
kept encrypted on the user's machine; every public data route requires one,
except the health check, the public config, the sign-in routes and images.
Enforcement switches on automatically once a Google client is configured, so
the order is: ship an app version that can sign in, then add the credentials.

Setup (Google Cloud Console, once):

1. Create a project (e.g. "CS Fantasy Toolkit").
2. APIs & Services, OAuth consent screen: External, app name, support email,
   developer email. Scopes: email, profile, openid (non-sensitive, no
   verification needed). Publish the app (Testing mode only admits listed
   test users).
3. Credentials, Create credentials, OAuth client ID, type **Web application**,
   Authorised redirect URI `https://api.csfantasy.co.uk/auth/google/callback`.
4. Put the client ID and secret in `.runtime/google-oauth.json`:

   ```json
   {"client_id": "....apps.googleusercontent.com", "client_secret": "...",
    "redirect_base": "https://api.csfantasy.co.uk"}
   ```

   No restart needed; `/public/config` then reports `auth.required: true`
   and the app shows its sign-in screen.

Admins: an account with `is_admin` set is treated as the operator wherever
it signs in from: the installed app shows every operator tab (Events actions,
Scheduling, Users) and the API accepts operator routes from its
session, with no rate limits. Grant or revoke on the Users tab (an admin
cannot demote themselves there) or with
`POST /admin/users/{id}/admin {"is_admin": true}`. The Users tab lists every
account with last seen, first sign-in and active sessions.

Flow: the app opens the browser at `/auth/google/start` with a random state
and a challenge, the server does the Google exchange and verifies the ID
token, the app collects its session with `/auth/poll` (one-shot, verifier
checked). `/auth/me`, `/auth/signout`. Users are in the `users` table.

