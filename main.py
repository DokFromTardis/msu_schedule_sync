"""Zero-CLI entrypoint and application orchestration.

Reads configuration from `.env.config` and environment variables, then runs the
pipeline (logging setup → browser → fill filters → parse → optional grouping → sync).
"""

from __future__ import annotations

import shlex
import subprocess
import sys

from loguru import logger
from selenium.webdriver.remote.webdriver import WebDriver

from parse.browser import _should_reinit, init_driver
from parse.parse_sheet import build_events_for_group
from sync.serve.timetable_server import (
    ensure_group_meta,
    start_timetable_server_background,
    write_group_calendar_fs,
    write_groups_index,
)
from sync.telegram_bot.runtime import start_webhook_background
from sync.telegram_bot import (
    broadcast_diff_if_changes,
    load_schedule_snapshot,
    save_schedule_snapshot,
    compute_schedule_diff,
)
from utils import _log_samples, group_id_from_name, logged_sleep
from utils.config import AppConfig, ConfigError, load_env_config, setup_logging


def run(env_path: str = ".env.config") -> None:
    try:
        cfg: AppConfig = load_env_config(env_path)
    except ConfigError as ce:
        logger.error("Ошибка конфигурации: {}", ce)
        sys.exit(2)

    setup_logging(level=cfg.log_level, log_file=cfg.log_file, color=cfg.log_color)

    headless = cfg.headless if cfg.headless is not None else False
    dry_run = cfg.dry_run if cfg.dry_run is not None else False

    logger.debug(
        "Параметры запуска: headless={}, dry_run={}, tz={}, timeout={}, chrome_args={}",
        headless,
        dry_run,
        cfg.timezone,
        cfg.selenium_timeout,
        cfg.chrome_args,
    )
    # Start Telegram webhook (if enabled)
    bot_thread = start_webhook_background(cfg)

    # Timetable server
    timetable_server_thread = None
    if getattr(cfg, "serve_timetable", True):
        timetable_server_thread = start_timetable_server_background(
            host=getattr(cfg, "timetable_host", "127.0.0.1"),
            port=getattr(cfg, "timetable_port", 8080),
            base_path=getattr(cfg, "timetable_base_path", "/timetable"),
            storage_root=getattr(cfg, "timetable_storage_dir", "sync/var/timetable"),
            display_tz=getattr(cfg, "timezone", "Europe/Moscow"),
        )

    try:
        if dry_run:
            logger.info("Dry-run: внешние записи отключены")

        driver: WebDriver = init_driver(headless=headless, extra_args=cfg.chrome_args)
        try:
            # Track consecutive webdriver failures to apply backoff when reinitializing
            consecutive_wd_failures = 0

            while True:
                try:
                    # Quick health check: ensure driver session is responsive
                    try:
                        _ = driver.execute_script("return 1")
                    except Exception as ping_err:
                        if _should_reinit(ping_err):
                            try:
                                driver.quit()
                            except Exception:
                                pass
                            consecutive_wd_failures += 1
                            backoff = min(60, max(1, 2 ** min(consecutive_wd_failures, 5)))
                            logger.warning(
                                "WebDriver пинг не удался ({}). Переинициализация через {} с",
                                str(ping_err).splitlines()[0],
                                backoff,
                            )
                            from utils import logged_sleep as _sleep_logged

                            _sleep_logged(backoff, message="Пауза после сбоя WebDriver")
                            driver = init_driver(headless=headless, extra_args=cfg.chrome_args)
                        else:
                            logger.debug("WebDriver пинг ошибка: {}", ping_err)

                    # Iterate all configured groups; store snapshots per group
                    from dataclasses import replace as _dc_replace
                    from pathlib import Path as _P

                    storage_root = getattr(cfg, "timetable_storage_dir", "sync/var/timetable")
                    # Write groups mapping for landing page
                    try:
                        groups = getattr(cfg, "groups", [cfg.group])
                        write_groups_index(storage_root, groups)
                    except Exception:
                        logger.debug("Не удалось записать groups.json")
                    any_calendar_changed = False
                    for g in getattr(cfg, "groups", None) or [cfg.group]:
                        gid = group_id_from_name(g)
                        # group-specific snapshot path
                        snap_path = str(_P(storage_root) / gid / "last_schedule.json")
                        cfg_g: AppConfig = _dc_replace(
                            cfg, group=g, schedule_snapshot_path=snap_path
                        )
                        try:
                            items = build_events_for_group(driver, cfg_g)
                            _log_samples(items)
                            prev_items = load_schedule_snapshot(cfg_g.schedule_snapshot_path)

                            events_count = len(items)
                            changed = False
                            if bool(cfg_g.dry_run):
                                logger.info("Dry-run: запись календаря отключена")
                            else:
                                logger.info(
                                    "Записываем календарь для группы {} во внутреннее хранилище…",
                                    gid,
                                )
                                events_count, changed = write_group_calendar_fs(
                                    items,
                                    storage_root=storage_root,
                                    group_id=gid,
                                    timezone=cfg_g.timezone,
                                )
                                any_calendar_changed = any_calendar_changed or changed
                                logger.success(
                                    "Готово: записано событий {} для группы {} (изменения: {}).",
                                    events_count,
                                    gid,
                                    changed,
                                )
                                # If ICS file changed but schedule items did not, clarify at DEBUG level
                                try:
                                    full_diff = compute_schedule_diff(prev_items, items)
                                    full_changed = sum(
                                        len(full_diff[k]) for k in ("added", "removed", "modified")
                                    )
                                    if changed and full_changed == 0:
                                        logger.debug(
                                            "Изменился только файл ICS (метаданные/порядок); занятия не менялись"
                                        )
                                except Exception:
                                    pass
                                try:
                                    ensure_group_meta(storage_root, gid, cfg_g.group)
                                except Exception:
                                    logger.debug("Не удалось записать meta.json для группы {}", gid)

                            try:
                                save_schedule_snapshot(cfg_g.schedule_snapshot_path, items)
                                logger.info(
                                    "Снимок расписания сохранён: {}",
                                    cfg_g.schedule_snapshot_path,
                                )
                            except Exception:
                                logger.exception("Не удалось сохранить снимок расписания")

                            try:
                                broadcast_diff_if_changes(
                                    cfg_g, prev_items, items, dry_run=bool(cfg_g.dry_run)
                                )
                            except Exception:
                                logger.exception(
                                    "Сбой при отправке Telegram уведомления об изменениях"
                                )
                        except Exception as group_err:
                            logger.exception("Сбой обработки группы {}: {}", g, group_err)

                    # Optional external sync (e.g., CalDAV/vdirsyncer) when any calendar changed
                    if (
                        any_calendar_changed
                        and not dry_run
                        and getattr(cfg, "caldav_sync_cmd", None)
                    ):
                        cmd = cfg.caldav_sync_cmd or ""
                        try:
                            argv = shlex.split(cmd)
                            logger.info("Синхронизация CalDAV/vdirsyncer: {}", cmd)
                            res = subprocess.run(
                                argv,
                                capture_output=True,
                                text=True,
                                timeout=300,
                            )
                            if res.returncode == 0:
                                logger.success("Синхронизация CalDAV завершена")
                            else:
                                err = res.stderr.strip() or res.stdout.strip()
                                logger.error(
                                    "Синхронизация CalDAV завершилась с кодом {}: {}",
                                    res.returncode,
                                    err,
                                )
                        except subprocess.TimeoutExpired:
                            logger.error("Синхронизация CalDAV превысила таймаут 300с")
                        except FileNotFoundError:
                            logger.error("Команда синхронизации CalDAV не найдена: {}", cmd)
                        except Exception:
                            logger.exception("Сбой при выполнении внешней синхронизации")
                except Exception as loop_err:
                    # Reinitialize driver on fatal WebDriver errors with exponential backoff
                    if _should_reinit(loop_err):
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        consecutive_wd_failures += 1
                        backoff = min(120, max(1, 2 ** min(consecutive_wd_failures, 6)))
                        logger.warning(
                            "Сбой WebDriver: {}. Переинициализация через {} с",
                            str(loop_err).splitlines()[0],
                            backoff,
                        )
                        from utils import logged_sleep as _sleep_logged

                        _sleep_logged(backoff, message="Пауза после сбоя WebDriver")
                        driver = init_driver(headless=headless, extra_args=cfg.chrome_args)
                        continue
                    else:
                        # Non-WebDriver error; log with stack trace once and continue after normal wait
                        logger.exception("Сбой цикла обновления: {}", loop_err)

                # Sleep until the next watch cycle
                try:
                    wait_s = max(int(cfg.watch_interval_seconds), 1)
                except Exception:
                    wait_s = 300
                logged_sleep(wait_s, message="Ожидание до следующей проверки")
                # Reset failure counter on a clean cycle sleep
                consecutive_wd_failures = 0
        finally:
            try:
                driver.quit()
            except Exception:
                logger.warning("Не удалось корректно закрыть браузер")
    except SystemExit:
        raise
    except Exception as e:
        logger.exception("Необработанная ошибка: {}", e)
        # Try to report to Telegram admin if configured
        try:
            import utils.config as _cfg_module  # local import for safety without shadowing
            from sync.telegram_bot import TelegramNotifier

            cfg = _cfg_module.load_env_config(".env.config")
            if cfg.telegram_token and cfg.telegram_admin_user_id:
                notifier = TelegramNotifier(
                    cfg.telegram_token,
                    persist_dir=cfg.telegram_persist_dir,
                    admin_user_id=cfg.telegram_admin_user_id,
                )
                notifier.send_error(f"{e}\nСмотрите логи для деталей.")
        except Exception:
            logger.debug("Не удалось отправить ошибку администратору Telegram")
        sys.exit(1)

    # Keep process alive to serve the Telegram bot if it's running
    if bot_thread and bot_thread.is_alive():
        logger.info("Бот работает. Нажмите Ctrl+C для выхода…")
        try:
            bot_thread.join()
        except KeyboardInterrupt:
            logger.info("Остановка Telegram бота по Ctrl+C")

    if timetable_server_thread and timetable_server_thread.is_alive():
        logger.info("Сервер расписания работает. Нажмите Ctrl+C для выхода…")
        try:
            timetable_server_thread.join()
        except KeyboardInterrupt:
            logger.info("Остановка сервера расписания по Ctrl+C")


if __name__ == "__main__":
    run(".env.config")
