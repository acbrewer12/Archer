# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes, plus what is
specific to Archer.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial
tasks, use judgment.

---

# Part 1 — General

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes,
simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it
work") require constant clarification.

---

# Part 2 — Archer

## 5. Run it. Don't reason about it.

This is the house rule, and it overrides convenience everywhere below.

A change is not verified because it looks right, because the diff is small,
or because the mechanism is obvious. It is verified when it has been
executed and the output has been read. Nearly every real defect found in
this project was found by running something, and several confident,
well-argued analyses turned out to be wrong.

Concretely:
- Don't claim a fix works until you have run it and can quote the output.
- Prefer an experiment that could fail over an argument that can't.
- When you report results, say what you actually ran. If something was
  skipped or only parsed rather than executed, say that too.
- Distinguish "parses / compiles" from "runs" from "runs on the target
  hardware." They are three different claims.

## 6. Don't pattern-match a fix shape onto a different problem

Two bugs that look alike often aren't. This has cost real time here: a fix
modeled on an existing auth path broke 22 tests because the situation
differed in a way the shape hid.

Before reusing an approach from elsewhere in the codebase, confirm the
preconditions actually hold in the new location. Cite the specific evidence,
not the resemblance.

## 7. Layout

```
archer.py            Flask backend, ~12k lines. `display_app` is the app object.
blueprints/          auth, build, fans, modules, nav, roku, spotify, terminal, vehicle
pin.py               Owner PIN storage/verify. Stdlib only — see §9.
fcm_push.py          Push delivery
hsm.py, db.py, config.py, archer_state.py, serial_auth.py
archer-os/           Custom Linux distro for the truck head unit
pi/                  Raspberry Pi / OBD gatekeeper
android-apk/  roku/  wear-os/  archer-app/  archer-browser/  usb-os/  usb-tools/
```

Run the backend with `python3 archer.py`. It binds `0.0.0.0:7860`; override
with `PORT`. Dependencies are in `requirements.txt`.

## 8. Tests

**Tests live at the repo root, not in `tests/`.** `test_archer.py`,
`test_fcm_push.py`, and the browser-engine tests.

```
python3 -m pytest test_archer.py test_fcm_push.py -q
```

**There are 6 known pre-existing failures** in `TestRegisterDevice` and
`TestDeviceTierEndpoint`. They are unrelated to recent work. Don't report
them as new, and don't "fix" them as a side quest — but do confirm your
change didn't add to them. The reliable way to tell whose failure it is:

```
git stash push <file> && python3 -m pytest test_archer.py -q ; git stash pop
```

`display_app.testing` short-circuits `require_boot()`, so the boot and login
gates are bypassed under pytest. Gate behaviour has to be checked against a
running server, not the suite.

## 9. Auth model

- Tiers are **1–4**. **Tier 5 is a sentinel meaning "rejected credential"**,
  not a real access level. Anything that treats it as a tier is a bug — this
  class of "sentinel string masquerading as a real value" is worth watching
  for whenever `get_request_tier()` or nearby auth code is touched.
- Sessions are an HS256 JWT in the `archer_auth` cookie.
- `require_boot()` runs the login gate, then maintenance, then the boot gate.
  Only `text/html` page loads are gated; AJAX/SSE fall through and rely on
  each endpoint's own tier check.
- **Loopback exemptions exist and are deliberate**, but each one needs its
  own justification. `remote_addr` is trustworthy here specifically because
  there is no ProxyFix and no reverse proxy in front of the app. If that ever
  changes, every `127.0.0.1` check becomes spoofable.
- The owner PIN is hashed with scrypt in `pin.py` and **nowhere else**. The
  console login can't import Flask, so both it and the web login call that one
  module. A second copy would let the cost parameters drift apart and silently
  stop matching.

## 10. archer-os

A hand-built Debian image. Assume nothing from a normal distro.

- **`archer_init.c` is PID 1.** There is no systemd, no logind, no D-Bus
  session bus, no polkit. Consequences: `systemctl` doesn't exist; NetworkManager
  clients must run as root (`sudo nmtui`) because there is no polkit to authorise
  them; shutdown is `kill -USR1 1` (reboot) / `kill -USR2 1` (power off).
- It is **statically linked**, so NSS is unavailable — no `initgroups`,
  no `getpwnam`. Groups are parsed out of `/etc/group` by hand.
- **`build.sh` (USB/production) and `build-vm.sh` (VM) must stay in step.**
  The desktop/session regions are intentionally identical; only comment wording
  and VM-debug conveniences (e.g. a root password) differ. Change one, change
  both, then diff them with comments stripped to prove it.
- **The image is populated by `git archive HEAD`.** Uncommitted files do not
  reach it. Commit before building. Repo-root files land in `/opt/archer/`;
  files under `archer-os/` need an explicit copy step in both build scripts.
- Hand-written GRUB entries in `/etc/grub.d/40_archer` do **not** inherit
  `GRUB_CMDLINE_LINUX_DEFAULT` — the cmdline must be written into the entry.
- **There is no GPU driver.** simpledrm is mode-setting only, so Chromium
  renders through SwiftShader and its first paint is many seconds out. Anything
  that must feel fast cannot wait on the browser. This is why the login is on
  the console rather than in the dashboard.
- Xorg is deliberately non-setuid; the session starts unprivileged via
  `Xorg.wrap`, with a sudo fallback.
- **Nothing in archer-os is verified until the image is rebuilt and booted.**
  Parsing a build script proves nothing about the system it produces. Say so
  explicitly when reporting.

## 11. Environment traps

- **`pgrep -f` / `pkill -f` match their own shell's command line.** A pattern
  typed inline will match the very command running it. This has produced false
  positives *and* killed the calling shell. Put the pattern in a script file, or
  make it non-self-matching (`"archer\.py"`).
- **`/etc/archer/master.key` is pre-existing state. Never delete it.** Check
  timestamps before cleaning anything under `/etc/archer`; only remove files you
  created.
- Outbound HTTPS goes through an agent proxy, so a repo may appear reachable
  here that isn't publicly reachable. **This repo is private.** Don't infer
  access from a successful `git ls-remote`.
- Clean up test servers, test credentials, and `/run` markers when done.

## 12. Git

Work on the designated feature branch; push with
`git push -u origin <branch>`.

**Other sessions push to the same branch.** If a push is rejected, fetch and
look at what landed before doing anything. Rebase onto it — never force-push
over someone else's commit — then re-run the suite, because the merged code is
not the code you tested.
