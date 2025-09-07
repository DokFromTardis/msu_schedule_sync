import re
import time

from loguru import logger
import os
from datetime import datetime
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

FACULTY_SELECT = "select#timetableform-facultyid"
COURSE_SELECT = "select#timetableform-course"
GROUP_SELECT = "select#timetableform-groupid"
TABLE_SELECTOR = "table#timeTable"


def wait_for_select_options(driver, select_css: str, min_count: int = 1, timeout: int = 20) -> None:
    """Wait until a <select> has at least `min_count` <option> items."""

    WebDriverWait(driver, timeout).until(
        lambda d: len(
            d.find_element(By.CSS_SELECTOR, select_css).find_elements(By.TAG_NAME, "option")
        )
        >= min_count
    )


def _normalize_label(s: str) -> str:
    """Normalize select option labels for robust matching.

    - Collapse any runs of spaces/underscores to a single underscore
    - Lowercase
    - Trim
    """
    t = (s or "").strip()
    t = re.sub(r"[\s_]+", "_", t)
    return t.lower()


def select_by_visible_text(select_or_element, text_target: str) -> None:
    """Click an option by exact text first, then by substring if needed.

    Accepts either a Select instance or the underlying <select> WebElement.
    """

    try:
        options = select_or_element.options  # Select
    except AttributeError:
        options = select_or_element.find_elements(By.TAG_NAME, "option")

    target = (text_target or "").strip()
    target_norm = _normalize_label(target)

    # 1) Exact match
    for opt in options:
        if opt.text.strip() == target:
            opt.click()
            return
    # 2) Case-insensitive exact
    for opt in options:
        if opt.text.strip().lower() == target.lower():
            opt.click()
            return
    # 3) Normalized exact (handles single vs double underscores)
    for opt in options:
        if _normalize_label(opt.text) == target_norm:
            opt.click()
            return
    # 4) Substring (raw)
    for opt in options:
        if target and target in opt.text.strip():
            opt.click()
            return
    # 5) Normalized substring
    for opt in options:
        if target_norm and target_norm in _normalize_label(opt.text):
            opt.click()
            return
    # 6) Numeric prefix fallback (e.g., '101' matches '101б_Философия')
    m = re.match(r"^(\d+)", target)
    if m:
        prefix = m.group(1)
        for opt in options:
            if opt.text.strip().startswith(prefix):
                opt.click()
                logger.debug("Выбран по числовому префиксу '{}': '{}'", prefix, opt.text.strip())
                return

    # Log available options to ease troubleshooting
    available = ", ".join(o.text.strip() for o in options)
    raise RuntimeError(f"Не удалось выбрать '{text_target}' в селекте; варианты: {available}")


def fill_filters(
    driver,
    faculty: str,
    course: str,
    group: str,
    *,
    timeout: int = 20,
) -> None:
    """Fill and apply timetable filters (faculty, course, group) and wait for table.

    Retries on StaleElementReference and click intercepts.
    """

    attempts = 0
    last_stage = "init"
    while True:
        attempts += 1
        try:
            # Wait for the selects to be present (some pages may not have the form id)
            last_stage = "wait_faculty_select"
            logger.debug("Ожидание селекта факультета ({})", FACULTY_SELECT)
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, FACULTY_SELECT))
            )

            # Faculty
            last_stage = "select_faculty"
            faculty_sel = Select(driver.find_element(By.CSS_SELECTOR, FACULTY_SELECT))
            logger.info("Выбираем факультет: {}", faculty)
            select_by_visible_text(faculty_sel, faculty)

            # Course (loads dynamically after faculty)
            # Some faculties can have a single course option → allow 1
            last_stage = "wait_course_options"
            logger.debug("Ожидание опций курса ({})", COURSE_SELECT)
            wait_for_select_options(driver, COURSE_SELECT, min_count=1, timeout=timeout)
            last_stage = "select_course"
            course_sel = Select(driver.find_element(By.CSS_SELECTOR, COURSE_SELECT))
            logger.info("Выбираем курс: {}", course)
            select_by_visible_text(course_sel, str(course))

            # Group (loads dynamically after course)
            # Some courses can have a single group option → allow 1
            last_stage = "wait_group_options"
            logger.debug("Ожидание опций группы ({})", GROUP_SELECT)
            wait_for_select_options(driver, GROUP_SELECT, min_count=1, timeout=timeout)
            last_stage = "select_group"
            group_sel = Select(driver.find_element(By.CSS_SELECTOR, GROUP_SELECT))
            logger.info("Выбираем группу: {}", group)
            select_by_visible_text(group_sel, group)

            # Table is updated via AJAX; wait until it is present
            last_stage = "wait_table"
            logger.debug("Ожидание таблицы расписания ({})", TABLE_SELECTOR)
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, TABLE_SELECTOR))
            )
            return
        except (StaleElementReferenceException, ElementClickInterceptedException, TimeoutException) as e:
            if attempts >= 3:
                logger.error("Сбой на этапе '{}': {}", last_stage, str(e).splitlines()[0])
                # Try to capture a screenshot to ease diagnostics
                try:
                    dbg_dir = os.path.join("sync", "var", "debug")
                    os.makedirs(dbg_dir, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    path = os.path.join(dbg_dir, f"selenium-timeout-{last_stage}-{ts}.png")
                    if hasattr(driver, "save_screenshot"):
                        driver.save_screenshot(path)
                        logger.info("Скриншот сохранён: {}", path)
                except Exception:
                    pass
                raise
            logger.warning(
                "Повторный выбор фильтров после сбоя на этапе '{}' (попытка {})",
                last_stage,
                attempts,
            )
            # Refresh page between attempts to recover inconsistent state
            try:
                driver.refresh()
            except Exception:
                pass
            time.sleep(0.3)
