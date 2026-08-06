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
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
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
    WebAppInfo,
)
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
PRICE_OBYEKTIVKA = int(os.getenv("PRICE_OBYEKTIVKA", "9000"))

# Buyurtmalar shu yerda xotirada saqlanadi (server qayta ishga tushsa
# tozalanadi - hozircha kichik hajmda ishlatilgani uchun yetarli)
orders = {}

# ─────────────────────────────────────────────────────────
# Xavfsizlik: spam himoyasi, matn uzunligi chegarasi,
# eskirgan buyurtmalarni avtomatik tozalash
# ─────────────────────────────────────────────────────────
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
    """1-qadam: forma ma'lumotlarini qabul qilib, buyurtma yaratadi va
    to'lov ko'rsatmalarini qaytaradi (hali hujjat yaratilmaydi)."""
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

    order_id = uuid.uuid4().hex[:12]
    orders[order_id] = {
        "service": "obyektivka",
        "chat_id": chat_id,
        "doc_data": doc_data,
        "photo_bytes": photo_bytes,
        "fmt": fmt,
        "status": "to'lov_kutilmoqda",
        "receipt_file_id": None,
        "result_bytes": None,
        "result_filename": None,
        "created_at": time.time(),
    }

    return web.json_response({
        "ok": True,
        "order_id": order_id,
        "amount": PRICE_OBYEKTIVKA,
        "card_number": CARD_NUMBER,
        "card_holder": CARD_HOLDER,
    })


async def api_create_slide_order(request):
    """Slayd xizmati uchun 1-qadam: mavzu/soni/shablonni qabul qilib, buyurtma
    yaratadi va to'lov ko'rsatmalarini qaytaradi (hali slayd yaratilmaydi)."""
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

    amount = num_slides * slide_gen.PRICE_PER_SLIDE

    order_id = uuid.uuid4().hex[:12]
    orders[order_id] = {
        "service": "slayd",
        "chat_id": chat_id,
        "topic": topic,
        "num_slides": num_slides,
        "template": template,
        "status": "to'lov_kutilmoqda",
        "receipt_file_id": None,
        "result_bytes": None,
        "result_filename": None,
        "created_at": time.time(),
    }

    return web.json_response({
        "ok": True,
        "order_id": order_id,
        "amount": amount,
        "card_number": CARD_NUMBER,
        "card_holder": CARD_HOLDER,
    })


async def api_create_referat_order(request):
    """Referat/Mustaqil ish uchun 1-qadam: mavzu/turi/bet soni/talaba
    ma'lumotlarini qabul qilib, buyurtma yaratadi va to'lov ko'rsatmalarini
    qaytaradi (hali hujjat yaratilmaydi)."""
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

    amount = pages * referat_gen.PRICE_PER_PAGE

    order_id = uuid.uuid4().hex[:12]
    orders[order_id] = {
        "service": "referat",
        "chat_id": chat_id,
        "topic": topic,
        "ish_turi": ish_turi,
        "pages": pages,
        "fio": fio,
        "muassasa": muassasa,
        "fakultet": fakultet,
        "guruh": guruh,
        "status": "to'lov_kutilmoqda",
        "receipt_file_id": None,
        "result_bytes": None,
        "result_filename": None,
        "created_at": time.time(),
    }

    return web.json_response({
        "ok": True,
        "order_id": order_id,
        "amount": amount,
        "card_number": CARD_NUMBER,
        "card_holder": CARD_HOLDER,
    })


async def api_create_test_order(request):
    """Test tayyorlash uchun 1-qadam: mavzu/savollar soni/formatni qabul
    qilib, buyurtma yaratadi va to'lov ko'rsatmalarini qaytaradi (hali test
    yaratilmaydi)."""
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

    amount = num_questions * test_gen.PRICE_PER_QUESTION

    order_id = uuid.uuid4().hex[:12]
    orders[order_id] = {
        "service": "test",
        "chat_id": chat_id,
        "topic": topic,
        "num_questions": num_questions,
        "fmt": fmt,
        "status": "to'lov_kutilmoqda",
        "receipt_file_id": None,
        "result_bytes": None,
        "result_filename": None,
        "created_at": time.time(),
    }

    return web.json_response({
        "ok": True,
        "order_id": order_id,
        "amount": amount,
        "card_number": CARD_NUMBER,
        "card_holder": CARD_HOLDER,
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

    service = order.get("service", "obyektivka")
    if service == "slayd":
        amount = order["num_slides"] * slide_gen.PRICE_PER_SLIDE
        details = (
            f"Xizmat: Slayd tayyorlash\n"
            f"Mavzu: {order['topic']}\n"
            f"Slaydlar soni: {order['num_slides']}\n"
            f"Shablon: {slide_gen.TEMPLATES.get(order['template'], order['template'])}\n"
            f"Summa: {amount} so'm"
        )
    elif service == "referat":
        amount = order["pages"] * referat_gen.PRICE_PER_PAGE
        referat_fio = order.get("fio") or "Noma'lum"
        details = (
            f"Xizmat: {referat_gen.ISH_TURLARI.get(order['ish_turi'], 'Referat')}\n"
            f"Mavzu: {order['topic']}\n"
            f"Bet soni: {order['pages']}\n"
            f"F.I.O: {referat_fio}\n"
            f"Summa: {amount} so'm"
        )
    elif service == "test":
        amount = order["num_questions"] * test_gen.PRICE_PER_QUESTION
        details = (
            f"Xizmat: Test tayyorlash\n"
            f"Mavzu: {order['topic']}\n"
            f"Savollar soni: {order['num_questions']}\n"
            f"Format: {order['fmt']}\n"
            f"Summa: {amount} so'm"
        )
    else:
        fio = order["doc_data"].get("fio") or "Noma'lum"
        details = (
            f"Xizmat: Obyektivka\n"
            f"F.I.O: {fio}\n"
            f"Format: {order['fmt']}\n"
            f"Summa: {PRICE_OBYEKTIVKA} so'm"
        )

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
