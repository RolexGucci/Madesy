"""
AI-kuchaytirilgan 3x4 rasm tayyorlovchi modul (OpenAI GPT Image 2).

Eski `photo_3x4.py` (onnxruntime + u2netp, faqat fon o'chirish) o'rniga
keladi. Farqi: bu modul nafaqat fonni oq qiladi, balki kiyimni ham
rasmiy qora kostyum-galstukka (yoki ayollar uchun kostyum-ko'ylakka)
almashtiradi va yuzni tabiiy tarzda tiniqlashtiradi - hammasi bitta
GPT Image so'rovida.

Model: gpt-image-2, /v1/images/edits endpointi orqali.
Eslatma: input_fidelity parametri gpt-image-2 tomonidan qo'llab-
quvvatlanmaydi (faqat gpt-image-1/1.5 uchun ishlaydi) - shuning uchun
yuz saqlash faqat promptdagi aniq matn ko'rsatmalariga tayanadi.

Oqim:
  1. Foydalanuvchi selfie yuboradi (istalgan fon/kiyim bilan).
  2. GPT Image /v1/images/edits so'rovi yuboriladi: fonni oq qil,
     kiyimni rasmiy qora kostyumga almashtir - lekin odamning o'ziga
     xos qiyofasini o'zgartirma (bu talab promptdagi aniq matn
     ko'rsatmalari orqali beriladi).
  3. Natija rasm 3x4 sm hujjat o'lchamiga (472x630 px, 400 DPI)
     moslashtiriladi.
  4. Ikkita chiqish tayyorlanadi:
       - bitta 3x4 rasm (JPEG)
       - 10x15 sm varaqda 8 nusxa, kesish chiziqlari bilan (JPEG,
         PIL orqali mahalliy joylashtiriladi - AI emas, chunki bu
         tezroq, arzonroq va piksel-aniq natija beradi)

Narx siyosati (bot.py'da amalga oshiriladi, bu yerda faqat texnik
generatsiya funksiyasi bor):
  - Bitta urinish - to'lov bilan (6000 so'm). Qayta ishlash imkoniyati
    yo'q, chunki amalda AI natijasi deyarli bir xil chiqadi va qayta
    urinish faqat qo'shimcha xarajat keltiradi.
"""
import base64
import io
import logging
import os

import requests
from PIL import Image, ImageDraw, ImageOps

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_IMAGE_EDIT_URL = "https://api.openai.com/v1/images/edits"
OPENAI_IMAGE_MODEL = "gpt-image-2"

# 3x4 sm hujjat rasm o'lchami - 400 DPI'da (yuqori chop etish sifati uchun,
# 300 DPI standart minimumdan yuqoriroq, mijoz qog'ozga chiqarib kesib
# olganda piksellashish bo'lmasligi uchun)
DPI = 400
MM_TO_INCH = 1 / 25.4
OUTPUT_WIDTH = round(30 * MM_TO_INCH * DPI)   # 3 sm = 30 mm
OUTPUT_HEIGHT = round(40 * MM_TO_INCH * DPI)  # 4 sm = 40 mm
TARGET_RATIO = OUTPUT_WIDTH / OUTPUT_HEIGHT  # 3:4 = 0.75

MAX_INPUT_DIM = 1536  # OpenAI'ga yuborishdan oldin kichraytirish (xarajat/tezlik uchun)

PRICE_3X4 = 6000
MAX_FREE_RETRIES = 0  # qayta urinish yo'q - 1 marta to'lov, 1 marta natija

# 15x10 sm (albom/landscape yo'nalishda - standart foto qog'oz chop etish
# formati), 300 DPI'da piksel o'lchami
SHEET_WIDTH = 1772   # 15 sm
SHEET_HEIGHT = 1181  # 10 sm

