"""
HujjatBot - Telegram bot
Obyektivka (MA'LUMOTNOMA) va 3x4 rasm xizmati.

Ishga tushirish:
    export BOT_TOKEN="sizning-bot-tokeningiz"
    python3 bot.py
"""
import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    Update,
    WebAppInfo,
)
import aiohttp
from aiohttp import web
from PIL import Image, ImageOps

from obyektivka_docx import build_obyektivka_docx
from obyektivka_pdf import build_obyektivka_pdf
import slide_gen
import referat_gen
import test_gen
import photo_3x4


def fix_photo_orientation(path: str) -> None:
    """Telefon kamerasi rasmni 'aylantirish' EXIF belgisi bilan saqlaydi -
    docx/pdf esa buni hisobga olmay xom holicha qo'yadi, natijada qiyshiq
    chiqadi. Bu funksiya rasmni to'g'ri burchakka aylantirib, qayta saqlaydi.
    """
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        img.convert("RGB").save(path, format="JPEG", quality=95)
    except Exception:
        logger.exception("Rasm orientatsiyasini tuzatishda xato")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "SIZNING_BOT_TOKENINGIZ_BU_YERGA")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://madesy.onrender.com/webapp/")

# ─────────────────────────────────────────────────────────
# To'lov sozlamalari (P2P, qo'lda tasdiqlash)
# ─────────────────────────────────────────────────────────
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "800101122"))
CARD_NUMBER = os.getenv("CARD_NUMBER", "5614 6865 0227 5798")
CARD_HOLDER = os.getenv("CARD_HOLDER", "TEMURJON BAXODIROV")
PRICE_OBYEKTIVKA = int(os.getenv("PRICE_OBYEKTIVKA", "8000"))

# Buyurtmalar shu yerda xotirada saqlanadi (server qayta ishga tushsa
# tozalanadi - hozircha kichik hajmda ishlatilgani uchun yetarli)
orders = {}

# Foydalanuvchilar balansi (chat_id -> so'm). Balans to'ldirish P2P + admin
# tasdiqlash orqali amalga oshadi; xizmatlar (Obyektivka/Slayd/Referat)
# esa shu balansdan avtomatik yechiladi - har safar qayta to'lov qilish
# shart emas.
# ─────────────────────────────────────────────────────────
# Supabase - balansni doimiy (server qayta ishga tushsa ham
# yo'qolmaydigan) saqlash uchun. Render'ning fayl tizimi
# "ephemeral" (vaqtinchalik) bo'lgani uchun oddiy Python
# lug'ati yoki fayl ishlatib bo'lmaydi - har bir qayta
# ishga tushishda hammasi 0'ga qaytib qolar edi.
# ─────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

_supabase_session: aiohttp.ClientSession | None = None


def _supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


async def _get_supabase_session() -> aiohttp.ClientSession:
    global _supabase_session
    if _supabase_session is None or _supabase_session.closed:
        _supabase_session = aiohttp.ClientSession()
    return _supabase_session


async def db_fetch_balance(chat_id: int) -> int:
    """Supabase'dan foydalanuvchi balansini o'qiydi. Agar hali yozuv
    bo'lmasa yoki Supabase sozlanmagan bo'lsa, 0 qaytaradi."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/balances"
        params = {"chat_id": f"eq.{chat_id}", "select": "balance"}
        async with session.get(url, headers=_supabase_headers(), params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.error(f"Supabase o'qishda xato: {resp.status} {await resp.text()}")
                return 0
            rows = await resp.json()
            if rows:
                return int(rows[0].get("balance", 0))
            return 0
    except Exception:
        logger.exception("Supabase'dan balans o'qishda xato")
        return 0


async def db_save_balance(chat_id: int, balance: int):
    """Supabase'ga foydalanuvchi balansini yozadi (upsert - yozuv bo'lsa
    yangilaydi, bo'lmasa yaratadi)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/balances"
        headers = _supabase_headers()
        headers["Prefer"] = "resolution=merge-duplicates"
        payload = {"chat_id": chat_id, "balance": balance}
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status not in (200, 201):
                logger.error(f"Supabase'ga yozishda xato: {resp.status} {await resp.text()}")
    except Exception:
        logger.exception("Supabase'ga balans yozishda xato")


