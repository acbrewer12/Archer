/*
 * archer_init.c — Archer OS PID 1
 *
 * This is the first userspace program the kernel runs after boot.
 * It replaces systemd. Every process on the system is a child of this one.
 *
 * HOW IT WORKS (read before changing anything):
 *
 *   1. Mount virtual filesystems (/proc /sys /dev /tmp)
 *      These are NOT real disk files — they're kernel interfaces exposed
 *      as a filesystem so normal tools can read them with open()/read().
 *
 *   2. Set hostname from /etc/hostname
 *
 *   3. Bring up loopback interface (127.0.0.1)
 *      Without this, nothing can connect to localhost.
 *
 *   4. Fork services:
 *        - NetworkManager (or wpa_supplicant) for WiFi/tethering
 *        - Archer Flask app (the actual truck AI)
 *
 *   5. Main loop: reap zombie children + handle shutdown signals
 *      If we don't waitpid() for dead children, they stay as zombies
 *      in the process table forever, leaking PID slots.
 *
 * COMPILE:
 *   gcc -static -Os -o archer_init archer_init.c
 *   (static = no shared lib deps, -Os = optimize for size)
 *
 * INSTALL:
 *   copy to /sbin/archer_init in the OS image
 *   add init=/sbin/archer_init to kernel cmdline in GRUB
 */

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <net/if.h>
#include <sys/socket.h>
#include <dirent.h>

/* ── tunables ────────────────────────────────────────────────────── */
#define ARCHER_VENV   "/opt/archer/.venv/bin/python3"
#define ARCHER_APP    "/opt/archer/archer.py"
#define ARCHER_DIR    "/opt/archer"
#define ARCHER_USER   "archer"

#define LOG_PATH      "/run/archer_init.log"
#define STATUS_PATH   "/run/archer_status"

/* ── global state ────────────────────────────────────────────────── */
static volatile int g_shutdown = 0;
static volatile int g_shutdown_type = RB_POWER_OFF;

static pid_t pid_network  = -1;
static pid_t pid_avahi    = -1;
static pid_t pid_archer   = -1;
static pid_t pid_getty1   = -1;
static pid_t pid_getty2   = -1;

/* ── logging ─────────────────────────────────────────────────────── */

/*
 * We can't use syslog (systemd isn't running). We write to two places:
 *   - /dev/kmsg  (kernel log ring buffer — readable via dmesg)
 *   - /run/archer_init.log  (plain file for the Archer shell to display)
 */
static void ilog(const char *level, const char *msg)
{
    /* /dev/kmsg accepts lines prefixed with priority like "<6>message\n" */
    int kmsg = open("/dev/kmsg", O_WRONLY | O_NOCTTY);
    if (kmsg >= 0) {
        dprintf(kmsg, "<6>[archer_init] %s: %s\n", level, msg);
        close(kmsg);
    }

    /* Also write to our log file */
    int lf = open(LOG_PATH, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (lf >= 0) {
        dprintf(lf, "[archer_init] %s: %s\n", level, msg);
        close(lf);
    }
}

#define LOG(msg)  ilog("INFO",  msg)
#define WARN(msg) ilog("WARN",  msg)
#define ERR(msg)  ilog("ERROR", msg)

/* ── signal handling ─────────────────────────────────────────────── */

/*
 * The kernel sends SIGTERM to PID 1 when the user runs `systemctl poweroff`
 * (or we can send it ourselves). SIGINT comes from Ctrl+Alt+Del (if configured).
 *
 * We set a flag here and let the main loop handle the actual shutdown.
 * Signal handlers must be async-signal-safe — no malloc, no printf.
 */
static void sig_shutdown(int signum)
{
    g_shutdown = 1;
    if (signum == SIGUSR2) {
        g_shutdown_type = RB_POWER_OFF;
    } else if (signum == SIGUSR1) {
        g_shutdown_type = RB_AUTOBOOT;  /* reboot */
    } else {
        g_shutdown_type = RB_POWER_OFF;
    }
}

static void setup_signals(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = sig_shutdown;
    sigemptyset(&sa.sa_mask);

    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGUSR1, &sa, NULL);   /* reboot */
    sigaction(SIGUSR2, &sa, NULL);   /* poweroff */
    sigaction(SIGINT,  &sa, NULL);   /* Ctrl+Alt+Del */

    /* Ignore SIGHUP and SIGPIPE — common noise from terminals/pipes */
    signal(SIGHUP,  SIG_IGN);
    signal(SIGPIPE, SIG_IGN);
}

/* ── persisted boot log ──────────────────────────────────────────── */