_PROMPT = """Rasmiy 3×4 pasport uslubidagi hujjat rasmi yasang.
Sof oq fonda, soya yo'q.
Oddiy smartfon selfisini rasmiy professional portretga aylantiring.
Odam kameraga to'g'ri qarab, yuzi to'liq frontal holatda, neytral ifoda bilan, faqat bosh va yelka qismi ko'rinsin.
Kiyimni rasmiy qilib o'zgartiring:
Agar ayol bo'lsa -> qora pidjak + oq yoqa ko'ylak
Agar erkak bo'lsa -> qora kostyum pidjagi + oq yoqa ko'ylak + qora galstuk
Yuz, soch, teri rangi, yuz xususiyatlari (xol, mo'ylov, soqol, makiyaj va h.k.)ni aniq saqlang.
Professional studiya yoritishi, aniq fokus, toza va rasmiy ko'rinish.
Standart 3×4 nisbatga qat'iy qirqib oling."""


def _prepare_input_image(image_bytes: bytes) -> bytes:
    """Kirish rasmini OpenAI'ga yuborishdan oldin tayyorlaydi - EXIF
    burilishini to'g'irlaydi va hajmini kamaytiradi (xarajat va tezlik
    uchun). PNG formatida saqlanadi, chunki OpenAI images/edits
    endpointi PNG/WEBP/JPG qabul qiladi va PNG eng ishonchli variant."""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    if max(img.size) > MAX_INPUT_DIM:
        img.thumbnail((MAX_INPUT_DIM, MAX_INPUT_DIM), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_formal_portrait(image_bytes: bytes) -> bytes:
    """OpenAI GPT Image 2 (/v1/images/edits) orqali selfie'ni rasmiy
    portretga aylantiradi.

    image_bytes: xom selfie baytlari.

    Qaytaradi: OpenAI tomonidan generatsiya qilingan xom rasm baytlari
    (hali 3x4 o'lchamiga moslashtirilmagan - buni tozalash caller
    tomonida amalga oshiriladi, chunki ba'zan chetlarni qayta kadrlash
    kerak bo'ladi).
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY sozlanmagan. Render'da Environment bo'limiga qo'shing.")

    prepared = _prepare_input_image(image_bytes)

    files = {
        "image[]": ("selfie.png", prepared, "image/png"),
    }
    data = {
        "model": OPENAI_IMAGE_MODEL,
        "prompt": _PROMPT,
        "size": "1024x1536",  # 3:4 portret nisbatiga eng yaqin standart o'lcham
        "background": "opaque",
        "n": 1,
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    resp = requests.post(
        OPENAI_IMAGE_EDIT_URL, headers=headers, data=data, files=files, timeout=90
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI rasm xatosi ({resp.status_code}): {resp.text[:500]}")

    result = resp.json()
    try:
        b64_image = result["data"][0]["b64_json"]
    except (KeyError, IndexError) as e:
        logger.error(f"OpenAI javobini o'qib bo'lmadi: {result}")
        raise RuntimeError(f"OpenAI javobida rasm topilmadi: {result}") from e

    return base64.b64decode(b64_image)


def _crop_to_3x4(image_bytes: bytes) -> Image.Image:
    """OpenAI natijasini markazdan 3:4 nisbatga moslashtirib qirqadi va
    aniq OUTPUT_WIDTH x OUTPUT_HEIGHT o'lchamiga keltiradi."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    current_ratio = w / h

    if current_ratio > TARGET_RATIO:
        # juda keng - yon tomonlardan qirqamiz
        new_w = int(h * TARGET_RATIO)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    elif current_ratio < TARGET_RATIO:
        # juda baland - tepa/pastdan qirqamiz (yuzga ko'proq joy qoldirib,
        # tepadan ozroq, pastdan ko'proq olib tashlaymiz)
        new_h = int(w / TARGET_RATIO)
        top = int((h - new_h) * 0.35)
        img = img.crop((0, top, w, top + new_h))

    # Agar AI'dan kelgan (qirqilgan) rasm bizga kerakli chop etish
    # o'lchamidan (472x630, 400 DPI) kichik bo'lsa, buni logga yozib
    # qo'yamiz - sifat pasayishi ehtimoli haqida bilib turish uchun.
    # Amalda Gemini/GPT Image odatda 1024x1024 va undan katta rasm
    # qaytaradi, shuning uchun bu deyarli hech qachon ishga tushmaydi.
    cropped_w, cropped_h = img.size
    if cropped_w < OUTPUT_WIDTH or cropped_h < OUTPUT_HEIGHT:
        logger.warning(
            f"AI natijasi kutilganidan kichikroq ({cropped_w}x{cropped_h}, "
            f"kerak: {OUTPUT_WIDTH}x{OUTPUT_HEIGHT}) - kattalashtirish sifatga "
            f"biroz ta'sir qilishi mumkin."
        )

    img = img.resize((OUTPUT_WIDTH, OUTPUT_HEIGHT), Image.LANCZOS)
    return img


def process_photo_to_3x4(image_bytes: bytes) -> bytes:
    """To'liq oqim: selfie -> OpenAI GPT Image 2 qayta ishlash -> 3x4
    o'lchamga moslashtirish. Qaytaradi: bitta 3x4 JPEG bayt."""
    ai_result = generate_formal_portrait(image_bytes)
    final_img = _crop_to_3x4(ai_result)

    out_buf = io.BytesIO()
    final_img.save(out_buf, format="JPEG", quality=95)
    return out_buf.getvalue()


def build_sheet_8x(single_3x4_bytes: bytes) -> bytes:
    """Bitta 3x4 rasmdan 15x10 sm (albom) varaqda 8 nusxa (4 ustun x 2
    qator) joylashtirilgan, kesish chiziqlari bilan JPEG tayyorlaydi - chop
    etish uchun tayyor. Har bir rasm atrofida yupqa och kulrang hoshiya
    (ramka) chiziladi - bu kesish chizig'ini aniqroq ko'rsatadi va
    mijoz qaychi bilan kesib olganda chegarani aniq ajratadi."""
    photo = Image.open(io.BytesIO(single_3x4_bytes)).convert("RGB")

    sheet = Image.new("RGB", (SHEET_WIDTH, SHEET_HEIGHT), (245, 245, 245))

    cols, rows = 4, 2
    margin_x = 30
    margin_y = 30
    gap = 18
    border_width = 2  # rasm atrofidagi yupqa och kulrang hoshiya

    cell_w = (SHEET_WIDTH - 2 * margin_x - (cols - 1) * gap) // cols
    cell_h = (SHEET_HEIGHT - 2 * margin_y - (rows - 1) * gap) // rows

    photo_resized = photo.resize((cell_w, cell_h), Image.LANCZOS)

    draw = ImageDraw.Draw(sheet)
    dash_len, dash_gap = 6, 5
    border_color = (190, 190, 190)

    def _dashed_line(p1, p2):
        x1, y1 = p1
        x2, y2 = p2
        if x1 == x2:  # vertikal
            y = y1
            while y < y2:
                draw.line([(x1, y), (x1, min(y + dash_len, y2))], fill=(150, 150, 150), width=1)
                y += dash_len + dash_gap
        else:  # gorizontal
            x = x1
            while x < x2:
                draw.line([(x, y1), (min(x + dash_len, x2), y1)], fill=(150, 150, 150), width=1)
                x += dash_len + dash_gap

    for row in range(rows):
        for col in range(cols):
            x = margin_x + col * (cell_w + gap)
            y = margin_y + row * (cell_h + gap)
            sheet.paste(photo_resized, (x, y))

            # rasm atrofidagi yupqa och kulrang hoshiya (ramka)
            draw.rectangle(
                [x - border_width, y - border_width, x + cell_w + border_width - 1, y + cell_h + border_width - 1],
                outline=border_color,
                width=border_width,
            )

            # kesish chiziqlari (rasmning hoshiyasidan biroz tashqarida)
            pad = 4 + border_width
            _dashed_line((x - pad, y - pad), (x + cell_w + pad, y - pad))
            _dashed_line((x - pad, y + cell_h + pad), (x + cell_w + pad, y + cell_h + pad))
            _dashed_line((x - pad, y - pad), (x - pad, y + cell_h + pad))
            _dashed_line((x + cell_w + pad, y - pad), (x + cell_w + pad, y + cell_h + pad))

    out_buf = io.BytesIO()
    sheet.save(out_buf, format="JPEG", quality=95)
    return out_buf.getvalue()
