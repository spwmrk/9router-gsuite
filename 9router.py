#!/usr/bin/env python3
"""
9Router — Auto OAuth for Google Accounts

Automates Google OAuth login via System Chrome (Playwright) and
connects each account to 9Router.

Usage:
    pip install playwright && playwright install chromium
    python 9router.py

Config (env vars):
    ROUTER_URL  — base URL   (default: http://localhost:20128)
    MAX_RETRY   — retries     (default: 3)
    HEADLESS    — headless    (default: 0)
    WORKERS     — parallel    (default: 1)
"""

import json
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from playwright.sync_api import sync_playwright, Page, Playwright
except ImportError:
    sys.exit("[!] pip install playwright && playwright install chromium")


# ═══════════════════════════════════════════════════════════════
#  ANSI Colors (TrueColor)
# ═══════════════════════════════════════════════════════════════

os.system("")  # enable ANSI on Windows

BOLD = "\033[1m"
DIM  = "\033[2m"
RST  = "\033[0m"

RED    = "\033[1;38;2;255;80;80m"
ORANGE = "\033[1;38;2;255;165;0m"
YELLOW = "\033[1;38;2;255;220;50m"
GREEN  = "\033[1;38;2;80;220;100m"
CYAN   = "\033[1;38;2;80;220;220m"
BLUE   = "\033[1;38;2;100;140;255m"
PURPLE = "\033[1;38;2;180;120;255m"
WHT    = "\033[37m"

_RAINBOW_RGB = [
    (255, 0, 0), (255, 127, 0), (255, 255, 0),
    (0, 200, 0), (0, 200, 200), (66, 133, 244), (139, 92, 246),
]

_RAINBOW_ANSI = [RED, ORANGE, YELLOW, GREEN, CYAN, BLUE, PURPLE]


def _lerp(c1: tuple, c2: tuple, t: float) -> tuple:
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _rainbow_at(pos: int, total: int) -> tuple:
    idx = pos / max(total, 1) * (len(_RAINBOW_RGB) - 1)
    i = min(int(idx), len(_RAINBOW_RGB) - 2)
    return _lerp(_RAINBOW_RGB[i], _RAINBOW_RGB[i + 1], idx - i)


def _rainbow_text(text: str) -> str:
    out, vi = "", 0
    visible = sum(1 for c in text if c != " ")
    for ch in text:
        if ch == " ":
            out += ch
        else:
            r, g, b = _rainbow_at(vi, visible)
            out += f"\033[1;38;2;{r};{g};{b}m{ch}"
            vi += 1
    return out + RST


# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════

BASE_URL     = os.getenv("ROUTER_URL", "http://localhost:20128")
REDIRECT_URI = f"{BASE_URL}/callback"
MAX_RETRY    = int(os.getenv("MAX_RETRY", "3"))
HEADLESS     = os.getenv("HEADLESS", "0") == "1"
WORKERS      = max(1, int(os.getenv("WORKERS", "3")))

AUTHORIZE_EP = "/api/oauth/antigravity/authorize"
EXCHANGE_EP  = "/api/oauth/antigravity/exchange"

SCRIPT_DIR    = Path(__file__).parent
ACCOUNTS_FILE = SCRIPT_DIR / "accounts.txt"
RESULTS_FILE  = SCRIPT_DIR / "results.txt"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Timeouts (ms)
TIMEOUT_NAV      = 90_000
TIMEOUT_ELEMENT  = 60_000
TIMEOUT_CLICK    = 10_000
TIMEOUT_HANDLER  = 2_000
TIMEOUT_FAST     = 800
TIMEOUT_CALLBACK = 90_000
WAIT_AFTER_CLICK = 800
WAIT_POLL        = 300
WAIT_RETRY       = 3.0

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

LAUNCH_ARGS = [
    "--no-sandbox", "--no-first-run", "--start-maximized",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars", "--disable-gpu", "--disable-extensions",
    "--disable-default-apps", "--disable-sync", "--disable-translate",
    "--disable-popup-blocking", "--disable-background-networking",
    "--disable-component-update", "--disable-hang-monitor",
    "--metrics-recording-only",
]