/*
 * Verbose kernel logging (loglevel=7) scrolls by far too fast to read or
 * photograph live. dmesg dumps the whole kernel ring buffer (cumulative —
 * every message printed since boot, even ones long since scrolled off
 * screen) to a real file on the root ext4 filesystem, so it survives and
 * can be read later: from the maintenance shell (tty2), or by mounting the
 * disk image from the host after shutdown.
 */
#define BOOT_LOG_PATH "/var/log/archer_boot_dmesg.log"

static void dump_boot_log(void)
{
    pid_t pid = fork();
    if (pid == 0) {
        int fd = open(BOOT_LOG_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd >= 0) { dup2(fd, STDOUT_FILENO); close(fd); }
        int null = open("/dev/null", O_RDWR);
        if (null >= 0) { dup2(null, STDIN_FILENO); dup2(null, STDERR_FILENO); close(null); }
        char *argv[] = { (char *)"dmesg", NULL };
        execv("/bin/dmesg", argv);
        execv("/usr/bin/dmesg", argv);
        _exit(1);
    }
    if (pid > 0) waitpid(pid, NULL, 0);
}

/* ── filesystem mounts ───────────────────────────────────────────── */

/*
 * These are virtual filesystems — they don't live on any disk.
 * The kernel creates them in RAM. We mount them so that:
 *   /proc  — process info, /proc/cmdline, /proc/meminfo, etc.
 *   /sys   — hardware device tree, power management
 *   /dev   — device files (tty, disk, null, zero, urandom...)
 *   /tmp   — tmpfs, cleared on reboot
 *   /run   — runtime data, PID files, sockets (also tmpfs)
 */
static void mount_virtual_fs(void)
{
    /* Create mount points if they don't exist */
    mkdir("/proc", 0555);
    mkdir("/sys",  0755);
    mkdir("/dev",  0755);
    mkdir("/tmp",  0777);
    mkdir("/run",  0755);

    if (mount("proc",    "/proc", "proc",    MS_NOEXEC | MS_NOSUID | MS_NODEV, NULL) < 0)
        WARN("mount /proc failed (may already be mounted)");

    if (mount("sysfs",   "/sys",  "sysfs",   MS_NOEXEC | MS_NOSUID | MS_NODEV, NULL) < 0)
        WARN("mount /sys failed");

    /* devtmpfs: kernel automatically creates device nodes in /dev */
    if (mount("devtmpfs","/dev",  "devtmpfs",MS_NOSUID, "mode=0755") < 0)
        WARN("mount /dev (devtmpfs) failed — device files may be missing");

    /* devpts: pseudo-terminal support (needed for SSH, screen, etc.) */
    mkdir("/dev/pts", 0755);
    mount("devpts", "/dev/pts", "devpts", MS_NOEXEC | MS_NOSUID, "mode=0620,ptmxmode=0666");

    /* tmpfs for /tmp and /run */
    if (mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777") < 0)
        WARN("mount /tmp failed");

    if (mount("tmpfs", "/run", "tmpfs", MS_NOSUID | MS_NODEV, "mode=0755") < 0)
        WARN("mount /run failed");

    LOG("virtual filesystems mounted");
}

/* ── hostname ────────────────────────────────────────────────────── */

static void set_hostname(void)
{
    char buf[64] = "archer";  /* default */
    int fd = open("/etc/hostname", O_RDONLY);
    if (fd >= 0) {
        ssize_t n = read(fd, buf, sizeof(buf) - 1);
        close(fd);
        if (n > 0) {
            buf[n] = '\0';
            /* strip trailing newline */
            char *nl = strchr(buf, '\n');
            if (nl) *nl = '\0';
        }
    }

    if (sethostname(buf, strlen(buf)) < 0)
        WARN("sethostname failed");
    else
        LOG("hostname set");
}

/* ── loopback interface ──────────────────────────────────────────── */

/*
 * Bring up the loopback interface (lo / 127.0.0.1).
 * Without this, nothing can connect to localhost — Flask won't be reachable
 * even from the same machine.
 *
 * We use ioctl(SIOCSIFFLAGS) to set IFF_UP | IFF_RUNNING on the "lo" interface.
 * This is what `ip link set lo up` does under the hood.
 */
static void bring_up_loopback(void)
{
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        WARN("loopback: socket() failed");
        return;
    }

    struct ifreq ifr;
    memset(&ifr, 0, sizeof(ifr));
    strncpy(ifr.ifr_name, "lo", IFNAMSIZ - 1);

    /* Get current flags */
    if (ioctl(sock, SIOCGIFFLAGS, &ifr) < 0) {
        WARN("loopback: SIOCGIFFLAGS failed");
        close(sock);
        return;
    }

    /* Set UP + RUNNING */
    ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
    if (ioctl(sock, SIOCSIFFLAGS, &ifr) < 0)
        WARN("loopback: SIOCSIFFLAGS failed");
    else
        LOG("loopback interface up");

    close(sock);
}