# ─────────────────────────────────────────────────────────
# Foydalanuvchilar statistikasi - kimlar obuna bo'lgan, oxirgi qachon
# faol bo'lgan, botni bloklaganmi. Faqat admin (/stats) uchun.
# Alohida "users" jadvali - balansga aralashmaydi.
# ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def db_touch_user(chat_id: int, username: str | None):
    """Foydalanuvchi har safar xabar yozganda yoki tugma bosganda
    last_seen'ni yangilaydi (upsert - yozuv bo'lmasa yaratadi)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/users"
        headers = _supabase_headers()
        headers["Prefer"] = "resolution=merge-duplicates"
        payload = {
            "chat_id": chat_id,
            "username": username,
            "last_seen": _now_iso(),
            "is_blocked": False,
        }
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status not in (200, 201):
                logger.error(f"Supabase users yozishda xato: {resp.status} {await resp.text()}")
    except Exception:
        logger.exception("Supabase'ga user faolligini yozishda xato")


async def db_mark_user_blocked(chat_id: int):
    """Foydalanuvchiga xabar yuborishga urinilganda Telegram
    'Forbidden' xatosi qaytsa (bot bloklangan/chat o'chirilgan),
    shu funksiya chaqirilib is_blocked=true qilib qo'yiladi."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/users"
        headers = _supabase_headers()
        params = {"chat_id": f"eq.{chat_id}"}
        payload = {"is_blocked": True}
        async with session.patch(url, headers=headers, params=params, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status not in (200, 204):
                logger.error(f"Supabase user blocklashda xato: {resp.status} {await resp.text()}")
    except Exception:
        logger.exception("Supabase'da user'ni bloklangan deb belgilashda xato")


async def _supabase_count(table: str, filters: dict) -> int:
    """PostgREST'ning 'Content-Range' javob sarlavhasidan foydalanib,
    filtrga mos qatorlar sonini (butun jadvalni yuklamasdan) qaytaradi."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        headers = _supabase_headers()
        headers["Prefer"] = "count=exact"
        params = {**filters, "select": "chat_id", "limit": "1"}
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            content_range = resp.headers.get("Content-Range", "")
            if "/" in content_range:
                total = content_range.split("/")[-1]
                if total.isdigit():
                    return int(total)
            return 0
    except Exception:
        logger.exception(f"Supabase hisoblashda xato ({table})")
        return 0


async def db_fetch_active_chat_ids() -> list[int]:
    """Xabar (post) yuborish uchun - bloklamagan barcha obunachilar
    ro'yxatini qaytaradi. Madesy hozircha 1-300 kishi doirasida
    ishlayotgani uchun bitta so'rovda hammasi olinadi."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/users"
        params = {"select": "chat_id", "is_blocked": "eq.false", "limit": "5000"}
        async with session.get(url, headers=_supabase_headers(), params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.error(f"Users ro'yxatini olishda xato: {resp.status} {await resp.text()}")
                return []
            rows = await resp.json()
            return [r["chat_id"] for r in rows if "chat_id" in r]
    except Exception:
        logger.exception("Users ro'yxatini olishda xato")
        return []


class UserTrackingMiddleware(BaseMiddleware):
    """Har bir kelgan update (xabar yoki tugma bosish) uchun
    foydalanuvchining last_seen vaqtini fonda (bloklamasdan) yangilaydi."""

    async def __call__(self, handler, event: Update, data: dict):
        chat_id = None
        username = None
        if event.message and event.message.from_user:
            chat_id = event.message.from_user.id
            username = event.message.from_user.username
        elif event.callback_query and event.callback_query.from_user:
            chat_id = event.callback_query.from_user.id
            username = event.callback_query.from_user.username

        if chat_id:
            asyncio.create_task(db_touch_user(chat_id, username))

        return await handler(event, data)


# Tezkor kirish uchun xotirada ham saqlaymiz (kesh) - lekin manba
# (source of truth) har doim Supabase, shuning uchun server qayta
# ishga tushsa ham ma'lumot yo'qolmaydi.
user_balances: dict[int, int] = {}

MIN_TOPUP = 1000
MAX_TOPUP = 500_000


async def get_balance(chat_id: int) -> int:
    if chat_id in user_balances:
        return user_balances[chat_id]
    balance = await db_fetch_balance(chat_id)
    user_balances[chat_id] = balance
    return balance


async def add_balance(chat_id: int, amount: int):
    current = await get_balance(chat_id)
    new_balance = current + amount
    user_balances[chat_id] = new_balance
    await db_save_balance(chat_id, new_balance)


async def deduct_balance(chat_id: int, amount: int) -> bool:
    """Balansdan yechishga harakat qiladi. Agar yetarli bo'lsa True qaytarib
    yechadi, aks holda False qaytarib hech narsa o'zgartirmaydi."""
    current = await get_balance(chat_id)
    if current >= amount:
        new_balance = current - amount
        user_balances[chat_id] = new_balance
        await db_save_balance(chat_id, new_balance)
        return True
    return False

# ─────────────────────────────────────────────────────────
# Promokodlar - marketing uchun. Mijoz kod kiritsa, admin
# tasdig'isiz balans avtomatik to'ldiriladi. Har bir promokod
# cheklangan marta (max_uses) ishlatilishi mumkin, va bitta
# foydalanuvchi bitta promokodni faqat bir marta ishlata oladi.
#
# YANGI PROMOKOD QO'SHISH: kod yozish shart emas - Supabase
# dashboard > Table Editor > promo_codes jadvaliga yangi qator
# qo'shsangiz bo'ldi (code, amount, max_uses, is_active=true).
# ─────────────────────────────────────────────────────────

async def db_fetch_promo(code: str) -> dict | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/promo_codes"
        params = {"code": f"eq.{code}", "select": "code,amount,max_uses,used_count,is_active"}
        async with session.get(url, headers=_supabase_headers(), params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.error(f"Promo o'qishda xato: {resp.status} {await resp.text()}")
                return None
            rows = await resp.json()
            return rows[0] if rows else None
    except Exception:
        logger.exception("Promo kodni o'qishda xato")
        return None


async def db_check_promo_redeemed(code: str, chat_id: int) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/promo_redemptions"
        params = {"code": f"eq.{code}", "chat_id": f"eq.{chat_id}", "select": "id"}
        async with session.get(url, headers=_supabase_headers(), params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return False
            rows = await resp.json()
            return len(rows) > 0
    except Exception:
        logger.exception("Promo ishlatilganini tekshirishda xato")
        return False


async def db_insert_promo_redemption(code: str, chat_id: int) -> bool:
    """True - muvaffaqiyatli yozildi. False - (code, chat_id) uchun
    unique constraint buzildi, ya'ni bu foydalanuvchi allaqachon
    ishlatgan (yoki DB xatosi)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/promo_redemptions"
        payload = {"code": code, "chat_id": chat_id, "redeemed_at": _now_iso()}
        async with session.post(url, headers=_supabase_headers(), json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status in (200, 201):
                return True
            if resp.status == 409:
                return False
            logger.error(f"Promo redemption yozishda xato: {resp.status} {await resp.text()}")
            return False
    except Exception:
        logger.exception("Promo redemption yozishda xato")
        return False


async def db_delete_promo_redemption(code: str, chat_id: int):
    """Balans oshirilmagan holatda (limit tugagani aniqlansa) yozuvni
    orqaga qaytarish (rollback) uchun."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/promo_redemptions"
        params = {"code": f"eq.{code}", "chat_id": f"eq.{chat_id}"}
        async with session.delete(url, headers=_supabase_headers(), params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status not in (200, 204):
                logger.error(f"Promo redemption o'chirishda xato: {resp.status} {await resp.text()}")
    except Exception:
        logger.exception("Promo redemption o'chirishda xato")


async def db_increment_promo_usage(code: str, expected_used_count: int) -> bool:
    """Optimistik concurrency: used_count hali ham expected_used_count
    bo'lsagina +1 qiladi. Boshqa birov bir vaqtda ishlatib ulgurgan bo'lsa
    (used_count o'zgargan bo'lsa) 0 qator yangilanadi -> False."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        session = await _get_supabase_session()
        url = f"{SUPABASE_URL}/rest/v1/promo_codes"
        headers = _supabase_headers()
        headers["Prefer"] = "return=representation"
        params = {"code": f"eq.{code}", "used_count": f"eq.{expected_used_count}"}
        payload = {"used_count": expected_used_count + 1}
        async with session.patch(url, headers=headers, params=params, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status not in (200, 206):
                logger.error(f"Promo usage oshirishda xato: {resp.status} {await resp.text()}")
                return False
            rows = await resp.json()
            return len(rows) > 0
    except Exception:
        logger.exception("Promo usage oshirishda xato")
        return False


async def apply_promo_code(chat_id: int, raw_code: str) -> dict:
    """Promokodni tekshiradi va to'g'ri bo'lsa balansni oshiradi.
    Natija: {"ok": bool, "message": str, "balance": int, "amount": int}.
    Bitta joyda - bot chatidagi handler ham, Mini App'dagi
    /api/redeem_promo endpointi ham shu funksiyani chaqiradi."""
    code = (raw_code or "").strip().upper()

    if not code:
        return {"ok": False, "message": "Promokodni kiriting."}
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {"ok": False, "message": "Promokod tizimi hozircha ishlamayapti, birozdan keyin urinib ko'ring."}

    promo = await db_fetch_promo(code)
    if not promo:
        return {"ok": False, "message": "Bunday promokod topilmadi."}
    if not promo.get("is_active", True):
        return {"ok": False, "message": "Bu promokod hozircha faol emas."}

    max_uses = int(promo.get("max_uses") or 0)
    used_count = int(promo.get("used_count") or 0)
    amount = int(promo.get("amount") or 0)

    if max_uses and used_count >= max_uses:
        return {"ok": False, "message": "Bu promokodning limiti tugagan."}

    if await db_check_promo_redeemed(code, chat_id):
        return {"ok": False, "message": "Siz bu promokodni allaqachon ishlatgansiz."}

    if not await db_insert_promo_redemption(code, chat_id):
        return {"ok": False, "message": "Siz bu promokodni allaqachon ishlatgansiz."}

    if not await db_increment_promo_usage(code, used_count):
        # Shu oraliqda limit tugagan (bir nechta kishi bir vaqtda urinib ko'rgan) - orqaga qaytaramiz
        await db_delete_promo_redemption(code, chat_id)
        return {"ok": False, "message": "Bu promokodning limiti hozirgina tugadi. Kechirasiz!"}

    await add_balance(chat_id, amount)
    new_balance = await get_balance(chat_id)
    return {
        "ok": True,
        "message": f"Promokod qabul qilindi! Balansingizga {amount} so'm qo'shildi.",
        "amount": amount,
        "balance": new_balance,
    }


async def redeem_promo_code(message: Message, raw_code: str):
    """Bot chatida (/promo yoki 'Promokod' tugmasi orqali) ishlatiladi."""
    result = await apply_promo_code(message.from_user.id, raw_code)
    if result["ok"]:
        await message.answer(f"🎉 {result['message']}\n💰 Joriy balans: {result['balance']} so'm")
    else:
        await message.answer(f"❌ {result['message']}")


ORDER_EXPIRY_SECONDS = 30 * 60  # to'lanmagan buyurtma 30 daqiqadan keyin bekor bo'ladi
RATE_LIMIT_WINDOW = 60  # soniya
RATE_LIMIT_MAX_REQUESTS = 5  # shu oynada bitta foydalanuvchi yarata oladigan buyurtmalar soni
MAX_TEXT_LEN = 300  # mavzu/F.I.O/muassasa kabi matn maydonlari uchun maksimal uzunlik
MAX_RECEIPT_PHOTO_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_PHOTO_MEGAPIXELS = 100  # zaxira chegara (draft mode orqali 50MP+ suratlar ham muammosiz qayta ishlanadi)

_rate_limit_hits: dict[int, list[float]] = {}


def check_rate_limit(chat_id: int) -> bool:
    """True qaytaradi - agar foydalanuvchi ruxsat etilgan chegarada bo'lsa.
    False qaytaradi - agar u juda tez-tez so'rov yuborayotgan bo'lsa (spam)."""
    now = time.time()
    hits = _rate_limit_hits.setdefault(chat_id, [])
    hits[:] = [t for t in hits if now - t < RATE_LIMIT_WINDOW]
    if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    hits.append(now)
    return True


def clean_text_field(value: str, max_len: int = MAX_TEXT_LEN) -> str:
    """Matn maydonini kesib, xavfsiz uzunlikka qisqartiradi."""
    return (value or "").strip()[:max_len]


def cleanup_expired_orders():
    """To'lanmagan va muddati o'tgan buyurtmalarni xotiradan o'chiradi."""
    now = time.time()
    expired = [
        oid for oid, o in orders.items()
        if o.get("status") == "to'lov_kutilmoqda" and now - o.get("created_at", now) > ORDER_EXPIRY_SECONDS
    ]
    for oid in expired:
        del orders[oid]
    if expired:
        logger.info(f"{len(expired)} ta eskirgan buyurtma tozalandi")


async def cleanup_expired_orders_loop():
    """Fon jarayoni: har 5 daqiqada eskirgan buyurtmalarni tozalab turadi."""
    while True:
        await asyncio.sleep(5 * 60)
        try:
            cleanup_expired_orders()
        except Exception as e:
            logger.error(f"cleanup_expired_orders xatosi: {e}")

router = Router()

# ─────────────────────────────────────────────────────────
# Savollar ro'yxati (ishchi va talaba uchun deyarli bir xil)
# ─────────────────────────────────────────────────────────

COMMON_FIELDS = [
    ("fio", "To'liq ismingizni kiriting (FIO)\n\nMisol: Aliyev Sardor Rustamovich"),
    ("tug_yil", "Tug'ilgan sanangiz?\n\nMisol: 15.03.1995"),
    ("tug_joy", "Tug'ilgan joyingiz?\n\nMisol: Surxondaryo viloyati, Termiz shahri"),
    ("millat", "Millatingiz?\n\nMisol: O'zbek"),
    ("partiya", "Partiyaviyligingiz?\n\nMisol: Yo'q"),
]

ISHCHI_EXTRA = [
    ("tashkilot", "Ish joyingiz (tashkilot va bo'lim)?\n\nMisol: Termiz shahar Tibbiyot birlashmasi, umumiy qabul bo'limi"),
    ("malumot", "Ma'lumot darajangiz?\n\nMisol: Oliy / O'rta maxsus"),
    ("tamomlagan", "Qaysi o'quv yurtini, qachon tamomlagansiz?\n\nMisol: 1992-yil Termiz tibbiyot texnikumi"),
    ("mutaxassislik", "Mutaxassisligingiz?\n\nMisol: Hamshira"),
]

TALABA_EXTRA = [
    ("tashkilot", "O'quv yurtingiz va fakultetingiz?\n\nMisol: Termiz Davlat Universiteti, Tarix fakulteti"),
    ("kurs", "Kursingiz va ta'lim darajangiz?\n\nMisol: 3-kurs, bakalavriat"),
    ("malumot", "Oldingi ta'limingiz (maktab/kollej)?\n\nMisol: 2020-yil Termiz 15-maktab"),
    ("mutaxassislik", "O'qish yo'nalishingiz (mutaxassisligingiz)?\n\nMisol: Tarix"),
]

TAIL_FIELDS = [
    ("ilmiy_daraja", "Ilmiy darajangiz bormi?\n\nBo'lmasa: Yo'q"),
    ("ilmiy_unvon", "Ilmiy unvoningiz bormi?\n\nBo'lmasa: Yo'q"),
    ("chet_til", "Qaysi chet tillarini bilasiz?\n\nMisol: Rus tili, Ingliz tili"),
    ("harbiy", "Harbiy (maxsus) unvoningiz bormi?\n\nBo'lmasa: Yo'q"),
    ("mukofot", "Davlat mukofoti bilan taqdirlanganmisiz?\n\nBo'lmasa: Yo'q"),
    ("deputat", "Xalq deputatlari yoki boshqa saylanadigan organ a'zosimisiz?\n\nBo'lmasa: Yo'q"),
    ("tel", "Telefon raqamingiz?\n\nMisol: 90 123 45 67"),
    ("pasport", "Pasport seriya va raqamingiz?\n\nMisol: AA 1234567"),
    ("jshshir", "JSHSHIR raqamingiz?\n\n(O'tkazib yuborish uchun: -)"),
]


def build_field_list(turi: str):
    extra = ISHCHI_EXTRA if turi == "ishchi" else TALABA_EXTRA
    # tashkilot ustidagi joyni COMMON dan oldin qo'yamiz - sarlavha uchun
    return [("_yil_marker", None)] + COMMON_FIELDS[:1] + extra[:1] + \
           (extra[1:2] if turi == "talaba" else []) + COMMON_FIELDS[1:] + extra[2 if turi=="talaba" else 1:] + TAIL_FIELDS


# ─────────────────────────────────────────────────────────
# FSM holatlari
# ─────────────────────────────────────────────────────────

class ObyektivkaForm(StatesGroup):
    choosing_type = State()
    answering = State()
    mehnat_yil = State()
    mehnat_joy = State()
    mehnat_menu = State()
    qar_munosabat = State()
    qar_fio = State()
    qar_tug = State()
    qar_ish = State()
    qar_turar = State()
    qar_menu = State()
    waiting_photo = State()
    choosing_format = State()


class PromoForm(StatesGroup):
    waiting_code = State()


class BroadcastForm(StatesGroup):
    waiting_content = State()
    confirming = State()


# ─────────────────────────────────────────────────────────
# Klaviaturalar
# ─────────────────────────────────────────────────────────

def kb_home():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Obyektivka", callback_data="menu_obyektivka")],
        [InlineKeyboardButton(text="📸 3x4 Rasm", callback_data="menu_photo")],
    ])


def kb_webapp():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 START", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="🎁 Promokod", callback_data="menu_promo")],
    ])


def kb_doc_type():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👔 Xodim obyektivkasi", callback_data="type_ishchi")],
        [InlineKeyboardButton(text="🎓 Talaba obyektivkasi", callback_data="type_talaba")],
    ])


