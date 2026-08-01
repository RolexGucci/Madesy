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
import json
import logging
import os
import tempfile
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
    Message,
    WebAppInfo,
)
from aiohttp import web

from obyektivka_docx import build_obyektivka_docx
from obyektivka_pdf import build_obyektivka_pdf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "SIZNING_BOT_TOKENINGIZ_BU_YERGA")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://madesy.onrender.com/webapp/")

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
        [InlineKeyboardButton(text="🚀 Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))],
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
        "👋 Assalomu alaykum!\n\n"
        "<b>HujjatBot</b> — rasmiy hujjatlaringizni tez tayyorlaydi.\n\n"
        "Quyidagi tugma orqali ilovani oching:",
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
    return web.Response(text="HujjatBot ishlayapti ✅")


async def serve_webapp_index(request):
    index_path = os.path.join(WEBAPP_DIR, "index.html")
    return web.FileResponse(index_path)


async def api_generate(request):
    bot: Bot = request.app["bot"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Noto'g'ri so'rov"}, status=400)

    init_data = payload.get("initData", "")
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "Autentifikatsiya xatosi"}, status=401)

    chat_id = user.get("id")
    turi = payload.get("turi")
    answers = payload.get("answers", {})
    mehnat = payload.get("mehnat", [])
    qarindoshlar = payload.get("qarindoshlar", [])
    fmt = payload.get("format", "word")
    photo_b64 = payload.get("photo_base64")

    doc_data = {
        "turi": turi,
        "fio": answers.get("fio", ""),
        "sarlavha_yil": "2026-yil:",
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
        "mehnat": mehnat,
        "qarindoshlar": qarindoshlar,
    }

    safe_name = (doc_data["fio"] or "obyektivka").replace(" ", "_")

    with tempfile.TemporaryDirectory() as tmp:
        photo_path = None
        if photo_b64:
            try:
                header, encoded = photo_b64.split(",", 1)
                photo_path = os.path.join(tmp, "photo.jpg")
                with open(photo_path, "wb") as f:
                    f.write(base64.b64decode(encoded))
            except Exception:
                photo_path = None

        try:
            if fmt == "pdf":
                pdf_buf = build_obyektivka_pdf(doc_data, photo_path=photo_path)
                await bot.send_document(
                    chat_id,
                    BufferedInputFile(pdf_buf.read(), filename=f"{safe_name}.pdf"),
                    caption="✅ Obyektivkangiz tayyor!",
                )
            else:
                docx_buf = build_obyektivka_docx(doc_data, photo_path=photo_path)
                await bot.send_document(
                    chat_id,
                    BufferedInputFile(docx_buf.read(), filename=f"{safe_name}.docx"),
                    caption="✅ Obyektivkangiz tayyor!",
                )
        except Exception as e:
            logger.exception("Hujjat yasashda/yuborishda xato")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    return web.json_response({"ok": True})


async def start_web_server(bot: Bot):
    """Render 'Web Service' bepul rejasi uchun - portni tinglab turadi.
    Shuningdek Mini App statik fayllarini va API'ni ham shu orqali beradi.
    """
    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20MB - selfie/rasm yuklashlar uchun
    app["bot"] = bot
    app.router.add_get("/", health)
    app.router.add_get("/webapp/", serve_webapp_index)
    app.router.add_post("/api/generate", api_generate)
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
    await start_web_server(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