/* ── spawn a service ─────────────────────────────────────────────── */

/*
 * fork() + exec() to start a background service.
 *
 * fork() makes an exact copy of our process. Both parent and child continue
 * from the same point, but fork() returns:
 *   - 0 in the child
 *   - child PID in the parent
 *   - -1 on error
 *
 * In the child we exec() the new program — this replaces the child's entire
 * memory with the new program. The child's PID stays the same.
 *
 * envp: NULL means inherit our environment.
 * argv: NULL-terminated list of arguments (argv[0] is program name by convention).
 */
static pid_t spawn(const char *path, char *const argv[], const char *workdir, uid_t uid, gid_t gid)
{
    pid_t pid = fork();

    if (pid < 0) {
        ERR("fork() failed");
        return -1;
    }

    if (pid == 0) {
        /* ── we are the child ── */

        /* Change working directory if specified */
        if (workdir && chdir(workdir) < 0) {
            /* non-fatal, just warn via kmsg */
            int k = open("/dev/kmsg", O_WRONLY | O_NOCTTY);
            if (k >= 0) { dprintf(k, "<4>[archer_init] chdir(%s) failed\n", workdir); close(k); }
        }

        /* Drop privileges if uid != 0 */
        if (uid != 0) {
            if (setgid(gid) < 0 || setuid(uid) < 0) _exit(1);
        }

        /* Redirect stdin from /dev/null so the service doesn't block on input */
        int null_fd = open("/dev/null", O_RDONLY);
        if (null_fd >= 0) {
            dup2(null_fd, STDIN_FILENO);
            close(null_fd);
        }

        /* stdout/stderr: redirect to /dev/kmsg so we can see them in dmesg */
        int kmsg = open("/dev/kmsg", O_WRONLY | O_NOCTTY);
        if (kmsg >= 0) {
            dup2(kmsg, STDOUT_FILENO);
            dup2(kmsg, STDERR_FILENO);
            close(kmsg);
        }

        execv(path, argv);

        /* If exec fails, we must exit — don't return into parent's code */
        _exit(127);
    }

    /* ── we are the parent ── */
    return pid;
}

/* ── ethernet interface ──────────────────────────────────────────────
 *
 * NetworkManager handles WiFi and USB tethering, but it depends on
 * D-Bus being fully ready. For wired ethernet we use dhclient directly —
 * simpler, faster, no D-Bus dependency. Scans /sys/class/net/ for the
 * first physical NIC (has a 'device' symlink), sets IFF_UP, runs dhclient.
 */
static void bring_up_ethernet(void)
{
    char iface[IFNAMSIZ] = {0};

    DIR *d = opendir("/sys/class/net");
    if (!d) { WARN("ethernet: cannot open /sys/class/net"); return; }

    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.') continue;
        if (strcmp(e->d_name, "lo") == 0) continue;
        char devpath[256];
        snprintf(devpath, sizeof(devpath), "/sys/class/net/%s/device", e->d_name);
        if (access(devpath, F_OK) == 0) {
            strncpy(iface, e->d_name, IFNAMSIZ - 1);
            break;
        }
    }
    closedir(d);

    if (iface[0] == '\0') { WARN("ethernet: no physical interface found"); return; }

    char msg[80];
    snprintf(msg, sizeof(msg), "ethernet: bringing up %s", iface);
    LOG(msg);

    /* Set IFF_UP on the interface */
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock >= 0) {
        struct ifreq ifr;
        memset(&ifr, 0, sizeof(ifr));
        strncpy(ifr.ifr_name, iface, IFNAMSIZ - 1);
        if (ioctl(sock, SIOCGIFFLAGS, &ifr) == 0) {
            ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
            ioctl(sock, SIOCSIFFLAGS, &ifr);
        }
        close(sock);
    }

    /* dhclient sets the IP address and default route, then exits */
    char *argv[] = { "/sbin/dhclient", "-v", iface, NULL };
    pid_t pid = spawn("/sbin/dhclient", argv, "/", 0, 0);
    if (pid > 0) {
        snprintf(msg, sizeof(msg), "dhclient started on %s (pid %d)", iface, pid);
        LOG(msg);
    } else {
        WARN("dhclient failed — no IP on wired interface");
    }
}

/* ── get uid/gid for a username ─────────────────────────────────── */

