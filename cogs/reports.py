"""Отчёты для Сената.

- Кадровый отчёт: состав по званиям, кто в брейкваре, кто неактивен 14+ дней.
- Недельный доклад: каждый понедельник в 10:00 (часовой пояс TZ_OFFSET) в «Сенат»;
  также по кнопке на панели Сената.

Активность человека — самое позднее из: сообщение в чате, голосовой канал, отметка на
операции, голосование, выборы, «Принято» под эдиктом, благодарность, действие со складом.
"""
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks

import config

log = logging.getLogger("reports")

INACTIVE_DAYS = 14
STATE_FILE = config.DATA_DIR / "reports_state.json"


# ---------- сбор данных ----------

def _q(conn, sql: str, args: tuple = ()) -> list:
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:
        log.exception("Запрос отчёта не удался: %s", sql[:60])
        return []


def last_activity() -> dict[int, int]:
    """user_id → время последней активности (по всем модулям)."""
    from cogs import consulate, edicts, elections, operations, profiles, warehouses
    last: dict[int, int] = {}

    def upd(rows) -> None:
        for uid, ts in rows:
            if uid and ts and ts > last.get(uid, 0):
                last[uid] = int(ts)

    if profiles.db is not None:
        c = profiles.db.conn
        upd(_q(c, "SELECT user_id, last_message FROM activity"))
        for uid, day in _q(c, "SELECT user_id, MAX(day) FROM voice GROUP BY user_id"):
            try:
                upd([(uid, int(datetime.strptime(day, "%Y-%m-%d").timestamp()) + 86399)])
            except (TypeError, ValueError):
                pass
        upd(_q(c, "SELECT from_id, MAX(ts) FROM thanks GROUP BY from_id"))
    if operations.db is not None:
        upd(_q(operations.db.conn, "SELECT user_id, MAX(ts) FROM responses GROUP BY user_id"))
        upd(_q(operations.db.conn,
               "SELECT a.user_id, MAX(o.starts_at) FROM attendance a JOIN operations o ON o.id = a.op_id "
               "GROUP BY a.user_id"))
    if consulate.db is not None:
        upd(_q(consulate.db.conn, "SELECT user_id, MAX(ts) FROM votes GROUP BY user_id"))
    if elections.db is not None:
        upd(_q(elections.db.conn, "SELECT voter_id, MAX(ts) FROM ballots GROUP BY voter_id"))
    if edicts.db is not None:
        upd(_q(edicts.db.conn, "SELECT user_id, MAX(ts) FROM acks GROUP BY user_id"))
    if warehouses.db is not None:
        upd(_q(warehouses.db.conn, "SELECT user_id, MAX(ts) FROM audit_log GROUP BY user_id"))
    return last


def legion_members(guild: discord.Guild) -> list[discord.Member]:
    from cogs.consulate import legion_roles
    roles = legion_roles()
    return [m for m in guild.members if not m.bot and any(r.id in roles for r in m.roles)]


def top_rank_id(member: discord.Member) -> Optional[int]:
    from cogs.profiles import rank_order
    held = {r.id for r in member.roles}
    for rid in rank_order():
        if rid in held:
            return rid
    return None


# ---------- кадровый отчёт ----------

def inactive_members(guild: discord.Guild) -> list[tuple[int, discord.Member]]:
    """(последняя активность или 0, участник) — неактивные дольше INACTIVE_DAYS, без тех, кто в брейкваре."""
    from cogs import profiles
    activity = last_activity()
    on_vacation = {v["user_id"] for v in profiles.db.vacations()} if profiles.db else set()
    since = profiles.db.tracking_since() if profiles.db else int(time.time())
    border = time.time() - INACTIVE_DAYS * 86400
    result = []
    for m in legion_members(guild):
        if m.id in on_vacation:
            continue
        joined = int(m.joined_at.timestamp()) if m.joined_at else 0
        last = activity.get(m.id, 0)
        # человек неактивен, если с начала учёта (и с момента входа) прошло больше срока и активности не было
        if max(last, joined, since) < border:
            result.append((last, m))
    result.sort(key=lambda x: x[0])
    return result