def kb_mehnat_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yana qo'shish", callback_data="mehnat_add")],
        [InlineKeyboardButton(text="✅ Davom etish", callback_data="mehnat_done")],
    ])


def kb_qar_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yana qarindosh qo'shish", callback_data="qar_add")],
        [InlineKeyboardButton(text="✅ Tayyorlashga o'tish", callback_data="qar_done")],
    ])


def kb_format():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📘 Word (.docx)", callback_data="fmt_word")],
        [InlineKeyboardButton(text="📕 PDF", callback_data="fmt_pdf")],
    ])


def kb_skip_photo():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Rasmsiz davom etish", callback_data="photo_skip")],
    ])


# ─────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✨ <b>Madesy AI</b>\n\n"
        "📄 Obyektivka\n"
        "📊 Slayd\n"
        "📝 Test\n"
        "🖼️ 3×4 rasm\n\n"
        "Oson Sifatli Arzon",
        reply_markup=kb_webapp(),
        parse_mode="HTML",
    )


@router.message(F.text == "/stats")
async def cmd_stats(message: Message):
    """Faqat admin uchun: obunachilar, faollar va bloklaganlar soni."""
    if message.from_user.id != ADMIN_CHAT_ID:
        return  # boshqa hech kim uchun javob bermaydi

    total = await _supabase_count("users", {})
    blocked = await _supabase_count("users", {"is_blocked": "eq.true"})
    active = total - blocked

    fifteen_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    recent_active = await _supabase_count(
        "users", {"last_seen": f"gte.{fifteen_min_ago}", "is_blocked": "eq.false"}
    )

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    today_active = await _supabase_count("users", {"last_seen": f"gte.{today_start}"})

    text = (
        "📊 <b>Madesy statistikasi</b>\n\n"
        f"👥 Jami obunachilar: <b>{total}</b>\n"
        f"✅ Faol (bloklamagan): <b>{active}</b>\n"
        f"🚫 Bloklagan: <b>{blocked}</b>\n\n"
        f"🟢 So'nggi 15 daqiqada faol: <b>{recent_active}</b>\n"
        f"📅 Bugun faol bo'lganlar: <b>{today_active}</b>\n"
    )
    await message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "menu_promo")
async def menu_promo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PromoForm.waiting_code)
    await callback.message.answer("🎁 Promokodni kiriting:")
    await callback.answer()


@router.message(PromoForm.waiting_code)
async def promo_code_entered(message: Message, state: FSMContext):
    await state.clear()
    await redeem_promo_code(message, message.text or "")


@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    await redeem_promo_code(message, command.args or "")


# ─────────────────────────────────────────────────────────
# Umumiy xabar (post) yuborish - faqat admin uchun.
# Rasmli yoki rasmsiz bo'lishi mumkin. Telegram limitlariga
# tegmaslik uchun soniyasiga 5 kishiga yuboriladi.
# ─────────────────────────────────────────────────────────

BROADCAST_PER_SECOND = 5


@router.message(Command("post"))
async def cmd_post(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    await state.set_state(BroadcastForm.waiting_content)
    await message.answer(
        "📢 Yubormoqchi bo'lgan postni yuboring.\n\n"
        "• Faqat matn - matnli post bo'ladi\n"
        "• Rasm + izoh (caption) - rasmli post bo'ladi\n\n"
        "Bekor qilish uchun /cancel yozing."
    )


@router.message(BroadcastForm.waiting_content, Command("cancel"))
async def broadcast_cancel_input(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.")


@router.message(BroadcastForm.waiting_content)
async def broadcast_content_received(message: Message, state: FSMContext):
    photo_file_id = None
    text = None

    if message.photo:
        photo_file_id = message.photo[-1].file_id
        text = message.caption or ""
    elif message.text:
        text = message.text
    else:
        await message.answer("Iltimos, matn yoki rasm (izoh bilan) yuboring.")
        return

    if not text or not text.strip():
        await message.answer("Post matni bo'sh bo'lmasligi kerak. Qaytadan yuboring.")
        return

    await state.update_data(broadcast_text=text, broadcast_photo=photo_file_id)
    await state.set_state(BroadcastForm.confirming)

    recipient_count = await _supabase_count("users", {"is_blocked": "eq.false"})

    preview_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Barchaga yuborish ({recipient_count} kishi)", callback_data="broadcast_confirm")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="broadcast_cancel")],
    ])

    if photo_file_id:
        await message.answer_photo(photo_file_id, caption=text, reply_markup=preview_kb)
    else:
        await message.answer(text, reply_markup=preview_kb)


@router.callback_query(BroadcastForm.confirming, F.data == "broadcast_cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Bekor qilindi")
    await callback.message.answer("Post yuborilmadi.")


