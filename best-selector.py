#!/usr/bin/env python3
"""Pick (and optionally apply) the most stable proxy for every selector group via sbcli.

Strategy (per group):
  1. Run PASSES delay tests (group delay) spaced by PASS_INTERVAL (+ jitter).
     A member missing from a pass result counts as a failure (None).
  2. Merge fresh samples with recent cached samples from earlier sessions (lower weight).
  3. Filter: min weighted success ratio, max median delay, max p90 delay.
  4. Score (lower is better): blend of median, mean, p90 and jitter (stdev),
     multiplied by a failure penalty.
  5. Stickiness: keep the current member if it is close enough to the best one.
  6. Otherwise choose randomly among near-best candidates, weighted by 1/score.
  7. Fallbacks: relax the filters if nobody qualifies; if nobody answered in any
     pass, leave the group untouched.
"""
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX platform: skip locking
    fcntl = None

# ----------------------------- configuration -----------------------------
APPLY = True                  # True: apply the best member; False: dry run (report only)

API_URL = os.environ.get("SINGBOX_CLASH_API", "http://127.0.0.1:9999")
API_TOKEN = os.environ.get("SINGBOX_CLASH_TOKEN", "")
SBCLI = "/home/admin/.local/bin/sbcli"

BASE_DIR = Path(__file__).resolve().parent
TAGS_FILE = BASE_DIR / "data" / "selector_tags.txt"
CACHE_FILE = BASE_DIR / "data" / "selector_cache.json"
LOCK_FILE = BASE_DIR / "data" / "selector.lock"
RANKING_FILE = BASE_DIR / "data" / "group_ranking.txt"
SUPER_TAGS_FILE = BASE_DIR / "data" / "super_selector_tags.txt"

PASSES = 2                    # delay-test passes per group
PASS_INTERVAL = 9.0           # seconds between pass starts
PASS_JITTER = 3.0             # random +/- seconds added to the interval
PROBE_TIMEOUT_MS = 6000       # per-probe timeout passed to sbcli (-t)
PROBE_URL = ""                # empty = sbcli default (gstatic 204)

MIN_SUCCESS_RATIO = 0.7       # weighted share of successful probes
MAX_MEDIAN_MS = 1500          # hard cut on median delay
MAX_P90_MS = 3000             # hard cut on ~p90 delay (equals the max on tiny samples)

W_MEDIAN, W_MEAN, W_P90, W_STDEV = 0.45, 0.20, 0.20, 0.15
FAIL_PENALTY = 3.0            # score *= 1 + FAIL_PENALTY * failure_ratio

# A candidate is "close enough" if score <= max(best * (1 + REL), best + ABS_MS).
# The absolute floor stops flapping on measurement noise when scores are small.
STICKY_REL, STICKY_ABS_MS = 0.25, 60      # keep the current member
RANDOM_REL, RANDOM_ABS_MS = 0.15, 30      # random pick among near-best members

OLD_WEIGHT = 0.5              # weight of cached samples vs. fresh ones
CACHE_MAX_SAMPLES = PASSES * 2  # samples kept per member (history ~ as heavy as fresh data)
HISTORY_MAX_AGE = 12 * 3600   # cached samples older than this are ignored (seconds)
CACHE_TTL = 3 * 24 * 3600     # drop groups/members unseen for this long (seconds)

TABLE_ROWS = 6                # members shown per group in the report

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ

