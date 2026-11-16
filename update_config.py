#!/usr/bin/env python3
import json
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

# Source
BASE = "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/"
FILES = [
    "BLACK_SS%2BAll_RUS.txt",
    "BLACK_SS_WEAK_DPI_RUS.txt",
    "BLACK_VLESS_RUS.txt",
    "BLACK_VLESS_RUS_mobile.txt",
    "WHITE-CIDR-RU-all.txt",
]

# curl settings
CURL_BIN = "curl"
CURL_RETRIES = 4
CURL_RETRY_DELAY = 1
CURL_TIMEOUT = 9

# Paths
DATA_DIR = Path("data")
OUT_DIR = DATA_DIR / "downloads"
RESULT_FILE = DATA_DIR / "unique_links.txt"
OUTBOUNDS_JSON = DATA_DIR / "outbounds.json"
SELECTORS_JSON = DATA_DIR / "selectors.json"
TAGS_FILE = DATA_DIR / "outbound_tags.txt"
SELECTOR_TAGS_FILE = DATA_DIR / "selector_tags.txt"
SUPER_SELECTOR_TAGS_FILE = DATA_DIR / "super_selector_tags.txt"
LINKS_FILE = DATA_DIR / "links.txt"
CONFIG_JSON = Path("config.json")

ALL_PREFIX = ""

# Processing
SHUFFLE = True
RANDOM_SEED = time.time_ns()  # set an int for reproducible shuffle
CHUNK_SIZE = 33
SELECTOR_TYPE = "selector"
SELECTOR_PREFIX = f"{ALL_PREFIX}selector-"
SUPER_SELECTOR_PREFIX = f"{ALL_PREFIX}super-selector-"
SUPER_SELECTOR_COUNT = 3

# Common inbound settings
INBOUND_LISTEN = "::"
USER_PASSWORD = "?"
USER_PREFIX = f"{ALL_PREFIX}ss"

# Inbounds: any of them can be removed from the list
INBOUNDS = [
    {
        "scheme": "hy2",
        "type": "hysteria2",
        "tag": f"{ALL_PREFIX}hy2-super-selector-in",
        "port": 8080,
        "username_key": "name",
        "link_tag_prefix": "xhy2-",
        "opt": {
            "tls": {
                "enabled": True,
                "server_name": "dobro.ru",
                "alpn": ["h3"],
                "certificate_path": "/etc/sing-box/cert.pem",
                "key_path": "/etc/sing-box/key.pem",
            }
        },
    },
    {
        "scheme": "socks5",
        "type": "mixed",
        "tag": f"{ALL_PREFIX}socks5-super-selector-in",
        "port": 8080,
        "username_key": "username",
        "link_tag_prefix": "xsocks5-",
        "opt": {},  # no tls
    },
]

# Link settings
LINK_HOST = "99.99.99.99"
LINK_TAG_AUTO = "auto"
LINK_TAG_SS = "ss"

# Auto (urltest) settings
AUTO_ENABLED = True
AUTO_PREFIX = f"{ALL_PREFIX}auto-super-selector-"
AUTO_INTERVAL = "7m"
AUTO_TOLERANCE = 40
AUTO_IDLE_TIMEOUT = "33m"

# sbcli settings
SBCLI_BIN = "/home/admin/.local/bin/sbcli"


def download(url: str, dest: Path) -> bool:
    cmd = [
        CURL_BIN, "-L", "-f", "-sS",
        "--retry", str(CURL_RETRIES),
        "--retry-all-errors",
        "--retry-delay", str(CURL_RETRY_DELAY),
        "--max-time", str(CURL_TIMEOUT),
        "-o", str(dest),
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] {url}: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True

