"""Страж агента сверки заявок (репозиторий Andrey-Lisnevskii/p6-reminder).

Только читает: статус запусков через GitHub API и состояние бота через Telegram API.
Тревоги шлёт в личный чат (ALERT_CHAT_ID). В чат отдела ничего не отправляется.

Секреты: GH_READ_TOKEN, TELEGRAM_BOT_TOKEN, ALERT_CHAT_ID.
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

AGENT_REPO = "Andrey-Lisnevskii/p6-reminder"
AGENT_WORKFLOW = "agent.yml"
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "-1004300461257")
MSK = ZoneInfo("Europe/Moscow")
WORK_START, WORK_END = (9, 30), (17, 30)
MAX_SILENCE_MIN = int(os.environ.get("MAX_SILENCE_MIN", "20"))
MIN_LEFT_ALERT = int(os.environ.get("MIN_LEFT_ALERT", "100"))
REPEAT_MIN = 60          # повтор той же тревоги не чаще раза в час
STATE_FILE = "state.json"
TG_ERROR_MARKERS = ("Telegram вернул ошибку", "Telegram отклонил", "Telegram: не удалось")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALERT_CHAT = os.environ["ALERT_CHAT_ID"]
GH = os.environ["GH_READ_TOKEN"]


def http(url, headers=None, data=None, raw=False):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return body if raw else json.loads(body)


def gh(path, raw=False):
    return http(f"https://api.github.com{path}",
                {"Authorization": f"Bearer {GH}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "p6-watchdog"}, raw=raw)


def tg(method, **params):
    return http(f"https://api.telegram.org/bot{TOKEN}/{method}",
                {"Content-Type": "application/json"}, json.dumps(params).encode())


def is_working_time(now):
    if now.weekday() >= 5:
        return False
    try:  # производственный календарь: 1 — выходной
        day = http(f"https://isdayoff.ru/{now:%Y%m%d}", raw=True).decode().strip()
        if day == "1":
            return False
    except Exception:
        pass
    t = (now.hour, now.minute)
    return WORK_START <= t <= WORK_END


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def check_runs(now_utc, working):
    problems = {}
    data = gh(f"/repos/{AGENT_REPO}/actions/workflows/{AGENT_WORKFLOW}/runs?per_page=10")
    runs = data.get("workflow_runs", [])
    if not runs:
        problems["no_runs"] = "Агент: в GitHub нет ни одного запуска."
        return problems, None
    last = runs[0]
    if working:
        # молчание считаем от начала рабочего дня, если запусков ещё не было
        age = (now_utc - parse(last["created_at"])).total_seconds() / 60
        start = datetime.now(MSK).replace(hour=WORK_START[0], minute=WORK_START[1], second=0, microsecond=0)
        since_open = (now_utc - start.astimezone(timezone.utc)).total_seconds() / 60
        if age > MAX_SILENCE_MIN and since_open > MAX_SILENCE_MIN:
            problems["silence"] = (f"Агент не запускался {int(age)} мин "
                                   f"(последний запуск {parse(last['created_at']).astimezone(MSK):%H:%M}). "
                                   f"Проверьте задание в cron-job.org и лимит минут GitHub.")
    done = [r for r in runs if r["status"] == "completed"]
    bad = ("failure", "timed_out", "startup_failure")
    # одиночный сбой (например, обрыв IMAP у mail.ru) не тревожит: агент перезапускается сам каждые 6 минут
    if len(done) >= 2 and done[0]["conclusion"] in bad and done[1]["conclusion"] in bad:
        r = done[0]
        problems["failed"] = (f"Два запуска агента подряд завершились ошибкой (последний в "
                              f"{parse(r['created_at']).astimezone(MSK):%H:%M}): {r['html_url']}")
    return problems, (done[0] if done else None)


def check_telegram_in_log(run):
    """Ищет ошибки отправки в Telegram в логе последнего завершённого запуска."""
    if not run:
        return {}
    try:
        z = zipfile.ZipFile(io.BytesIO(gh(f"/repos/{AGENT_REPO}/actions/runs/{run['id']}/logs", raw=True)))
    except Exception as e:
        return {"log_read": f"Не удалось прочитать лог запуска агента: {e}"}
    for name in z.namelist():
        text = z.read(name).decode("utf-8", "ignore")
        for line in text.splitlines():
            if any(m in line for m in TG_ERROR_MARKERS):
                return {"tg_send": f"В логе агента ошибка отправки в Telegram: {line.split('Z ', 1)[-1][:200]} ({run['html_url']})"}
    return {}


def minutes_used(now_utc):
    """Оценка израсходованных минут Actions в закрытом репозитории агента за текущий месяц.
    Каждый запуск округляется вверх до целой минуты (как биллинг GitHub для одного job)."""
    since = now_utc.strftime("%Y-%m-01")
    total, page = 0, 1
    while page <= 40:
        runs = gh(f"/repos/{AGENT_REPO}/actions/runs?per_page=100&page={page}&created=%3E%3D{since}"
                  ).get("workflow_runs", [])
        for r in runs:
            if r["status"] != "completed" or not r.get("run_started_at"):
                continue
            sec = (parse(r["updated_at"]) - parse(r["run_started_at"])).total_seconds()
            total += max(1, -(-int(sec) // 60))
        if len(runs) < 100:
            break
        page += 1
    return total + int(os.environ.get("MINUTES_OFFSET", "0"))


def check_minutes(now_utc):
    quota = int(os.environ.get("MINUTES_QUOTA", "2000"))
    try:
        used = minutes_used(now_utc)
    except Exception as e:
        print("минуты: не удалось посчитать:", e)
        return {}
    left = quota - used
    print(f"минуты Actions: израсходовано ~{used}, осталось ~{left} из {quota}")
    if left < MIN_LEFT_ALERT:
        return {"minutes": (f"Бесплатные минуты GitHub Actions почти закончились: осталось около {max(left, 0)} "
                            f"из {quota} (оценка по длительности запусков). Агент остановится, когда лимит кончится. "
                            f"Реже запускайте агента или проверьте https://github.com/settings/billing/summary")}
    return {}


def check_bot():
    problems = {}
    try:
        me = tg("getMe")
        if not me.get("ok"):
            raise RuntimeError(me)
        bot_id = me["result"]["id"]
    except Exception as e:
        return {"bot": f"Telegram: токен бота не работает ({e}). Сообщения в чат отдела не уйдут."}
    try:
        m = tg("getChatMember", chat_id=GROUP_CHAT_ID, user_id=bot_id)["result"]
        status = m.get("status")
        if status not in ("member", "administrator", "creator"):
            problems["group"] = f"Бот больше не участник чата отдела (статус: {status})."
        elif status == "member" and m.get("can_send_messages") is False:
            problems["group"] = "Боту запретили писать в чат отдела."
    except urllib.error.HTTPError as e:
        problems["group"] = f"Telegram: чат отдела недоступен для бота (HTTP {e.code}). Возможно, сменился id чата."
    except Exception as e:
        problems["group"] = f"Telegram: не удалось проверить чат отдела ({e})."
    return problems


def main():
    now_utc = datetime.now(timezone.utc)
    now = now_utc.astimezone(MSK)
    working = is_working_time(now)
    problems = {}
    run_problems, last_done = check_runs(now_utc, working)
    problems.update(run_problems)
    problems.update(check_telegram_in_log(last_done) if working else {})
    problems.update(check_bot())
    problems.update(check_minutes(now_utc))

    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        state = {}
    new_state, lines = {}, []
    for key, text in problems.items():
        last = state.get(key)
        repeat = 360 if key == "minutes" else REPEAT_MIN
        if not last or (now_utc.timestamp() - last) / 60 >= repeat:
            lines.append("⚠️ " + text)
            new_state[key] = now_utc.timestamp()
        else:
            new_state[key] = last
    resolved = [k for k in state if k not in problems]
    if resolved:
        lines.append("✅ Восстановилось: " + ", ".join(resolved))
    print(f"{now:%d.%m %H:%M} рабочее время={working} проблемы={list(problems)} отправляем={len(lines)}")
    if lines:
        tg("sendMessage", chat_id=ALERT_CHAT, text="Страж агента\n\n" + "\n\n".join(lines),
           disable_web_page_preview=True)
    json.dump(new_state, open(STATE_FILE, "w"))
    if os.environ.get("TEST_ALERT"):
        tg("sendMessage", chat_id=ALERT_CHAT, text="Страж: тестовое сообщение. Канал тревог работает.")


if __name__ == "__main__":
    main()