def personnel_embeds(guild: discord.Guild) -> list[discord.Embed]:
    from cogs import profiles
    members = legion_members(guild)
    on_vacation = {v["user_id"]: v for v in profiles.db.vacations()} if profiles.db else {}
    since = profiles.db.tracking_since() if profiles.db else int(time.time())
    now = time.time()

    counts = Counter(top_rank_id(m) for m in members)
    rank_lines = [f"<@&{rid}> — **{counts[rid]}**" for rid in profiles.rank_order() if counts.get(rid)]
    main = discord.Embed(title="👥 КАДРОВЫЙ ОТЧЁТ", color=discord.Color.dark_gold())
    main.description = (f"Всего в Легионе: **{len(members)}**\n\n" + "\n".join(rank_lines))[:4000]
    if on_vacation:
        vac = [f"<@{uid}> — до <t:{v['until']}:D>" for uid, v in on_vacation.items()]
        main.add_field(name=f"🏖️ В брейкваре ({len(vac)})", value="\n".join(vac)[:1024], inline=False)

    inactive = inactive_members(guild)

    second = discord.Embed(title=f"💤 Неактивны {INACTIVE_DAYS}+ дней: {len(inactive)}", color=discord.Color.orange())
    lines = []
    for last, m in inactive[:60]:
        rid = top_rank_id(m)
        rank = f"<@&{rid}>" if rid else ""
        when = f"<t:{last}:R>" if last else "нет данных"
        lines.append(f"{m.mention} {rank} — {when}")
    second.description = "\n".join(lines)[:4000] or "Неактивных нет. 💪"
    if len(inactive) > 60:
        second.description += f"\n…и ещё {len(inactive) - 60}"
    days_tracked = (now - since) / 86400
    note = ("Активность: чат, голос, операции, голосования, выборы, эдикты, благодарности, склады. "
            "Кто в брейкваре — не учитывается.")
    if days_tracked < INACTIVE_DAYS:
        note += (f"\n⚠️ Учёт чата и голоса ведётся с <t:{since}:D> — список станет точным "
                 f"<t:{since + INACTIVE_DAYS * 86400}:R>.")
    second.add_field(name="Как считается", value=note, inline=False)
    return [main, second]


# ---------- недельный доклад ----------

