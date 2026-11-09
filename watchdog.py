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
import hashlib
import re
import zipfile
from email.utils import parsedate_to_datetime
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
    last = None
    for attempt in range(4):  # обрывы соединения с Telegram бывают разовыми
        try:
            return http(f"https://api.telegram.org/bot{TOKEN}/{method}",
                        {"Content-Type": "application/json"}, json.dumps(params).encode())
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


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


TS_RE = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ")
LETTER_RE = re.compile(r"^- (?P<date>[^|]+?) \| тема: (?P<subj>.*?) \| от: .*? \| вложения: .*? \| тип: (?P<kind>order|reply) \| поставщик П(?P<n>\d+)")
NONE_RE = re.compile(r"^- (?P<date>[^|]+?) \| тема: (?P<subj>.*?) \| от: .*? \| тип: order \| поставщик ПNone")
CLOSE_RE = re.compile(r"Сверка П(?P<n>\d+): заявка (?P<t>\d\d:\d\d)(?!\d)")
REMIND_RE = re.compile(r"Напоминание (?P<k>\d) отправлено П(?P<n>\d+) на (?P<addr>\S+)")
SEEN_TTL_H = 12


def run_lines(run):
    z = zipfile.ZipFile(io.BytesIO(gh(f"/repos/{AGENT_REPO}/actions/runs/{run['id']}/logs", raw=True)))
    out = []
    for name in z.namelist():
        for raw in z.read(name).decode("utf-8", "ignore").splitlines():
            parts = raw.split("\t", 2)
            line = parts[2] if len(parts) == 3 else raw
            out.append(TS_RE.sub("", line).strip())
    return out


def scan_agent_logs(state, now_utc):
    """Новые запуски агента: непонятые письма и напоминания поставщикам, которые уже писали сегодня.
    При первом запуске только запоминает состояние (старое не присылаем)."""
    first = "scanned" not in state
    scanned = set(state.get("scanned", []))
    replies = state.setdefault("replies", {})
    seen = state.setdefault("seen", {})
    today = now_utc.astimezone(MSK).date()
    day = state.get("day")
    if not day or day.get("date") != today.isoformat():
        day = state["day"] = {"date": today.isoformat(), "orders": {}, "replies": {}, "backfilled": False, "slot": -1}
    if not day["backfilled"]:
        scanned = set()  # заново читаем все запуски дня, чтобы собрать заявки и ответы с начала дня
    if "closed" not in day:  # данные дня собраны старой версией: читаем запуски дня заново
        day["closed"], day["backfilled"] = {}, False
        scanned = set()
    day_start = datetime.combine(today, datetime.min.time(), MSK).replace(hour=6).astimezone(timezone.utc)
    since = day_start if not day["backfilled"] else now_utc - timedelta(hours=2)
    found, page = [], 1
    while page <= 4:  # за день бывает до ~100 запусков
        data = gh(f"/repos/{AGENT_REPO}/actions/workflows/{AGENT_WORKFLOW}/runs?per_page=100&page={page}"
                  f"&created=%3E%3D{since:%Y-%m-%dT%H:%M:%SZ}")
        found += data.get("workflow_runs", [])
        if len(data.get("workflow_runs", [])) < 100:
            break
        page += 1
    runs = sorted((r for r in found if r["status"] == "completed" and str(r["id"]) not in scanned),
                  key=lambda r: r["created_at"])
    recent = now_utc - timedelta(hours=2)
    logs = []
    for r in runs:
        try:
            logs.append((r, run_lines(r)))
        except Exception as e:
            print(f"лог запуска {r['id']} не прочитан: {e}")
    for r, lines in logs:  # 1) заявки и ответы поставщиков за сегодня
        for line in lines:
            m = LETTER_RE.match(line)
            if not m:
                mn = NONE_RE.match(line)
                if mn:
                    try:
                        dtn = parsedate_to_datetime(mn["date"].strip()).astimezone(MSK)
                    except Exception:
                        continue
                    if dtn.date() == today:
                        day.setdefault("orders_none", {})[f"{dtn.isoformat()}|{mn['subj'][:40]}"] = 1
                continue
            try:
                dt = parsedate_to_datetime(m["date"].strip()).astimezone(MSK)
            except Exception:
                continue
            if dt.date() != today:
                continue
            key = f"{dt.isoformat()}|{m['n']}|{m['subj'][:40]}"
            if m["kind"] == "order":
                day["orders"][key] = int(m["n"])
            else:
                day["replies"][key] = int(m["n"])
                cur = replies.get(m["n"])
                if not cur or dt.isoformat() > cur["dt"]:
                    replies[m["n"]] = {"dt": dt.isoformat(), "subj": m["subj"][:60]}
        if any("DRY_RUN: true" in l for l in lines):
            continue  # проверочные запуски ничего не закрывают
        for line in lines:
            mc = CLOSE_RE.search(line)
            if mc:
                day.setdefault("closed", {})[f"{mc['n']}|{mc['t']}"] = int(mc["n"])
    day["backfilled"] = True
    events = []

    def once(text):
        key = hashlib.md5(text.encode()).hexdigest()[:12]
        if key in seen and (now_utc.timestamp() - seen[key]) / 3600 < SEEN_TTL_H:
            return False
        seen[key] = now_utc.timestamp()
        return True

    for r, lines in logs:  # 2) события (только по недавним запускам)
        if parse(r["created_at"]) < recent:
            continue
        rt = parse(r["created_at"]).astimezone(MSK)
        for line in lines:
            if line.startswith("⚠️") and "в чат не отправляется" in line:
                text = line.split(" — в чат")[0]
                if once(text):
                    events.append(f"ℹ️ Агент не смог разобрать письмо, в чат отдела оно не ушло. {text[2:].strip()} ({r['html_url']})")
                continue
            m = REMIND_RE.search(line)
            if m:
                rep = replies.get(m["n"])
                if rep and rep["dt"] < rt.isoformat():
                    text = (f"Напоминание поставщику П{m['n']} ушло в {rt:%H:%M}, хотя сегодня от него уже есть письмо "
                            f"({parse_iso(rep['dt']):%H:%M}, «{rep['subj']}»). Проверьте, не ответил ли он по существу")
                    if once(text):
                        events.append("⚠️ " + text)
    scanned |= {str(r["id"]) for r, _ in logs}
    state["scanned"] = sorted(scanned, key=int)[-300:]
    state["seen"] = {k: v for k, v in seen.items() if (now_utc.timestamp() - v) / 3600 < SEEN_TTL_H * 2}
    state["replies"] = {n: v for n, v in replies.items() if v["dt"][:10] == today.isoformat()}
    return [] if first else events


