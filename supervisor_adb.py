import ctypes
from ctypes import wintypes
import json
import msvcrt
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

try:
    import psutil
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "psutil"], check=True)
    import psutil

try:
    import win32gui
    import win32process
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "pywin32"], check=True)
    import win32gui
    import win32process

from nonecap_manager import NoneCapManager, apply_nonecap_key
from coregen5_navigation import navigate

# Win32 Constants and Types
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

class Coord(ctypes.Structure):
    _fields_ = [('X', wintypes.SHORT), ('Y', wintypes.SHORT)]

class Rect(ctypes.Structure):
    _fields_ = [(name, wintypes.SHORT) for name in ('Left', 'Top', 'Right', 'Bottom')]

class ScreenInfo(ctypes.Structure):
    _fields_ = [
        ('size', Coord), ('cursor', Coord), ('attributes', wintypes.WORD),
        ('window', Rect), ('max_size', Coord),
    ]

class KeyEvent(ctypes.Structure):
    _fields_ = [
        ('down', wintypes.BOOL), ('repeat', wintypes.WORD),
        ('vk', wintypes.WORD), ('scan', wintypes.WORD),
        ('char', ctypes.c_wchar), ('state', wintypes.DWORD),
    ]

class InputEvent(ctypes.Structure):
    _fields_ = [('kind', wintypes.WORD), ('key', KeyEvent)]

kernel32.AttachConsole.argtypes = [wintypes.DWORD]
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ScreenInfo)]
kernel32.ReadConsoleOutputCharacterW.argtypes = [
    wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, Coord, ctypes.POINTER(wintypes.DWORD),
]
kernel32.WriteConsoleInputW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(InputEvent), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
kernel32.PeekConsoleInputW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(InputEvent), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]

THREAD_SUSPEND_RESUME = 0x0002
OpenThread = kernel32.OpenThread
OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenThread.restype = wintypes.HANDLE
SuspendThread = kernel32.SuspendThread
SuspendThread.argtypes = [wintypes.HANDLE]
SuspendThread.restype = wintypes.DWORD
ResumeThread = kernel32.ResumeThread
ResumeThread.argtypes = [wintypes.HANDLE]
ResumeThread.restype = wintypes.DWORD

BASE_DIR = Path(__file__).resolve().parent
GEN_EXE = BASE_DIR / "gen.exe"
STATS_FILE = BASE_DIR / "session_stats.json"
HEARTBEAT_FILE = BASE_DIR / "supervisor_heartbeat.txt"

ADB_BIN = BASE_DIR / "platform-tools" / "adb.exe"
if not ADB_BIN.is_file():
    ADB_BIN = "adb"

POST_REGISTRATION_COOLDOWN = 8.0

# Styling
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def log(tag: str, msg: str, color: str = ""):
    ts = time.strftime("%H:%M:%S")
    print(f"{BOLD}[{ts}]{RESET} {color}[{tag}]{RESET} {msg}", flush=True)