def weekly_embed(guild: discord.Guild, days: int = 7) -> discord.Embed:
    from cogs import consulate, edicts, elections, operations, profiles, senate, warehouses
    from cogs.registration import load_data

    now = int(time.time())
    since = now - days * 86400
    e = discord.Embed(title="📊 НЕДЕЛЬНЫЙ ДОКЛАД СЕНАТУ",
                      description=f"<t:{since}:D> — <t:{now}:D>", color=discord.Color.dark_gold())

    # Регистратум
    apps = load_data()
    submitted = approved = rejected = 0
    for a in apps.values():
        try:
            if a.get("submitted_at") and datetime.fromisoformat(a["submitted_at"]).timestamp() >= since:
                submitted += 1
            if a.get("reviewed_at") and datetime.fromisoformat(a["reviewed_at"]).timestamp() >= since:
                if a.get("status") == "approved":
                    approved += 1
            if a.get("status") == "rejected" and a.get("submitted_at") and \
                    datetime.fromisoformat(a["submitted_at"]).timestamp() >= since:
                rejected += 1
        except (TypeError, ValueError):
            pass
    e.add_field(name="📝 Регистратум", value=f"Анкет: {submitted}\nПринято: {approved}\nОтклонено: {rejected}",
                inline=True)

    # Операции
    if operations.db is not None:
        c = operations.db.conn
        held = _q(c, "SELECT COUNT(*) FROM operations WHERE status != 'cancelled' AND starts_at BETWEEN ? AND ?",
                  (since, now))[0][0]
        came = _q(c, "SELECT a.user_id, COUNT(*) FROM attendance a JOIN operations o ON o.id = a.op_id "
                     "WHERE o.starts_at BETWEEN ? AND ? GROUP BY a.user_id ORDER BY COUNT(*) DESC", (since, now))
        text = f"Проведено: {held}\nОтметок явки: {sum(n for _, n in came)}"
        if came:
            text += "\nЧаще всех: " + ", ".join(f"<@{u}> ({n})" for u, n in came[:3])
        e.add_field(name="⚔️ Операции", value=text[:1024], inline=True)

    # Логистика
    if warehouses.db is not None:
        c = warehouses.db.conn
        refreshes = _q(c, "SELECT user_id, COUNT(*) FROM audit_log WHERE action = 'refresh' AND ts >= ? "
                          "GROUP BY user_id ORDER BY COUNT(*) DESC", (since,))
        created = _q(c, "SELECT COUNT(*) FROM warehouses WHERE created_at >= ?", (since,))[0][0]
        active = _q(c, "SELECT COUNT(*) FROM warehouses WHERE status != 'closed'")[0][0]
        text = (f"Складов активно: {active}\nНовых: {created}\n"
                f"Обновлений: {sum(n for _, n in refreshes)}")
        if refreshes:
            text += "\nЛучшие логисты: " + ", ".join(f"<@{u}> ({n})" for u, n in refreshes[:3])
        e.add_field(name="🚚 Логистика", value=text[:1024], inline=False)

    # Производство
    try:
        from cogs import prefecture
        if prefecture.db is not None:
            c = prefecture.db.conn
            made = _q(c, "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND message_id IS NOT NULL", (since,))[0][0]
            delivered = _q(c, "SELECT t.user_id, COUNT(*) FROM orders o JOIN order_takers t ON t.order_id = o.id "
                              "WHERE o.status = 'delivered' AND o.updated_at >= ? GROUP BY t.user_id "
                              "ORDER BY COUNT(*) DESC", (since,))
            done = _q(c, "SELECT COUNT(*) FROM orders WHERE status = 'delivered' AND updated_at >= ?", (since,))[0][0]
            open_now = len(prefecture.db.open_orders())
            text = f"Заказов: {made}\nДоставлено: {done}\nОткрыто сейчас: {open_now}"
            if delivered:
                text += "\nЛучшие Фабрумы: " + ", ".join(f"<@{u}> ({n})" for u, n in delivered[:3])
            e.add_field(name="🏭 Производство", value=text[:1024], inline=False)
    except Exception:
        log.exception("Доклад: производство")

    # Консулат
    civic = []
    if consulate.db is not None:
        civic.append(f"Голосований: {_q(consulate.db.conn, 'SELECT COUNT(*) FROM polls WHERE created_at >= ? AND message_id IS NOT NULL', (since,))[0][0]}")
    if edicts.db is not None:
        civic.append(f"Эдиктов: {_q(edicts.db.conn, 'SELECT COUNT(*) FROM edicts WHERE status = ? AND publish_at >= ?', ('published', since))[0][0]}")
    if elections.db is not None:
        civic.append(f"Выборов: {_q(elections.db.conn, 'SELECT COUNT(*) FROM elections WHERE created_at >= ? AND message_id IS NOT NULL', (since,))[0][0]}")
    if civic:
        e.add_field(name="🏛️ Консулат", value="\n".join(civic), inline=True)

    # Награды, благодарности, голос
    if profiles.db is not None:
        c = profiles.db.conn
        awards = _q(c, "SELECT user_id, award FROM awards WHERE ts >= ? ORDER BY ts", (since,))
        if awards:
            lines = [f"{profiles.AWARDS.get(a, ('🏅', a))[0]} <@{u}>" for u, a in awards[:8]]
            e.add_field(name=f"🏅 Награды ({len(awards)})", value="\n".join(lines)[:1024], inline=True)
        thanks = _q(c, "SELECT to_id, COUNT(*) FROM thanks WHERE ts >= ? GROUP BY to_id ORDER BY COUNT(*) DESC",
                    (since,))
        if thanks:
            e.add_field(name="🤝 Благодарности",
                        value="\n".join(f"<@{u}> — {n}" for u, n in thanks[:5])[:1024], inline=True)
        day_from = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        voice = _q(c, "SELECT user_id, SUM(seconds) FROM voice WHERE day >= ? GROUP BY user_id "
                      "ORDER BY SUM(seconds) DESC LIMIT 5", (day_from,))
        if voice:
            e.add_field(name="🎧 Больше всех в голосе",
                        value="\n".join(f"<@{u}> — {s / 3600:.1f} ч" for u, s in voice)[:1024], inline=True)
        vac = profiles.db.vacations()
        e.add_field(name="🏖️ В брейкваре", value=str(len(vac)), inline=True)

    # Сенат
    if senate.db is not None:
        c = senate.db.conn
        warns = _q(c, "SELECT COUNT(*) FROM notes WHERE kind = 'warning' AND ts >= ?", (since,))[0][0]
        promos = _q(c, "SELECT p.rank_id, pc.user_id FROM promotions p JOIN promo_candidates pc ON pc.promo_id = p.id "
                       "WHERE pc.result = 'approved' AND p.decided_ts >= ?", (since,))
        bl = _q(c, "SELECT COUNT(*) FROM blacklist WHERE active = 1 AND ts >= ?", (since,))[0][0]
        done = _q(c, "SELECT COUNT(*) FROM tasks WHERE status = 'done' AND closed_at >= ?", (since,))
        late = _q(c, "SELECT COUNT(*) FROM tasks WHERE status = 'open' AND deadline IS NOT NULL AND deadline < ?",
                  (now,))
        text = f"Выговоров: {warns}\nВ чёрный список: {bl}"
        if done or late:
            text += f"\nПоручений выполнено: {done[0][0] if done else 0} · просрочено: {late[0][0] if late else 0}"
        if promos:
            text += "\nПовышены: " + ", ".join(f"<@{u}> → <@&{r}>" for r, u in promos[:8])
        e.add_field(name="🎖️ Сенат", value=text[:1024], inline=False)

    # Неактивные
    e.add_field(name=f"💤 Неактивны {INACTIVE_DAYS}+ дней",
                value=f"{len(inactive_members(guild))} — подробно: кнопка «👥 Кадры» на панели Сената", inline=False)
    e.set_footer(text="Senatus Populusque Legionis")
    return e


# ---------- расписание ----------

def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


class Reports(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.weekly.start()

    async def cog_unload(self) -> None:
        self.weekly.cancel()

    @tasks.loop(minutes=10)
    async def weekly(self) -> None:
        if not config.SENATE_CHANNEL_ID:
            return
        from cogs.operations import local_tz
        now = datetime.now(local_tz())
        if now.weekday() != 0 or now.hour < 10:  # понедельник, с 10:00
            return
        week = now.strftime("%G-%V")
        state = _state()
        if state.get("last_week") == week:
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        channel = self.bot.get_channel(config.SENATE_CHANNEL_ID)
        if guild is None or channel is None:
            return
        try:
            await channel.send(embed=weekly_embed(guild), allowed_mentions=discord.AllowedMentions.none())
            state["last_week"] = week
            _save_state(state)
            log.info("Недельный доклад отправлен")
        except discord.HTTPException as e:
            log.error("Не удалось отправить недельный доклад: %s", e)

    @weekly.before_loop
    async def before_weekly(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reports(bot))
