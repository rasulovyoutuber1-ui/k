import asyncio
import logging
import time
from typing import Optional, List, Dict, Any

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError

# ==================== KONFIGURATSIYA ====================
BOT_TOKEN = "8732171636:AAGc0HkdTpjKOlpVjplNeRpvSZBOZa-53EM"         # BotFather tokeni
OWNER_ID = 8318241400                    # Owner Telegram ID
DB_PATH = "anime_bot.db"
PORT = 8080                               # Render uchun port

# Asosiy kanal sozlamalari (Link va ID)
CHANNEL_USERNAME = "@kagedub1"        # Kanal yoki kanal botining userneymi (obuna uchun)
CHANNEL_URL = "https://t.me/@kagedub1"   # Kanalning to'g'ridan-to'g'ri havolasi (linki)

ROLE_CACHE_TTL = 300
SUB_CACHE_TTL = 45
SUB_CACHE_NEGATIVE_TTL = 20

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ==================== KESH ====================
class FastCache:
    def __init__(self):
        self._store: Dict[str, tuple] = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        value, expires = item
        if time.time() > expires:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value, ttl: float):
        self._store[key] = (value, time.time() + ttl)

    def delete(self, key: str):
        self._store.pop(key, None)

role_cache = FastCache()
sub_cache = FastCache()

# ==================== FSM ====================
class PostBuilder(StatesGroup):
    media = State()
    text = State()
    buttons = State()
    confirm = State()

class AnimeAdd(StatesGroup):
    title = State()
    code = State()
    genres = State()
    description = State()
    year = State()
    poster = State()

class AnimeEdit(StatesGroup):
    select = State()
    field = State()
    value = State()

class EpisodeAdd(StatesGroup):
    anime_code = State()
    number = State()
    title = State()
    file = State()

class EpisodeDelete(StatesGroup):
    anime_code = State()
    number = State()

