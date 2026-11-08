"""External HTTPS checks for D-Map and Telegram incident notifications."""

import json
import logging
import os
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from secrets import compare_digest
from string import Template
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import LockError, LockNotOwnedError, RedisError
from redis.retry import Retry

ORIGIN = "https://map.dalazareva.ru"
CHECK_TARGETS = {"/map/": "Карта", "/map/cabinet/": "Кабинет", "/map/health": "API"}
STATE_KEY = "dmap:uptime:v1:state"
LOCK_KEY = "dmap:uptime:v1:lock"
FAILURES_BEFORE_ALERT = 2
MIN_CHECK_INTERVAL = 60
SCHEDULE_INTERVAL = 120
STALE_AFTER = 2 * SCHEDULE_INTERVAL + MIN_CHECK_INTERVAL
HTTP_TIMEOUT = 5
LOG = logging.getLogger("gunicorn.error")
MOSCOW = timezone(timedelta(hours=3))
DASHBOARD = Template(Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8"))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(path):
    req = Request(ORIGIN + path, headers={"User-Agent": "D-Map-Uptime/1.0"})
    try:
        with build_opener(NoRedirect()).open(req, timeout=HTTP_TIMEOUT) as response:
            if response.status != 200:
                return f"HTTP {response.status}"
            content_type = response.headers.get_content_type()
            if path == "/map/health":
                if content_type != "application/json":
                    return "вместо health получен другой ответ"
                data = json.loads(response.read(4096))
                if (
                    not isinstance(data, dict)
                    or data.get("ok") is not True
                    or not re.fullmatch(r"[0-9a-f]{40}", str(data.get("release", "")))
                ):
                    return "health не подтвердил работоспособность"
            elif content_type != "text/html" or not response.read(1024).lstrip().lower().startswith(
                (b"<!doctype html", b"<html")
            ):
                return "вместо страницы получен другой ответ"
    except HTTPError as error:
        error.close()
        return f"HTTP {error.code}"
    except (URLError, OSError, HTTPException) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, socket.gaierror):
            return "ошибка DNS"
        if isinstance(reason, ssl.SSLError):
            return "ошибка TLS"
        if isinstance(reason, TimeoutError):
            return "тайм-аут"
        return "ошибка соединения"
    except (UnicodeError, ValueError):
        return "некорректный ответ health"
    return None


def check_targets():
    with ThreadPoolExecutor(max_workers=len(CHECK_TARGETS)) as executor:
        results = executor.map(probe, CHECK_TARGETS)
        return {path: error for path, error in zip(CHECK_TARGETS, results) if error}


class TelegramError(RuntimeError):
    """Telegram rejected a request or could not be reached."""


def telegram(token, method, payload):
    req = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with build_opener(NoRedirect()).open(req, timeout=HTTP_TIMEOUT) as response:
            result = json.load(response)
    except HTTPError as error:
        error.close()
        raise TelegramError(f"Telegram HTTP {error.code}") from None
    except (OSError, ValueError, HTTPException):
        raise TelegramError("Telegram недоступен или вернул некорректный ответ") from None
    if not isinstance(result, dict) or result.get("ok") is not True or "result" not in result:
        raise TelegramError("Telegram отклонил запрос")
    return result["result"]


def send_alert(chat_ids, notify, message, on_delivery=None):
    error = None
    for chat_id in chat_ids:
        try:
            notify(chat_id, message)
        except TelegramError as exc:
            error = exc
        else:
            if on_delivery is not None:
                on_delivery(chat_id)
    if error is not None:
        raise error


def run_check(redis, notify, chat_ids, get_errors=check_targets):
    with redis.lock(LOCK_KEY, timeout=90, blocking=False):
        state = json.loads(redis.get(STATE_KEY) or "{}")
        now = int(time.time())
        if now - state.get("checked_at", 0) < MIN_CHECK_INTERVAL:
            return {"status": "recently_checked"}

        errors = get_errors()
        failures = min(state.get("failures", 0) + 1, FAILURES_BEFORE_ALERT) if errors else 0
        previously_alerted = state.get("alerted_chats", chat_ids[:1] if state.get("alerted") else ())
        alerted = set(previously_alerted).intersection(chat_ids)

        def save_state():
            redis.set(STATE_KEY, json.dumps({
                "checked_at": now, "failures": failures, "alerted_chats": sorted(alerted),
                "errors": errors,
            }))

        def mark_alerted(chat_id):
            alerted.add(chat_id)
            save_state()

        def mark_recovered(chat_id):
            alerted.remove(chat_id)
            save_state()

        save_state()
        if failures == FAILURES_BEFORE_ALERT:
            details = "\n".join(f"• {path} — {error}" for path, error in errors.items())
            pending = [chat_id for chat_id in chat_ids if chat_id not in alerted]
            send_alert(
                pending, notify, f"🔴 D-Map · проверка из Render не прошла дважды подряд\n{details}",
                mark_alerted,
            )
        elif not errors:
            pending = [chat_id for chat_id in chat_ids if chat_id in alerted]
            send_alert(
                pending, notify, "🟢 D-Map · карта, кабинет и health снова отвечают из Render",
                mark_recovered,
            )

        return {"status": "degraded" if errors else "healthy", "errors": errors}


