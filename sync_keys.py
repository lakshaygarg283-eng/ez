import sys
from pathlib import Path
from nonecap_manager import apply_nonecap_key, check_nonecap_stats

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python sync_keys.py <NEW_NONECAP_KEY>")
        sys.exit(1)

    new_key = sys.argv[1].strip()
    print(f"Validating key: {new_key[:12]}...")
    ok, res = check_nonecap_stats(new_key)
    if ok and isinstance(res, dict):
        bal = res.get("credits_balance", 0)
        print(f"[+] Key verified! Live balance: {bal:,} credits.")
        apply_nonecap_key(new_key)
        print("[+] Successfully synchronized key across config.json, provision.json, and nonecap_key.txt.")
    else:
        print(f"[-] Key validation failed: {res}")
        choice = input("Apply anyway? (y/N): ").strip().lower()
        if choice == 'y':
            apply_nonecap_key(new_key)
            print("[+] Key applied.")