def _paint(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def green(t): return _paint("32;1", t)
def red(t): return _paint("31", t)
def yellow(t): return _paint("33", t)
def dim(t): return _paint("2", t)
def bold(t): return _paint("1", t)

def log(msg="", indent=0):
    print(f"{datetime.now():%H:%M:%S} {'  ' * indent}{msg}", flush=True)

def row(msg=""):
    print(" " * 9 + msg, flush=True)

UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")

def short(name):
    m = UUID_RE.search(name)
    if m:
        return m.group()[:8]
    return name if len(name) <= 16 else name[:15] + "…"

class SbcliError(RuntimeError):
    pass

def sbcli(*args, timeout=30.0, parse=True):
    args = [str(a) for a in args]
    cmd = [SBCLI, "-a", API_URL] + (["-T", API_TOKEN] if API_TOKEN else []) + args
    label = " ".join(args[:2])  # never include the token in messages
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SbcliError(f"{label}: {exc}") from exc
    if res.returncode != 0:
        raise SbcliError(f"{label}: {(res.stderr or res.stdout).strip()[:200]}")
    if not parse:
        return res.stdout
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise SbcliError(f"{label}: invalid JSON") from exc
    if not isinstance(data, dict):
        raise SbcliError(f"{label}: unexpected response")
    return data

def get_group(tag):
    info = sbcli("-g", tag)
    return info.get("now"), [m for m in (info.get("all") or []) if isinstance(m, str)]

def probe_group(tag):
    """Return {member: delay_ms} for members that answered (others are absent).

    If the delay command itself fails but the API still serves the group, the
    failure is treated as 'nobody answered' (some controllers return an error
    when every probe times out). If the API is unreachable, SbcliError is raised
    and the caller ignores the pass.
    """
    args = ["-D", tag, "-t", PROBE_TIMEOUT_MS] + (["-u", PROBE_URL] if PROBE_URL else [])
    try:
        result = sbcli(*args, timeout=PROBE_TIMEOUT_MS / 1000 + 20)
    except SbcliError:
        get_group(tag)  # raises SbcliError if the API/group is really unavailable
        return {}
    return {
        k: int(v) for k, v in result.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
    }

def select(tag, name):
    sbcli("-s", tag, name, parse=False)

def clean_samples(raw):
    if not isinstance(raw, list):
        return []
    out = [int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None for x in raw]
    return out[-CACHE_MAX_SAMPLES:]

def load_cache():
    try:
        data = json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(cache, fh, separators=(",", ":"))
        os.replace(tmp, CACHE_FILE)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

def save_ranking(ranking):
    RANKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=RANKING_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            for tag, _score in ranking:
                fh.write(tag + "\n")
        os.replace(tmp, RANKING_FILE)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

def read_super_tags():
    try:
        lines = SUPER_TAGS_FILE.read_text().splitlines()
    except OSError:
        return []
    seen, tags = set(), []
    for line in lines:
        tag = line.strip()
        if tag and not tag.startswith("#") and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags

def apply_super_ranking(ranking):
    supers = read_super_tags()
    if not supers:
        log(yellow("super: no super-selector tags, skipped"))
        return 0
    if not ranking:
        log(yellow("super: empty ranking, skipped"))
        return 0

    log(bold("Applying ranking to super-selectors"))
    errors = 0
    for i, super_tag in enumerate(supers):
        group_tag = ranking[i % len(ranking)][0]
        try:
            select(super_tag, group_tag)
            if get_group(super_tag)[0] != group_tag:
                raise SbcliError("selection did not stick")
            log(f"{short(super_tag):<9} ← {short(group_tag)}", 1)
        except SbcliError as exc:
            log(f"{short(super_tag):<9} {red('✗ ' + str(exc))}", 1)
            errors += 1
    return errors

def prune_cache(cache, tags):
    now, removed = time.time(), 0
    for tag in list(cache):
        e = cache[tag]
        bad = (
            tag not in tags
            or not isinstance(e, dict)
            or not isinstance(e.get("m"), dict)
            or now - float(e.get("t", 0) or 0) > CACHE_TTL
        )
        if bad:
            del cache[tag]
            removed += 1
    return removed

def prune_members(entry, members):
    now, removed, alive = time.time(), 0, set(members)
    for name in list(entry["m"]):
        m = entry["m"][name]
        if name not in alive or not isinstance(m, dict) or now - float(m.get("t", 0) or 0) > CACHE_TTL:
            del entry["m"][name]
            removed += 1
    return removed

def history(entry, name):
    m = entry["m"].get(name)
    if not m or time.time() - float(m.get("t", 0) or 0) > HISTORY_MAX_AGE:
        return []
    return clean_samples(m.get("s"))

def analyze(samples):
    total_w = sum(w for _, w in samples)
    oks = [(d, w) for d, w in samples if d is not None]
    if not oks or total_w <= 0:
        return None
    delays = sorted(d for d, _ in oks)
    n = len(delays)
    ok_w = sum(w for _, w in oks)
    median = statistics.median(delays)
    mean = sum(d * w for d, w in oks) / ok_w
    p90 = delays[min(n - 1, math.ceil(0.9 * n) - 1)]
    stdev = statistics.pstdev(delays) if n > 1 else 0.0
    ratio = ok_w / total_w
    base = W_MEDIAN * median + W_MEAN * mean + W_P90 * p90 + W_STDEV * stdev
    return {
        "ratio": ratio, "median": median, "p90": p90, "stdev": stdev,
        "score": base * (1 + FAIL_PENALTY * (1 - ratio)),
    }

def fail_reason(s):
    if s["ratio"] < MIN_SUCCESS_RATIO:
        return f"ok {s['ratio']:.0%} < {MIN_SUCCESS_RATIO:.0%}"
    if s["median"] > MAX_MEDIAN_MS:
        return f"median > {MAX_MEDIAN_MS}ms"
    if s["p90"] > MAX_P90_MS:
        return f"p90 > {MAX_P90_MS}ms"
    return ""

def limit(best, rel, abs_ms):
    return max(best * (1 + rel), best + abs_ms)

def choose(stats, current):
    strict = [n for n, s in stats.items() if not fail_reason(s)]
    relaxed = not strict
    if strict:
        pool = strict
    else:  # nobody passed: prefer reliability
        top = max(s["ratio"] for s in stats.values())
        pool = [n for n, s in stats.items() if s["ratio"] >= top - 0.1]

    best_name = min(pool, key=lambda n: stats[n]["score"])
    best = stats[best_name]["score"]

    if current in pool:
        keep_limit = limit(best, STICKY_REL, STICKY_ABS_MS)
        cur = stats[current]["score"]
        if cur <= keep_limit:
            return current, f"keep current: score {cur:.0f} <= {keep_limit:.0f} (best {best:.0f})", relaxed
        why = f"current score {cur:.0f} > {keep_limit:.0f}"
    elif current in stats:
        why = "current failed filters"
    else:
        why = "current did not respond"

    near_limit = limit(best, RANDOM_REL, RANDOM_ABS_MS)
    near = [n for n in pool if stats[n]["score"] <= near_limit]
    pick = random.choices(near, weights=[1.0 / max(stats[n]["score"], 1.0) for n in near], k=1)[0]
    how = "best score" if len(near) == 1 else f"random among {len(near)} near-best (<= {near_limit:.0f}), weight 1/score"
    return pick, f"{why}; {how}", relaxed

def print_table(stats, total, current, pick):
    ordered = sorted(stats, key=lambda n: (bool(fail_reason(stats[n])), stats[n]["score"]))
    want = set(ordered[:TABLE_ROWS]) | {pick, current}
    shown = [n for n in ordered if n in want]

    row(dim(f"   {'member':<9} {'now':>4} {'ok%':>5} {'med':>5} {'p90':>5} {'jit':>5} {'score':>6}  note"))
    for n in shown:
        s = stats[n]
        marker = ("→" if n == pick else " ") + ("●" if n == current else " ")
        line = (f"{marker} {short(n):<9} {s['fresh']:>4} {s['ratio'] * 100:>4.0f}% {s['median']:>5.0f} "
                f"{s['p90']:>5} {s['stdev']:>5.0f} {s['score']:>6.0f}  ")
        reason = fail_reason(s)
        if n == pick:
            row(green(line + "✓ selected"))
        else:
            row(line + (red("✗ " + reason) if reason else "✓"))
    tail = []
    if len(stats) > len(shown):
        tail.append(f"+{len(stats) - len(shown)} more answered")
    if total > len(stats):
        tail.append(f"{total - len(stats)} never answered")
    if tail:
        row(dim("   " + ", ".join(tail)))

def read_tags():
    try:
        lines = TAGS_FILE.read_text().splitlines()
    except OSError as exc:
        sys.exit(f"cannot read {TAGS_FILE}: {exc}")
    seen, tags = set(), []
    for line in lines:
        tag = line.strip()
        if tag and not tag.startswith("#") and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags

def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")
    if fcntl is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("another instance is already running")
    return fh

def run():
    started = time.monotonic()
    tags = read_tags()
    if not tags:
        sys.exit(f"no tags in {TAGS_FILE}")

    mode = green("APPLY") if APPLY else yellow("DRY-RUN (nothing will be changed)")
    log(f"{bold('▶ best_selector')} | mode: {mode} | groups: {len(tags)} | "
        f"passes: {PASSES} every ~{PASS_INTERVAL:.0f}s")
    row(dim("legend: → selected   ● current   now = answers in this session"))

    cache = load_cache()
    removed_groups = prune_cache(cache, set(tags))
    removed_members = 0

    # Refresh group membership every session (groups and members can change).
    groups = {}
    for tag in tags:
        label = short(tag)
        try:
            current, members = get_group(tag)
        except SbcliError as exc:
            log(f"{label}  {red('✗ skipped:')} {exc}", 1)
            continue
        if not members:
            log(f"{label}  {yellow('skipped: empty group')}", 1)
            cache.pop(tag, None)
            continue
        entry = cache.setdefault(tag, {"t": time.time(), "m": {}})
        removed_members += prune_members(entry, members)
        known = sum(1 for m in members if history(entry, m))
        groups[tag] = {"label": label, "current": current, "members": members,
                       "new": {m: [] for m in members}, "passes": 0}
        log(f"{label}  {len(members)} members, current {short(current or '-')}, "
            f"history for {known}", 1)
    log(f"cache: removed {removed_groups} stale groups, {removed_members} stale members")
    if not groups:
        sys.exit("no usable groups")

    # Multiple passes spaced in time; each pass probes every group once.
    for p in range(1, PASSES + 1):
        pass_start = time.monotonic()
        log(bold(f"Pass {p}/{PASSES}"))
        for tag, g in groups.items():
            try:
                answered = probe_group(tag)
            except SbcliError as exc:
                log(f"{g['label']}  {red('✗ pass ignored:')} {exc}", 1)
                continue
            hits = {m: d for m, d in answered.items() if m in g["new"]}
            for m in g["members"]:
                g["new"][m].append(hits.get(m))  # None = no response
            g["passes"] += 1
            fastest = f"fastest {min(hits.values())}ms" if hits else red("no answers")
            log(f"{g['label']}  {len(hits):>2}/{len(g['members'])} answered, {fastest}", 1)
        if p < PASSES:
            wait = PASS_INTERVAL + random.uniform(-PASS_JITTER, PASS_JITTER) - (time.monotonic() - pass_start)
            if wait > 0:
                log(dim(f"waiting {wait:.0f}s"))
                time.sleep(wait)

    # Evaluate, apply, update cache.
    log(bold("Results"))
    summary, errors, now = [], 0, time.time()
    ranking = []  # +++ ranking: (tag, score) of the selected member per group
    for tag, g in groups.items():
        entry, label, current = cache[tag], g["label"], g["current"]
        log(f"━━ {bold(label)} ━━", 0)
        if g["passes"] == 0:
            log(red("no successful passes, selection unchanged"), 1)
            summary.append((label, "ERROR", "-", None))
            errors += 1
            continue

        stats = {}
        for m in g["members"]:
            fresh, old = g["new"][m], history(entry, m)
            if fresh:
                entry["m"][m] = {"s": clean_samples(old + fresh), "t": now}
            res = analyze([(d, OLD_WEIGHT) for d in old] + [(d, 1.0) for d in fresh])
            if res:
                res["fresh"] = f"{sum(d is not None for d in fresh)}/{len(fresh)}"
                stats[m] = res
        entry["t"] = now

        if not stats:
            log(yellow("nobody answered in any pass, selection unchanged"), 1)
            summary.append((label, "KEPT", short(current or "-"), None))
            continue

        pick, reason, relaxed = choose(stats, current)
        print_table(stats, len(g["members"]), current, pick)
        if relaxed:
            log(yellow("no member passed the filters, using relaxed pool"), 1)
        log(f"decision: {reason}", 1)
        ranking.append((tag, stats[pick]["score"]))  # +++ ranking

        med = stats[pick]["median"]
        if pick == current:
            log(green(f"✔ kept {short(pick)}"), 1)
            summary.append((label, "KEPT", short(pick), med))
        elif not APPLY:
            log(yellow(f"dry-run: would switch {short(current or '-')} → {short(pick)}"), 1)
            summary.append((label, "WOULD SET", short(pick), med))
        else:
            try:
                select(tag, pick)
                if get_group(tag)[0] != pick:
                    raise SbcliError("selection did not stick")
                log(green(f"✔ switched {short(current or '-')} → {short(pick)}"), 1)
                summary.append((label, "SET", short(pick), med))
            except SbcliError as exc:
                log(red(f"✗ apply failed: {exc}"), 1)
                summary.append((label, "ERROR", short(pick), med))
                errors += 1

    ranking.sort(key=lambda kv: kv[1])
    if ranking:
        log(bold("Group ranking (best → worst)"))
        for rank, (tag, score) in enumerate(ranking, 1):
            row(f"{rank:>2}. {short(tag):<9} score {score:>6.0f}")
        try:
            save_ranking(ranking)
        except OSError as exc:
            log(red(f"ranking save failed: {exc}"))
            errors += 1
    else:
        log(yellow("no ranked groups (nobody answered)"))
    
    if APPLY:
        errors += apply_super_ranking(ranking)
    else:
        log(yellow("super: dry-run, ranking not applied to super-selectors"))

    try:
        save_cache(cache)
    except OSError as exc:
        log(red(f"cache save failed: {exc}"))
        errors += 1

    log(bold("Summary"))
    row(dim(f"{'group':<9} {'action':<10} {'member':<9} {'median':>6}"))
    for label, action, member, med in summary:
        color = {"SET": green, "ERROR": red, "WOULD SET": yellow}.get(action, lambda t: t)
        row(color(f"{label:<9} {action:<10} {member:<9} {('%.0fms' % med) if med is not None else '-':>6}"))
    log(f"done in {time.monotonic() - started:.0f}s, errors: {errors}")
    return 1 if errors else 0

def main():
    lock = acquire_lock()  # keep a reference for the process lifetime
    try:
        code = run()
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        code = 130
    finally:
        lock.close()
    sys.exit(code)

if __name__ == "__main__":
    main()
