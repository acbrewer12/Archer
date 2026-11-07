# archer-gstack-mcp

A remote MCP server exposing a small, gstack-inspired sprint workflow as real
tool calls (not just Claude following written instructions), so it's usable
from Claude mobile, not just a desktop Claude Code session with a local
daemon.

Hosted on Render as a persistent service. Add it to Claude as a custom
connector (Settings → Connectors → Add custom connector) using this
service's `/mcp/<MCP_PATH_SECRET>` URL.

## Tools

- **run_tests(branch)** — clones the branch fresh, runs
  `python3 -m pytest test_archer.py test_fcm_push.py -q`, and flags any
  failures beyond the 6 known pre-existing ones (see repo `CLAUDE.md` §8).
- **ship(branch, title, body)** — runs the suite, then opens a PR into
  `main` if one isn't already open. Never pushes or merges to `main`
  itself.
- **land_and_deploy(pr_number)** — squash-merges an already-open PR into
  `main`. That merge triggers the repo's existing `sync-to-hf` GitHub
  Action, which mirrors to the HuggingFace Space — this tool never touches
  the Pi or the truck.
- **qa_browser(paths)** — drives a real headless Chromium against the
  read-only HuggingFace Space mirror (`aydencatman-archer.hf.space`, no
  relay/vehicle control) and reports console errors, failed requests, and
  status per page.
- **retro(days)** — lists commits on `main` over the last N days, for a
  quick look back.

## What this intentionally does NOT do

No local browser daemon, no cookie import, no standalone CLIs, no CI/deploy
platform integration beyond what the repo's own GitHub Actions already do.
It only ever talks to GitHub's API and the public HuggingFace mirror —
never the Raspberry Pi, the OBD adapter, or the truck.

## Deploying

Render service, Node runtime, root directory `gstack-mcp/`:

- Build command: `npm install`
- Start command: `npm start`
- Required env vars (set in Render's dashboard, not in this repo):
  - `GITHUB_TOKEN` — fine-grained PAT scoped to `acbrewer12/archer` with
    Contents: Read & write and Pull requests: Read & write.
  - `MCP_PATH_SECRET` — a long random string; the server's endpoint is
    `/mcp/<this value>`. Treat the full URL as a secret.