def render_dashboard(state):
    errors = state.get("errors")
    checked_at = state.get("checked_at", 0)
    checked_at_text = "ещё не было"
    if checked_at:
        checked_at_text = datetime.fromtimestamp(checked_at, MOSCOW).strftime("%d.%m.%Y, %H:%M МСК")

    has_result = isinstance(errors, dict) and checked_at > 0
    if not has_result:
        status, summary = "unknown", "Ожидаем результат проверки"
    elif time.time() - checked_at > STALE_AFTER:
        status, summary = "unknown", "Результат проверки устарел"
    elif errors:
        status, summary = "degraded", "Обнаружены проблемы"
    else:
        status, summary = "healthy", "Все сервисы доступны"

    targets = []
    for path, name in CHECK_TARGETS.items():
        if status == "unknown":
            target_status, label = "unknown", "Нет данных"
        elif path in errors:
            target_status, label = "degraded", "Сбой"
        else:
            target_status, label = "healthy", "В норме"
        targets.append(f'<li><span>{name}</span><span class="badge" data-state="{target_status}">{label}</span></li>')

    return DASHBOARD.substitute(
        state=status, summary=summary, checked_at=checked_at_text, targets="\n      ".join(targets),
    )


def run_schedule(check):
    while True:
        started_at = time.monotonic()
        try:
            result = check()
            LOG.info("Scheduled D-Map check: %s", result["status"])
        except TelegramError as error:
            LOG.warning("Scheduled D-Map check could not deliver a Telegram alert: %s", error)
        except Exception as error:
            if isinstance(error, LockError) and not isinstance(error, LockNotOwnedError):
                LOG.info("Scheduled D-Map check skipped because a check is already running")
            else:
                LOG.error("Scheduled D-Map check failed (%s)", type(error).__name__)
        time.sleep(max(0, SCHEDULE_INTERVAL - (time.monotonic() - started_at)))


def create_app(*, schedule=False):
    check_token = os.environ["CHECK_TOKEN"]
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    recipients = os.environ.get("TELEGRAM_CHAT_IDS", os.environ.get("TELEGRAM_CHAT_ID", ""))
    chat_ids = tuple(dict.fromkeys(part.strip() for part in recipients.split(",")))
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", check_token):
        raise ValueError("CHECK_TOKEN must be a random URL-safe secret of 32–128 characters")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", bot_token):
        raise ValueError("Invalid TELEGRAM_BOT_TOKEN format")
    if len(chat_ids) > 2 or any(not re.fullmatch(r"[1-9][0-9]*", chat_id) for chat_id in chat_ids):
        raise ValueError("TELEGRAM_CHAT_IDS must list one or two private Telegram chat IDs")

    redis = Redis.from_url(
        os.environ["REDIS_URL"], socket_timeout=2, socket_connect_timeout=2,
        retry=Retry(NoBackoff(), 0),
    )

    def notify(chat_id, message):
        telegram(bot_token, "sendMessage", {"chat_id": int(chat_id), "text": message})

    def check():
        return run_check(redis, notify, chat_ids)

    def app(environ, start_response):
        def respond(status, payload, content_type="application/json"):
            if content_type == "application/json":
                payload = json.dumps(payload, ensure_ascii=False)
            body = payload.encode("utf-8")
            start_response(status, [
                ("Content-Type", f"{content_type}; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        path = environ["PATH_INFO"]
        method = environ["REQUEST_METHOD"]
        if path == "/" and method == "GET":
            try:
                state = json.loads(redis.get(STATE_KEY) or "{}")
            except (RedisError, ValueError):
                return respond("503 Service Unavailable", render_dashboard({}), "text/html")
            return respond("200 OK", render_dashboard(state if isinstance(state, dict) else {}), "text/html")
        if path == "/healthz" and method == "GET":
            return respond("200 OK", {"ok": True})
        if path not in ("/check", "/test-alert") or method != "POST":
            return respond("404 Not Found", {"ok": False})
        auth = environ.get("HTTP_AUTHORIZATION", "").encode("latin-1")
        if not compare_digest(auth, f"Bearer {check_token}".encode("ascii")):
            return respond("401 Unauthorized", {"ok": False})

        try:
            if path == "/test-alert":
                send_alert(chat_ids, notify, "✅ D-Map · тестовое уведомление из Render")
                return respond("200 OK", {"status": "test_sent"})
            return respond("200 OK", check())
        except TelegramError as error:
            LOG.warning("%s", error)
            return respond("503 Service Unavailable", {"status": "telegram_unavailable"})
        except RedisError as error:
            if isinstance(error, LockError) and not isinstance(error, LockNotOwnedError):
                return respond("202 Accepted", {"status": "check_in_progress"})
            LOG.warning("Monitor state unavailable (%s)", type(error).__name__)
            return respond("503 Service Unavailable", {"status": "state_unavailable"})

    if schedule:
        Thread(target=run_schedule, args=(check,), name="dmap-scheduler", daemon=True).start()

    return app