def build_summary(day, now):
    """Заявки по поставщикам: сколько отправлено и сколько закрыто сверкой; три группы."""
    orders = {}  # номер -> множество «чч:мм» заявок
    for key, n in day["orders"].items():
        orders.setdefault(n, set()).add(datetime.fromisoformat(key.split("|")[0]).strftime("%H:%M"))
    closed = {}
    for key, n in day.get("closed", {}).items():
        t = key.split("|")[1]
        closed.setdefault(n, set()).add(t)
        orders.setdefault(n, set()).add(t)  # закрытая заявка точно была (например, у П6 в теме нет адреса)
    full, part, none = [], [], []
    for n in sorted(orders):
        total, done = len(orders[n]), len(closed.get(n, set()) & orders[n])
        (full if done == total else part if done else none).append((n, done, total))
    final = (now.hour, now.minute) >= WORK_END
    head = f"<b>{'Итог дня' if final else 'Сводка'} по поставщикам, {now:%H:%M}</b>"
    if not orders:
        return head + "\n\nЗаявок пока нет."
    all_t = sum(t for _, _, t in full + part + none)
    all_d = sum(d for _, d, _ in full + part + none)
    out = [head, f"Заявок отправлено: {all_t} · закрыто: {all_d} · не закрыто: {all_t - all_d}", ""]
    out.append(f"✅ <b>Ответили на все ({len(full)})</b>")
    out.append(", ".join(f"П{n}" for n, _, _ in full) or "—")
    out.append("")
    out.append(f"🟡 <b>Ответили частично ({len(part)})</b>")
    for n, d, t in sorted(part, key=lambda x: (x[1] - x[2], x[0])):
        out.append(f"П{n} — закрыто {d} из {t}, осталось {t - d}")
    if not part:
        out.append("—")
    out.append("")
    out.append(f"❌ <b>Не отвечают ({len(none)})</b>")
    for n, d, t in sorted(none, key=lambda x: (-x[2], x[0])):
        out.append(f"П{n} — заявок {t}, закрыто 0")
    if not none:
        out.append("—")
    extra = len(day.get("orders_none", {}))
    if extra:
        out += ["", f"Ещё {extra} заявок без адреса поставщика в теме, в списке могут быть не учтены."]
    return "\n".join(out)


def parse_iso(ts):
    return datetime.fromisoformat(ts)


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
    if "alerts" not in state:  # старый формат: плоский словарь «тревога -> время»
        state = {"alerts": {k: v for k, v in state.items() if isinstance(v, (int, float))}}
    old_alerts = state["alerts"]
    events = []
    if working or "scanned" not in state:
        try:
            events = scan_agent_logs(state, now_utc)
        except Exception as e:
            print("разбор логов агента не удался:", e)
    new_alerts, lines = {}, []
    for key, text in problems.items():
        last = old_alerts.get(key)
        repeat = 360 if key == "minutes" else REPEAT_MIN
        if not last or (now_utc.timestamp() - last) / 60 >= repeat:
            lines.append("⚠️ " + text)
            new_alerts[key] = now_utc.timestamp()
        else:
            new_alerts[key] = last
    resolved = [k for k in old_alerts if k not in problems]
    if resolved:
        lines.append("✅ Восстановилось: " + ", ".join(resolved))
    if len(events) > 8:
        events = events[:8] + [f"…и ещё {len(events) - 8} событий, смотрите логи агента"]
    lines += events
    summary = None
    day = state.get("day")
    if day and (working or os.environ.get("TEST_ALERT")):
        slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
        due = now.minute % 30 < 15 and day.get("slot") != slot and (now.hour, now.minute) >= WORK_START
        if due or os.environ.get("TEST_ALERT"):
            summary = build_summary(day, now)
            if due:
                day["slot"] = slot
    state["alerts"] = new_alerts
    print(f"{now:%d.%m %H:%M} рабочее время={working} проблемы={list(problems)} события={len(events)} отправляем={len(lines)}")
    if lines:
        tg("sendMessage", chat_id=ALERT_CHAT, text="Страж агента\n\n" + "\n\n".join(lines),
           disable_web_page_preview=True)
    if summary:
        tg("sendMessage", chat_id=ALERT_CHAT, text=summary, parse_mode="HTML")
        print("сводка отправлена:", summary.replace("\n", " | ")[:400])
    json.dump(state, open(STATE_FILE, "w"), ensure_ascii=False)
    if os.environ.get("TEST_ALERT"):
        tg("sendMessage", chat_id=ALERT_CHAT, text="Страж: тестовое сообщение. Канал тревог работает.")


if __name__ == "__main__":
    main()