def run_auto_navigator_subproc(pid: int):
    """Subprocess attached directly to gen.exe console buffer to answer prompts and skip intervals."""
    kernel32.FreeConsole()
    for _ in range(40):
        if kernel32.AttachConsole(pid):
            break
        if not psutil.pid_exists(pid):
            sys.exit(1)
        time.sleep(0.1)
    else:
        sys.exit(1)

    handles = []
    try:
        for device in ('CONOUT$', 'CONIN$'):
            handle = kernel32.CreateFileW(device, 0xC0000000, 3, None, 3, 0, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
        output, keyboard = handles

        def disable_quick_edit():
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(keyboard, ctypes.byref(mode)):
                updated = (mode.value & ~0x0040) | 0x0080
                if updated != mode.value:
                    kernel32.SetConsoleMode(keyboard, updated)

        disable_quick_edit()

        def read_screen(through_cursor=False):
            screen = ScreenInfo()
            if not kernel32.GetConsoleScreenBufferInfo(output, ctypes.byref(screen)):
                return []
            width = screen.size.X
            if through_cursor:
                top = max(0, screen.cursor.Y - 2)
                count = width * (screen.cursor.Y - top) + screen.cursor.X
            else:
                top = max(screen.window.Top, screen.cursor.Y - 24)
                bottom = min(screen.size.Y - 1, max(screen.cursor.Y + 8, screen.window.Bottom))
                count = width * (bottom - top + 1)
            if count <= 0:
                return []
            buf = ctypes.create_unicode_buffer(count + 1)
            read = wintypes.DWORD()
            if not kernel32.ReadConsoleOutputCharacterW(output, buf, count, Coord(0, top), ctypes.byref(read)):
                return []
            return [buf[i:min(i + width, read.value)].strip() for i in range(0, read.value, width)]

        def send_key(key):
            if key == 'enter':
                vk, scan, char, state = 0x0D, 0x1C, '\r', 0
            elif key == 'backspace':
                vk, scan, char, state = 0x08, 0x0E, '\b', 0
            elif key == 'down':
                vk, scan, char, state = 0x28, 0x50, '\0', 0x100
            else:
                vk, char, state = ord(key), key, 0
                scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
            events = (InputEvent * 2)(
                InputEvent(1, KeyEvent(True, 1, vk, scan, char, state)),
                InputEvent(1, KeyEvent(False, 1, vk, scan, char, state)),
            )
            written = wintypes.DWORD()
            kernel32.WriteConsoleInputW(keyboard, events, 2, ctypes.byref(written))

        def active_adb_wait():
            lines = [line.casefold() for line in read_screen(through_cursor=True) if line]
            if not lines or 'enter=skip' not in lines[-1]:
                return None
            last = lines[-1]
            if 'adb' in last and 'waiting for usb device' in last:
                return last
            if len(lines) > 1 and not last.startswith(('[', '->')):
                combined = ' '.join(lines[-2:])
                if 'adb' in lines[-2] and 'waiting for usb device' in combined:
                    return combined
            return None

        def enter_is_pending():
            events = (InputEvent * 64)()
            available = wintypes.DWORD()
            if not kernel32.PeekConsoleInputW(keyboard, events, len(events), ctypes.byref(available)):
                return True
            return available.value == len(events) or any(
                event.kind == 1 and event.key.down and event.key.vk == 0x0D
                for event in events[:available.value]
            )

        # 1. Complete initial startup navigation (Email -> 9999 -> Connection Mode)
        navigate(read_screen, send_key, lambda: psutil.pid_exists(pid), target_mode="adb")

        # 2. Keep running actively to skip interval (e.g. "Waiting for USB device (Enter=skip)")
        adb_prompt = None
        adb_presses = 0
        next_adb_enter = 0.0
        next_mode_check = 0.0

        while psutil.pid_exists(pid):
            now = time.monotonic()
            if now >= next_mode_check:
                disable_quick_edit()
                next_mode_check = now + 1.0

            prompt = active_adb_wait()
            if prompt != adb_prompt:
                adb_prompt = prompt
                adb_presses = 0
                next_adb_enter = now

            if prompt and adb_presses < 3 and now >= next_adb_enter and not enter_is_pending():
                send_key('enter')
                adb_presses += 1
                next_adb_enter = now + 1.0

            time.sleep(0.15)

    finally:
        for h in handles:
            kernel32.CloseHandle(h)
    sys.exit(0)


class SupervisorADB:
    def __init__(self):
        self.gen_process: subprocess.Popen | None = None
        self.gen_pid: int | None = None
        self.is_rotating = False
        self.running = True
        self.in_menu = False

        self.session_active = True
        self.session_id = f"sess_{int(time.time())}"
        self.start_time = time.strftime('%Y-%m-%d %H:%M:%S')
        self.session_valid = 0
        self.session_locked = 0
        self.base_raw_valid = 0
        self.base_raw_locked = 0
        self.rotations_count = 0
        self.current_exit_ip = "Unknown"

        self.nonecap = NoneCapManager()

    def update_heartbeat(self):
        try:
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        except Exception:
            pass

    def save_stats(self):
        data = {
            "session_active": self.session_active,
            "session_id": self.session_id,
            "start_time": self.start_time,
            "valid_count": self.session_valid,
            "locked_count": self.session_locked,
            "rotations_completed": self.rotations_count,
            "current_ip": self.current_exit_ip,
            "last_update": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        try:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def start_new_session(self):
        self.session_active = True
        self.session_id = f"sess_{int(time.time())}"
        self.start_time = time.strftime('%Y-%m-%d %H:%M:%S')
        curr_v, curr_l = self.read_gen_title_counts()
        self.base_raw_valid = curr_v if curr_v is not None else 0
        self.base_raw_locked = curr_l if curr_l is not None else 0
        self.session_valid = 0
        self.session_locked = 0
        self.save_stats()
        log('+', f"=== NEW SESSION STARTED [{self.session_id}] at {self.start_time} ===", GREEN)

    def finish_session(self):
        if not self.session_active:
            log('!', "No active session to finish.", YELLOW)
            return
        log('+', f"================ SESSION FINISHED [{self.session_id}] ================", GREEN)
        log('+', f"  * Finished at:   {time.strftime('%Y-%m-%d %H:%M:%S')}", GREEN)
        log('+', f"  * Total Valid:   {self.session_valid}", GREEN)
        log('+', f"  * Total Locked:  {self.session_locked}", GREEN)
        log('+', f"===============================================================", GREEN)
        self.session_active = False
        self.save_stats()

    def prompt_session_menu(self):
        self.in_menu = True
        try:
            status_str = f"{GREEN}ACTIVE (ID: {self.session_id}){RESET}" if self.session_active else f"{YELLOW}INACTIVE{RESET}"
            print(f"\n{CYAN}{BOLD}==================== SESSION MANAGEMENT ===================={RESET}")
            print(f"  • Status:       {status_str}")
            print(f"  • Valid EVs:    {BOLD}{GREEN}{self.session_valid}{RESET}")
            print(f"  • Locked EVs:   {BOLD}{YELLOW}{self.session_locked}{RESET}")
            print(f"{CYAN}------------------------------------------------------------{RESET}")
            print(f"  [{GREEN}{BOLD}S{RESET}] Start New Session (resets counter for fresh batch)")
            print(f"  [{RED}{BOLD}F{RESET}] Finish Current Session (ends and saves stats)")
            print(f"  [{YELLOW}{BOLD}U{RESET}] Update NoneCap Key")
            print(f"  [{CYAN}{BOLD}C{RESET}] Cancel / Back to Dashboard")
            print(f"{CYAN}============================================================{RESET}")
            sys.stdout.write(f"{CYAN}> {RESET}")
            sys.stdout.flush()
            choice = sys.stdin.readline().strip().lower()
            if choice == 's':
                self.start_new_session()
            elif choice == 'f':
                self.finish_session()
            elif choice == 'u':
                self.prompt_key_update()
            else:
                log('*', "Returning to dashboard...", YELLOW)
        finally:
            self.in_menu = False

    def prompt_key_update(self):
        print(f"\n{YELLOW}{BOLD}==================== UPDATE NONECAP KEY ===================={RESET}")
        print(f"{YELLOW}Paste your new NoneCap key (nc_live_...) or press Enter to cancel:{RESET}")
        sys.stdout.write(f"{CYAN}> {RESET}")
        sys.stdout.flush()
        cand = sys.stdin.readline().strip()
        if cand:
            apply_nonecap_key(cand)
            self.nonecap.current_key = cand
            self.nonecap.refresh()
            log('+', f"NoneCap key updated successfully!", GREEN)

    def freeze_gen(self):
        if not self.gen_pid:
            return
        try:
            proc = psutil.Process(self.gen_pid)
            for th in proc.threads():
                h_thread = OpenThread(THREAD_SUSPEND_RESUME, False, th.id)
                if h_thread:
                    SuspendThread(h_thread)
                    kernel32.CloseHandle(h_thread)
        except Exception:
            pass

    def unfreeze_gen(self):
        if not self.gen_pid:
            return
        try:
            proc = psutil.Process(self.gen_pid)
            for th in proc.threads():
                h_thread = OpenThread(THREAD_SUSPEND_RESUME, False, th.id)
                if h_thread:
                    ResumeThread(h_thread)
                    kernel32.CloseHandle(h_thread)
        except Exception:
            pass

    def run_adb(self, *args) -> tuple[int, str]:
        cmd = [str(ADB_BIN)] + list(args)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return res.returncode, res.stdout.strip()
        except Exception as e:
            return -1, str(e)

    def verify_adb_device(self) -> bool:
        code, out = self.run_adb("devices")
        lines = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("*")]
        devices = [l for l in lines[1:] if "\tdevice" in l]
        if devices:
            log("+", f"ADB Device connected: {devices[0].split()[0]}", GREEN)
            return True
        log("!", "No active ADB device detected. (Please ensure USB debugging is enabled on phone)", YELLOW)
        return False

    def get_public_ip(self) -> str:
        try:
            res = subprocess.run(["curl.exe", "-s", "--max-time", "3", "https://api.ipify.org"],
                                 capture_output=True, text=True)
            ip = res.stdout.strip()
            if ip and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                return ip
        except Exception:
            pass
        return ""

    def rotate_adb(self) -> bool:
        self.is_rotating = True
        self.freeze_gen()
        old_ip = self.current_exit_ip

        log("*", "Initiating ADB Cellular IP rotation (Airplane Mode Toggle)...", YELLOW)

        # 1. Airplane Mode ON
        self.run_adb("shell", "cmd", "connectivity", "airplane-mode", "enable")
        self.run_adb("shell", "settings", "put", "global", "airplane_mode_on", "1")
        time.sleep(3.5)

        # 2. Airplane Mode OFF
        self.run_adb("shell", "cmd", "connectivity", "airplane-mode", "disable")
        self.run_adb("shell", "settings", "put", "global", "airplane_mode_on", "0")

        # 3. Active Cellular Handshake Polling
        log("*", "Waiting for LTE/5G baseband modem to re-establish cellular data IP...", CYAN)
        start_time = time.time()
        new_ip = ""
        while time.time() - start_time < 25:
            time.sleep(1.0)
            candidate = self.get_public_ip()
            if candidate:
                if candidate != old_ip:
                    new_ip = candidate
                    break
                elif not new_ip:
                    new_ip = candidate

        if new_ip:
            self.current_exit_ip = new_ip
            self.rotations_count += 1
            log("+", f"Confirmed new Cellular Exit IP: {self.current_exit_ip} [Rotations: {self.rotations_count}]", GREEN)
            time.sleep(2.0)
        else:
            log("!", "Cellular handshake timeout! Resuming with existing route...", YELLOW)

        self.unfreeze_gen()
        self.is_rotating = False
        self.save_stats()
        self.update_title()
        return bool(new_ip)

    def kill_orphans(self):
        for proc in psutil.process_iter(['name']):
            try:
                pname = proc.info['name'].lower()
                if pname in ['gen.exe', 'brave.exe']:
                    proc.kill()
            except Exception:
                pass
        time.sleep(1)

    def launch_gen(self):
        self.kill_orphans()
        log("*", f"Launching stock binary: {GEN_EXE.name}...", CYAN)
        self.gen_process = subprocess.Popen(
            [str(GEN_EXE)],
            cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        self.gen_pid = self.gen_process.pid
        log("+", f"{GEN_EXE.name} running with PID: {self.gen_pid}", GREEN)

        # Launch auto-navigator attached to gen.exe console buffer
        cmd = [sys.executable, os.path.abspath(__file__), '--auto-nav', str(self.gen_pid)]
        subprocess.Popen(cmd, cwd=str(BASE_DIR))
        log("*", "Auto-Navigator: Attached to gen.exe console buffer (Screen Reader Active)...", CYAN)

    def read_gen_title_counts(self) -> tuple[int | None, int | None]:
        raw_v = None
        raw_l = None

        def enum_win(hwnd, _):
            nonlocal raw_v, raw_l
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if 'gen' in title.lower() or 'evs' in title.lower():
                    m = re.search(r'\[Sess(?:ion)?:\s*(\d+)\s+Valid,\s*(\d+)\s+Locked\]', title)
                    if m:
                        raw_v = int(m.group(1))
                        raw_l = int(m.group(2))

        win32gui.EnumWindows(enum_win, None)
        return raw_v, raw_l

    def update_title(self):
        bal_str = f"{self.nonecap.balance:,} cr" if self.nonecap.balance is not None else "Checking..."
        title = f"Supervisor-ADB | [Sess: {self.session_valid} Valid, {self.session_locked} Locked] | NoneCap: {bal_str} | IP: {self.current_exit_ip} | Rotations: {self.rotations_count}"
        ctypes.windll.kernel32.SetConsoleTitleW(title)

    def keyboard_listener(self):
        """Allows user to press 'S' to start new session, 'M' for menu."""
        while self.running:
            try:
                if msvcrt.kbhit() and not self.in_menu:
                    ch = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                    if ch in ('s', 'm', ' '):
                        self.prompt_session_menu()
            except Exception:
                pass
            time.sleep(0.1)

    def monitor_loop(self):
        last_raw_v = None
        last_nonecap_refresh = 0

        while self.running:
            time.sleep(1.5)
            self.update_heartbeat()

            # Refresh NoneCap stats
            if time.time() - last_nonecap_refresh > 30:
                info = self.nonecap.refresh()
                if info.get("status") == "ok":
                    log("$", f"NoneCap Live Balance: {info['balance']:,} credits | Solves: {info['solves']}", GREEN)
                self.update_title()
                last_nonecap_refresh = time.time()

            # Check valid/locked counts from gen title
            curr_v, curr_l = self.read_gen_title_counts()
            if curr_v is not None and curr_v != last_raw_v:
                if last_raw_v is not None and curr_v > last_raw_v:
                    diff = curr_v - last_raw_v
                    self.session_valid += diff
                    if curr_l is not None:
                        self.session_locked = curr_l
                    self.save_stats()
                    self.update_title()
                    log('+', f"Account #{self.session_valid} successfully registered & saved! [Session Valid: {self.session_valid}]", GREEN)
                    log('*', f"Account registered & saved -> Waiting {POST_REGISTRATION_COOLDOWN}s safety cooldown before Cellular rotation...", CYAN)
                    time.sleep(POST_REGISTRATION_COOLDOWN)
                    self.rotate_adb()
                else:
                    if self.base_raw_valid == 0 and curr_v > 0:
                        self.base_raw_valid = curr_v
                last_raw_v = curr_v

            # Watchdog: restart gen.exe if it exited
            if self.gen_process and self.gen_process.poll() is not None:
                log("!", "gen.exe exited unexpectedly! Restarting in 3s...", RED)
                time.sleep(3)
                self.launch_gen()

    def run(self):
        log("*", "=== STARTING SUPERVISOR-ADB (Cellular Airplane Mode) ===", CYAN)
        log("*", f"Controls: Press [{BOLD}S{RESET}{CYAN}] or [{BOLD}M{RESET}{CYAN}] anytime for Session Management Menu{RESET}", CYAN)
        self.verify_adb_device()
        self.current_exit_ip = self.get_public_ip() or "Direct/Local"
        log("+", f"Current Exit IP: {self.current_exit_ip}", GREEN)

        self.launch_gen()

        # Start keyboard menu listener
        kb_thread = threading.Thread(target=self.keyboard_listener, daemon=True)
        kb_thread.start()

        try:
            self.monitor_loop()
        except KeyboardInterrupt:
            log("!", "Supervisor shutdown requested by user.", YELLOW)
        finally:
            self.running = False
            self.kill_orphans()
            log("*", "Supervisor-ADB terminated cleanly.", CYAN)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == '--auto-nav':
        run_auto_navigator_subproc(int(sys.argv[2]))
    else:
        sup = SupervisorADB()
        sup.run()
