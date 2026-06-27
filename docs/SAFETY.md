# Archer — Hardware Safety Checklist

Pre-drive checklist for Archer AI installation on the 2006 GMC Sierra 2500HD.
Run this before the first drive after any hardware change or Archer update.

---

## Before Every Drive

- [ ] OBD adapter (OBDLink MX+) seated fully in port — not dangling or loose
- [ ] All USB cables routed away from the gear selector and handbrake
- [ ] Raspberry Pi powered on and indicator LED is solid green
- [ ] Phone mounted in cradle — not held in hand, not loose on seat
- [ ] `maintenance_mode` is OFF (banner absent on Tier 1 drive screen)
- [ ] Mirror display brightness adjusted for ambient light (night: dim/red mode)

---

## After Hardware Installation / Wiring Change

- [ ] Inspect all taps on the fuse box — verify correct fuse amperage
- [ ] Confirm relay board grounds to chassis (not floating)
- [ ] Check all butt connectors / T-taps for secure crimp — tug-test each
- [ ] Verify no wires cross the shifter throw or steering column
- [ ] Run OBD emulator (`USE_EMULATOR=true`) before connecting live OBD

---

## OBD Gatekeeper

- [ ] `obd_auth.key` exists at `/etc/archer/obd_auth.key` on the Pi
- [ ] Key file is `chmod 400` and owned by `archer` user (not world-readable)
- [ ] Gatekeeper service is enabled: `systemctl is-enabled obd_gatekeeper`
- [ ] Test auth handshake on bench before in-vehicle install
- [ ] Rotate key after any suspected compromise (see key rotation guide in README)

---

## Raspberry Pi

- [ ] Pi is mounted with standoffs — not resting on bare metal
- [ ] Pi enclosure ventilation holes are clear and unobstructed
- [ ] Power supply is 5V/3A minimum (USB-C); check voltage under load
- [ ] MicroSD card is Class 10 or better; consider a pSLC card for heat tolerance
- [ ] SSH is disabled or key-only if Pi has network access outside the truck

---

## Software / Auth

- [ ] `archer.env` is **not** committed to git — verify with `git status`
- [ ] `google-services.json` is **not** committed to git
- [ ] `obd_auth.key` is **not** committed to git
- [ ] Owner PIN is set (`ARCHER_OWNER_PIN` in env) — not left blank
- [ ] All non-owner registered devices have Tier 2–4 (not Tier 1) access
- [ ] One-time invite codes have been revoked after use

---

## Voice / AI Safety

- [ ] Voice rate limit active: max 3 commands/min above 10 mph
- [ ] `DANGEROUS_COMMANDS` list up to date for your workflow
- [ ] Valet mode (Tier 4) active when leaving truck with a third party
- [ ] Tested "help" and "what can you do" commands at standstill before driving

---

## Emergency Procedures

| Situation | Action |
|-----------|--------|
| OBD adapter pulls loose while moving | Pull over safely — OBD disconnect does not affect engine operation |
| Pi crashes / Archer unresponsive | Power cycle the Pi via the fuse switch; truck operates normally without Archer |
| Relay stuck OPEN (OBD always unlocked) | Remove the OBD adapter from the port; relay failure does not affect vehicle |
| Gatekeeper permanent lockout | SSH to Pi and run `sudo systemctl restart obd_gatekeeper` to reset counters |
| Suspected auth breach | Revoke all codes, deregister all non-owner MACs from the Tier 1 admin panel |

---

## Not Checked Here (Requires Real-World Testing)

- Pi vibration tolerance in the cab at highway speed
- Heat soak behavior in direct sun (parked)
- Alternator noise on the Pi's 5V rail (check for voltage ripple)
- Mirror display glare / anti-glare treatment for daytime visibility

---

*Last updated: 2026-06-27 — Archer v2.x*
