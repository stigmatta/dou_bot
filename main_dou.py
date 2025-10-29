# main_dou.py (env-ready)
import asyncio
import html
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import aiohttp
import feedparser

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # dotenv is optional in production (when env vars are injected by the platform)
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# --- Configuration from environment (.env) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "PLACEHOLDER_TOKEN")
OWNER_NAME = os.getenv("OWNER_NAME", "Author Name")
OWNER_URL = os.getenv("OWNER_URL", "https://example.com")
ALLOW_RU = os.getenv("ALLOW_RU", "true").strip().lower() in ("1", "true", "yes", "y")

if not BOT_TOKEN or BOT_TOKEN == "PLACEHOLDER_TOKEN":
    raise RuntimeError("BOT_TOKEN is not set. Create a .env with BOT_TOKEN=... or set it in your environment.")

# ---------- Domain ----------
COUNTRIES: List[Tuple[str, str]] = [
    ("🇺🇦 Украина (DOU.ua)", "UA"),
    ("🇪🇺 ЄС / за кордоном (DOU.ua)", "INTL"),
    ("Не важно", "ANY"),
]

SPHERES: List[Tuple[str, str]] = [
    ("QA / Тестирование", "QA"),
    ("Backend", "BACKEND"),
    ("Frontend", "FRONTEND"),
    ("Data / ML", "DATA"),
    ("DevOps / SRE", "DEVOPS"),
    ("PM / BA", "PMBA"),
    ("Design / UX", "DESIGN"),
    ("Не важно", "ANY"),
]

FORMATS: List[Tuple[str, str]] = [
    ("🧑‍💻 Удалёнка", "REMOTE"),
    ("🏢 Офис / Гибрид", "OFFICE"),
    ("🧩 Частичная занятость", "PARTTIME"),
    ("📄 Контракт", "CONTRACT"),
    ("Не важно", "ANY"),
]

# ---------- State ----------
class JobWizard(StatesGroup):
    country = State()
    sphere = State()
    format_ = State()
    review = State()

# ---------- Helpers ----------
def kb_options(options: List[Tuple[str, str]], prefix: str, add_back: bool = False, add_reset: bool = True):
    kb = InlineKeyboardBuilder()
    for title, value in options:
        kb.button(text=title, callback_data=f"{prefix}:{value}")
    kb.adjust(2)
    row = []
    if add_back: row.append(("⬅️ Назад", "nav:back"))
    if add_reset: row.append(("♻️ Сброс", "nav:reset"))
    if row:
        for text, data in row:
            kb.button(text=text, callback_data=data)
        kb.adjust(2)
    return kb.as_markup()

def kb_review():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Найти вакансии", callback_data="do:search")
    kb.button(text="✏️ Изменить выбор", callback_data="nav:edit")
    kb.button(text="💾 Сохранить пресет", callback_data="do:save")
    kb.button(text="♻️ Сброс", callback_data="nav:reset")
    # 🔗 кнопка с твоим LinkedIn
    kb.button(text=f"👤 Автор: {OWNER_NAME}", url=OWNER_URL)
    kb.adjust(2)
    return kb.as_markup()

def val2label(value: str, options: List[Tuple[str, str]]) -> str:
    for t, v in options:
        if v == value:
            return t
    return value

@dataclass
class Prefs:
    country: Optional[str] = None
    sphere: Optional[str] = None
    format_: Optional[str] = None

def prefs_to_text(p: Prefs) -> str:
    country = val2label(p.country, COUNTRIES) if p.country else "—"
    sphere  = val2label(p.sphere,  SPHERES)  if p.sphere  else "—"
    fmt     = val2label(p.format_, FORMATS) if p.format_ else "—"
    return f"🌍 Страна: {country}\n🧭 Сфера: {sphere}\n🧩 Формат: {fmt}"

def normalize_html(s: str) -> str:
    return html.escape(s, quote=True)

