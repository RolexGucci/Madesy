"""
3x4 rasm tayyorlovchi modul.

Oqim:
  1. rembg (u2netp - yengil model, ~4.7 MB) - foydalanuvchi yuborgan
     selfie/rasmdan orqa fonni olib tashlaydi (shaffof fon bilan).
  2. Pillow - shaffof fon o'rniga toza OQ fon qo'yiladi, so'ng odam
     figurasi atrofida mos joylashtirib standart 3x4 sm hujjat rasm
     o'lchamiga (354x472 px, 300 DPI) qirqiladi/moslashtiriladi.

To'liq lokal ishlaydi - tashqi API kerak emas, bepul va tezkor.
"""
import io
import logging

from PIL import Image
from rembg import remove, new_session

logger = logging.getLogger(__name__)

# silueta - u2netp'dan sifatliroq, lekin hali ham yengil (~43 MB),
# Render bepul tarifida (512 MB RAM) ishonchli ishlaydi
_MODEL_NAME = "silueta"
_session = None

# Standart 3x4 sm hujjat rasm o'lchami, 300 DPI da
OUTPUT_WIDTH = 354
OUTPUT_HEIGHT = 472
TARGET_RATIO = OUTPUT_WIDTH / OUTPUT_HEIGHT  # 3:4 = 0.75

WHITE = (255, 255, 255)

PRICE_3X4 = 0  # bepul xizmat


def _get_session():
    """rembg sessiyasini "dangasa" (lazy) yuklaydi - birinchi so'rovda
    bir marta model xotiraga yuklanadi, keyingi so'rovlarda qayta
    ishlatiladi."""
    global _session
    if _session is None:
        _session = new_session(_MODEL_NAME)
    return _session


def _find_subject_bbox(alpha_img: Image.Image, threshold: int = 20):
    """Alfa-kanal (shaffoflik) xaritasidan odam figurasi joylashgan
    to'rtburchak sohani (bounding box) topadi."""
    bbox = alpha_img.point(lambda p: 255 if p > threshold else 0).getbbox()
    return bbox


def process_3x4(image_bytes: bytes) -> bytes:
    """Kirish: xom rasm baytlari (JPEG/PNG/HEIC va h.k.).
    Chiqish: OQ fonli, 3x4 sm hujjat o'lchamiga moslashtirilgan JPEG bayt."""
    session = _get_session()

    original = Image.open(io.BytesIO(image_bytes))
    original = original.convert("RGB")

    # 1-qadam: orqa fonni olib tashlash (natija RGBA, fon shaffof)
    removed = remove(original, session=session)
    if removed.mode != "RGBA":
        removed = removed.convert("RGBA")

    # 2-qadam: odam figurasi joylashgan sohani aniqlash
    alpha = removed.split()[-1]
    bbox = _find_subject_bbox(alpha)
    if bbox is None:
        # Figura topilmadi (masalan fon deyarli yo'q edi) - butun rasmni ishlatamiz
        bbox = (0, 0, removed.width, removed.height)

    subj_left, subj_top, subj_right, subj_bottom = bbox
    subj_w = subj_right - subj_left
    subj_h = subj_bottom - subj_top

    # 3-qadam: hujjat foto uslubida margin qo'shish - figuraning atrofida
    # yon tomonlardan ~18%, tepadan ~12%, pastdan ~4% bo'sh joy qoldiramiz
    # (standart passport-uslubidagi kadrlash)
    pad_x = subj_w * 0.18
    pad_top = subj_h * 0.12
    pad_bottom = subj_h * 0.04

    crop_left = subj_left - pad_x
    crop_right = subj_right + pad_x
    crop_top = subj_top - pad_top
    crop_bottom = subj_bottom + pad_bottom

    crop_w = crop_right - crop_left
    crop_h = crop_bottom - crop_top

    # 4-qadam: 3:4 nisbatga moslashtirish (kengroq tomonni kengaytiramiz,
    # markazdan chetga chiqmasdan)
    current_ratio = crop_w / crop_h
    if current_ratio > TARGET_RATIO:
        # juda keng - balandlikni oshiramiz
        needed_h = crop_w / TARGET_RATIO
        extra = (needed_h - crop_h) / 2
        crop_top -= extra
        crop_bottom += extra
    else:
        # juda tor - kenglikni oshiramiz
        needed_w = crop_h * TARGET_RATIO
        extra = (needed_w - crop_w) / 2
        crop_left -= extra
        crop_right += extra

    # Rasm chegarasidan tashqariga chiqmaslik uchun cheklaymiz
    crop_left = max(0, crop_left)
    crop_top = max(0, crop_top)
    crop_right = min(removed.width, crop_right)
    crop_bottom = min(removed.height, crop_bottom)

    cropped = removed.crop((int(crop_left), int(crop_top), int(crop_right), int(crop_bottom)))

    # 5-qadam: OQ fon ustiga qo'yish (shaffof qismlarni oq bilan to'ldirish)
    white_bg = Image.new("RGBA", cropped.size, WHITE + (255,))
    composed = Image.alpha_composite(white_bg, cropped).convert("RGB")

    # 6-qadam: aniq 3x4 sm hujjat o'lchamiga moslashtirish
    final = composed.resize((OUTPUT_WIDTH, OUTPUT_HEIGHT), Image.LANCZOS)

    out_buf = io.BytesIO()
    final.save(out_buf, format="JPEG", quality=95)
    out_buf.seek(0)
    return out_buf.read()