/*
 * We can't use getpwnam() in a static binary easily (it calls NSS which
 * dynamically loads libraries — impossible in a static build).
 * Instead, we parse /etc/passwd directly. This is exactly what musl libc does.
 */
static int get_uid_gid(const char *username, uid_t *uid, gid_t *gid)
{
    FILE *f = fopen("/etc/passwd", "r");
    if (!f) return -1;

    char line[256];
    while (fgets(line, sizeof(line), f)) {
        /* Format: name:password:uid:gid:gecos:home:shell */
        char name[64];
        uid_t u; gid_t g;
        if (sscanf(line, "%63[^:]:%*[^:]:%u:%u:", name, &u, &g) == 3) {
            if (strcmp(name, username) == 0) {
                *uid = u;
                *gid = g;
                fclose(f);
                return 0;
            }
        }
    }
    fclose(f);
    return -1;  /* not found */
}

/* ── verify a file is safe to execute with root privileges ─────────
 *
 * We are PID 1, running as root. /opt/archer (application code, including
 * the venv's python3 interpreter) is owned by the unprivileged 'archer'
 * user — see build.sh's `chown -R archer:archer /opt/archer`. That means
 * anything under /opt/archer must be treated as untrusted input when we
 * are about to run it *before* dropping privileges: if the 'archer'
 * account is ever compromised locally, a rewritten script or interpreter
 * there would otherwise get executed as root on the next boot.
 *
 * "Safe" means: owned by root (uid 0) and not writable by group or other.
 * Fail closed — anything that doesn't pass this check must not run as
 * root; the caller should skip it rather than trust it blindly.
 */
static int is_safe_to_run_as_root(const char *path)
{
    struct stat st;
    if (stat(path, &st) < 0) {
        WARN("privilege check: stat() failed on a file we were about to run as root");
        return 0;
    }
    if (st.st_uid != 0) {
        WARN("privilege check: file is not owned by root — refusing to run it as root");
        return 0;
    }
    if (st.st_mode & (S_IWGRP | S_IWOTH)) {
        WARN("privilege check: file is group/world writable — refusing to run it as root");
        return 0;
    }
    return 1;
}

/* ── start all services ──────────────────────────────────────────── */

