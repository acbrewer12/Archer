#!/usr/bin/env bash
# Read-only post-boot check: did everything come back on its own, in order?
# Run as: sudo bash boot_check.sh
# Changes nothing. A service whose "since" is near the boot time and whose
# restarts= is 0 started cleanly by itself at boot.

TS_IP=$(tailscale ip -4 2>/dev/null | head -1)

echo "== boot =="
echo "booted:  $(uptime -s)"
journalctl --list-boots --no-pager 2>/dev/null | tail -2
echo
echo "== memory =="
free -h | head -2
echo
echo "== services =="
for s in tailscaled caddy archer docker grafana-server prometheus prometheus-node-exporter ngrok; do
    printf '%-25s enabled=%-9s active=%-9s restarts=%-3s since=%s\n' "$s" \
        "$(systemctl is-enabled "$s" 2>&1)" "$(systemctl is-active "$s")" \
        "$(systemctl show -p NRestarts --value "$s")" \
        "$(systemctl show -p ActiveEnterTimestamp --value "$s")"
done
echo
echo "== caddy waits for tailscale? =="
echo "configured: after-tailscaled=$(systemctl show -p After --value caddy | tr ' ' '\n' | grep -cx tailscaled.service)" \
     "restart=$(systemctl show -p Restart --value caddy) restartsec=$(systemctl show -p RestartUSec --value caddy)" \
     "nonlocal_bind=$(sysctl -n net.ipv4.ip_nonlocal_bind)"
echo "this boot, in order:"
journalctl -b -o short-precise --no-pager -u tailscaled -u caddy 2>/dev/null \
    | grep -E "Start(ing|ed) |Stopped|Failed|failed|cannot assign" | head -12
echo "caddy bind errors this boot: $(journalctl -b --no-pager -u caddy 2>/dev/null | grep -ciE 'cannot assign|bind: ')"
echo
echo "== listeners =="
ss -ltnH '( sport = :80 or sport = :8080 or sport = :7860 )' | awk '{print $4}' | sort
echo
echo "== requests =="
printf 'public block  :80/fans       -> %s\n' "$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1/fans)"
printf 'private block %s:8080/health -> %s\n' "${TS_IP:-?}" "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://$TS_IP:8080/health")"
printf 'archer        :7860/health   -> %s\n' "$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:7860/health)"
printf 'prometheus    :9090/-/ready  -> %s\n' "$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:9090/-/ready)"
