#!/usr/bin/env python3
"""Select the most stable and fast Tor bridges and write them to bridges.conf."""
import fcntl, json, logging, os, random, re, shutil, socket, statistics
import subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor

# ---- Settings ----
LIST_URL = ("https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/"
            "main/TOR-BRIDGES/TOR_BRIDGES_TOP100.txt")
OUT = "/etc/tor/bridges.conf"
STATE_DIR = "data"
CACHE = f"{STATE_DIR}/bridges.list"
HISTORY_FILE = f"{STATE_DIR}/bs_history.json"
LOCK_FILE = f"{STATE_DIR}/bs_lock"
PLUGIN = "/usr/bin/obfs4proxy"        # or /usr/bin/lyrebird
SERVICE = "tor@default"

KEEP = 5                  # bridges kept in config
MAX_CANDIDATES = 30       # bridges probed per run
KNOWN_TOP = 10            # best known bridges always re-probed
WORKERS = 6
BOOT_TIMEOUT = 75         # seconds to reach Bootstrapped 100%
BOOT_WEIGHT = 0.25        # weight of bootstrap time in latency score
URL = "https://check.torproject.org/api/ip"
REQUESTS = 5              # requests per probe, each on a new circuit
REQ_TIMEOUT = 15
MIN_SUCCESS = 4           # min successful requests for a probe to count
HISTORY = 8               # probe results remembered per bridge
MIN_RUNS = 2              # min history length to be "eligible"
MIN_RELIABILITY = 0.75    # min success ratio over history to be "eligible"
SWITCH_FACTOR = 1.5       # keep current bridge unless worse than best * factor
MIN_LIST_SIZE = 5         # sanity check for downloaded list

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("bridge-select")

ADDR_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+|\[[0-9a-fA-F:]+\]):(\d+)")


def parse(text):
    """Return {host:port: bridge_line}; skip comments and empty lines."""
    res = {}
    for line in text.splitlines():
        line = re.sub(r"^Bridge\s+", "", line.strip(), flags=re.I)
        if not line or line.startswith("#"):
            continue
        m = ADDR_RE.search(line)
        if m:
            res[f"{m.group(1)}:{m.group(2)}"] = line
    return res


def fetch_list():
    """Download the bridge list with curl; fall back to the cached copy."""
    tmp = CACHE + ".tmp"
    r = subprocess.run(
        ["curl", "-fsSL", "--retry", "5", "--retry-delay", "3",
         "--retry-all-errors", "--connect-timeout", "10", "--max-time", "30",
         "-o", tmp, LIST_URL], capture_output=True, text=True)
    if r.returncode == 0:
        bridges = parse(open(tmp).read())
        if len(bridges) >= MIN_LIST_SIZE:
            os.replace(tmp, CACHE)
            log.info("downloaded list: %d bridges", len(bridges))
            return bridges
        log.warning("downloaded list looks invalid (%d bridges)", len(bridges))
    else:
        log.warning("list download failed: %s", r.stderr.strip())
    if os.path.exists(tmp):
        os.remove(tmp)
    if os.path.exists(CACHE):
        bridges = parse(open(CACHE).read())
        log.warning("using cached list: %d bridges", len(bridges))
        return bridges
    log.error("no list available (download failed, no cache)")
    sys.exit(1)


def tcp_ok(item):
    key, host, port = item
    try:
        socket.create_connection((host, port), timeout=4).close()
        return key, True
    except OSError:
        return key, False