@router.callback_query(BroadcastForm.confirming, F.data == "broadcast_confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    text = data.get("broadcast_text")
    photo_file_id = data.get("broadcast_photo")
    await state.clear()
    await callback.answer()
    await callback.message.answer("🚀 Yuborish boshlandi, bu biroz vaqt olishi mumkin...")
    asyncio.create_task(run_broadcast(bot, callback.from_user.id, text, photo_file_id))


async def run_broadcast(bot: Bot, admin_chat_id: int, text: str, photo_file_id: str | None):
    """Barcha (bloklamagan) obunachilarga postni yuboradi. Telegram
    limitlariga tegmaslik uchun bir vaqtda BROADCAST_PER_SECOND tadan
    yuboradi, har paketdan keyin 1 soniya kutadi."""
    chat_ids = await db_fetch_active_chat_ids()
    total = len(chat_ids)
    sent = 0
    blocked = 0
    failed = 0

    async def _send_one(chat_id: int):
        nonlocal sent, blocked, failed
        try:
            if photo_file_id:
                await bot.send_photo(chat_id, photo_file_id, caption=text)
            else:
                await bot.send_message(chat_id, text)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
            await db_mark_user_blocked(chat_id)
        except Exception:
            failed += 1
            logger.exception(f"Broadcast: {chat_id} ga yuborishda xato")

    for i in range(0, total, BROADCAST_PER_SECOND):
        batch = chat_ids[i:i + BROADCAST_PER_SECOND]
        await asyncio.gather(*(_send_one(cid) for cid in batch))
        if i + BROADCAST_PER_SECOND < total:
            await asyncio.sleep(1)

    try:
        await bot.send_message(
            admin_chat_id,
            f"✅ Post yuborildi!\n\n"
            f"👥 Jami: {total}\n"
            f"📨 Yetkazildi: {sent}\n"
            f"🚫 Bloklangan (avtomatik belgilandi): {blocked}\n"
            f"⚠️ Boshqa xato: {failed}",
        )
    except Exception:
        logger.exception("Admin'ga broadcast hisobotini yuborishda xato")


@router.callback_query(F.data == "menu_home")
async def menu_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Nima kerak?", reply_markup=kb_home())
    await callback.answer()


# ─────────────────────────────────────────────────────────
# 3x4 rasm - hozircha o'chirilgan (tez orada)
# ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu_photo")
async def menu_photo(callback: CallbackQuery, state: FSMContext):
    await callback.answer("📸 3x4 rasm xizmati tez orada qo'shiladi!", show_alert=True)


# ─────────────────────────────────────────────────────────
# Obyektivka - boshlash
# ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu_obyektivka")
async def menu_obyektivka(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ObyektivkaForm.choosing_type)
    await callback.message.edit_text(
        "Qanday obyektivka kerak?",
        reply_markup=kb_doc_type(),
    )
    await callback.answer()


@router.callback_query(ObyektivkaForm.choosing_type, F.data.startswith("type_"))
async def choose_type(callback: CallbackQuery, state: FSMContext):
    turi = callback.data.split("_")[1]  # ishchi | talaba
    fields = build_field_list(turi)

    await state.update_data(
        turi=turi,
        fields=fields,
        field_idx=0,
        answers={},
        mehnat=[],
        qarindoshlar=[],
    )
    await state.set_state(ObyektivkaForm.answering)
    await ask_next_field(callback.message, state)
    await callback.answer()


async def ask_next_field(message: Message, state: FSMContext):
    data = await state.get_data()
    fields = data["fields"]
    idx = data["field_idx"]

    # yil marker maxsus - bu sarlavha yilini so'raydi
    field_id, question = fields[idx]
    if field_id == "_yil_marker":
        await message.answer(
            "Hujjat qaysi yil uchun tayyorlanadi?\n\nMisol: 2026-yil",
            reply_markup=None,
        )
        return

    total = len(fields)
    await message.answer(f"<b>[{idx + 1}/{total}]</b>\n\n{question}", parse_mode="HTML")


@router.message(ObyektivkaForm.answering)
async def handle_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    fields = data["fields"]
    idx = data["field_idx"]
    field_id, _ = fields[idx]

    answers = data["answers"]
    key = "sarlavha_yil" if field_id == "_yil_marker" else field_id
    answers[key] = message.text.strip()

    idx += 1
    await state.update_data(answers=answers, field_idx=idx)

    if idx >= len(fields):
        # Savollar tugadi -> mehnat faoliyatiga o'tamiz
        await state.set_state(ObyektivkaForm.mehnat_yil)
        await message.answer(
            "✅ Asosiy ma'lumotlar qabul qilindi!\n\n"
            "📋 <b>Mehnat faoliyati</b>\n\nBirinchi ish/o'qish davri - yillarini kiriting.\n\nMisol: 1993-2002",
            parse_mode="HTML",
        )
    else:
        await ask_next_field(message, state)


# ─────────────────────────────────────────────────────────
# Mehnat faoliyati
# ─────────────────────────────────────────────────────────

@router.message(ObyektivkaForm.mehnat_yil)
async def mehnat_yil_handler(message: Message, state: FSMContext):
    await state.update_data(_mehnat_yil=message.text.strip())
    await state.set_state(ObyektivkaForm.mehnat_joy)
    await message.answer("Endi tashkilot nomi va lavozimni kiriting.\n\nMisol: Termiz shahar Tibbiyot birlashmasi")


@router.message(ObyektivkaForm.mehnat_joy)
async def mehnat_joy_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    mehnat = data["mehnat"]
    mehnat.append({"yillar": data["_mehnat_yil"], "tashkilot": message.text.strip()})
    await state.update_data(mehnat=mehnat)
    await state.set_state(ObyektivkaForm.mehnat_menu)

    summary = "\n".join(f"• {m['yillar']} yy. - {m['tashkilot']}" for m in mehnat)
    await message.answer(
        f"✅ Qo'shildi!\n\n<b>Hozirgi ro'yxat:</b>\n{summary}\n\nYana qo'shamizmi?",
        parse_mode="HTML",
        reply_markup=kb_mehnat_menu(),
    )


@router.callback_query(ObyektivkaForm.mehnat_menu, F.data == "mehnat_add")
async def mehnat_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ObyektivkaForm.mehnat_yil)
    await callback.message.edit_text("Keyingi davr - yillarini kiriting.\n\nMisol: 2002-2015")
    await callback.answer()


@router.callback_query(ObyektivkaForm.mehnat_menu, F.data == "mehnat_done")
async def mehnat_done(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ObyektivkaForm.qar_munosabat)
    await callback.message.edit_text(
        "👨‍👩‍👧‍👦 <b>Yaqin qarindoshlari</b>\n\n"
        "Ota, ona, aka/opa, turmush o'rtog'i, farzandlar haqida ma'lumot kiritamiz.\n\n"
        "Birinchi qarindoshning qarindoshligini kiriting.\n\nMisol: Otasi",
        parse_mode="HTML",
    )
    await callback.answer()


# ─────────────────────────────────────────────────────────
# Qarindoshlar
# ─────────────────────────────────────────────────────────

@router.message(ObyektivkaForm.qar_munosabat)
async def qar_munosabat_handler(message: Message, state: FSMContext):
    await state.update_data(_qar_munosabat=message.text.strip())
    await state.set_state(ObyektivkaForm.qar_fio)
    await message.answer("To'liq ismini kiriting (FIO).\n\nMisol: Aliyev Rustam Karimovich")


@router.message(ObyektivkaForm.qar_fio)
async def qar_fio_handler(message: Message, state: FSMContext):
    await state.update_data(_qar_fio=message.text.strip())
    await state.set_state(ObyektivkaForm.qar_tug)
    await message.answer("Tug'ilgan yili va joyini kiriting.\n\nMisol: 1965-yil Surxondaryo viloyati Termiz shahri")


@router.message(ObyektivkaForm.qar_tug)
async def qar_tug_handler(message: Message, state: FSMContext):
    await state.update_data(_qar_tug=message.text.strip())
    await state.set_state(ObyektivkaForm.qar_ish)
    await message.answer("Ish joyi va lavozimini kiriting.\n\nMisol: Haydovchi / Vafot etgan")


@router.message(ObyektivkaForm.qar_ish)
async def qar_ish_handler(message: Message, state: FSMContext):
    await state.update_data(_qar_ish=message.text.strip())
    await state.set_state(ObyektivkaForm.qar_turar)
    await message.answer("Turar joyini kiriting.\n\nMisol: Termiz shahri Manguzar mahallasi Shifokor ko'chasi 34-uy")


@router.message(ObyektivkaForm.qar_turar)
async def qar_turar_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    qarindoshlar = data["qarindoshlar"]
    qarindoshlar.append({
        "munosabat": data["_qar_munosabat"],
        "fio": data["_qar_fio"],
        "tug": data["_qar_tug"],
        "ish": data["_qar_ish"],
        "turar": message.text.strip(),
    })
    await state.update_data(qarindoshlar=qarindoshlar)
    await state.set_state(ObyektivkaForm.qar_menu)

    summary = "\n".join(f"• {q['munosabat']}: {q['fio']}" for q in qarindoshlar)
    await message.answer(
        f"✅ Qo'shildi!\n\n<b>Hozirgi ro'yxat:</b>\n{summary}\n\nYana qo'shamizmi?",
        parse_mode="HTML",
        reply_markup=kb_qar_menu(),
    )