DELIMITERS = ("|", ":", ";", ",", "\t", " ")


# ═══════════════════════════════════════════════════════════════
#  Data Types
# ═══════════════════════════════════════════════════════════════

@dataclass
class Account:
    email: str
    password: str


@dataclass
class Result:
    email: str
    ok: bool
    connection: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class CaptureState:
    code: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return self.code is not None or self.error is not None

    def to_dict(self) -> dict:
        if self.code:
            return {"code": self.code, "state": self.state}
        if self.error:
            return {"error": self.error}
        return {}


@dataclass
class LogBuffer:
    """Thread-safe output buffer for one account."""
    lines: list[str] = field(default_factory=list)
    last_key: str = ""
    seen: set = field(default_factory=set)
    step_idx: int = 0
    rail_color: str = DIM

    def log(self, icon: str, msg: str, once: bool = False):
        key = f"{icon}:{msg}"
        if (once and key in self.seen) or key == self.last_key:
            return
        self.last_key = key
        self.seen.add(key)
        rail = f"{self.rail_color}│{RST}"
        if icon == ".":
            c = _STEP_COLORS[self.step_idx % len(_STEP_COLORS)]
            self.step_idx += 1
            self.lines.append(f"  {rail}  {c}··{RST}  {msg}")
        else:
            c, sym = _ICONS.get(icon, (WHT, icon))
            self.lines.append(f"  {rail}  {c}{sym}{RST}  {msg}")


# ═══════════════════════════════════════════════════════════════
#  Console Output
# ═══════════════════════════════════════════════════════════════

_TABLE_W = 59
_print_lock = threading.Lock()
_file_lock = threading.Lock()

_STEP_COLORS = [CYAN, GREEN, YELLOW, ORANGE, PURPLE, BLUE]
_ICONS = {
    ">": (CYAN, ">>"), "+": (GREEN, "OK"), "!": (ORANGE, "!!"),
    "x": (RED,  "XX"), "~": (YELLOW, "<>"),
}


def _acc_color(index: int) -> str:
    return _RAINBOW_ANSI[index % len(_RAINBOW_ANSI)]


def _vlen(s: str) -> int:
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def _table(rows: list[tuple[str, str]], title: str = ""):
    w = _TABLE_W
    dashes = w - _vlen(title) - 5
    print(f"  {DIM}┌─ {RST}{BOLD}{title}{RST}{DIM} " + ("─" * max(dashes, 0)) + f"┐{RST}")
    for label, value in rows:
        pad = w - 4 - _vlen(label) - _vlen(value)
        print(f"  {DIM}│{RST} {label}" + (" " * max(pad, 1)) + f"{value} {DIM}│{RST}")
    print(f"  {DIM}└" + ("─" * (w - 2)) + f"┘{RST}")


# ═══════════════════════════════════════════════════════════════
#  Banner
# ═══════════════════════════════════════════════════════════════

_BANNER = [
    " █████╗ ██████╗  ██████╗ ██╗   ██╗████████╗███████╗██████╗ ",
    "██╔══██╗██╔══██╗██╔═══██╗██║   ██║╚══██╔══╝██╔════╝██╔══██╗",
    "╚██████║██████╔╝██║   ██║██║   ██║   ██║   █████╗  ██████╔╝",
    " ╚═══██║██╔══██╗██║   ██║██║   ██║   ██║   ██╔══╝  ██╔══██╗",
    " █████╔╝██║  ██║╚██████╔╝╚██████╔╝   ██║   ███████╗██║  ██║",
    " ╚════╝ ╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝   ╚══════╝╚═╝  ╚═╝",
]


