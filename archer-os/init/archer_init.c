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

/* ── start all services ──────────────────────────────────────────── */

static void start_services(void)
{
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

    /* 4. Getty on tty1 — gives us an interactive login shell on the console.
     *    --autologin archer: no password prompt, logs straight in as archer.
     *    Without this, keystrokes appear on screen but nothing processes them
     *    because our init doesn't run bash by default.
     *    We open /dev/tty1 explicitly so stdout/stderr go there, not /dev/kmsg. */
    {
        pid_t pid = fork();
        if (pid == 0) {
            /* Open tty1 as stdin/stdout/stderr for the getty process */
            int tty = open("/dev/tty1", O_RDWR | O_NOCTTY);
            if (tty >= 0) {
                dup2(tty, STDIN_FILENO);
                dup2(tty, STDOUT_FILENO);
                dup2(tty, STDERR_FILENO);
                if (tty > STDERR_FILENO) close(tty);
            }
            /* setsid: make this process a session leader so it can control tty1 */
            setsid();
            /* TIOCSCTTY: claim tty1 as our controlling terminal */
            ioctl(STDIN_FILENO, TIOCSCTTY, 1);

            char *argv[] = {
                "/sbin/agetty",
                "--autologin", "archer",
                "--noclear",
                "tty1",
                "linux",
                NULL
            };
            execv("/sbin/agetty", argv);
            _exit(1);
        }
        if (pid > 0)
            LOG("getty started on tty1");
    }

    /* Small delay: let NetworkManager initialize before Archer tries to use the network */
    sleep(2);

    /* 5. Archer Flask app — the truck AI */
    {
        char *argv[] = { ARCHER_VENV, ARCHER_APP, NULL };
        char *env[]  = {
            "HOME=/home/archer",
            "USER=archer",
            "PORT=5000",
            "PYTHONUNBUFFERED=1",    /* don't buffer stdout — we see logs immediately */
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

    LOG("archer_init: initialization complete, starting services");

    /*
     * Step 5: Start services
     * NetworkManager → avahi → Archer (with 2s delay between NM and Archer)
     */
    start_services();
    write_status();

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
            /* Log which service died */
            char msg[64];
            if      (dead == pid_archer)  { snprintf(msg, sizeof(msg), "Archer (pid %d) exited", dead); ERR(msg); pid_archer = -1; }
            else if (dead == pid_network) { snprintf(msg, sizeof(msg), "NetworkManager (pid %d) exited", dead); WARN(msg); pid_network = -1; }
            else if (dead == pid_avahi)   { snprintf(msg, sizeof(msg), "avahi (pid %d) exited", dead); WARN(msg); pid_avahi = -1; }
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