@router.callback_query(ObyektivkaForm.qar_menu, F.data == "qar_add")
async def qar_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ObyektivkaForm.qar_munosabat)
    await callback.message.edit_text("Keyingi qarindoshning qarindoshligini kiriting.\n\nMisol: Onasi")
    await callback.answer()


@router.callback_query(ObyektivkaForm.qar_menu, F.data == "qar_done")
async def qar_done(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ObyektivkaForm.waiting_photo)
    await callback.message.edit_text(
        "📸 Endi 3x4 rasmingizni yuboring — hujjatga joylashtiramiz.\n\n"
        "Rasmsiz davom etish ham mumkin (o'sha joy bo'sh qoladi).",
        reply_markup=kb_skip_photo(),
    )
    await callback.answer()


@router.message(ObyektivkaForm.waiting_photo, F.photo)
async def obyektivka_photo_received(message: Message, state: FSMContext, bot: Bot):
    # eng katta o'lchamdagi versiyasini olamiz
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)

    tmp_dir = tempfile.mkdtemp()
    photo_path = os.path.join(tmp_dir, "photo.jpg")
    await bot.download_file(file.file_path, destination=photo_path)
    fix_photo_orientation(photo_path)

    await state.update_data(photo_path=photo_path)
    await state.set_state(ObyektivkaForm.choosing_format)
    await message.answer(
        "✅ Rasm qabul qilindi!\n\nQaysi formatda olmoqchisiz?",
        reply_markup=kb_format(),
    )


@router.callback_query(ObyektivkaForm.waiting_photo, F.data == "photo_skip")
async def obyektivka_photo_skip(callback: CallbackQuery, state: FSMContext):
    await state.update_data(photo_path=None)
    await state.set_state(ObyektivkaForm.choosing_format)
    await callback.message.edit_text(
        "Qaysi formatda olmoqchisiz?",
        reply_markup=kb_format(),
    )
    await callback.answer()


# ─────────────────────────────────────────────────────────
# Format tanlash + hujjat generatsiyasi
# ─────────────────────────────────────────────────────────

@router.callback_query(ObyektivkaForm.choosing_format, F.data.startswith("fmt_"))
async def choose_format(callback: CallbackQuery, state: FSMContext, bot: Bot):
    fmt = callback.data.split("_")[1]  # word | pdf
    await state.update_data(fmt=fmt)
    await callback.message.edit_text("⚙️ Hujjat tayyorlanmoqda, biroz kuting...")

    data = await state.get_data()
    answers = data["answers"]
    doc_data = {
        "turi": data["turi"],
        "fio": answers.get("fio", ""),
        "sarlavha_yil": answers.get("sarlavha_yil", ""),
        "tashkilot": answers.get("tashkilot", ""),
        "tug_yil": answers.get("tug_yil", ""),
        "tug_joy": answers.get("tug_joy", ""),
        "millat": answers.get("millat", ""),
        "partiya": answers.get("partiya", ""),
        "malumot": answers.get("malumot", ""),
        "tamomlagan": answers.get("tamomlagan", ""),
        "mutaxassislik": answers.get("mutaxassislik", ""),
        "ilmiy_daraja": answers.get("ilmiy_daraja", ""),
        "ilmiy_unvon": answers.get("ilmiy_unvon", ""),
        "chet_til": answers.get("chet_til", ""),
        "harbiy": answers.get("harbiy", ""),
        "mukofot": answers.get("mukofot", ""),
        "deputat": answers.get("deputat", ""),
        "tel": answers.get("tel", ""),
        "pasport": answers.get("pasport", ""),
        "jshshir": answers.get("jshshir", "") if answers.get("jshshir") != "-" else "",
        "mehnat": data["mehnat"],
        "qarindoshlar": data["qarindoshlar"],
    }

    photo_path = data.get("photo_path")
    safe_name = doc_data["fio"].replace(" ", "_") or "obyektivka"

    if fmt == "pdf":
        pdf_buf = build_obyektivka_pdf(doc_data, photo_path=photo_path)
        await bot.send_document(
            callback.from_user.id,
            BufferedInputFile(pdf_buf.read(), filename=f"{safe_name}.pdf"),
            caption="✅ Obyektivkangiz tayyor!",
        )
    else:
        docx_buf = build_obyektivka_docx(doc_data, photo_path=photo_path)
        await bot.send_document(
            callback.from_user.id,
            BufferedInputFile(docx_buf.read(), filename=f"{safe_name}.docx"),
            caption="✅ Obyektivkangiz tayyor!",
        )

    if photo_path and os.path.exists(photo_path):
        try:
            os.remove(photo_path)
            os.rmdir(os.path.dirname(photo_path))
        except OSError:
            pass

    await callback.message.answer("Yana nima kerak?", reply_markup=kb_home())
    await state.clear()
    await callback.answer()


# ─────────────────────────────────────────────────────────
# Ishga tushirish
# ─────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────
# Mini App backend
# ─────────────────────────────────────────────────────────

WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")


def validate_init_data(init_data: str, bot_token: str):
    """Telegram WebApp initData imzosini tekshiradi.
    To'g'ri bo'lsa foydalanuvchi ma'lumotlarini (dict) qaytaradi, aks holda None.
    """
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    user_raw = parsed.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None
    return user


async def health(request):
    return web.Response(text="Madesy ishlayapti ✅")


async def serve_webapp_index(request):
    index_path = os.path.join(WEBAPP_DIR, "index.html")
    return web.FileResponse(index_path)


async def api_create_order(request):
    """Obyektivka: forma ma'lumotlarini qabul qilib, balansdan narxini
    yechadi va hujjatni DARHOL yaratib, chatga yuboradi (P2P/admin
    tasdiqlash shart emas - to'lov oldindan balans orqali qilingan)."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    init_data = payload.get("initData", "")
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    chat_id = user.get("id")
    if not check_rate_limit(chat_id):
        return web.json_response(
            {"ok": False, "error": "Juda ko'p so'rov yubordingiz. Iltimos, biroz kuting."},
            status=429,
        )
    turi = payload.get("turi")
    answers = payload.get("answers", {})
    mehnat = payload.get("mehnat", [])
    qarindoshlar = payload.get("qarindoshlar", [])
    fmt = payload.get("format", "word")
    photo_b64 = payload.get("photo_base64")

    doc_data = {
        "turi": turi,
        "fio": clean_text_field(answers.get("fio", "")),
        "sarlavha_yil": "2026-yil:",
        "tashkilot": clean_text_field(answers.get("tashkilot", "")),
        "tug_yil": clean_text_field(answers.get("tug_yil", ""), 20),
        "tug_joy": clean_text_field(answers.get("tug_joy", "")),
        "millat": clean_text_field(answers.get("millat", ""), 50),
        "partiya": clean_text_field(answers.get("partiya", "")),
        "malumot": clean_text_field(answers.get("malumot", "")),
        "tamomlagan": clean_text_field(answers.get("tamomlagan", "")),
        "mutaxassislik": clean_text_field(answers.get("mutaxassislik", "")),
        "ilmiy_daraja": clean_text_field(answers.get("ilmiy_daraja", "")),
        "ilmiy_unvon": clean_text_field(answers.get("ilmiy_unvon", "")),
        "chet_til": clean_text_field(answers.get("chet_til", ""), 100),
        "harbiy": clean_text_field(answers.get("harbiy", "")),
        "mukofot": clean_text_field(answers.get("mukofot", "")),
        "deputat": clean_text_field(answers.get("deputat", "")),
        "tel": clean_text_field(answers.get("tel", ""), 30),
        "pasport": clean_text_field(answers.get("pasport", ""), 30),
        "jshshir": clean_text_field(answers.get("jshshir", ""), 20) if answers.get("jshshir") != "-" else "",
        "mehnat": mehnat,
        "qarindoshlar": qarindoshlar,
    }

    photo_bytes = None
    if photo_b64:
        try:
            header, encoded = photo_b64.split(",", 1)
            photo_bytes = base64.b64decode(encoded)
        except Exception:
            photo_bytes = None

    cost = PRICE_OBYEKTIVKA
    if await get_balance(chat_id) < cost:
        return web.json_response({
            "ok": False,
            "error": "Balansingizda yetarli mablag' yo'q",
            "insufficient_balance": True,
            "balance": await get_balance(chat_id),
            "required": cost,
        }, status=402)

    await deduct_balance(chat_id, cost)

    temp_order = {"doc_data": doc_data, "photo_bytes": photo_bytes, "fmt": fmt}
    bot: Bot = request.app["bot"]
    try:
        async with _generation_semaphore:
            result_bytes, filename = await asyncio.to_thread(_build_obyektivka_result, temp_order)
    except Exception:
        await add_balance(chat_id, cost)  # xato - balansni qaytaramiz
        logger.exception("Obyektivka generatsiyasida xato")
        return web.json_response(
            {"ok": False, "error": "Hujjat yaratishda xatolik. Balansingiz qaytarildi."},
            status=500,
        )

    try:
        await bot.send_document(
            chat_id,
            BufferedInputFile(result_bytes, filename=filename),
            caption="✅ Hujjatingiz tayyor!",
        )
    except Exception:
        logger.exception("Mijozga faylni yuborishda xato")

    return web.json_response({"ok": True, "balance": await get_balance(chat_id)})


async def api_create_slide_order(request):
    """Slayd: mavzu/soni/shablonni qabul qilib, balansdan narxini yechadi
    va taqdimotni DARHOL yaratib, chatga yuboradi."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    init_data = payload.get("initData", "")
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    chat_id = user.get("id")
    if not check_rate_limit(chat_id):
        return web.json_response(
            {"ok": False, "error": "Juda ko'p so'rov yubordingiz. Iltimos, biroz kuting."},
            status=429,
        )
    topic = clean_text_field(payload.get("topic") or "")
    template = payload.get("template", "blue")
    try:
        num_slides = int(payload.get("num_slides", 0))
    except (TypeError, ValueError):
        num_slides = 0

    if not topic:
        return web.json_response({"ok": False, "error": "Mavzu kiritilmagan"}, status=400)
    if not (slide_gen.MIN_SLIDES <= num_slides <= slide_gen.MAX_SLIDES):
        return web.json_response(
            {"ok": False, "error": f"Slaydlar soni {slide_gen.MIN_SLIDES}-{slide_gen.MAX_SLIDES} oralig'ida bo'lishi kerak"},
            status=400,
        )
    if template not in slide_gen.TEMPLATES:
        template = "blue"

    cost = num_slides * slide_gen.PRICE_PER_SLIDE
    if await get_balance(chat_id) < cost:
        return web.json_response({
            "ok": False,
            "error": "Balansingizda yetarli mablag' yo'q",
            "insufficient_balance": True,
            "balance": await get_balance(chat_id),
            "required": cost,
        }, status=402)

    await deduct_balance(chat_id, cost)

    temp_order = {"topic": topic, "num_slides": num_slides, "template": template}
    bot: Bot = request.app["bot"]
    try:
        async with _generation_semaphore:
            result_bytes, filename = await asyncio.to_thread(_build_slide_result, temp_order)
    except Exception:
        await add_balance(chat_id, cost)
        logger.exception("Slayd generatsiyasida xato")
        return web.json_response(
            {"ok": False, "error": "Taqdimot yaratishda xatolik. Balansingiz qaytarildi."},
            status=500,
        )

    try:
        await bot.send_document(
            chat_id,
            BufferedInputFile(result_bytes, filename=filename),
            caption="✅ Taqdimotingiz tayyor!",
        )
        if temp_order.get("extra_result_bytes"):
            await bot.send_document(
                chat_id,
                BufferedInputFile(temp_order["extra_result_bytes"], filename=temp_order["extra_result_filename"]),
                caption="📎 Rasmsiz variant (bir xil mazmun, rasmlarsiz)",
            )
    except Exception:
        logger.exception("Mijozga faylni yuborishda xato")

    return web.json_response({"ok": True, "balance": await get_balance(chat_id)})


