from __future__ import annotations

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.options import Options

DEFAULT_WINDOW_SIZE = "1400,1000"


def init_driver(
    headless: bool,
    window_size: str = DEFAULT_WINDOW_SIZE,
    extra_args: list[str] | None = None,
):
    """Create and return a configured Chrome WebDriver instance."""

    opts = Options()
    if headless:
        # Prefer new headless; some environments require classic flag, so allow override via extra_args
        opts.add_argument("--headless=new")
    if window_size:
        opts.add_argument(f"--window-size={window_size}")
    # Stability flags for headless/server environments
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--remote-allow-origins=*")
    for a in extra_args or []:
        opts.add_argument(a)
    driver = webdriver.Chrome(options=opts)
    return driver


def _should_reinit(err: Exception) -> bool:
    """Return True if a WebDriver error warrants reinitializing the driver.

    Matches common Selenium/urllib3 low-level connection errors and session failures.
    """

    msg = str(err).lower()
    patterns = [
        "connection refused",
        "maxretryerror",
        "httpconnectionpool",
        "newconnectionerror",
        "failed to establish a new connection",
        "invalid session id",
        "chrome not reachable",
        "disconnected: not connected to devtools",
    ]
    return any(p in msg for p in patterns)


def open_timetable_page(driver, url: str, *, timeout: int = 20, retries: int = 2) -> None:
    """Ensure the timetable page is open and ready.

    Navigates to `url` if needed and waits for the faculty select to appear.
    Retries navigation a couple of times on slow or flaky loads.
    """

    FACULTY_SELECT = "select#timetableform-facultyid"

    def _has_faculty_select() -> bool:
        try:
            driver.find_element(By.CSS_SELECTOR, FACULTY_SELECT)
            return True
        except Exception:
            return False

    attempts = 0
    while True:
        attempts += 1
        # Load the page if URL differs or controls are missing
        try:
            current = getattr(driver, "current_url", "") or ""
        except Exception:
            current = ""
        if (not current) or (url and not current.startswith(url)) or (not _has_faculty_select()):
            driver.get(url)

        try:
            # Wait for DOM ready and the faculty select presence
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, FACULTY_SELECT))
            )
            return
        except Exception:
            if attempts > max(1, int(retries)):
                raise
            # Retry a clean navigation on the next loop
            continue