def make_link(ib: dict, user: dict) -> str:
    key = ib["username_key"]
    n = int(user[key][len(USER_PREFIX):])
    short = LINK_TAG_AUTO if n == 0 else f"{LINK_TAG_SS}{n}"
    tag = quote(ib["link_tag_prefix"] + short, safe="")
    if ib["scheme"] == "hy2":
        cred = f"{quote(user['password'], safe='')}"
        sni = ib["opt"]["tls"]["server_name"]
        return f"hy2://{cred}@{LINK_HOST}:{ib['port']}/?sni={sni}&insecure=1#{tag}"
    cred = f"{quote(user[key], safe='')}:{quote(user['password'], safe='')}"
    return f"socks5://{cred}@{LINK_HOST}:{ib['port']}#{tag}"

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Download
    downloaded = []
    for name in FILES:
        dest = OUT_DIR / unquote(name)
        print(f"Downloading {name} ...")
        if download(BASE + name, dest):
            downloaded.append(dest)
            print(f"  OK -> {dest}")

    # 2. Collect unique lines, shuffle, save full list
    all_lines = []
    for path in downloaded:
        with path.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    all_lines.append(line)

    unique_lines = list(dict.fromkeys(all_lines))

    if SHUFFLE:
        random.Random(RANDOM_SEED).shuffle(unique_lines)

    RESULT_FILE.write_text("\n".join(unique_lines) + "\n", encoding="utf-8")

    print(f"\nTotal lines: {len(all_lines)}")
    print(f"Unique lines: {len(unique_lines)}")
    print(f"Result: {RESULT_FILE}")

    # 3. Run sbcli on the full list
    cmd = [SBCLI_BIN, "-lZ", str(RESULT_FILE), "-o", str(OUTBOUNDS_JSON)]
    print("Running:", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        sys.exit(f"[ERROR] {SBCLI_BIN} not found in PATH")
    except subprocess.CalledProcessError as e:
        sys.exit(f"[ERROR] {SBCLI_BIN} exited with code {e.returncode}")

    # 4. Read outbounds JSON, collect tags
    outbounds = json.loads(OUTBOUNDS_JSON.read_text(encoding="utf-8"))
    tags = [o["tag"] for o in outbounds if isinstance(o, dict) and o.get("tag")]
    tags = list(dict.fromkeys(tags))
    if not tags:
        sys.exit(f"[ERROR] no tags found in {OUTBOUNDS_JSON}")
    print(f"Tags: {len(tags)}")

    TAGS_FILE.write_text("\n".join(tags) + "\n", encoding="utf-8")
    print(f"Tags -> {TAGS_FILE}")

    # 5. Split tags into chunks, build selectors
    selectors = [
        {
            "type": SELECTOR_TYPE,
            "tag": f"{SELECTOR_PREFIX}{uuid.uuid4()}",
            "outbounds": tags[i:i + CHUNK_SIZE],
        }
        for i in range(0, len(tags), CHUNK_SIZE)
    ]

    SELECTORS_JSON.write_text(
        json.dumps(selectors, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Selectors: {len(selectors)} -> {SELECTORS_JSON}")

    # 6. Save selector tags to a separate file
    SELECTOR_TAGS_FILE.write_text(
        "\n".join(s["tag"] for s in selectors) + "\n", encoding="utf-8"
    )
    print(f"Selector tags -> {SELECTOR_TAGS_FILE}")

    # 7. Build super selectors, each containing all regular selectors
    selector_tags = [s["tag"] for s in selectors]
    super_selectors = [
        {
            "type": SELECTOR_TYPE,
            "tag": f"{SUPER_SELECTOR_PREFIX}{uuid.uuid4()}",
            "outbounds": list(selector_tags),
        }
        for _ in range(SUPER_SELECTOR_COUNT)
    ]
    print(f"Super selectors: {len(super_selectors)}")

    SUPER_SELECTOR_TAGS_FILE.write_text(
        "\n".join(s["tag"] for s in super_selectors) + "\n", encoding="utf-8"
    )
    print(f"Super selector tags -> {SUPER_SELECTOR_TAGS_FILE}")

    # 8. Optional auto outbound (urltest over all regular selectors)
    autos = []
    if AUTO_ENABLED:
        autos.append({
            "type": "urltest",
            "tag": f"{AUTO_PREFIX}{uuid.uuid4()}",
            "outbounds": list(selector_tags),
            "interval": AUTO_INTERVAL,
            "tolerance": AUTO_TOLERANCE,
            "idle_timeout": AUTO_IDLE_TIMEOUT,
        })

    # 9. Build inbounds with users and routes
    # user0 -> auto (optional), user1..N -> super selectors (same for every inbound)
    targets = autos + super_selectors
    first = 0 if AUTO_ENABLED else 1
    inbounds = []
    rules = []
    links = []
    for ib in INBOUNDS:
        key = ib["username_key"]
        users = [
            {key: f"{USER_PREFIX}{i}", "password": f"{USER_PASSWORD}{i}"}
            for i in range(first, first + len(targets))
        ]
        inbounds.append({
            "type": ib["type"],
            "tag": ib["tag"],
            "listen": INBOUND_LISTEN,
            "listen_port": ib["port"],
            "users": users,
        } | ib["opt"])
        rules += [
            {
                "inbound": [ib["tag"]],
                "auth_user": [user[key]],
                "action": "route",
                "outbound": t["tag"],
            }
            for user, t in zip(users, targets)
        ]
        links += [make_link(ib, u) for u in users]

    # 10. Save links
    LINKS_FILE.write_text("\n".join(links) + "\n", encoding="utf-8")
    print(f"Links -> {LINKS_FILE}")
    for link in links:
        print(f"  {link}")

    # 11. Build final config
    config = {
        "inbounds": inbounds,
        "outbounds": outbounds + selectors + super_selectors + autos,
        "route": {"rules": rules},
    }
    CONFIG_JSON.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Config -> {CONFIG_JSON}")


if __name__ == "__main__":
    main()
