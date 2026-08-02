# Archer Self-Hosting Migration — Handoff

Moving Archer's backend from HuggingFace Spaces to a home server, solving four real, already-documented problems: the Cloudflare-blocking-price-checker issue (datacenter IP → residential IP fixes this), Ollama re-downloading the model on every restart (HuggingFace containers don't persist storage; a home server does), the 60-day GitHub Actions auto-disable risk, and free-tier CPU timeouts that made even small local models struggle.

## The core architecture decision: private and public traffic are handled completely differently

This is the piece that makes self-hosting safe instead of scary. Archer has two genuinely different audiences:

**Private (family — phone, watch, Roku):** routed through **Tailscale**, a private mesh network between specifically-approved devices. No ports opened to the public internet, no exposed attack surface, no static IP needed. Devices connect as if they're on the home network, from anywhere.

**Public (the fan page, meant for strangers by design):** this one genuinely needs to stay internet-reachable, so it gets the traditional treatment — **Caddy** as a reverse proxy (automatic HTTPS via Let's Encrypt, far less config than nginx+certbot) plus **Dynamic DNS** to handle a home IP that changes periodically.

Treating these the same way is what makes self-hosting feel like it requires hardening a whole server against the internet. Splitting them means the private, more-important side barely needs traditional security work at all.

Both Caddy blocks proxy to the **same** archer.py process and port — there's only one backend. Tier separation (owner / passenger / family / valet) happens entirely inside archer.py itself via `get_request_tier()` (MAC whitelist, signed JWT cookie, or owner PIN), the exact same way it already works on the public HuggingFace Space today with no network-level gating at all. See "Real open questions" below for what this changed from the original draft.

## Setup steps, in order

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

**4. Install and enable the systemd service** (`archer.service`, in this folder) so archer.py survives reboots and crashes automatically. Update `User=`/`WorkingDirectory=`/`ExecStart=` to match the real install location first:
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