async def api_create_referat_order(request):
    """Referat/Mustaqil ish: mavzu/turi/bet soni/talaba ma'lumotlarini
    qabul qilib, balansdan narxini yechadi va hujjatni DARHOL yaratib,
    chatga yuboradi."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    init_data = payload.get("initData", "")
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    chat_id = user.get("id")
    if not check_rate_limit(chat_id):
        return web.json_response(
            {"ok": False, "error": "Juda ko'p so'rov yubordingiz. Iltimos, biroz kuting."},
            status=429,
        )
    topic = clean_text_field(payload.get("topic") or "")
    ish_turi = payload.get("ish_turi", "referat")
    fio = clean_text_field(payload.get("fio") or "")
    muassasa = clean_text_field(payload.get("muassasa") or "")
    fakultet = clean_text_field(payload.get("fakultet") or "")
    guruh = clean_text_field(payload.get("guruh") or "", 60)
    try:
        pages = int(payload.get("pages", 0))
    except (TypeError, ValueError):
        pages = 0

    if not topic:
        return web.json_response({"ok": False, "error": "Mavzu kiritilmagan"}, status=400)
    if ish_turi not in referat_gen.ISH_TURLARI:
        ish_turi = "referat"
    if not (referat_gen.MIN_PAGES <= pages <= referat_gen.MAX_PAGES):
        return web.json_response(
            {"ok": False, "error": f"Bet soni {referat_gen.MIN_PAGES}-{referat_gen.MAX_PAGES} oralig'ida bo'lishi kerak"},
            status=400,
        )

    cost = pages * referat_gen.PRICE_PER_PAGE
    if await get_balance(chat_id) < cost:
        return web.json_response({
            "ok": False,
            "error": "Balansingizda yetarli mablag' yo'q",
            "insufficient_balance": True,
            "balance": await get_balance(chat_id),
            "required": cost,
        }, status=402)

    await deduct_balance(chat_id, cost)

    temp_order = {
        "topic": topic, "ish_turi": ish_turi, "pages": pages,
        "fio": fio, "muassasa": muassasa, "fakultet": fakultet, "guruh": guruh,
    }
    bot: Bot = request.app["bot"]
    try:
        async with _generation_semaphore:
            result_bytes, filename = await asyncio.to_thread(_build_referat_result, temp_order)
    except Exception:
        await add_balance(chat_id, cost)
        logger.exception("Referat generatsiyasida xato")
        return web.json_response(
            {"ok": False, "error": "Hujjat yaratishda xatolik. Balansingiz qaytarildi."},
            status=500,
        )

    try:
        await bot.send_document(
            chat_id,
            BufferedInputFile(result_bytes, filename=filename),
            caption="✅ Hujjatingiz tayyor!",
        )
    except Exception:
        logger.exception("Mijozga faylni yuborishda xato")

    return web.json_response({"ok": True, "balance": await get_balance(chat_id)})


async def api_create_test_order(request):
    """Test tayyorlash: mavzu/savollar soni/formatni qabul qilib, DARHOL
    (bepul, to'lovsiz) testni yaratib, chatga yuboradi."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    init_data = payload.get("initData", "")
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    chat_id = user.get("id")
    if not check_rate_limit(chat_id):
        return web.json_response(
            {"ok": False, "error": "Juda ko'p so'rov yubordingiz. Iltimos, biroz kuting."},
            status=429,
        )
    topic = clean_text_field(payload.get("topic") or "")
    fmt = payload.get("format", "word")
    try:
        num_questions = int(payload.get("num_questions", 0))
    except (TypeError, ValueError):
        num_questions = 0

    if not topic:
        return web.json_response({"ok": False, "error": "Mavzu kiritilmagan"}, status=400)
    if not (test_gen.MIN_QUESTIONS <= num_questions <= test_gen.MAX_QUESTIONS):
        return web.json_response(
            {"ok": False, "error": f"Savollar soni {test_gen.MIN_QUESTIONS}-{test_gen.MAX_QUESTIONS} oralig'ida bo'lishi kerak"},
            status=400,
        )
    if fmt not in ("word", "pdf"):
        fmt = "word"

    temp_order = {"topic": topic, "num_questions": num_questions, "fmt": fmt}
    bot: Bot = request.app["bot"]
    try:
        async with _generation_semaphore:
            result_bytes, filename = await asyncio.to_thread(_build_test_result, temp_order)
    except Exception:
        logger.exception("Test generatsiyasida xato")
        return web.json_response(
            {"ok": False, "error": "Test yaratishda xatolik yuz berdi. Qaytadan urinib ko'ring."},
            status=500,
        )

    try:
        await bot.send_document(
            chat_id,
            BufferedInputFile(result_bytes, filename=filename),
            caption="✅ Testingiz tayyor!",
        )
    except Exception:
        logger.exception("Mijozga faylni yuborishda xato")

    return web.json_response({"ok": True})


async def api_get_balance(request):
    """Foydalanuvchining joriy balansini qaytaradi."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    user = validate_init_data(payload.get("initData", ""), BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    return web.json_response({"ok": True, "balance": await get_balance(user.get("id"))})


async def api_create_topup_order(request):
    """Balansni to'ldirish uchun buyurtma yaratadi - P2P to'lov ko'rsatmalarini
    qaytaradi, admin chekni tasdiqlagach balans avtomatik oshadi."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    user = validate_init_data(payload.get("initData", ""), BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    chat_id = user.get("id")
    if not check_rate_limit(chat_id):
        return web.json_response(
            {"ok": False, "error": "Juda ko'p so'rov yubordingiz. Iltimos, biroz kuting."},
            status=429,
        )

    try:
        amount = int(payload.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0

    if not (MIN_TOPUP <= amount <= MAX_TOPUP):
        return web.json_response(
            {"ok": False, "error": f"Summa {MIN_TOPUP}-{MAX_TOPUP} so'm oralig'ida bo'lishi kerak"},
            status=400,
        )

    order_id = uuid.uuid4().hex[:12]
    orders[order_id] = {
        "service": "topup",
        "chat_id": chat_id,
        "amount": amount,
        "status": "to'lov_kutilmoqda",
        "receipt_file_id": None,
        "created_at": time.time(),
    }

    return web.json_response({
        "ok": True,
        "order_id": order_id,
        "amount": amount,
        "card_number": CARD_NUMBER,
        "card_holder": CARD_HOLDER,
    })


async def api_redeem_promo(request):
    """Mini App'dagi 'Hisobni to'ldirish' oynasida promokod kiritilganda
    chaqiriladi - balansni darhol (admin tasdig'isiz) oshiradi."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    user = validate_init_data(payload.get("initData", ""), BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    chat_id = user.get("id")
    if not check_rate_limit(chat_id):
        return web.json_response(
            {"ok": False, "error": "Juda ko'p so'rov yubordingiz. Iltimos, biroz kuting."},
            status=429,
        )

    code = str(payload.get("code", ""))
    result = await apply_promo_code(chat_id, code)
    if not result["ok"]:
        return web.json_response({"ok": False, "error": result["message"]}, status=400)

    return web.json_response({
        "ok": True,
        "message": result["message"],
        "balance": result["balance"],
        "amount": result["amount"],
    })


async def api_create_photo3x4_order(request):
    """3x4 rasm uchun 1-qadam: foydalanuvchi rasmini qabul qilib, buyurtma
    yaratadi va to'lov ko'rsatmalarini qaytaradi (hali rasm qayta ishlanmaydi -
    bu admin tasdiqlagach sodir bo'ladi)."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    init_data = payload.get("initData", "")
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    chat_id = user.get("id")
    if not check_rate_limit(chat_id):
        return web.json_response(
            {"ok": False, "error": "Juda ko'p so'rov yubordingiz. Iltimos, biroz kuting."},
            status=429,
        )

    photo_b64 = payload.get("photo_base64")
    if not photo_b64:
        return web.json_response({"ok": False, "error": "Rasm tanlanmagan"}, status=400)

    try:
        header, encoded = photo_b64.split(",", 1)
    except Exception:
        return web.json_response({"ok": False, "error": "Rasmni o'qib bo'lmadi"}, status=400)

    if not header.startswith("data:image/"):
        return web.json_response({"ok": False, "error": "Faqat rasm fayli qabul qilinadi"}, status=400)

    try:
        photo_bytes = base64.b64decode(encoded)
    except Exception:
        return web.json_response({"ok": False, "error": "Rasmni o'qib bo'lmadi"}, status=400)

    if len(photo_bytes) > MAX_RECEIPT_PHOTO_BYTES:
        return web.json_response({"ok": False, "error": "Rasm hajmi juda katta (5 MB dan oshmasin)"}, status=400)

    # Piksel o'lchamini tekshirish - bu PIL header'ni o'qiydi, to'liq dekodlamaydi,
    # shuning uchun arzon (tez, xotira sarflamaydigan) tekshiruv
    try:
        with Image.open(io.BytesIO(photo_bytes)) as probe_img:
            width, height = probe_img.size
    except Exception:
        return web.json_response({"ok": False, "error": "Rasm formatini aniqlab bo'lmadi"}, status=400)

    megapixels = (width * height) / 1_000_000
    if megapixels > MAX_PHOTO_MEGAPIXELS:
        return web.json_response(
            {"ok": False, "error": f"Rasm o'lchami juda katta ({width}x{height}). Iltimos, kichikroq rasm yuboring."},
            status=400,
        )

    # 3x4 xizmati bepul - to'lov/chek bosqichisiz darhol tayyorlab, botga yuboramiz
    bot: Bot = request.app["bot"]
    try:
        async with _photo3x4_semaphore:
            result_bytes = await asyncio.to_thread(photo_3x4.process_3x4, photo_bytes)
    except Exception as e:
        logger.error(f"3x4 rasm xatosi: {e}")
        return web.json_response(
            {"ok": False, "error": "Rasmni qayta ishlashda xatolik yuz berdi. Boshqa rasm bilan urinib ko'ring."},
            status=500,
        )

    await bot.send_document(
        chat_id,
        BufferedInputFile(result_bytes, filename="3x4_rasm.jpg"),
        caption="✅ 3×4 rasmingiz tayyor!",
    )

    return web.json_response({"ok": True, "free": True})


async def api_submit_receipt(request):
    """2-qadam: foydalanuvchi chek rasmini va 'To'ladim' bosganini yuboradi -
    admin'ga tasdiqlash uchun jo'natiladi."""
    bot: Bot = request.app["bot"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    init_data = payload.get("initData", "")
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    order_id = payload.get("order_id")
    receipt_b64 = payload.get("receipt_base64")
    order = orders.get(order_id)
    if not order or order["chat_id"] != user.get("id"):
        return web.json_response({"ok": False, "error": "Buyurtma topilmadi"}, status=404)
    if order["status"] != "to'lov_kutilmoqda":
        return web.json_response({"ok": False, "error": "Bu buyurtma allaqachon yuborilgan"}, status=400)
    if not receipt_b64:
        return web.json_response({"ok": False, "error": "Chek rasmi topilmadi"}, status=400)

    try:
        header, encoded = receipt_b64.split(",", 1)
    except Exception:
        return web.json_response({"ok": False, "error": "Chek rasmini o'qib bo'lmadi"}, status=400)

    if not header.startswith("data:image/"):
        return web.json_response({"ok": False, "error": "Faqat rasm fayli qabul qilinadi"}, status=400)

    try:
        receipt_bytes = base64.b64decode(encoded)
    except Exception:
        return web.json_response({"ok": False, "error": "Chek rasmini o'qib bo'lmadi"}, status=400)

    if len(receipt_bytes) > MAX_RECEIPT_PHOTO_BYTES:
        return web.json_response({"ok": False, "error": "Rasm hajmi juda katta (5 MB dan oshmasin)"}, status=400)

    order["status"] = "tekshirilmoqda"

    service = order.get("service", "topup")
    if service == "topup":
        details = (
            f"Xizmat: Balans to'ldirish\n"
            f"Summa: {order['amount']} so'm\n"
            f"Joriy balans: {await get_balance(order['chat_id'])} so'm"
        )
    else:
        # Eski xizmatlar (obyektivka/slayd/referat/test) endi balans/bepul
        # orqali darhol ishlaydi va bu yerga umuman kelmaydi - bu shoxobcha
        # faqat kutilmagan/eski buyurtmalar uchun zaxira sifatida qoldirilgan.
        details = f"Xizmat: {service}\nBuyurtma ID: {order_id}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"order_ok:{order_id}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"order_no:{order_id}"),
    ]])

    try:
        msg = await bot.send_photo(
            ADMIN_CHAT_ID,
            BufferedInputFile(receipt_bytes, filename="chek.jpg"),
            caption=f"💳 <b>Yangi to'lov</b>\n\n{details}\nBuyurtma ID: <code>{order_id}</code>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        order["admin_msg_id"] = msg.message_id
    except Exception:
        logger.exception("Admin'ga chek yuborishda xato")
        return web.json_response({"ok": False, "error": "Adminga yuborishda xato"}, status=500)

    return web.json_response({"ok": True})


async def api_order_status(request):
    """3-qadam: Mini App shu endpoint orqali holatni tekshirib turadi (polling)."""
    order_id = request.query.get("order_id", "")
    order = orders.get(order_id)
    if not order:
        return web.json_response({"ok": False, "error": "Buyurtma topilmadi"}, status=404)
    return web.json_response({"ok": True, "status": order["status"]})



def _build_obyektivka_result(order):
    """Obyektivka hujjatini yasaydi. (result_bytes, result_filename) qaytaradi."""
    doc_data = order["doc_data"]
    fmt = order["fmt"]
    safe_name = (doc_data["fio"] or "obyektivka").replace(" ", "_")

    with tempfile.TemporaryDirectory() as tmp:
        photo_path = None
        if order["photo_bytes"]:
            photo_path = os.path.join(tmp, "photo.jpg")
            with open(photo_path, "wb") as f:
                f.write(order["photo_bytes"])
            fix_photo_orientation(photo_path)

        if fmt == "pdf":
            buf = build_obyektivka_pdf(doc_data, photo_path=photo_path)
            filename = f"{safe_name}.pdf"
        else:
            buf = build_obyektivka_docx(doc_data, photo_path=photo_path)
            filename = f"{safe_name}.docx"
        return buf.read(), filename


def _build_slide_result(order):
    """Slayd taqdimotini yasaydi (Gemini + Pexels + python-pptx) - ikkala
    variantda (rasmli va rasmsiz), matn/rasmlarni faqat bir marta
    generatsiya qilib. Tarmoq so'rovlari borligi uchun bu funksiya
    asyncio.to_thread orqali chaqiriladi."""
    with_images, without_images = slide_gen.build_slide_deck_dual(
        order["topic"], order["num_slides"], order["template"]
    )
    safe_name = order["topic"].replace(" ", "_")[:40] or "taqdimot"
    # Ikkinchi (rasmsiz) faylni buyurtma ob'ektiga saqlab qo'yamiz -
    # order_approve uni asosiy fayldan keyin alohida yuboradi
    order["extra_result_bytes"] = without_images
    order["extra_result_filename"] = f"{safe_name}_rasmsiz.pptx"
    return with_images, f"{safe_name}_rasmli.pptx"


def _build_referat_result(order):
    """Referat/Mustaqil ish hujjatini yasaydi (Gemini + python-docx).
    Tarmoq so'rovi borligi uchun bu funksiya asyncio.to_thread orqali
    chaqiriladi."""
    data = referat_gen.build_referat_docx(
        order["topic"],
        order["ish_turi"],
        order["pages"],
        order.get("fio", ""),
        order.get("muassasa", ""),
        order.get("fakultet", ""),
        order.get("guruh", ""),
    )
    safe_name = order["topic"].replace(" ", "_")[:40] or "referat"
    filename = f"{safe_name}.docx"
    return data.read(), filename


def _build_test_result(order):
    """Test hujjatini yasaydi (Gemini + python-docx/reportlab).
    Tarmoq so'rovi borligi uchun bu funksiya asyncio.to_thread orqali
    chaqiriladi."""
    questions = test_gen.generate_test_questions(order["topic"], order["num_questions"])
    safe_name = order["topic"].replace(" ", "_")[:40] or "test"
    if order["fmt"] == "pdf":
        buf = test_gen.build_test_pdf(order["topic"], questions)
        filename = f"{safe_name}.pdf"
    else:
        buf = test_gen.build_test_docx(order["topic"], questions)
        filename = f"{safe_name}.docx"
    return buf.read(), filename


# Xavfsizlik: bir vaqtning o'zida cheksiz ko'p og'ir generatsiya jarayoni
# (Gemini/Pexels so'rovi + hujjat yig'ish) ishga tushib, serverning cheklangan
# xotirasini (Render bepul tarifida 512 MB) to'ldirib, uni yiqitib qo'ymasligi
# uchun bir vaqtda maksimal shuncha jarayon ishlashiga ruxsat beramiz - qolganlari
# navbatda avtomatik kutadi (xato bermaydi, faqat biroz sekinroq bo'ladi).
MAX_CONCURRENT_GENERATIONS = 3
_generation_semaphore = asyncio.Semaphore(MAX_CONCURRENT_GENERATIONS)

# 3x4 rasm qayta ishlash boshqa xizmatlarga (Slayd/Referat/Test) qaraganda
# ancha ko'proq xotira talab qiladi (onnxruntime + segmentatsiya modeli).
# Shu sabab bu xizmat uchun alohida, faqat 1 ta bir vaqtdagi jarayonga
# ruxsat beruvchi navbat o'rnatamiz - bir vaqtning o'zida 2+ ta og'ir
# rasm jarayoni serverning 512 MB xotira chegarasini oshirib yubormasligi
# uchun.
_photo3x4_semaphore = asyncio.Semaphore(1)


@router.callback_query(F.data.startswith("order_ok:"))
async def order_approve(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return

    order_id = callback.data.split(":", 1)[1]
    order = orders.get(order_id)
    if not order:
        await callback.answer("Buyurtma topilmadi", show_alert=True)
        return

    # Himoya: "Tasdiqlash" tugmasi bir necha marta (masalan sekin internetda
    # ikki marta) bosilib ketsa, bir xil buyurtma uchun QAYTA generatsiya
    # boshlanib, mijozga bir nechta fayl yuborilib ketmasligi uchun.
    if order.get("status") in ("ishlanmoqda", "tayyor"):
        await callback.answer("Bu buyurtma allaqachon qayta ishlangan/ishlanmoqda", show_alert=True)
        return
    order["status"] = "ishlanmoqda"
    await callback.answer("Tasdiqlanmoqda...")
    service = order.get("service", "obyektivka")

    # Balans to'ldirish - fayl generatsiya qilinmaydi, shunchaki mijoz
    # balansiga summa qo'shiladi va unga xabar yuboriladi.
    if service == "topup":
        await add_balance(order["chat_id"], order["amount"])
        new_balance = await get_balance(order["chat_id"])
        order["status"] = "tayyor"
        try:
            await bot.send_message(
                order["chat_id"],
                f"✅ Balansingiz {order['amount']} so'mga to'ldirildi!\n💰 Joriy balans: {new_balance} so'm",
            )
        except TelegramForbiddenError:
            await db_mark_user_blocked(order["chat_id"])
        except Exception:
            logger.exception("Mijozga balans xabarini yuborishda xato")
        await callback.message.edit_caption(
            caption=(callback.message.caption or "") + f"\n\n✅ Tasdiqlandi. Yangi balans: {new_balance} so'm",
        )
        return

    if _generation_semaphore.locked():
        try:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + "\n\n⏳ Navbatda (server band, biroz kuting)...",
            )
        except Exception:
            pass

    try:
        async with _generation_semaphore:
            if service == "slayd":
                result_bytes, filename = await asyncio.to_thread(_build_slide_result, order)
            elif service == "referat":
                result_bytes, filename = await asyncio.to_thread(_build_referat_result, order)
            elif service == "test":
                result_bytes, filename = await asyncio.to_thread(_build_test_result, order)
            else:
                result_bytes, filename = await asyncio.to_thread(_build_obyektivka_result, order)
        order["result_bytes"] = result_bytes
        order["result_filename"] = filename
        order["status"] = "tayyor"
    except Exception as e:
        logger.exception("Tasdiqlangandan keyin natija yasashda xato")
        order["status"] = "xato"
        await callback.message.edit_caption(
            caption=(callback.message.caption or "") + f"\n\n❌ Xato: {str(e)[:200]}",
        )
        return

    # Mijoz Mini App'da qolgan-qolmaganidan qat'iy nazar, faylni to'g'ridan-to'g'ri
    # uning chatiga ham yuboramiz - shunda hech qanday holatda yo'qolib qolmaydi.
    try:
        caption = "✅ To'lovingiz tasdiqlandi! " + (
            "Taqdimotingiz tayyor." if service == "slayd" else "Hujjatingiz tayyor."
        )
        await bot.send_document(
            order["chat_id"],
            BufferedInputFile(order["result_bytes"], filename=order["result_filename"]),
            caption=caption,
        )
        # Slayd xizmatida ikkinchi (rasmsiz) variantni ham alohida yuboramiz
        if service == "slayd" and order.get("extra_result_bytes"):
            await bot.send_document(
                order["chat_id"],
                BufferedInputFile(order["extra_result_bytes"], filename=order["extra_result_filename"]),
                caption="📎 Rasmsiz variant (bir xil mazmun, rasmlarsiz)",
            )
    except TelegramForbiddenError:
        await db_mark_user_blocked(order["chat_id"])
    except Exception:
        logger.exception("Mijozga tayyor faylni chatga yuborishda xato")

    await callback.message.edit_caption(
        caption=(callback.message.caption or "") + "\n\n✅ Tasdiqlandi, mijozga chatga yuborildi.",
    )


@router.callback_query(F.data.startswith("order_no:"))
async def order_reject(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return

    order_id = callback.data.split(":", 1)[1]
    order = orders.get(order_id)
    if order and order.get("status") not in ("rad_etildi", "ishlanmoqda", "tayyor"):
        order["status"] = "rad_etildi"
        try:
            await bot.send_message(
                order["chat_id"],
                "❌ To'lovingiz rad etildi. Siz to'lov qilmagansiz. Qaytadan urinib ko'ring.",
            )
        except TelegramForbiddenError:
            await db_mark_user_blocked(order["chat_id"])
        except Exception:
            logger.exception("Mijozga rad etish xabarini yuborishda xato")

    await callback.answer("Rad etildi")
    await callback.message.edit_caption(
        caption=(callback.message.caption or "") + "\n\n❌ Rad etildi.",
    )


async def start_web_server(bot: Bot):
    """Render 'Web Service' bepul rejasi uchun - portni tinglab turadi.
    Shuningdek Mini App statik fayllarini va API'ni ham shu orqali beradi.
    """
    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20MB - selfie/rasm yuklashlar uchun
    app["bot"] = bot
    app.router.add_get("/", health)
    app.router.add_get("/webapp/", serve_webapp_index)
    app.router.add_post("/api/create_order", api_create_order)
    app.router.add_post("/api/create_slide_order", api_create_slide_order)
    app.router.add_post("/api/create_referat_order", api_create_referat_order)
    app.router.add_post("/api/create_test_order", api_create_test_order)
    app.router.add_post("/api/create_photo3x4_order", api_create_photo3x4_order)
    app.router.add_post("/api/get_balance", api_get_balance)
    app.router.add_post("/api/create_topup_order", api_create_topup_order)
    app.router.add_post("/api/redeem_promo", api_redeem_promo)
    app.router.add_post("/api/submit_receipt", api_submit_receipt)
    app.router.add_get("/api/order_status", api_order_status)
    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server {port}-portda ishga tushdi (Render uchun, Mini App bilan)")


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(UserTrackingMiddleware())
    dp.include_router(router)
    logger.info("Bot ishga tushdi...")

    # Doimiy "Menyu" tugmasi - foydalanuvchi har safar /start yozmasdan,
    # chatning pastki qismidagi tugma orqali Mini App'ni bir bosishda ochadi
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Botni ochish", web_app=WebAppInfo(url=WEBAPP_URL))
        )
        logger.info("Menyu tugmasi (Mini App) o'rnatildi")
    except Exception:
        logger.exception("Menyu tugmasini o'rnatishda xato")

    asyncio.create_task(cleanup_expired_orders_loop())
    await start_web_server(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
