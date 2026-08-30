# Archer Self-Hosting Migration — Handoff

Moving Archer's backend from HuggingFace Spaces to a home server, solving four real, already-documented problems: the Cloudflare-blocking-price-checker issue (datacenter IP → residential IP fixes this), Ollama re-downloading the model on every restart (HuggingFace containers don't persist storage; a home server does), the 60-day GitHub Actions auto-disable risk, and free-tier CPU timeouts that made even small local models struggle.

## The core architecture decision: private and public traffic are handled completely differently

This is the piece that makes self-hosting safe instead of scary. Archer has two genuinely different audiences:

**Private (family — phone, watch, Roku):** routed through **Tailscale**, a private mesh network between specifically-approved devices. No ports opened to the public internet, no exposed attack surface, no static IP needed. Devices connect as if they're on the home network, from anywhere.

**Public (the fan page, meant for strangers by design):** this one genuinely needs to stay internet-reachable, so it gets the traditional treatment — **Caddy** as a reverse proxy (automatic HTTPS via Let's Encrypt, far less config than nginx+certbot) plus **Dynamic DNS** to handle a home IP that changes periodically.

Treating these the same way is what makes self-hosting feel like it requires hardening a whole server against the internet. Splitting them means the private, more-important side barely needs traditional security work at all.

Both Caddy blocks proxy to the **same** archer.py process and port — there's only one backend. Tier separation (owner / passenger / family / valet) happens entirely inside archer.py itself via `get_request_tier()` (MAC whitelist, signed JWT cookie, or owner PIN), the exact same way it already works on the public HuggingFace Space today with no network-level gating at all. See "Real open questions" below for what this changed from the original draft.

## Setup steps, in order

**0. Create a dedicated `archer` system user before anything else.**
Running archer.py as your own login works, but `/etc/archer` holds the HSM
key (`master.key`) and `archer.env` secrets — files that should only be
readable by the process that needs them, not by every process running as
you. archer-os (the in-truck image) already solved this with a dedicated
`archer` user and a `root:archer`, mode `1770` directory; apply the same
pattern here from the start instead of hitting the permission error later
(archer.py's own env-file loader comment calls this out: a secrets file
root drops in with the default `0600` is unreadable by a non-owning user
even inside a `1770` directory — the *file* itself also needs `chown
root:archer` + a group-readable mode):
```
sudo useradd -r -m -d /opt/archer -s /usr/sbin/nologin archer
sudo mkdir -p /etc/archer
sudo chown root:archer /etc/archer
sudo chmod 1770 /etc/archer

# Clone/copy the repo to /opt/archer, owned by the new user:
sudo git clone <this-repo> /opt/archer   # or copy an existing checkout
sudo chown -R archer:archer /opt/archer

# archer.env holds secrets — created by root, group-readable by archer:
sudo cp /opt/archer/archer.env.example /etc/archer/archer.env
sudo nano /etc/archer/archer.env          # fill in real values
sudo chown root:archer /etc/archer/archer.env
sudo chmod 640 /etc/archer/archer.env
```
archer.py loads `/etc/archer/archer.env` itself on startup (no systemd
`EnvironmentFile=` needed) — see the `_load_env_file()` comment near the
top of archer.py for the exact permission failure mode this avoids.

**1. Install Tailscale on the server:**
```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```
Follow the printed URL to authenticate. Then install the Tailscale app on the phone, watch, and any other family device that needs private access — each one just needs to join the same Tailnet.

**2. Install Caddy:**
```
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

**3. Fill in and deploy the Caddyfile** (in this folder) — replace every `<placeholder>` with real values once the Tailnet name and public domain are known. Generate basic-auth password hashes with `caddy hash-password`. Deploy with:
```
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

**4. Install and enable the systemd service** (`archer.service`, in this folder) so archer.py survives reboots and crashes automatically. It already assumes step 0's layout (`archer` user, `/opt/archer`) — only touch `User=`/`WorkingDirectory=`/`ExecStart=` if you installed somewhere else:
```
sudo cp archer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable archer
sudo systemctl start archer
# Check it's actually running:
journalctl -u archer -f
```

**5. Set up Dynamic DNS** for the public fan page only (the private side doesn't need this — Tailscale handles its own addressing). Specific steps depend on the router/ISP; most home routers have built-in DDNS client support for common providers.

**6. Everyone signs in at the same URL.** Once steps 1-4 are done, every family member visits the same `<machine>.<tailnet>.ts.net` address (past Caddy's basic-auth prompt) and archer.py routes each of them to their own tier's dashboard automatically based on their registered device/JWT — no separate per-tier URLs to hand out.

## Discord bot setup (legacy — superseded by Slack below, code left in place)

Ported to Slack per a stated preference for a more professional platform —
see "Slack bot setup" below, which is now the live/intended integration.
This section and the Discord code in archer.py (`discord_config`,
`discord_alert`, `/discord/interactions`, etc.) are untouched and still
work if you set `discord_config['enabled'] = True`; nothing here was
deleted, just superseded. Originally, this replaced the earlier Mattermost
self-hosting idea, which needed more RAM than this 8GB box has to spare.
Archer's Discord integration is two independent pieces:

- **Outbound alerts** (`discord_alert`/`discord_send` in archer.py) — plain
  webhooks, no bot required. Create a webhook per channel (Channel Settings
  -> Integrations -> Webhooks) and set the URLs with `set_discord_webhook()`
  or directly in `discord_config`.
- **Inbound slash commands + buttons** (`/discord/interactions` in
  archer.py) — needs a real Discord app/bot, covered below.

**a. Create the app:** [discord.com/developers/applications](https://discord.com/developers/applications)
-> New Application. Under **General Information**, copy the **Public Key**
and **Application ID**. Under **Bot**, create a bot and copy its **Token**
(only needed for command registration, not at runtime — see below).

**b. Invite the bot to your server:** OAuth2 -> URL Generator -> scopes
`bot` + `applications.commands` -> minimal permissions (Send Messages,
Use Application Commands) -> open the generated URL and add it to your
server. Copy the **alerts channel's ID** (right-click it -> Copy Channel
ID; enable Developer Mode in Discord settings first) and **your own user
ID** the same way.

**c. Fill in `/etc/archer/archer.env`** (from step 0) with `DISCORD_BOT_TOKEN`,
`DISCORD_PUBLIC_KEY`, `DISCORD_APPLICATION_ID`, `DISCORD_OWNER_ID`,
`DISCORD_ALERTS_CHANNEL_ID`, and set `discord_config['enabled'] = True`
(or wire a route/PIN command to flip it — it defaults off).

**d. Register the slash commands** (one-time, or whenever the command list
in `discord_register_commands.py` changes):
```
DISCORD_BOT_TOKEN=<token> DISCORD_APPLICATION_ID=<app-id> python3 discord_register_commands.py
```

**e. Set the Interactions Endpoint URL** in the Discord app's General
Information page to `https://<your-public-domain.com>/discord/interactions`
— the **public** Caddy block, not the Tailscale one, since Discord's
servers need to reach it from the open internet. Discord sends a signed
PING to verify the endpoint the moment you save this field, so archer.py
must already be running with `DISCORD_PUBLIC_KEY` set before you save it,
or Discord will reject the URL.

**f. Restart the service** so the new env vars load:
```
sudo systemctl restart archer
```

**Not yet verified against a live bot** — this was built and reviewed
against Discord's documented interaction contract, but never round-tripped
against an actual Discord server (no network path to Discord's API from
the environment this was built in). Before relying on it: register the
commands, save the Interactions Endpoint URL, and confirm `/status` and a
crash-alert button both actually work end-to-end.

## Slack bot setup (slash commands, alert buttons, digests, tiered channels)

The live integration — same feature set as the Discord build above, ported
to Slack's actual mechanisms (HMAC-SHA256 signing instead of Ed25519,
Block Kit instead of embeds, `response_url` instead of interaction-token
webhook edits), plus a real structural addition Discord's build never had:
**content is routed to one of three channels by Archer's existing tier
system**, not broadcast to one flat channel.

**Why three channels, and why stop at three:** Tier 1 (Owner) gets
everything — every alert, full `/vstatus` and `/digest` output. Tier 2
(Passenger) gets a real subset: alerts and command output relevant to
someone riding along (parking status, request-system activity, safety
alerts), not raw engine diagnostics. Tier 3 (Family) is the most
restricted, matching the Family web dashboard's actual read-only,
safety-status-only behavior (PRODUCT.md) — no raw numbers at all, just
whether things are normal. Valet and Public/Fan are deliberately excluded:
Valet has no ongoing user who'd plausibly be in the workspace, and Fan is
explicitly no-login by design. The exact routing table lives in archer.py
as `_SLACK_ALERT_TIERS` (which alert types reach which tiers) and
`_slack_vstatus_message()`/`_slack_digest_message()` (what `/vstatus` and
`/digest` actually say per tier) — read those before changing what's
"passenger-relevant" vs. "owner-only," since that judgment call is spelled
out there, not hidden in this doc.

**a. Create the Slack app:** [api.slack.com/apps](https://api.slack.com/apps)
-> Create New App -> From scratch. Under **Basic Information**, copy the
**Signing Secret**. Under **OAuth & Permissions**, add the `chat:write`
bot scope, install the app to your workspace, and copy the **Bot User
OAuth Token** (starts with `xoxb-`).

**b. Create three channels** (e.g. `#archer-owner`, `#archer-passenger`,
`#archer-family`) and invite the bot to each (`/invite @Archer` in each
channel). Copy each channel's ID (View channel details -> bottom of the
panel) and each authorized person's Slack member ID (their profile ->
More -> Copy member ID).

**c. Configure Slash Commands:** App dashboard -> **Slash Commands** ->
Create New Command, once each for `/vstatus`, `/ask`, `/parking`, `/digest`
— Slack rejects `/status` as a reserved command name, hence `vstatus`
("vehicle status") — Request URL for all four is
`https://<your-public-domain.com>/slack/interactions` (the **public**
Caddy block — Slack's servers need to reach it from the open internet,
same reasoning as Discord's Interactions Endpoint above).

**d. Configure Interactivity:** App dashboard -> **Interactivity &
Shortcuts** -> turn it on, same Request URL as step c
(`/slack/interactions` — archer.py tells slash-command and button-click
payloads apart by their shape, so one URL covers both; no separate
registration script either, unlike Discord — Slack commands are configured
entirely in this dashboard, not via an API call).

**e. Fill in `/etc/archer/archer.env`** (from step 0) with
`SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_OWNER`,
`SLACK_CHANNEL_PASSENGER`, `SLACK_CHANNEL_FAMILY`,
`SLACK_OWNER_USER_IDS`, `SLACK_PASSENGER_USER_IDS`,
`SLACK_FAMILY_USER_IDS` (all comma-separated if more than one person per
tier), and set `slack_config['enabled'] = True`. No new pip dependency —
unlike Discord's `pynacl` requirement, Slack's HMAC-SHA256 scheme is
stdlib-only (`hmac`/`hashlib`).

**f. Restart the service:**
```
sudo systemctl restart archer
```

**Not yet verified against a live workspace** — same caveat as the
Discord build: built and tested against Slack's documented request/
response contract (30 passing tests, including real HMAC-SHA256 signature
verification and tier-routing correctness), but never round-tripped
against an actual Slack app, since this environment has no network path to
Slack's API either. Before relying on it: save the Request URL in both the
Slash Commands and Interactivity pages (Slack validates it on save, same
as Discord), then confirm `/vstatus` returns different content to a Tier 1
vs. Tier 3 user, and that a crash alert's buttons actually work, end to
end, in a real workspace.

## Real open questions — resolved against the actual source

The original draft of this handoff flagged three things as unconfirmed. Checked directly against `archer.py`:

- **~~The exact port archer.py's main app listens on~~ — RESOLVED: 7860, not 5000.** `archer.py` reads `port = int(os.environ.get('PORT', 7860))`, and the Dockerfile confirms it (`PORT=7860 python3 archer.py`). The Caddyfile and archer.service in this folder both use 7860.
- **~~Whether the tier-specific servers (ports 5002-5005) are actually meant to run standalone~~ — RESOLVED: they don't exist.** There is exactly one `Flask()` instance and one `.run()` call in the entire codebase — grepped and confirmed. The "5001 main / 5002 ayden / 5003 passenger / 5004 family / 5005 valet" text that appears in archer.py's `fetch_ngrok_url()` startup banner is stale/aspirational leftover text, not real listening ports — nothing in the code ever binds to them. There's also no `SPACE_ID`-gated logic disabling per-tier ports anywhere; that assumption in the original draft didn't check out. All four tiers are routes on the single app, resolved by `get_request_tier()`. The Caddyfile has been simplified from five blocks down to two (private + public) accordingly.
- **Whether Drive Replay's video storage needs its own disk space planning — still genuinely open.** Confirmed Drive Replay doesn't exist in the codebase yet: `cameras` support today is just an `<iframe>` pointed at a URL you set via `set_camera_url()` (e.g. a third-party camera app's stream), with no recording or storage code anywhere. Nothing to plan for until that feature actually gets built.

## Updating the Roku and Wear OS apps once the new address is live

Both were already fixed to use centralized config instead of scattered hardcoded URLs — this is now a one-line change per app, not a hunt through files:
- **Roku**: `source/Config.brs`, the `GetArcherBaseUrl()` function
- **Wear OS**: `app/build.gradle.kts`, the `ARCHER_BASE_URL` buildConfigField (a second Gradle flavor for "home" vs "cloud" is sketched out in a comment there, ready to uncomment once the home address is known)

Neither can be filled in yet — the real value is `<your-tailscale-machine-name>.<your-tailnet-name>.ts.net`, which only exists after step 1 above runs on your actual server.

## What genuinely gets easier vs. what's a real new tradeoff

**Easier:** no more Cloudflare-blocked price checker, no more Ollama re-downloading on every restart, no more 60-day auto-disable risk, no more free-tier CPU timeouts.

**Real new tradeoff, not hidden:** home internet going down takes remote access down with it — checking status or watching Drive Replay from outside won't work during an outage. Archer's core truck functionality (Pi, OBD reading, voice, in-cabin UI) doesn't depend on the home server at all and keeps working regardless — this only affects the *remote* pieces. A real tradeoff, but a narrower one than "the whole system goes down."
