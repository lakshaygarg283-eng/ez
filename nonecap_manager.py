import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = Path(__file__).resolve().parent

KEY_FILE = BASE_DIR / "nonecap_key.txt"
KEYS_POOL_FILE = BASE_DIR / "nonecap_keys.txt"
EXHAUSTED_FILE = BASE_DIR / "nonecap_exhausted.txt"
CONFIG_FILE = BASE_DIR / "config.json"
EXT_PROVISION_FILE = BASE_DIR / "extension" / "orvigen_ext" / "provision.json"
BACKUP_PROVISION_FILE = BASE_DIR / "nonecap-extension-v0.3.102" / "provision.json"

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://nonecap.com/',
    'Origin': 'https://nonecap.com',
    'Sec-Ch-Ua': '"Chromium";v="130", "Brave";v="130", "Not?A_Brand";v="99"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-site',
}

# Styling
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def read_json_safe(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def write_json_safe(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, path)


def get_current_key() -> str:
    if KEY_FILE.is_file():
        k = KEY_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k
    cfg = read_json_safe(CONFIG_FILE)
    return cfg.get("captcha", {}).get("nonecap_key", "").strip()


def check_nonecap_stats(key: str) -> tuple[bool, dict | str]:
    if not key:
        return False, "Empty key"
    url = "https://api.nonecap.com/v1/ext/stats"
    headers = dict(DEFAULT_HEADERS)
    headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return True, data
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            err_msg = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        if e.code == 401:
            return False, "401 Unauthorized"
        elif e.code == 403:
            return False, "403 Forbidden"
        elif e.code == 429:
            return False, "429 Rate Limited"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"NETWORK_ERROR: {e}"


def get_exhausted_keys() -> set[str]:
    if not EXHAUSTED_FILE.is_file():
        return set()
    try:
        lines = EXHAUSTED_FILE.read_text(encoding="utf-8").splitlines()
        return {line.strip() for line in lines if line.strip() and not line.startswith("#")}
    except Exception:
        return set()


def mark_key_exhausted(key: str, reason: str = "Credits exhausted"):
    if not key:
        return
    with open(EXHAUSTED_FILE, "a", encoding="utf-8") as f:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{key}  # {timestamp} - {reason}\n")


def get_available_backup_keys() -> list[str]:
    exhausted = get_exhausted_keys()
    candidates = []
    if KEYS_POOL_FILE.is_file():
        lines = KEYS_POOL_FILE.read_text(encoding="utf-8").splitlines()
        for line in lines:
            k = line.strip()
            if k and not k.startswith("#") and k not in exhausted:
                if k not in candidates:
                    candidates.append(k)
    cfg = read_json_safe(CONFIG_FILE)
    backup_list = cfg.get("captcha", {}).get("backup_keys", [])
    for k in backup_list:
        k = k.strip()
        if k and k not in exhausted and k not in candidates:
            candidates.append(k)
    return candidates


def apply_nonecap_key(new_key: str):
    new_key = new_key.strip()
    if not new_key:
        return False
    KEY_FILE.write_text(new_key + "\n", encoding="utf-8")

    cfg = read_json_safe(CONFIG_FILE)
    cfg.setdefault("captcha", {})["nonecap_key"] = new_key
    write_json_safe(CONFIG_FILE, cfg)

    for prov_path in [EXT_PROVISION_FILE, BACKUP_PROVISION_FILE]:
        prov = read_json_safe(prov_path)
        prov["key"] = new_key
        prov["apiKey"] = new_key
        prov.setdefault("settings", {
            "autoSolve": True,
            "style": "human",
            "showOverlay": True,
            "grid": True,
            "drag": True
        })
        write_json_safe(prov_path, prov)

    return True


def rotate_to_next_key(current_key: str, reason: str = "Credits exhausted") -> str | None:
    if current_key:
        mark_key_exhausted(current_key, reason)

    candidates = get_available_backup_keys()
    for cand in candidates:
        if cand != current_key:
            ok, stats = check_nonecap_stats(cand)
            if ok and isinstance(stats, dict) and stats.get("credits_balance", 0) > 80:
                apply_nonecap_key(cand)
                return cand
            elif not ok:
                mark_key_exhausted(cand, f"Invalid candidate: {stats}")

    return None


class NoneCapManager:
    def __init__(self):
        self.current_key = get_current_key()
        self.balance = None
        self.solves = 0
        self.solve_rate = 0.0
        self.last_balance = None
        self.consecutive_errors = 0

    def refresh(self) -> dict:
        if not self.current_key:
            self.current_key = get_current_key()

        ok, res = check_nonecap_stats(self.current_key)
        if ok and isinstance(res, dict):
            self.consecutive_errors = 0
            new_bal = res.get("credits_balance", 0)
            self.solves = res.get("month_solves", 0)
            sr = res.get("solve_rate")
            self.solve_rate = float(sr) * 100.0 if sr is not None else 0.0

            self.last_balance = self.balance
            self.balance = new_bal

            if self.balance < 80:
                new_key = rotate_to_next_key(self.current_key, f"Low balance ({self.balance} cr)")
                if new_key:
                    self.current_key = new_key
                    self.refresh()
            return {
                "status": "ok",
                "balance": self.balance,
                "solves": self.solves,
                "rate": self.solve_rate,
                "key": self.current_key[:12] + "..."
            }
        else:
            self.consecutive_errors += 1
            err_str = str(res)
            if "401" in err_str or "403" in err_str or self.consecutive_errors >= 3:
                new_key = rotate_to_next_key(self.current_key, f"API Error: {err_str}")
                if new_key:
                    self.current_key = new_key
                    return self.refresh()
            return {
                "status": "error",
                "message": err_str,
                "balance": self.balance,
                "key": self.current_key[:12] + "..." if self.current_key else "None"
            }


def standalone_cli():
    """Interactive command-line interface when nonecap_manager.py is run directly."""
    while True:
        curr_key = get_current_key()
        print(f"\n{CYAN}{BOLD}==================== NONECAP MANAGER DASHBOARD ===================={RESET}")
        print(f"  • Active Key:   {YELLOW}{curr_key[:14]}...{curr_key[-6:] if len(curr_key) > 20 else ''}{RESET}")

        print(f"  • Checking live balance...", end="", flush=True)
        ok, res = check_nonecap_stats(curr_key)
        if ok and isinstance(res, dict):
            bal = res.get('credits_balance', 0)
            solves = res.get('month_solves', 0)
            sr = res.get('solve_rate')
            rate_str = f"{float(sr)*100:.1f}%" if sr is not None else "N/A"
            color = GREEN if bal >= 80 else RED
            print(f"\r  • Balance:      {BOLD}{color}{bal:,} credits{RESET} ({rate_str} success | {solves} solves)")
        else:
            print(f"\r  • Balance:      {RED}Error: {res}{RESET}")

        print(f"{CYAN}-------------------------------------------------------------------{RESET}")
        print(f"  [{GREEN}{BOLD}1{RESET}] Check All Keys in Pool ({len(get_available_backup_keys())} available)")
        print(f"  [{YELLOW}{BOLD}2{RESET}] Paste / Apply New Active Key")
        print(f"  [{CYAN}{BOLD}3{RESET}] Rotate to Next Backup Key Now")
        print(f"  [{RED}{BOLD}Q{RESET}] Quit")
        print(f"{CYAN}==================================================================={RESET}")
        sys.stdout.write(f"{CYAN}> {RESET}")
        sys.stdout.flush()

        choice = sys.stdin.readline().strip().lower()
        if choice == '1':
            keys = get_available_backup_keys()
            print(f"\n{BOLD}Scanning {len(keys)} backup keys...{RESET}")
            for i, k in enumerate(keys, 1):
                ok, res = check_nonecap_stats(k)
                status = f"{GREEN}{res.get('credits_balance', 0):,} cr{RESET}" if ok and isinstance(res, dict) else f"{RED}{res}{RESET}"
                active_tag = f" {CYAN}[ACTIVE]{RESET}" if k == curr_key else ""
                print(f"  [{i}] {k[:14]}... : {status}{active_tag}")
            input("\nPress Enter to continue...")
        elif choice == '2':
            print(f"\n{YELLOW}Paste your new NoneCap key (nc_live_...):{RESET}")
            sys.stdout.write(f"{CYAN}> {RESET}")
            sys.stdout.flush()
            cand = sys.stdin.readline().strip()
            if cand:
                apply_nonecap_key(cand)
                print(f"{GREEN}[+] Key applied successfully!{RESET}")
                time.sleep(1)
        elif choice == '3':
            nxt = rotate_to_next_key(curr_key, "Manual rotation requested from CLI")
            if nxt:
                print(f"{GREEN}[+] Switched to: {nxt[:14]}...{RESET}")
            else:
                print(f"{RED}[!] No valid backup keys remaining in pool!{RESET}")
            time.sleep(1.5)
        elif choice in ('q', 'exit'):
            break


if __name__ == "__main__":
    standalone_cli()