# ---------- Optional RU filter ----------
FORBIDDEN_TERMS = (
    " russia ", " россия ", " росія ", " рф ",
    " moscow ", " москва ",
    " saint petersburg ", " st. petersburg ", " санкт-петербург ", " санкт петербург ",
)
def contains_forbidden(text: str) -> bool:
    if ALLOW_RU:
        return False
    t = f" { (text or '').lower() } "
    return any(term in t for term in FORBIDDEN_TERMS)

# ---------- DOU maps ----------
UA_CATEGORY_MAP: Dict[str, Optional[str]] = {
    "QA": "QA",
    "FRONTEND": "Front End",
    "DEVOPS": "DevOps",
    "DESIGN": "Design",
    "DATA": "Data Science",
    "PMBA": "Project Manager",
    "BACKEND": None,   # backend покрываем поиском
    "ANY": None,
}
EU_CATEGORY_MAP: Dict[str, Optional[str]] = {
    "QA": "QA",
    "FRONTEND": "Front-end",
    "DEVOPS": "DevOps",
    "DESIGN": "Design",
    "DATA": "Data Science",
    "PMBA": "Project Manager",
    "BACKEND": None,
    "ANY": None,
}

# ---------- HTTP ----------
async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
        resp.raise_for_status()
        return await resp.text()

async def fetch_feed(session: aiohttp.ClientSession, url: str):
    txt = await fetch_text(session, url)
    return feedparser.parse(txt)

# ---------- Build URLs ----------
def build_dou_ua_feed_url(p: Prefs, *, search_terms: Optional[List[str]] = None, drop_category: bool = False) -> str:
    base = "https://jobs.dou.ua/vacancies/feeds/"
    params: Dict[str, str] = {}

    if not drop_category:
        cat = UA_CATEGORY_MAP.get(p.sphere or "ANY")
        if cat:
            params["category"] = cat

    if p.format_ == "REMOTE":
        params["remote"] = ""
    if p.country == "INTL":
        params["relocation"] = ""

    terms: List[str] = []
    if p.sphere == "BACKEND":
        terms.append("(Back-end OR Backend)")
    if search_terms:
        terms.extend(search_terms)

    if terms:
        params["search"] = " ".join(terms)
        if any(x in " ".join(terms).lower() for x in ["part", "time", "contract", "back-end", "backend"]):
            params["descr"] = "1"

    return base + "?" + urlencode(params, doseq=True)

# ---------- Scrapers ----------
async def fetch_dou_ua(p: Prefs, limit: int = 12, debug_urls: Optional[List[str]] = None) -> List[str]:
    tries = [
        build_dou_ua_feed_url(p, search_terms=None),
        build_dou_ua_feed_url(p, search_terms=["(part-time OR \"part time\" OR неповна зайнятість OR частичная занятость)"]),
        build_dou_ua_feed_url(p, search_terms=["(contract OR contractor OR контракт)"]),
        build_dou_ua_feed_url(p, search_terms=["(Part time OR Part-time OR Півставки)"]),
        build_dou_ua_feed_url(p, search_terms=None, drop_category=True),
    ]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for url in tries:
            if debug_urls is not None:
                debug_urls.append(url)
            try:
                feed = await fetch_feed(session, url)
            except Exception as e:
                logging.warning("DOU.ua feed error for %s: %s", url, e)
                continue
            out: List[str] = []
            for e in feed.entries[:120]:
                title = getattr(e, "title", "")
                link  = getattr(e, "link", "")
                if not title or not link:
                    continue
                if contains_forbidden(title) or contains_forbidden(link):
                    continue
                out.append(f'<a href="{link}">{normalize_html(title)}</a>')
                if len(out) >= limit:
                    break
            if out:
                return out
    return []