static void start_services(void)
{
    /* 0. Wired ethernet — start dhclient immediately so DHCP runs in the
     *    background while we bring up D-Bus and NetworkManager.
     *    By the time Archer starts (~6s later) the IP is already assigned. */
    bring_up_ethernet();

    uid_t archer_uid = 1000;
    gid_t archer_gid = 1000;
    get_uid_gid(ARCHER_USER, &archer_uid, &archer_gid);

    /* 1. D-Bus system daemon — must start BEFORE NetworkManager.
     *    NetworkManager uses D-Bus for all inter-process communication.
     *    Without it, NM starts but can't manage interfaces. */
    {
        /* Create the D-Bus runtime directory if missing */
        mkdir("/run/dbus", 0755);
        char *argv[] = { "/usr/bin/dbus-daemon", "--system", "--nofork",
                         "--nopidfile", NULL };
        pid_t pid = spawn("/usr/bin/dbus-daemon", argv, "/", 0, 0);
        if (pid > 0) {
            LOG("dbus-daemon started");
            sleep(2);  /* wait for D-Bus socket to be ready before NM connects */
        } else {
            WARN("dbus-daemon failed — NetworkManager may not work");
        }
    }

    /* 2. NetworkManager — manages WiFi and USB tethering */
    {
        char *argv[] = { "/usr/sbin/NetworkManager", "--no-daemon", NULL };
        pid_network = spawn("/usr/sbin/NetworkManager", argv, "/", 0, 0);
        if (pid_network > 0)
            LOG("NetworkManager started");
        else
            WARN("NetworkManager failed to start");
    }

    /* 3. Avahi daemon — mDNS, makes archer.local work on the LAN */
    {
        char *argv[] = { "/usr/sbin/avahi-daemon", "--no-chroot", NULL };
        pid_avahi = spawn("/usr/sbin/avahi-daemon", argv, "/", 0, 0);
        if (pid_avahi > 0)
            LOG("avahi-daemon started");
        /* non-critical, no warning if it fails */
    }

    /* 4a. Getty on tty1 — autologin as archer → .bash_profile → startx → kiosk.
     *     We open /dev/tty1 explicitly so stdin/stdout/stderr go there. */
    {
        pid_t pid = fork();
        if (pid == 0) {
            int tty = open("/dev/tty1", O_RDWR | O_NOCTTY);
            if (tty >= 0) {
                dup2(tty, STDIN_FILENO);
                dup2(tty, STDOUT_FILENO);
                dup2(tty, STDERR_FILENO);
                if (tty > STDERR_FILENO) close(tty);
            }
            setsid();
            ioctl(STDIN_FILENO, TIOCSCTTY, 1);
            char *argv[] = {
                "/sbin/agetty",
                "--autologin", "archer",
                "--noclear",
                "tty1", "linux", NULL
            };
            execv("/sbin/agetty", argv);
            _exit(1);
        }
        pid_getty1 = pid;
        if (pid_getty1 > 0)
            LOG("getty started on tty1 (kiosk)");
    }

    /* 4b. Getty on tty2 — maintenance shell, always accessible via Ctrl+Alt+F2.
     *     Deliberately NOT autologin: tty1 is the public-facing kiosk display,
     *     but tty2 is a full shell, and anyone with physical keyboard access to
     *     the head unit can hit Ctrl+Alt+F2. Autologin here would hand out an
     *     authenticated shell as 'archer' to anyone standing at the truck, no
     *     password required. Require a normal login prompt instead — the
     *     'archer' account has no password set by default (see build.sh), so
     *     until an operator explicitly runs `passwd archer`, tty2 is unusable,
     *     which is the safe default. */
    {
        pid_t pid = fork();
        if (pid == 0) {
            int tty = open("/dev/tty2", O_RDWR | O_NOCTTY);
            if (tty >= 0) {
                dup2(tty, STDIN_FILENO);
                dup2(tty, STDOUT_FILENO);
                dup2(tty, STDERR_FILENO);
                if (tty > STDERR_FILENO) close(tty);
            }
            setsid();
            ioctl(STDIN_FILENO, TIOCSCTTY, 1);
            char *argv[] = {
                "/sbin/agetty",
                "--noclear",
                "tty2", "linux", NULL
            };
            execv("/sbin/agetty", argv);
            _exit(1);
        }
        pid_getty2 = pid;
        if (pid_getty2 > 0)
            LOG("getty started on tty2 (maintenance, password login required)");
    }

    /* Small delay: let NetworkManager initialize before Archer tries to use the network */
    sleep(2);

    /* 5. OBD2 port authentication — send HMAC-SHA256 handshake to the Pi gatekeeper.
     *    The Pi keeps the OBD2 connector dead until we prove we hold the shared key.
     *    Non-blocking: if the Pi isn't present or auth fails, Archer still starts
     *    (just without OBD data). Result written to /run/archer_obd_auth for archer.py.
     *
     *    This runs as root — before we drop to the unprivileged 'archer' user —
     *    because it needs to read the root-only shared key at
     *    /etc/archer/obd_auth.key. That means we must NOT blindly trust the
     *    interpreter/script we're about to run: both live under /opt/archer,
     *    which is owned by 'archer' (see build.sh). Verify they are root-owned
     *    and not group/world-writable first; fail closed (skip auth, boot
     *    continues without OBD data) if that check doesn't pass. */
    {
        const char *obd_auth_script = "/opt/archer/archer-os/obd-auth/obd_auth_client.py";
        char *argv[] = {
            ARCHER_VENV,
            (char *)obd_auth_script,
            NULL
        };

        if (!is_safe_to_run_as_root(ARCHER_VENV) || !is_safe_to_run_as_root(obd_auth_script)) {
            ERR("OBD2 auth: skipped — interpreter or script is not root-owned/is writable by 'archer'");
            int fd = open("/run/archer_obd_auth", O_WRONLY | O_CREAT | O_TRUNC, 0644);
            if (fd >= 0) { dprintf(fd, "0\n"); close(fd); }
        } else {
            pid_t pid = fork();
            if (pid == 0) {
                int kmsg = open("/dev/kmsg", O_WRONLY | O_NOCTTY);
                if (kmsg >= 0) { dup2(kmsg, STDOUT_FILENO); dup2(kmsg, STDERR_FILENO); close(kmsg); }
                execv(ARCHER_VENV, argv);
                _exit(1);
            }
            if (pid > 0) {
                /* Wait up to 10 seconds — don't stall boot forever */
                int waited = 0;
                int auth_status = -1;
                while (waited < 10) {
                    pid_t r = waitpid(pid, &auth_status, WNOHANG);
                    if (r == pid) break;
                    sleep(1); waited++;
                }
                if (waited >= 10) {
                    WARN("OBD2 auth: timeout — killing auth process");
                    kill(pid, SIGKILL);
                    waitpid(pid, NULL, 0);
                }
                int ok = (WIFEXITED(auth_status) && WEXITSTATUS(auth_status) == 0) ? 1 : 0;
                int fd = open("/run/archer_obd_auth", O_WRONLY | O_CREAT | O_TRUNC, 0644);
                if (fd >= 0) { dprintf(fd, "%d\n", ok); close(fd); }
                if (ok) LOG("OBD2 authentication successful — port unlocked");
                else    WARN("OBD2 authentication skipped or failed");
            }
        }
    }

    /* 6. Archer Flask app — the truck AI */
    {
        char *argv[] = { ARCHER_VENV, ARCHER_APP, NULL };
        char *env[]  = {
            "HOME=/home/archer",
            "USER=archer",
            "HOST=0.0.0.0",          /* bind on all interfaces so host browser can reach it */
            "PORT=5000",
            "PYTHONUNBUFFERED=1",
            NULL
        };

        /*
         * We need execve (with env) here, not execv. Temporarily use a direct fork.
         * spawn() uses execv which inherits env — that's fine for most services,
         * but Archer needs PYTHONUNBUFFERED set.
         */
        pid_t pid = fork();
        if (pid == 0) {
            if (chdir(ARCHER_DIR) < 0) _exit(1);
            if (setgid(archer_gid) < 0 || setuid(archer_uid) < 0) _exit(1);

            int null_fd = open("/dev/null", O_RDONLY);
            if (null_fd >= 0) { dup2(null_fd, STDIN_FILENO); close(null_fd); }

            int kmsg = open("/dev/kmsg", O_WRONLY | O_NOCTTY);
            if (kmsg >= 0) { dup2(kmsg, STDOUT_FILENO); dup2(kmsg, STDERR_FILENO); close(kmsg); }

            execve(ARCHER_VENV, argv, env);
            _exit(127);
        }
        pid_archer = pid;
        if (pid_archer > 0)
            LOG("Archer app started");
        else
            ERR("Archer app failed to start — this is a critical failure");
    }
}