def probe(args):
    """Start a temporary Tor with one bridge. Return (key, score or None)."""
    idx, key, line = args
    port = 20000 + idx
    tmp = tempfile.mkdtemp(prefix="torprobe-")
    os.chmod(tmp, 0o700)
    open(f"{tmp}/torrc", "w").write(
        f"DataDirectory {tmp}/data\nSocksPort 127.0.0.1:{port}\n"
        f"ControlPort 0\nLog notice stdout\nUseBridges 1\n"
        f"ClientTransportPlugin obfs4 exec {PLUGIN}\nBridge {line}\n")
    p = subprocess.Popen(["tor", "-f", f"{tmp}/torrc"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    timer = threading.Timer(BOOT_TIMEOUT, p.kill)
    timer.start()
    start, score = time.monotonic(), None
    try:
        for out in p.stdout:
            if "Bootstrapped 100%" not in out:
                continue
            timer.cancel()
            boot = time.monotonic() - start
            times = []
            for i in range(REQUESTS):
                t0 = time.monotonic()
                r = subprocess.run(
                    ["curl", "-s", "-m", str(REQ_TIMEOUT), "-x",
                     f"socks5h://r{i}:x@127.0.0.1:{port}", URL],
                    capture_output=True, text=True)
                if r.returncode == 0 and '"IsTor":true' in r.stdout.replace(" ", ""):
                    times.append(time.monotonic() - t0)
            if len(times) >= MIN_SUCCESS:
                score = statistics.median(times) + BOOT_WEIGHT * boot
                log.info("probe OK   %s boot=%.1fs ok=%d/%d median=%.1fs score=%.1f",
                         key, boot, len(times), REQUESTS,
                         statistics.median(times), score)
            else:
                log.info("probe FAIL %s requests ok=%d/%d", key, len(times), REQUESTS)
            break
        else:
            log.info("probe FAIL %s bootstrap failed or timed out", key)
    finally:
        timer.cancel()
        p.kill()
        p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    return key, score


def stats(hist):
    """Return (reliability, score); lower score is better."""
    ok = [x for x in hist if x is not None]
    if not hist or not ok:
        return 0.0, float("inf")
    rel = len(ok) / len(hist)
    return rel, statistics.median(ok) / rel ** 2


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("another run is in progress, exiting")
        return

    current = parse(open(OUT).read()) if os.path.exists(OUT) else {}
    bridges = {**current, **fetch_list()}      # current ones stay testable
    try:
        history = json.load(open(HISTORY_FILE))
    except (OSError, ValueError):
        history = {}
    history = {k: v for k, v in history.items() if k in bridges}

    # TCP prefilter
    items = [(k, *k.rsplit(":", 1)) for k in bridges]
    items = [(k, h.strip("[]"), int(p)) for k, h, p in items]
    with ThreadPoolExecutor(32) as ex:
        alive = {k for k, ok in ex.map(tcp_ok, items) if ok}
    for k in set(bridges) - alive:             # unreachable counts as failure
        if k in history:
            history[k] = (history[k] + [None])[-HISTORY:]
    log.info("bridges: total=%d tcp_alive=%d current=%d",
             len(bridges), len(alive), len(current))

    # Candidates: current + best known + random unseen/others
    picks = [k for k in current if k in alive]
    known = sorted((k for k in alive if k in history and k not in picks),
                   key=lambda k: stats(history[k])[1])
    picks += known[:KNOWN_TOP]
    rest = [k for k in alive if k not in picks]
    random.shuffle(rest)
    picks = (picks + rest)[:MAX_CANDIDATES]
    log.info("probing %d candidates", len(picks))

    jobs = [(i, k, bridges[k]) for i, k in enumerate(picks)]
    with ThreadPoolExecutor(WORKERS) as ex:
        results = dict(ex.map(probe, jobs))
    for k, score in results.items():
        history[k] = (history.get(k, []) + [score])[-HISTORY:]
    json.dump(history, open(HISTORY_FILE, "w"))

    # Rank bridges that worked in this run: eligible first, then by score
    ranked = []
    for k, score in results.items():
        if score is None:
            continue
        rel, sc = stats(history[k])
        eligible = len(history[k]) >= MIN_RUNS and rel >= MIN_RELIABILITY
        ranked.append((0 if eligible else 1, sc, k))
    ranked.sort()
    if not ranked:
        log.error("no working bridges found, config unchanged")
        return
    best_score = ranked[0][1]
    info = {k: (tier, sc) for tier, sc, k in ranked}

    # Hysteresis: keep good current bridges, fill the rest from ranking
    chosen = [k for k in current
              if k in info and info[k][0] == 0 and info[k][1] <= best_score * SWITCH_FACTOR]
    chosen = chosen[:KEEP]
    for _, _, k in ranked:
        if len(chosen) >= KEEP:
            break
        if k not in chosen:
            chosen.append(k)

    for k in chosen:
        rel, sc = stats(history[k])
        log.info("selected %s reliability=%.2f score=%.1f runs=%d",
                 k, rel, sc, len(history[k]))

    if set(chosen) == set(current):
        log.info("config unchanged")
        return
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        f.write("".join(f"Bridge {bridges[k]}\n" for k in chosen))
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)
    r = subprocess.run(["systemctl", "reload", SERVICE], capture_output=True, text=True)
    if r.returncode == 0:
        log.info("config updated, %s reloaded", SERVICE)
    else:
        log.error("reload failed: %s", r.stderr.strip())


main()