async def fetch_dou_eu(p: Prefs, limit: int = 12, relax_if_empty: bool = True) -> List[str]:
    url = "https://dou.eu/en/jobs"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        html_text = await fetch_text(session, url)
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "html.parser")
    cards = soup.select("a[href*='/en/jobs/']")
    results: List[str] = []

    want_cat = (p.sphere and EU_CATEGORY_MAP.get(p.sphere)) or None
    want_remote   = (p.format_ == "REMOTE")
    want_office   = (p.format_ == "OFFICE")
    want_pt       = (p.format_ == "PARTTIME")
    want_contract = (p.format_ == "CONTRACT")

    def match_card_text(text: str, allow_relax: bool) -> bool:
        if want_cat and want_cat.lower() not in text.lower(): return False
        if want_remote and ("Remote" not in text): return False
        if want_office and ("Remote" in text): return False
        if want_pt and not any(s in text for s in ["Part-time", "part-time", "Part time"]):
            if not allow_relax: return False
        if want_contract and "Contract" not in text:
            if not allow_relax: return False
        return True

    for a in cards:
        title = a.get_text(strip=True)
        card = a.find_parent()
        if not card:
            continue
        text = card.get_text(" ", strip=True)
        if contains_forbidden(title) or contains_forbidden(text):
            continue
        if not match_card_text(text, allow_relax=False):
            continue
        href = a.get("href")
        if not href:
            continue
        results.append(f'<a href="{href}">{normalize_html(title)}</a>')
        if len(results) >= limit:
            break

    if not results and relax_if_empty and (want_pt or want_contract):
        for a in cards:
            title = a.get_text(strip=True)
            card = a.find_parent()
            if not card:
                continue
            text = card.get_text(" ", strip=True)
            if contains_forbidden(title) or contains_forbidden(text):
                continue
            if not match_card_text(text, allow_relax=True):
                continue
            href = a.get("href")
            if not href:
                continue
            results.append(f'<a href="{href}">{normalize_html(title)}</a>')
            if len(results) >= limit:
                break
    return results

# ---------- Search orchestrator ----------
async def search_jobs(p: Prefs, debug_urls: Optional[List[str]] = None) -> List[str]:
    if p.country in ("UA", "INTL", "ANY", None):
        try:
            items = await fetch_dou_ua(p, debug_urls=debug_urls)
            if not items and p.format_ in ("PARTTIME", "CONTRACT"):
                return await fetch_dou_eu(p, relax_if_empty=True)
            if not items:
                return await fetch_dou_eu(p, relax_if_empty=False)
            return items
        except Exception as e:
            logging.warning("UA feed failed: %s", e)
            return await fetch_dou_eu(p, relax_if_empty=True)
    else:
        return await fetch_dou_eu(p, relax_if_empty=True)

# ---------- Router ----------
r = Router()

@r.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.set_state(JobWizard.country)
    await state.update_data(prefs=Prefs().__dict__, debug_urls=[])
    await m.answer(
        "🦉 Привет! Я Duo-бот для поиска работы.\n"
        f"Автор: <a href=\"{OWNER_URL}\">{OWNER_NAME}</a>\n\n"
        "Выбери страну/площадку:",
        reply_markup=kb_options(COUNTRIES, "country", add_back=False)
    )

@r.message(Command("about"))
async def cmd_about(m: Message):
    await m.answer(f"Автор бота: <a href=\"{OWNER_URL}\">{OWNER_NAME}</a> — LinkedIn", disable_web_page_preview=True)

@r.message(Command("reset"))
async def cmd_reset(m: Message, state: FSMContext):
    await state.clear()
    await cmd_start(m, state)

@r.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong ✅")

@r.message(Command("debug"))
async def cmd_debug(m: Message, state: FSMContext):
    data = await state.get_data()
    urls = data.get("debug_urls", [])
    if not urls:
        await m.answer("Пока нечего показывать. Нажми «Найти вакансии», а затем вызови /debug.")
        return
    text = "Последние запросы RSS (DOU.ua):\n" + "\n".join(f"• <code>{html.escape(u)}</code>" for u in urls[-6:])
    await m.answer(text)

@r.callback_query(F.data.startswith("country:"))
async def choose_country(c: CallbackQuery, state: FSMContext):
    code = c.data.split(":", 1)[1]
    data = await state.get_data()
    prefs = Prefs(**data.get("prefs", {}))
    prefs.country = code
    await state.update_data(prefs=prefs.__dict__)
    await state.set_state(JobWizard.sphere)
    await c.message.edit_text(
        "Отлично! Теперь выбери сферу:",
        reply_markup=kb_options(SPHERES, "sphere", add_back=True)
    )
    await c.answer()