# ==================== DATABASE ====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.execute("PRAGMA cache_size=-128000")

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at REAL DEFAULT (strftime('%s','now')),
                is_blocked INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at REAL DEFAULT (strftime('%s','now'))
            );

            CREATE TABLE IF NOT EXISTS anime (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL,
                genres TEXT NOT NULL DEFAULT '',
                description TEXT DEFAULT '',
                year INTEGER DEFAULT 0,
                poster_file_id TEXT,
                views INTEGER DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s','now'))
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                title TEXT DEFAULT '',
                file_id TEXT NOT NULL,
                file_type TEXT DEFAULT 'video',
                added_at REAL DEFAULT (strftime('%s','now')),
                UNIQUE(anime_id, number),
                FOREIGN KEY (anime_id) REFERENCES anime(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_anime_code ON anime(code);
            CREATE INDEX IF NOT EXISTS idx_anime_title ON anime(title);
            CREATE INDEX IF NOT EXISTS idx_episodes_anime ON episodes(anime_id);
        """)
        await db.commit()

# ---------- Users ----------
async def add_user(user_id: int, username: str | None, full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
            (user_id, username, full_name)
        )
        await db.commit()

async def mark_blocked(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (user_id,))
        await db.commit()

async def get_active_users() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_blocked=0") as cur:
            return [r[0] for r in await cur.fetchall()]

# ---------- Admins ----------
async def is_admin_db(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone() is not None

async def add_admin(user_id: int, by: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)", (user_id, by))
        await db.commit()
    role_cache.delete(f"role_{user_id}")

async def remove_admin(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await db.commit()
    role_cache.delete(f"role_{user_id}")

async def get_all_admins() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cur:
            return [r[0] for r in await cur.fetchall()]

# ---------- Anime ----------
async def add_anime(title: str, code: str, genres: str, description: str,
                    year: int, poster: str | None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO anime (title, code, genres, description, year, poster_file_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, code.upper(), genres.lower(), description, year, poster)
        )
        await db.commit()
        return cur.lastrowid

async def update_anime(anime_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [anime_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE anime SET {fields} WHERE id=?", values)
        await db.commit()

async def get_anime_by_code(code: str) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM anime WHERE code=?", (code.upper(),)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_anime_by_id(aid: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM anime WHERE id=?", (aid,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def search_anime(query: str, limit: int = 15) -> List[Dict]:
    q = f"%{query.lower()}%"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM anime 
               WHERE lower(title) LIKE ? OR lower(code) LIKE ? OR lower(genres) LIKE ?
               ORDER BY views DESC LIMIT ?""",
            (q, q, q, limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_random_anime() -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM anime ORDER BY RANDOM() LIMIT 1") as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_top_anime(limit: int = 10) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM anime ORDER BY views DESC LIMIT ?", (limit,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_newest_anime(limit: int = 10) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM anime ORDER BY created_at DESC LIMIT ?", (limit,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def increment_views(aid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE anime SET views = views + 1 WHERE id=?", (aid,))
        await db.commit()

# ---------- Episodes ----------
async def add_episode(anime_id: int, number: int, title: str, file_id: str, file_type: str = "video"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO episodes (anime_id, number, title, file_id, file_type)
               VALUES (?, ?, ?, ?, ?)""",
            (anime_id, number, title, file_id, file_type)
        )
        await db.commit()

async def delete_episode(anime_id: int, number: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM episodes WHERE anime_id=? AND number=?", (anime_id, number))
        await db.commit()

async def get_episodes(anime_id: int) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM episodes WHERE anime_id=? ORDER BY number", (anime_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_episode(anime_id: int, number: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM episodes WHERE anime_id=? AND number=?", (anime_id, number)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

# ==================== ROLLAR ====================
async def get_role(user_id: int) -> str:
    key = f"role_{user_id}"
    cached = role_cache.get(key)
    if cached:
        return cached
    if user_id == OWNER_ID:
        role = "owner"
    elif await is_admin_db(user_id):
        role = "admin"
    else:
        role = "user"
    role_cache.set(key, role, ROLE_CACHE_TTL)
    return role

async def is_owner(uid: int) -> bool:
    return await get_role(uid) == "owner"

async def is_staff(uid: int) -> bool:
    return await get_role(uid) in ("owner", "admin")

# ==================== MAJBURIY OBUNA ====================
async def check_subscription(bot: Bot, user_id: int) -> bool:
    key = f"sub_{user_id}"
    cached = sub_cache.get(key)
    if cached is not None:
        return cached

    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        is_sub = member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        is_sub = False

    ttl = SUB_CACHE_TTL if is_sub else SUB_CACHE_NEGATIVE_TTL
    sub_cache.set(key, is_sub, ttl)
    return is_sub

def sub_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📢 Kanalga obuna bo‘lish", url=CHANNEL_URL))
    builder.row(InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub"))
    return builder.as_markup()

# ==================== KLAVIATURALAR ====================
def main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🔍 Qidirish", callback_data="search"),
        InlineKeyboardButton(text="🎲 Tasodifiy", callback_data="random")
    )
    b.row(
        InlineKeyboardButton(text="🏆 Top", callback_data="top"),
        InlineKeyboardButton(text="🆕 Yangi", callback_data="newest")
    )
    return b.as_markup()

def staff_kb(is_owner: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="📝 Post yaratish", callback_data="post"),
        InlineKeyboardButton(text="➕ Anime qo‘shish", callback_data="add_anime")
    )
    b.row(
        InlineKeyboardButton(text="✏️ Anime tahrirlash", callback_data="edit_anime"),
        InlineKeyboardButton(text="📺 Qism qo‘shish", callback_data="add_ep")
    )
    b.row(
        InlineKeyboardButton(text="🗑 Qism o‘chirish", callback_data="del_ep")
    )
    if is_owner:
        b.row(
            InlineKeyboardButton(text="👤 Admin +", callback_data="add_admin"),
            InlineKeyboardButton(text="🚫 Admin −", callback_data="del_admin")
        )
        b.row(InlineKeyboardButton(text="📋 Adminlar", callback_data="list_admins"))
    b.row(
        InlineKeyboardButton(text="📊 Statistika", callback_data="stats"),
        InlineKeyboardButton(text="🏠 Asosiy", callback_data="main")
    )
    return b.as_markup()

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")
    ).as_markup()

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="🔙 Orqaga", callback_data="main")
    ).as_markup()

# ==================== ROUTER ====================
router = Router()

# ---------- START & SUBSCRIPTION ----------
@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    user = message.from_user
    await add_user(user.id, user.username, user.full_name)

    if not await check_subscription(bot, user.id):
        await message.answer(
            f"<b>Assalomu alaykum, {user.first_name}!</b>\n\n"
            "Botdan foydalanish uchun kanalimizga obuna bo‘ling:",
            reply_markup=sub_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return

    role = await get_role(user.id)
    text = f"<b>🎌 Anime Bot</b>\n\nSalom, <b>{user.first_name}</b>!\nKerakli bo‘limni tanlang:"
    if role == "owner":
        await message.answer(text, reply_markup=staff_kb(True), parse_mode=ParseMode.HTML)
    elif role == "admin":
        await message.answer(text, reply_markup=staff_kb(False), parse_mode=ParseMode.HTML)
    else:
        await message.answer(text, reply_markup=main_kb(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery, bot: Bot):
    sub_cache.delete(f"sub_{callback.from_user.id}")
    if await check_subscription(bot, callback.from_user.id):
        await callback.message.edit_text("✅ <b>Obuna tasdiqlandi!</b>", parse_mode=ParseMode.HTML)
        role = await get_role(callback.from_user.id)
        kb = staff_kb(True) if role == "owner" else (staff_kb(False) if role == "admin" else main_kb())
        await callback.message.answer("Asosiy menyu:", reply_markup=kb)
    else:
        await callback.answer("Hali kanalga obuna bo‘lmadingiz!", show_alert=True)
    await callback.answer()

@router.callback_query(F.data == "main")
async def cb_main(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        await callback.message.edit_text(
            "Avval kanalga obuna bo‘ling:",
            reply_markup=sub_keyboard()
        )
        await callback.answer()
        return

    role = await get_role(callback.from_user.id)
    text = "<b>🎌 Asosiy menyu</b>"
    if role == "owner":
        await callback.message.edit_text(text, reply_markup=staff_kb(True), parse_mode=ParseMode.HTML)
    elif role == "admin":
        await callback.message.edit_text(text, reply_markup=staff_kb(False), parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(text, reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    role = await get_role(callback.from_user.id)
    kb = staff_kb(True) if role == "owner" else (staff_kb(False) if role == "admin" else main_kb())
    await callback.message.edit_text("❌ Bekor qilindi.", reply_markup=kb)
    await callback.answer()

# ---------- QIDIRUV & KO‘RISH ----------
@router.callback_query(F.data == "search")
async def cb_search(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        await callback.answer("Obuna bo‘ling!", show_alert=True)
        return
    await callback.message.edit_text(
        "<b>🔍 Qidirish</b>\n\nAnime nomi yoki kodini yozing:",
        reply_markup=back_kb(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: Message, bot: Bot, state: FSMContext):
    if await state.get_state():
        return
    if not await check_subscription(bot, message.from_user.id):
        await message.answer("Avval kanalga obuna bo‘ling:", reply_markup=sub_keyboard())
        return

    query = message.text.strip()
    if len(query) < 2:
        return

    results = await search_anime(query)
    if not results:
        await message.answer(f"❌ «{query}» bo‘yicha hech narsa topilmadi.", reply_markup=back_kb())
        return

    b = InlineKeyboardBuilder()
    for a in results:
        b.row(InlineKeyboardButton(text=f"{a['title']} [{a['code']}]", callback_data=f"a_{a['id']}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="main"))
    await message.answer(f"<b>Natija:</b> {len(results)} ta", reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "random")
async def cb_random(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        await callback.answer("Obuna bo‘ling!", show_alert=True)
        return
    anime = await get_random_anime()
    if not anime:
        await callback.answer("Bazada anime yo‘q", show_alert=True)
        return
    await show_anime(callback.message, anime, edit=True)
    await callback.answer()

@router.callback_query(F.data == "top")
async def cb_top(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        await callback.answer("Obuna bo‘ling!", show_alert=True)
        return
    items = await get_top_anime()
    if not items:
        await callback.answer("Bo‘sh", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for i, a in enumerate(items, 1):
        b.row(InlineKeyboardButton(text=f"{i}. {a['title']} • {a['views']}👁", callback_data=f"a_{a['id']}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="main"))
    await callback.message.edit_text("<b>🏆 Top anime</b>", reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "newest")
async def cb_newest(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        await callback.answer("Obuna bo‘ling!", show_alert=True)
        return
    items = await get_newest_anime()
    if not items:
        await callback.answer("Bo‘sh", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for a in items:
        b.row(InlineKeyboardButton(text=f"🆕 {a['title']} [{a['code']}]", callback_data=f"a_{a['id']}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="main"))
    await callback.message.edit_text("<b>🆕 Eng yangi</b>", reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
    await callback.answer()

async def show_anime(message: Message, anime: Dict, edit: bool = False):
    await increment_views(anime["id"])
    eps = await get_episodes(anime["id"])
    text = (
        f"<b>🎌 {anime['title']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>Kod:</b> <code>{anime['code']}</code>\n"
        f"<b>Janr:</b> {anime['genres'].title() or '—'}\n"
        f"<b>Yil:</b> {anime['year'] or '—'}\n"
        f"<b>Qismlar:</b> {len(eps)}\n"
        f"<b>Ko‘rishlar:</b> {anime['views']+1} 👁\n\n"
        f"{anime['description'] or ''}"
    )
    b = InlineKeyboardBuilder()
    if eps:
        for ep in eps[:20]:
            title = ep['title'] or f"{ep['number']}-qism"
            b.row(InlineKeyboardButton(text=f"▶️ {title}", callback_data=f"ep_{anime['id']}_{ep['number']}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="main"))

    if anime.get("poster_file_id"):
        try:
            if edit:
                await message.delete()
            await message.answer_photo(anime["poster_file_id"], caption=text, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    if edit:
        await message.edit_text(text, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(text, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("a_"))
async def cb_anime(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        await callback.answer("Obuna bo‘ling!", show_alert=True)
        return
    aid = int(callback.data.split("_")[1])
    anime = await get_anime_by_id(aid)
    if not anime:
        await callback.answer("Topilmadi", show_alert=True)
        return
    await show_anime(callback.message, anime, edit=True)
    await callback.answer()

@router.callback_query(F.data.startswith("ep_"))
async def cb_episode(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        await callback.answer("Obuna bo‘ling!", show_alert=True)
        return
    _, aid, num = callback.data.split("_")
    ep = await get_episode(int(aid), int(num))
    if not ep:
        await callback.answer("Qism topilmadi", show_alert=True)
        return

    try:
        if ep["file_type"] == "video":
            await callback.message.answer_video(ep["file_id"], caption=f"<b>{ep['title'] or f'{num}-qism'}</b>", parse_mode=ParseMode.HTML)
        else:
            await callback.message.answer_document(ep["file_id"], caption=f"<b>{ep['title'] or f'{num}-qism'}</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await callback.answer("Fayl yuborib bo‘lmadi", show_alert=True)
        logger.error(e)
    await callback.answer()

# ==================== STAFF FUNKSIYALARI ====================
@router.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery):
    if not await is_staff(callback.from_user.id):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        users = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_blocked=0")).fetchone())[0]
        anime_c = (await (await db.execute("SELECT COUNT(*) FROM anime")).fetchone())[0]
        eps = (await (await db.execute("SELECT COUNT(*) FROM episodes")).fetchone())[0]
        admins = (await (await db.execute("SELECT COUNT(*) FROM admins")).fetchone())[0]
    text = (
        f"<b>📊 Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b>\n"
        f"🎌 Anime: <b>{anime_c}</b>\n"
        f"📺 Qismlar: <b>{eps}</b>\n"
        f"🛡 Adminlar: <b>{admins}</b>"
    )
    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()

# ---------- ADMIN BOSHQARUV (faqat Owner) ----------
@router.callback_query(F.data == "add_admin")
async def cb_add_admin(callback: CallbackQuery, state: FSMContext):
    if not await is_owner(callback.from_user.id):
        await callback.answer("Faqat Owner!", show_alert=True)
        return
    await state.set_state("wait_admin_id")
    await callback.message.edit_text("Yangi admin ID sini yuboring:", reply_markup=cancel_kb())
    await callback.answer()

@router.callback_query(F.data == "del_admin")
async def cb_del_admin(callback: CallbackQuery, state: FSMContext):
    if not await is_owner(callback.from_user.id):
        await callback.answer("Faqat Owner!", show_alert=True)
        return
    await state.set_state("wait_del_admin")
    await callback.message.edit_text("O‘chiriladigan admin ID sini yuboring:", reply_markup=cancel_kb())
    await callback.answer()

@router.callback_query(F.data == "list_admins")
async def cb_list_admins(callback: CallbackQuery):
    if not await is_owner(callback.from_user.id):
        await callback.answer("Faqat Owner!", show_alert=True)
        return
    admins = await get_all_admins()
    text = "<b>📋 Adminlar</b>\n\n" + ("\n".join(f"• <code>{a}</code>" for a in admins) if admins else "Bo‘sh")
    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.message(F.text.regexp(r"^\d+$"))
async def process_id(message: Message, state: FSMContext):
    st = await state.get_state()
    if st not in ("wait_admin_id", "wait_del_admin"):
        return
    if not await is_owner(message.from_user.id):
        await state.clear()
        return
    uid = int(message.text)
    if st == "wait_admin_id":
        if uid == OWNER_ID:
            await message.answer("O‘zingizni qo‘sha olmaysiz")
        else:
            await add_admin(uid, message.from_user.id)
            await message.answer(f"✅ <code>{uid}</code> admin qilindi", parse_mode=ParseMode.HTML)
    else:
        await remove_admin(uid)
        await message.answer(f"🚫 <code>{uid}</code> o‘chirildi", parse_mode=ParseMode.HTML)
    await state.clear()

# ---------- ANIME QO‘SHISH ----------
@router.callback_query(F.data == "add_anime")
async def cb_add_anime(callback: CallbackQuery, state: FSMContext):
    if not await is_staff(callback.from_user.id):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    await state.set_state(AnimeAdd.title)
    await callback.message.edit_text("1️⃣ Anime nomini yuboring:", reply_markup=cancel_kb())
    await callback.answer()

@router.message(AnimeAdd.title)
async def anime_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AnimeAdd.code)
    await message.answer("2️⃣ Unikal kod (masalan: NRTO):", reply_markup=cancel_kb())

@router.message(AnimeAdd.code)
async def anime_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    if await get_anime_by_code(code):
        await message.answer("Bu kod band! Boshqa kod yuboring:")
        return
    await state.update_data(code=code)
    await state.set_state(AnimeAdd.genres)
    await message.answer("3️⃣ Janrlar (vergul bilan):", reply_markup=cancel_kb())

@router.message(AnimeAdd.genres)
async def anime_genres(message: Message, state: FSMContext):
    await state.update_data(genres=message.text.strip().lower())
    await state.set_state(AnimeAdd.description)
    await message.answer("4️⃣ Anime tavsifi (description):", reply_markup=cancel_kb())

@router.message(AnimeAdd.description)
async def anime_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(AnimeAdd.year)
    await message.answer("5️⃣ Chiqqan yili (raqamda):", reply_markup=cancel_kb())

@router.message(AnimeAdd.year)
async def anime_year(message: Message, state: FSMContext):
    try:
        year = int(message.text.strip())
    except ValueError:
        await message.answer("Faqat raqam yuboring:")
        return
    await state.update_data(year=year)
    await state.set_state(AnimeAdd.poster)
    await message.answer("6️⃣ Poster rasm yuboring (yoki /skip):", reply_markup=cancel_kb())

@router.message(AnimeAdd.poster, F.photo)
async def anime_poster_photo(message: Message, state: FSMContext):
    await state.update_data(poster=message.photo[-1].file_id)
    await save_new_anime(message, state)

@router.message(AnimeAdd.poster, Command("skip"))
async def anime_poster_skip(message: Message, state: FSMContext):
    await state.update_data(poster=None)
    await save_new_anime(message, state)

async def save_new_anime(message: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    try:
        await add_anime(d['title'], d['code'], d['genres'], d['description'], d['year'], d.get('poster'))
        role = await get_role(message.from_user.id)
        kb = staff_kb(role == "owner")
        await message.answer(f"<b>✅ Anime qo‘shildi!</b>\n\n• Nomi: {d['title']}\n• Kodi: <code>{d['code']}</code>", reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

# ---------- ANIME TAHRIRLASH ----------
@router.callback_query(F.data == "edit_anime")
async def cb_edit_anime(callback: CallbackQuery, state: FSMContext):
    if not await is_staff(callback.from_user.id):
        return
    await state.set_state(AnimeEdit.select)
    await callback.message.edit_text("Tahrirlanadigan anime <b>kodini</b> yuboring:", reply_markup=cancel_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.message(AnimeEdit.select)
async def edit_select_anime(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    anime = await get_anime_by_code(code)
    if not anime:
        await message.answer("❌ Bunday kodli anime topilmadi. Qaytadan yuboring:")
        return
    await state.update_data(anime_id=anime['id'])
    await state.set_state(AnimeEdit.field)
    
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="Nomi", callback_data="ef_title"), InlineKeyboardButton(text="Kodi", callback_data="ef_code"))
    b.row(InlineKeyboardButton(text="Janr", callback_data="ef_genres"), InlineKeyboardButton(text="Tavsif", callback_data="ef_description"))
    b.row(InlineKeyboardButton(text="Yil", callback_data="ef_year"), InlineKeyboardButton(text="Poster", callback_data="ef_poster"))
    b.row(InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel"))
    
    await message.answer(f"<b>Tahrirlash:</b> {anime['title']}\nQaysi qismini o‘zgartirmoqchisiz?", reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("ef_"))
async def edit_field_select(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split("_")[1]
    await state.update_data(field=field)
    await state.set_state(AnimeEdit.value)
    await callback.message.edit_text(f"Yangi qiymatni yuboring ({field}):", reply_markup=cancel_kb())
    await callback.answer()

@router.message(AnimeEdit.value)
async def edit_value_process(message: Message, state: FSMContext):
    data = await state.get_data()
    anime_id = data['anime_id']
    field = data['field']
    
    val = message.photo[-1].file_id if (field == "poster" and message.photo) else message.text.strip()
    if field == "year":
        try:
            val = int(val)
        except ValueError:
            await message.answer("Faqat raqam yuboring:")
            return

    await update_anime(anime_id, **{field if field != "poster" else "poster_file_id": val})
    await state.clear()
    role = await get_role(message.from_user.id)
    kb = staff_kb(role == "owner")
    await message.answer("<b>✅ Muvaffaqiyatli tahrirlandi!</b>", reply_markup=kb, parse_mode=ParseMode.HTML)

# ---------- QISM QO‘SHISH VA O‘CHIRISH ----------
@router.callback_query(F.data == "add_ep")
async def cb_add_ep(callback: CallbackQuery, state: FSMContext):
    if not await is_staff(callback.from_user.id):
        return
    await state.set_state(EpisodeAdd.anime_code)
    await callback.message.edit_text("Anime kodini yuboring:", reply_markup=cancel_kb())
    await callback.answer()

@router.message(EpisodeAdd.anime_code)
async def ep_code(message: Message, state: FSMContext):
    anime = await get_anime_by_code(message.text.strip())
    if not anime:
        await message.answer("❌ Anime topilmadi. Kodni qayta yuboring:")
        return
    await state.update_data(anime_id=anime['id'])
    await state.set_state(EpisodeAdd.number)
    await message.answer("Qism raqamini yuboring (masalan: 1):", reply_markup=cancel_kb())

@router.message(EpisodeAdd.number)
async def ep_number(message: Message, state: FSMContext):
    try:
        num = int(message.text.strip())
    except ValueError:
        await message.answer("Faqat raqam kiriting:")
        return
    await state.update_data(number=num)
    await state.set_state(EpisodeAdd.title)
    await message.answer("Qism nomini yuboring (yoki /skip):", reply_markup=cancel_kb())

@router.message(EpisodeAdd.title, Command("skip"))
async def ep_title_skip(message: Message, state: FSMContext):
    await state.update_data(title="")
    await state.set_state(EpisodeAdd.file)
    await message.answer("Endi video yoki hujjat (file) yuboring:", reply_markup=cancel_kb())

@router.message(EpisodeAdd.title)
async def ep_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(EpisodeAdd.file)
    await message.answer("Endi video yoki hujjat yuboring:", reply_markup=cancel_kb())

@router.message(EpisodeAdd.file, F.video | F.document)
async def ep_file(message: Message, state: FSMContext):
    d = await state.get_data()
    if message.video:
        file_id = message.video.file_id
        file_type = "video"
    else:
        file_id = message.document.file_id
        file_type = "document"

    await add_episode(d['anime_id'], d['number'], d.get('title', ''), file_id, file_type)
    await state.clear()
    role = await get_role(message.from_user.id)
    kb = staff_kb(role == "owner")
    await message.answer(f"<b>✅ {d['number']}-qism qo‘shildi!</b>", reply_markup=kb, parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "del_ep")
async def cb_del_ep(callback: CallbackQuery, state: FSMContext):
    if not await is_staff(callback.from_user.id):
        return
    await state.set_state(EpisodeDelete.anime_code)
    await callback.message.edit_text("Qism o‘chiriladigan anime kodini yuboring:", reply_markup=cancel_kb())
    await callback.answer()

@router.message(EpisodeDelete.anime_code)
async def del_ep_code(message: Message, state: FSMContext):
    anime = await get_anime_by_code(message.text.strip())
    if not anime:
        await message.answer("❌ Anime topilmadi:")
        return
    await state.update_data(anime_id=anime['id'])
    await state.set_state(EpisodeDelete.number)
    await message.answer("O‘chiriladigan qism raqamini yuboring:", reply_markup=cancel_kb())

@router.message(EpisodeDelete.number)
async def del_ep_num(message: Message, state: FSMContext):
    d = await state.get_data()
    try:
        num = int(message.text.strip())
    except ValueError:
        await message.answer("Faqat raqam:")
        return
    await delete_episode(d['anime_id'], num)
    await state.clear()
    role = await get_role(message.from_user.id)
    kb = staff_kb(role == "owner")
    await message.answer(f"<b>🗑 {num}-qism o‘chirildi!</b>", reply_markup=kb, parse_mode=ParseMode.HTML)

# ---------- POST BUILDER ----------
@router.callback_query(F.data == "post")
async def cb_post(callback: CallbackQuery, state: FSMContext):
    if not await is_staff(callback.from_user.id):
        return
    await state.set_state(PostBuilder.media)
    await callback.message.edit_text("1️⃣ Rasm yoki video yuboring (yoki /skip):", reply_markup=cancel_kb())
    await callback.answer()

@router.message(PostBuilder.media, F.photo | F.video)
async def post_media(message: Message, state: FSMContext):
    mid = message.photo[-1].file_id if message.photo else message.video.file_id
    mtype = "photo" if message.photo else "video"
    await state.update_data(media=mid, mtype=mtype)
    await state.set_state(PostBuilder.text)
    await message.answer("2️⃣ Post matnini yuboring:", reply_markup=cancel_kb())

@router.message(PostBuilder.media, Command("skip"))
async def post_skip_media(message: Message, state: FSMContext):
    await state.update_data(media=None, mtype=None)
    await state.set_state(PostBuilder.text)
    await message.answer("2️⃣ Post matnini yuboring:", reply_markup=cancel_kb())

@router.message(PostBuilder.text)
async def post_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await state.set_state(PostBuilder.buttons)
    await message.answer(
        "3️⃣ Inline tugmalar (ixtiyoriy).\n"
        "Format: <code>Matn | https://t.me/...</code>\n"
        "Tugmasiz bo‘lsa /skip yuboring:",
        reply_markup=cancel_kb(),
        parse_mode=ParseMode.HTML
    )

@router.message(PostBuilder.buttons, Command("skip"))
async def post_skip_btn(message: Message, state: FSMContext):
    await state.update_data(buttons=[])
    await show_preview(message, state)

@router.message(PostBuilder.buttons)
async def post_buttons(message: Message, state: FSMContext):
    btns = []
    for line in message.text.strip().split("\n"):
        if "|" in line:
            p = line.split("|", 1)
            btns.append({"text": p[0].strip(), "url": p[1].strip()})
    await state.update_data(buttons=btns)
    await show_preview(message, state)

async def show_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(PostBuilder.confirm)
    
    b = InlineKeyboardBuilder()
    for btn in data.get('buttons', []):
        b.row(InlineKeyboardButton(text=btn['text'], url=btn['url']))
    b.row(
        InlineKeyboardButton(text="✅ Yuborish", callback_data="bc_send"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")
    )
    
    await message.answer("<b>👀 Post ko‘rinishi:</b>", parse_mode=ParseMode.HTML)
    if data.get('media'):
        if data['mtype'] == 'photo':
            await message.answer_photo(data['media'], caption=data['text'], reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
        else:
            await message.answer_video(data['media'], caption=data['text'], reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(data['text'], reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "bc_send", PostBuilder.confirm)
async def bc_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_staff(callback.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    
    users = await get_active_users()
    b = InlineKeyboardBuilder()
    for btn in data.get('buttons', []):
        b.row(InlineKeyboardButton(text=btn['text'], url=btn['url']))
    markup = b.as_markup() if data.get('buttons') else None
    
    success = 0
    await callback.message.edit_text(f"🚀 Yuborilmoqda... Jami: {len(users)}")
    
    for uid in users:
        try:
            if data.get('media'):
                if data['mtype'] == 'photo':
                    await bot.send_photo(uid, data['media'], caption=data['text'], reply_markup=markup, parse_mode=ParseMode.HTML)
                else:
                    await bot.send_video(uid, data['media'], caption=data['text'], reply_markup=markup, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(uid, data['text'], reply_markup=markup, parse_mode=ParseMode.HTML)
            success += 1
            await asyncio.sleep(0.03)
        except TelegramForbiddenError:
            await mark_blocked(uid)
        except Exception:
            pass
            
    role = await get_role(callback.from_user.id)
    kb = staff_kb(role == "owner")
    await callback.message.answer(f"<b>✅ Muvaffaqiyatli tarqatildi!</b>\nYetib bordi: {success} ta", reply_markup=kb, parse_mode=ParseMode.HTML)

# ==================== KEEP-ALIVE WEB SERVER ====================
async def handle_ping(request):
    return web.Response(text="Bot is active!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Keep-Alive web server {PORT}-portda ishga tushdi.")

# ==================== MAIN ====================
async def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("DIQQAT: BOT_TOKEN va OWNER_ID ni kiriting!")
        return
    await init_db()
    
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    await start_web_server()

    logger.info("Bot ishga tushdi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