/* ── write status file ───────────────────────────────────────────── */

/*
 * Write a simple status file that the archer_shell TUI can read.
 * Format: KEY=VALUE\n pairs.
 */
static void write_status(void)
{
    int fd = open(STATUS_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return;

    dprintf(fd, "network_pid=%d\n", (int)pid_network);
    dprintf(fd, "archer_pid=%d\n",  (int)pid_archer);
    dprintf(fd, "avahi_pid=%d\n",   (int)pid_avahi);
    dprintf(fd, "getty1_pid=%d\n",  (int)pid_getty1);
    dprintf(fd, "getty2_pid=%d\n",  (int)pid_getty2);
    dprintf(fd, "network_ok=%d\n",  pid_network > 0 ? 1 : 0);
    dprintf(fd, "archer_ok=%d\n",   pid_archer  > 0 ? 1 : 0);

    close(fd);
}

/* ── clean shutdown sequence ─────────────────────────────────────── */

/*
 * When shutting down, we need to:
 *   1. Send SIGTERM to all children (asks them to exit gracefully)
 *   2. Wait up to 5 seconds for them to exit
 *   3. Send SIGKILL to any that didn't listen
 *   4. sync() — flush all disk writes
 *   5. umount all filesystems
 *   6. reboot(RB_POWER_OFF) or reboot(RB_AUTOBOOT)
 *
 * The kernel will NOT do this for us — if PID 1 calls reboot() without
 * syncing, you can corrupt the filesystem.
 */
static void do_shutdown(int type)
{
    LOG("shutdown initiated");

    /* Signal all children */
    if (pid_archer  > 0) kill(pid_archer,  SIGTERM);
    if (pid_network > 0) kill(pid_network, SIGTERM);
    if (pid_avahi   > 0) kill(pid_avahi,   SIGTERM);

    /* Also kill entire process group (catches any grandchildren) */
    kill(-1, SIGTERM);

    /* Wait up to 5 seconds */
    for (int i = 0; i < 50; i++) {
        usleep(100000);  /* 100ms */
        pid_t dead = waitpid(-1, NULL, WNOHANG);
        if (dead < 0) break;  /* no more children */
    }

    /* Force-kill anything still alive */
    kill(-1, SIGKILL);
    while (waitpid(-1, NULL, WNOHANG) > 0) {}

    /* Flush all dirty buffers to disk */
    sync();
    sync();  /* belt and suspenders */

    /* Unmount filesystems in reverse order */
    umount2("/run",  MNT_DETACH);
    umount2("/tmp",  MNT_DETACH);
    umount2("/dev/pts", MNT_DETACH);
    umount2("/dev",  MNT_DETACH);
    umount2("/sys",  MNT_DETACH);
    umount2("/proc", MNT_DETACH);

    /* Tell the kernel to power off or reboot */
    reboot(type);

    /* Should never reach here */
    _exit(0);
}

/* ── restart Archer specifically ─────────────────────────────────── */

static void restart_archer(void)
{
    uid_t archer_uid = 1000;
    gid_t archer_gid = 1000;
    get_uid_gid(ARCHER_USER, &archer_uid, &archer_gid);

    char *argv[] = { ARCHER_VENV, ARCHER_APP, NULL };
    char *env[]  = {
        "HOME=/home/archer",
        "USER=archer",
        "PORT=5000",
        "PYTHONUNBUFFERED=1",
        NULL
    };

    pid_t pid = fork();
    if (pid == 0) {
        if (chdir(ARCHER_DIR) < 0) _exit(1);
        if (setgid(archer_gid) < 0 || setuid(archer_uid) < 0) _exit(1);
        int null_fd = open("/dev/null", O_RDONLY);
        if (null_fd >= 0) { dup2(null_fd, STDIN_FILENO); close(null_fd); }
        int kmsg = open("/dev/kmsg", O_WRONLY | O_NOCTTY);
        if (kmsg >= 0) { dup2(kmsg, STDOUT_FILENO); dup2(kmsg, STDERR_FILENO); close(kmsg); }
        execve(ARCHER_VENV, argv, env);
        _exit(127);
    }
    pid_archer = pid;
    if (pid_archer > 0)
        LOG("Archer restarted");
}

/* ═══════════════════════════════════════════════════════════════════
   MAIN
   ═══════════════════════════════════════════════════════════════════ */

int main(int argc __attribute__((unused)), char *argv[] __attribute__((unused)))
{
    /* Sanity check: we should only be run as PID 1 */
    if (getpid() != 1) {
        fprintf(stderr, "archer_init: must be run as PID 1 (got PID %d)\n", getpid());
        return 1;
    }

    /* Set a clean umask */
    umask(022);

    /*
     * Step 1: Mount virtual filesystems
     * Must happen before almost anything else — /proc doesn't exist until this.
     */
    mount_virtual_fs();

    /*
     * Step 2: Set up signal handlers
     * We need these registered before starting any services so we can handle
     * SIGTERM if shutdown is requested while we're still setting up.
     */
    setup_signals();

    /*
     * Step 3: Set hostname
     * Reads /etc/hostname. Now that /proc is mounted we can also read
     * kernel parameters from /proc/cmdline if needed.
     */
    set_hostname();

    /*
     * Step 4: Bring up loopback
     * Flask binds to 0.0.0.0:5000 but still needs lo up for localhost.
     */
    bring_up_loopback();

    /* Remount root read-write.
     * dracut hands us a ro root. We must remount rw before any userspace
     * process writes to /home, /var, /opt, etc.
     * This must happen AFTER mount_virtual_fs() so /dev/kmsg exists for WARN. */
    if (mount("none", "/", NULL, MS_REMOUNT | MS_NOATIME, "") < 0)
        WARN("remount / rw failed — filesystem may be read-only");
    else
        LOG("root filesystem remounted read-write");

    /* Load kernel modules that are =m (not built-in) but needed before udevd.
     * Network drivers: e1000 covers older VMware E1000 adapters.
     * GPU drivers: vmwgfx for VMware SVGA, then real-hardware drivers.
     * Failures are silently ignored — built-in drivers are already active.
     *
     * simpledrm deliberately isn't in this list — found by actually booting
     * this image: it used to be here as "drm_simpledrm", which was simply
     * the wrong module name (the real one is "simpledrm"), so this modprobe
     * call was silently failing every boot regardless. It's now
     * CONFIG_DRM_SIMPLEDRM=y (built in, see kernel/archer.config) instead
     * of fixing the name here, because loading it this late would have
     * been too late anyway — CONFIG_FB_EFI/CONFIG_FB_VESA are also built
     * in and would have already claimed the boot framebuffer by the time
     * this function runs, locking simpledrm out entirely (only one driver
     * can bind to it). GRUB_CMDLINE_LINUX_DEFAULT now passes
     * video=efifb:off video=vesafb:off so simpledrm gets it instead, at
     * the same early boot stage those would have. */
    {
        static const char *const mods[] = {
            /* network */
            "e1000", "e1000e", "vmxnet3", "r8169",
            /* gpu — try vmwgfx first, fall back to real-hardware drivers */
            "vmwgfx", "i915", "amdgpu", "nouveau",
            NULL
        };
        for (int i = 0; mods[i]; i++) {
            pid_t pid = fork();
            if (pid == 0) {
                int null = open("/dev/null", O_RDWR);
                if (null >= 0) { dup2(null,0); dup2(null,1); dup2(null,2); close(null); }
                char *argv[] = { "/sbin/modprobe", (char*)mods[i], NULL };
                execv("/sbin/modprobe", argv);
                _exit(1);
            }
            if (pid > 0) waitpid(pid, NULL, 0);
        }
        LOG("kernel modules loaded");
        sleep(1); /* let uevents settle so /dev/fb0 and eth0 appear */
    }

    /* Snapshot the kernel ring buffer now — if something hangs or panics
     * during service startup, we still have everything up to this point
     * saved to disk instead of lost in a fast-scrolling console. */
    dump_boot_log();

    LOG("archer_init: initialization complete, starting services");

    /*
     * Step 5: Start services
     * NetworkManager → avahi → Archer (with 2s delay between NM and Archer)
     */
    start_services();
    write_status();

    /* Re-snapshot now that every service has been launched — the ring
     * buffer is cumulative, so this overwrites the earlier file with a
     * more complete copy covering the rest of the boot sequence too. */
    dump_boot_log();

    LOG("archer_init: entering main loop (reap + restart)");

    /*
     * Step 6: Main loop
     *
     * PID 1 must NEVER exit. We sit here forever doing two things:
     *   a) Reap zombie children (waitpid with WNOHANG = don't block)
     *   b) Restart Archer if it crashed
     *   c) Check if a shutdown was requested (signal handler set g_shutdown)
     *
     * sleep(1) is intentional — we don't need sub-second precision here.
     * SIGTERM/SIGCHLD will interrupt the sleep.
     */
    int status_tick = 0;
    while (!g_shutdown) {
        /* Reap any dead children */
        pid_t dead;
        while ((dead = waitpid(-1, NULL, WNOHANG)) > 0) {
            char msg[64];
            if      (dead == pid_archer)  { snprintf(msg, sizeof(msg), "Archer (pid %d) exited", dead); ERR(msg); pid_archer = -1; }
            else if (dead == pid_network) { snprintf(msg, sizeof(msg), "NetworkManager (pid %d) exited", dead); WARN(msg); pid_network = -1; }
            else if (dead == pid_avahi)   { snprintf(msg, sizeof(msg), "avahi (pid %d) exited", dead); WARN(msg); pid_avahi = -1; }
            else if (dead == pid_getty1)  { pid_getty1 = -1; }
            else if (dead == pid_getty2)  { pid_getty2 = -1; }
        }

        /* Auto-restart Archer if it died */
        if (pid_archer <= 0) {
            restart_archer();
        }

        /* Auto-restart NetworkManager if it died (e.g. D-Bus wasn't ready at boot) */
        if (pid_network <= 0) {
            char *nm_argv[] = { "/usr/sbin/NetworkManager", "--no-daemon", NULL };
            pid_network = spawn("/usr/sbin/NetworkManager", nm_argv, "/", 0, 0);
            if (pid_network > 0) LOG("NetworkManager restarted");
        }

        /* Auto-restart gettys if kiosk/X exits or user logs out */
        if (pid_getty1 <= 0) {
            pid_t pid = fork();
            if (pid == 0) {
                int tty = open("/dev/tty1", O_RDWR | O_NOCTTY);
                if (tty >= 0) { dup2(tty, 0); dup2(tty, 1); dup2(tty, 2); close(tty); }
                setsid(); ioctl(0, TIOCSCTTY, 1);
                char *a[] = { "/sbin/agetty", "--autologin", "archer", "--noclear", "tty1", "linux", NULL };
                execv("/sbin/agetty", a); _exit(1);
            }
            pid_getty1 = pid;
        }
        if (pid_getty2 <= 0) {
            pid_t pid = fork();
            if (pid == 0) {
                int tty = open("/dev/tty2", O_RDWR | O_NOCTTY);
                if (tty >= 0) { dup2(tty, 0); dup2(tty, 1); dup2(tty, 2); close(tty); }
                setsid(); ioctl(0, TIOCSCTTY, 1);
                /* No --autologin here — see the tty2 spawn in start_services() for why. */
                char *a[] = { "/sbin/agetty", "--noclear", "tty2", "linux", NULL };
                execv("/sbin/agetty", a); _exit(1);
            }
            pid_getty2 = pid;
        }

        /* Update status file every 10 seconds */
        if (++status_tick >= 10) {
            write_status();
            status_tick = 0;
        }

        sleep(1);
    }

    /* Shutdown was requested */
    do_shutdown(g_shutdown_type);

    /* Never reached */
    return 0;
}