@r.callback_query(F.data.startswith("sphere:"))
async def choose_sphere(c: CallbackQuery, state: FSMContext):
    code = c.data.split(":", 1)[1]
    data = await state.get_data()
    prefs = Prefs(**data.get("prefs", {}))
    prefs.sphere = code
    await state.update_data(prefs=prefs.__dict__)
    await state.set_state(JobWizard.format_)
    await c.message.edit_text(
        "И последний шаг — выбери формат работы:",
        reply_markup=kb_options(FORMATS, "format", add_back=True)
    )
    await c.answer()

@r.callback_query(F.data.startswith("format:"))
async def choose_format(c: CallbackQuery, state: FSMContext):
    code = c.data.split(":", 1)[1]
    data = await state.get_data()
    prefs = Prefs(**data.get("prefs", {}))
    prefs.format_ = code
    await state.update_data(prefs=prefs.__dict__)
    await state.set_state(JobWizard.review)
    txt = "✅ Выбор сохранён!\n\n" + prefs_to_text(prefs) + "\n\nЧто делаем дальше?"
    await c.message.edit_text(txt, reply_markup=kb_review())
    await c.answer()

@r.callback_query(F.data == "nav:back")
async def go_back(c: CallbackQuery, state: FSMContext):
    cur = await state.get_state()
    if cur == JobWizard.sphere:
        await state.set_state(JobWizard.country)
        await c.message.edit_text(
            "Выбери страну/площадку:",
            reply_markup=kb_options(COUNTRIES, "country", add_back=False)
        )
    elif cur == JobWizard.format_:
        await state.set_state(JobWizard.sphere)
        await c.message.edit_text(
            "Выбери сферу:",
            reply_markup=kb_options(SPHERES, "sphere", add_back=True)
        )
    elif cur == JobWizard.review:
        await state.set_state(JobWizard.format_)
        await c.message.edit_text(
            "Снова формат работы:",
            reply_markup=kb_options(FORMATS, "format", add_back=True)
        )
    await c.answer()

@r.callback_query(F.data == "nav:reset")
async def do_reset(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await cmd_start(c.message, state)
    await c.answer("Сброшено")

@r.callback_query(F.data == "nav:edit")
async def edit_selection(c: CallbackQuery, state: FSMContext):
    await state.set_state(JobWizard.country)
    await c.message.edit_text(
        "Ок, поменяем. Выбери страну/площадку:",
        reply_markup=kb_options(COUNTRIES, "country", add_back=False)
    )
    await c.answer()

@r.callback_query(F.data == "do:search")
async def do_search(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    prefs = Prefs(**data.get("prefs", {}))
    debug_urls: List[str] = data.get("debug_urls", [])
    try:
        results = await search_jobs(prefs, debug_urls=debug_urls)
    except Exception as e:
        results = [f"Не удалось получить вакансии: {e}"]
    await state.update_data(debug_urls=debug_urls)

    header = "🔎 Нашёл вот что по твоим фильтрам:\n\n" + prefs_to_text(prefs) + "\n\n"
    body = "\n".join(f"• {r}" for r in results) if results else "Ничего не нашёл 🙈 Попробуй ослабить фильтры."
    tail = (
        "\n\nДля диагностики напиши /debug — покажу URL запросов к RSS."
        f"\nАвтор бота: <a href=\"{OWNER_URL}\">{OWNER_NAME}</a>"
    )
    await c.message.edit_text(header + body + tail, reply_markup=kb_review(), disable_web_page_preview=True)
    await c.answer("Готово!")

@r.callback_query(F.data == "do:save")
async def do_save(c: CallbackQuery, state: FSMContext):
    await c.answer("Сохранил пресет (демо).", show_alert=True)

# ---------- App ----------
async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(r)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    me = await bot.get_me()
    logging.info("Бот запущен: @%s (id=%s). Жду /start…", me.username, me.id)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