def _render_banner():
    lines = [l.rstrip() for l in _BANNER]
    w = max(len(l) for l in lines)
    lines = [l.ljust(w) for l in lines]
    border = f"  {DIM}{'─' * w}{RST}"
    print(f"\n{border}")
    for line in lines:
        out = "  "
        for j, ch in enumerate(line):
            if ch != " ":
                r, g, b = _rainbow_at(j, w)
                out += f"\033[1;38;2;{r};{g};{b}m{ch}"
            else:
                out += ch
        print(f"{out}{RST}")
    print(border)


# ═══════════════════════════════════════════════════════════════
#  File I/O
# ═══════════════════════════════════════════════════════════════

def _parse_line(line: str) -> Optional[Account]:
    for delim in DELIMITERS:
        if delim in line:
            email, pw = line.split(delim, 1)
            email, pw = email.strip(), pw.strip()
            if "@" in email and pw:
                return Account(email, pw)
            break
    return None


def _load_accounts() -> list[Account]:
    if not ACCOUNTS_FILE.exists():
        ACCOUNTS_FILE.write_text("email@example.com|password\n", encoding="utf-8")
        print(f"  {ORANGE}!!{RST}  {ACCOUNTS_FILE.name} not found — sample created")
        return []
    accounts = []
    for i, raw in enumerate(ACCOUNTS_FILE.read_text("utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        acc = _parse_line(raw)
        if acc:
            accounts.append(acc)
        else:
            print(f"  {ORANGE}!!{RST}  Skipped line {i}")
    return accounts


def _save_result(result: Result):
    with _file_lock:
        with open(RESULTS_FILE, "a", encoding="utf-8") as f:
            conn = result.connection or {}
            f.write(f"{result.email} | {conn.get('id', '')} | {conn.get('provider', '')}\n")


# ═══════════════════════════════════════════════════════════════
#  HTTP Client
# ═══════════════════════════════════════════════════════════════

def _http_headers() -> dict:
    return {
        "Accept": "*/*",
        "User-Agent": UA,
        "Cache-Control": "no-cache",
        "Referer": f"{BASE_URL}/dashboard/providers/antigravity",
    }


def _get_auth_url() -> dict:
    encoded = urllib.parse.quote(REDIRECT_URI, safe="")
    req = urllib.request.Request(
        f"{BASE_URL}{AUTHORIZE_EP}?redirect_uri={encoded}",
        headers=_http_headers(),
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if not data.get("authUrl"):
        raise ValueError("Empty authUrl")
    return data


def _exchange_code(code: str, code_verifier: str, state: str) -> dict:
    payload = json.dumps({
        "code": code,
        "redirectUri": REDIRECT_URI,
        "codeVerifier": code_verifier,
        "state": state,
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{EXCHANGE_EP}",
        data=payload,
        method="POST",
        headers={**_http_headers(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"status": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return {"status": e.code, "body": json.loads(body)}
        except json.JSONDecodeError:
            raise ValueError(f"Exchange HTTP {e.code}: {body}")


# ═══════════════════════════════════════════════════════════════
#  Browser Helpers
# ═══════════════════════════════════════════════════════════════

def _find_chrome() -> Optional[str]:
    candidates = list(CHROME_PATHS)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, r"Google\Chrome\Application\chrome.exe"))
    return next((p for p in candidates if os.path.isfile(p)), None)


def _launch_browser(pw: Playwright):
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("Chrome not found — install Google Chrome")
    return pw.chromium.launch(executable_path=chrome, headless=HEADLESS, args=LAUNCH_ARGS)


def _type_into(page: Page, selector: str, text: str):
    page.wait_for_selector(selector, state="visible", timeout=TIMEOUT_ELEMENT)
    page.click(selector, click_count=3)
    page.type(selector, text, delay=15)


def _safe_click(page: Page, selector: str, timeout: int = TIMEOUT_FAST) -> bool:
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout)
        page.click(selector)
        return True
    except Exception:
        return False


def _click_text(page: Page, texts: list[str], timeout: int = TIMEOUT_HANDLER) -> bool:
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for text in texts:
            for role in ("button", "link"):
                try:
                    loc = page.get_by_role(role, name=re.compile(re.escape(text), re.I)).first
                    if loc.is_visible():
                        loc.click()
                        return True
                except Exception:
                    pass
        time.sleep(0.15)
    return False


def _parse_callback(url: str) -> dict:
    if not re.search(r"localhost(:\d+)?/callback", url, re.I):
        return {}
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {k: params[k][0] for k in ("code", "error", "state") if k in params}


def _wait_nav(page: Page, prev_url: str, timeout: int = TIMEOUT_CLICK):
    if page.url != prev_url:
        return
    try:
        page.wait_for_url(lambda u: u != prev_url, timeout=timeout)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  Google OAuth Page Handlers
# ═══════════════════════════════════════════════════════════════

_JS_CLICK_RIGHTMOST = """() => {
    const btns = [...document.querySelectorAll('button')].filter(b =>
        b.offsetParent && !/cancel|sign.out/i.test(b.textContent.trim()));
    if (btns.length) {
        btns.sort((a,b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right);
        btns[0].click();
        return true;
    }
    return false;
}"""


def _handle_chooser(page: Page) -> bool:
    if "accountchooser" not in page.url.lower():
        return False
    _click_text(page, ["use another account", "add another account"], TIMEOUT_CLICK)
    return True


def _handle_tos(page: Page, buf: LogBuffer) -> bool:
    if not re.search(r"speedbump.*workspacetermsofservice", page.url, re.I):
        return False
    buf.log(".", "Workspace ToS", once=True)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(WAIT_AFTER_CLICK)
    if _click_text(page, ["i understand", "next", "continue", "accept", "agree"], TIMEOUT_HANDLER):
        page.wait_for_timeout(WAIT_AFTER_CLICK)
        return True
    page.evaluate(_JS_CLICK_RIGHTMOST)
    page.wait_for_timeout(WAIT_AFTER_CLICK)
    return True


def _handle_native_app(page: Page, buf: LogBuffer) -> bool:
    url = page.url
    is_native = bool(re.search(
        r"accounts\.google\.com/signin/oauth/(firstparty|nativeapp|approval)", url, re.I
    )) or bool(re.search(r"accounts\.google\.com/.*nativeapp", url, re.I))
    if not is_native:
        try:
            body = page.evaluate("document.body?.innerText||''")
            if not re.search(r"make sure.*(downloaded|app from google)", body, re.I):
                return False
        except Exception:
            return False
    buf.log(".", "Native app confirmation", once=True)
    if page.evaluate(_JS_CLICK_RIGHTMOST):
        page.wait_for_timeout(WAIT_AFTER_CLICK)
        return True
    if _click_text(page, ["sign in", "login", "continue", "allow", "next"], TIMEOUT_FAST):
        page.wait_for_timeout(WAIT_AFTER_CLICK)
        return True
    return False


def _handle_consent(page: Page) -> bool:
    if not re.search(r"accounts\.google\.com/(signin/oauth/consent|ServiceLogin|o/oauth2)", page.url, re.I):
        return False
    if _click_text(page, ["allow", "continue", "sign in", "confirm", "grant", "next"], TIMEOUT_HANDLER):
        page.wait_for_timeout(WAIT_AFTER_CLICK)
        return True
    for sel in ["#submit_approve_access", 'form button[type="submit"]', "#gaplustosNext"]:
        if _safe_click(page, sel):
            page.wait_for_timeout(WAIT_AFTER_CLICK)
            return True
    return False


# ═══════════════════════════════════════════════════════════════
#  OAuth Login Flow
# ═══════════════════════════════════════════════════════════════

def _login_and_capture(page: Page, account: Account, auth_url: str, buf: LogBuffer) -> dict:
    cap = CaptureState()

    def on_nav(frame):
        if frame != page.main_frame:
            return
        cb = _parse_callback(frame.url)
        if cb.get("code") and not cap.code:
            cap.code, cap.state = cb["code"], cb.get("state")
            buf.log("+", f"Code captured ({cap.code[:32]}...)")
        elif cb.get("error") and not cap.code and not cap.error:
            cap.error = cb["error"]
            buf.log("!", f"Callback: {cb['error']}", once=True)

    page.on("framenavigated", on_nav)

    try:
        page.goto(auth_url, wait_until="domcontentloaded", timeout=TIMEOUT_NAV)
    except Exception:
        pass
    if cap.resolved:
        return cap.to_dict()

    _handle_chooser(page)

    buf.log(".", "Login")
    _type_into(page, "#identifierId", account.email)
    url_before = page.url
    _safe_click(page, "#identifierNext", TIMEOUT_CLICK)
    _wait_nav(page, url_before)

    # Handle "Couldn't find your Google Account" re-entry
    if _safe_click(page, "#yDmH0d > div.VfPpkd-Sx9Kwc button > span", TIMEOUT_FAST):
        _handle_chooser(page)
        _type_into(page, "#identifierId", account.email)
        url_before = page.url
        _safe_click(page, "#identifierNext", TIMEOUT_CLICK)
        _wait_nav(page, url_before)

    _type_into(page, 'input[name="Passwd"]', account.password)
    _safe_click(page, "#passwordNext > div > button > span", TIMEOUT_CLICK)

    buf.log(".", "Waiting callback")
    deadline = time.time() + TIMEOUT_CALLBACK / 1000

    while time.time() < deadline:
        if cap.code:
            return cap.to_dict()
        if cap.error:
            page.wait_for_timeout(500)
            return cap.to_dict()

        try:
            cb = _parse_callback(page.url)
            if cb.get("code") and not cap.code:
                cap.code, cap.state = cb["code"], cb.get("state")
                return cap.to_dict()

            # Handlers that need buf
            if not cap.resolved and _handle_tos(page, buf):
                if not cap.resolved:
                    page.wait_for_timeout(WAIT_AFTER_CLICK)
            elif not cap.resolved and _handle_native_app(page, buf):
                if not cap.resolved:
                    page.wait_for_timeout(WAIT_AFTER_CLICK)
            elif not cap.resolved and _handle_consent(page):
                if not cap.resolved:
                    page.wait_for_timeout(WAIT_AFTER_CLICK)
        except Exception:
            pass
        page.wait_for_timeout(WAIT_POLL)

    return cap.to_dict() or {"error": "timeout"}


# ═══════════════════════════════════════════════════════════════
#  Account Processing
# ═══════════════════════════════════════════════════════════════

def _process_account(browser, account: Account, index: int, total: int) -> Result:
    c = _acc_color(index - 1)
    w = _TABLE_W
    buf = LogBuffer(rail_color=c)

    # Header
    title = f"{c}{index}{DIM}/{total}{RST}  {c}{BOLD}{account.email}{RST}"
    dashes = w - _vlen(title) - 5
    header = f"\n  {c}┌─ {RST}{title} {c}" + ("─" * max(dashes, 0)) + f"┐{RST}"
    footer = f"  {c}└" + ("─" * (w - 2)) + f"┘{RST}"

    result = None
    for attempt in range(MAX_RETRY):
        if attempt > 0:
            buf.log("~", f"Retry {attempt}/{MAX_RETRY - 1}")
            time.sleep(WAIT_RETRY)

        ctx = None
        try:
            buf.log(">", "Authorize")
            auth_data = _get_auth_url()

            ctx = browser.new_context(no_viewport=True, user_agent=UA)
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

            cb_result = _login_and_capture(ctx.new_page(), account, auth_data["authUrl"], buf)

            if cb_result.get("error"):
                if "access_denied" in str(cb_result["error"]) and attempt < MAX_RETRY - 1:
                    continue
                result = Result(account.email, False, error=cb_result["error"])
                break

            code = cb_result.get("code")
            if not code:
                result = Result(account.email, False, error="No code")
                break

            buf.log(">", "Exchange")
            state = auth_data.get("state") or cb_result.get("state")
            ex = _exchange_code(code, auth_data["codeVerifier"], state)

            if ex["body"].get("success"):
                conn = ex["body"]["connection"]
                buf.log("+", f"{conn['email']}  {conn['id'][:12]}")
                result = Result(account.email, True, connection=conn)
                break

            body = json.dumps(ex["body"], ensure_ascii=False)
            buf.log("x", body)
            result = Result(account.email, False, error=body)
            break

        except Exception as e:
            buf.log("x", str(e))
            result = Result(account.email, False, error=str(e))
            break

        finally:
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass

    if result is None:
        result = Result(account.email, False, error=f"Failed after {MAX_RETRY} retries")

    # Flush all output at once (thread-safe)
    with _print_lock:
        print(header)
        for line in buf.lines:
            print(line)
        print(footer)

    if result.ok:
        _save_result(result)

    return result


# ═══════════════════════════════════════════════════════════════
#  Worker Thread
# ═══════════════════════════════════════════════════════════════

def _worker(tasks: list[tuple[int, Account]], total: int, results: dict):
    """Each worker gets its own Playwright + Chrome instance."""
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        try:
            for index, account in tasks:
                r = _process_account(browser, account, index, total)
                results[index] = r
        finally:
            browser.close()


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    os.system("cls" if os.name == "nt" else "clear")
    _render_banner()

    accounts = _load_accounts()
    if not accounts:
        print(f"  {RED}No accounts.{RST} Edit accounts.txt")
        return

    # Health check
    print(f"  {DIM}Checking...{RST}", end="", flush=True)
    try:
        _get_auth_url()
        status = f"{GREEN}Online{RST}"
    except Exception:
        status = f"{RED}Offline{RST}"
    print(f"\r                    \r", end="")

    workers = min(WORKERS, len(accounts))

    _table([
        (f"{PURPLE}Target{RST}",   f"{BOLD}{BASE_URL}{RST}"),
        (f"{BLUE}Accounts{RST}",   f"{BOLD}{len(accounts)}{RST}"),
        (f"{CYAN}Retry{RST}",      f"{BOLD}{MAX_RETRY}{RST}"),
        (f"{YELLOW}Workers{RST}",  f"{BOLD}{workers}{RST}"),
        (f"{GREEN}Status{RST}",    status),
    ], title=_rainbow_text("Config"))

    if "Offline" in status:
        print(f"  {RED}9Router not reachable{RST}")
        return

    # Prepare
    RESULTS_FILE.write_text("", encoding="utf-8")
    all_tasks = list(enumerate(accounts, 1))  # [(1, acc), (2, acc), ...]
    results: dict[int, Result] = {}

    if workers == 1:
        # Sequential — single thread, no overhead
        _worker(all_tasks, len(accounts), results)
    else:
        # Split tasks round-robin across workers
        chunks: list[list[tuple[int, Account]]] = [[] for _ in range(workers)]
        for i, task in enumerate(all_tasks):
            chunks[i % workers].append(task)

        threads = []
        for chunk in chunks:
            if chunk:
                t = threading.Thread(target=_worker, args=(chunk, len(accounts), results))
                t.start()
                threads.append(t)
        for t in threads:
            t.join()

    # Summary
    sorted_results = [results[i] for i in sorted(results)]
    ok = sum(1 for r in sorted_results if r.ok)
    fail = len(sorted_results) - ok

    print()
    rows = []
    for i, r in enumerate(sorted_results):
        c = _acc_color(i)
        if r.ok:
            cid = (r.connection or {}).get("id", "")[:12]
            rows.append((f"{c}OK{RST}   {r.email}", f"{DIM}{cid}{RST}"))
        else:
            err = (r.error or "")[:28]
            rows.append((f"{RED}FAIL{RST} {r.email}", f"{DIM}{err}{RST}"))
    _table(rows, title=_rainbow_text("Results"))
    print(f"  {GREEN}{BOLD}{ok} ok{RST}  {RED}{BOLD}{fail} fail{RST}\n")


if __name__ == "__main__":
    main()
