"""
3x4 rasm tayyorlovchi modul.

Oqim:
  1. onnxruntime (to'g'ridan-to'g'ri, `rembg` paketisiz) - u2netp modeli
     yordamida foydalanuvchi yuborgan selfie/rasmdan odam figurasi
     maskasini (segmentation mask) hisoblaydi.
  2. Pillow - maskani alfa-kanal sifatida qo'llab, orqa fonni olib
     tashlaydi, o'rniga toza OQ fon qo'yadi, so'ng odam figurasi
     atrofida mos joylashtirib standart 3x4 sm hujjat rasm o'lchamiga
     (354x472 px, 300 DPI) qirqiladi/moslashtiriladi.

MUHIM: `rembg` paketining o'zi emas, faqat xom `onnxruntime` ishlatiladi.
Sababi - `rembg`ning __init__.py fayli har doim `pymatting`, `scipy`,
`scikit-image` (va ular orqali `numba`) kutubxonalarini ham yuklaydi,
garchi ular ishlatilmasa ham (~180+ MB ortiqcha xotira). Bu Render'ning
bepul tarifidagi 512 MB xotira chegarasidan chiqib ketishga sabab
bo'lgan. Xom onnxruntime + numpy + Pillow esa atigi ~40 MB baza xotira
ishlatadi - bu farq bepul serverda ishlash/ishlamaslik farqini beradi.

To'liq lokal ishlaydi - tashqi API kerak emas, bepul va tezkor.
"""
import io
import logging
import os

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
_MODEL_MD5 = "8e83ca70e441ab06c318d82300c84806"
_MODEL_DIR = os.path.expanduser("~/.u2net")
_MODEL_PATH = os.path.join(_MODEL_DIR, "u2netp.onnx")

_MODEL_INPUT_SIZE = (320, 320)
_MODEL_MEAN = (0.485, 0.456, 0.406)
_MODEL_STD = (0.229, 0.224, 0.225)

_session = None

# Standart 3x4 sm hujjat rasm o'lchami, 300 DPI da
OUTPUT_WIDTH = 354
OUTPUT_HEIGHT = 472
TARGET_RATIO = OUTPUT_WIDTH / OUTPUT_HEIGHT  # 3:4 = 0.75

# Telefon kamerasidan kelgan rasmlar odatda juda katta bo'ladi - qayta
# ishlashdan oldin xotira sarfini kamaytirish uchun kichraytiramiz.
MAX_INPUT_DIM = 1000

WHITE = (255, 255, 255)

PRICE_3X4 = 0  # bepul xizmat


def _ensure_model_downloaded():
    """u2netp.onnx modelini (agar hali yo'q bo'lsa) yuklab, diskka saqlaydi."""
    if os.path.exists(_MODEL_PATH):
        return
    import hashlib
    import requests

    os.makedirs(_MODEL_DIR, exist_ok=True)
    logger.info("u2netp.onnx modeli yuklanmoqda...")
    resp = requests.get(_MODEL_URL, timeout=60)
    resp.raise_for_status()
    data = resp.content

    checksum = hashlib.md5(data).hexdigest()
    if checksum != _MODEL_MD5:
        raise RuntimeError(f"Model fayli buzilgan (checksum mos kelmadi): {checksum}")

    tmp_path = _MODEL_PATH + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, _MODEL_PATH)
    logger.info("u2netp.onnx modeli muvaffaqiyatli yuklandi")


def _get_session():
    """onnxruntime InferenceSession'ni "dangasa" (lazy) yuklaydi - bu
    import/yuklash bot ishga tushganda emas, faqat birinchi haqiqiy
    so'rov kelganda amalga oshiriladi. Aks holda bot web-serverni
    portga ulanishidan oldin bu import tugashini kutib, Render buni
    "server javob bermayapti" deb noto'g'ri hisoblab qoladi."""
    global _session
    if _session is None:
        import onnxruntime as ort

        _ensure_model_downloaded()

        sess_opts = ort.SessionOptions()
        # Xotira sarfini minimal ushlab turish uchun thread sonini cheklaymiz
        sess_opts.intra_op_num_threads = 1
        sess_opts.inter_op_num_threads = 1
        sess_opts.enable_mem_pattern = False
        sess_opts.enable_cpu_mem_arena = False

        _session = ort.InferenceSession(
            _MODEL_PATH, sess_options=sess_opts, providers=["CPUExecutionProvider"]
        )
    return _session


def _predict_mask(img: Image.Image) -> Image.Image:
    """Berilgan rasm uchun odam figurasi maskasini (L-mode, oq=figura,
    qora=fon) hisoblaydi - u2netp modeli orqali."""
    import numpy as np

    session = _get_session()

    resized = img.convert("RGB").resize(_MODEL_INPUT_SIZE, Image.LANCZOS)
    arr = np.asarray(resized, dtype=np.float32) / 255.0

    for i in range(3):
        arr[:, :, i] = (arr[:, :, i] - _MODEL_MEAN[i]) / _MODEL_STD[i]

    tensor = np.expand_dims(arr.transpose(2, 0, 1), 0).astype(np.float32)

    input_name = session.get_inputs()[0].name
    outputs = session.run(None, {input_name: tensor})

    pred = outputs[0][:, 0, :, :]
    pred_min, pred_max = pred.min(), pred.max()
    pred = (pred - pred_min) / max(pred_max - pred_min, 1e-6)
    pred = np.squeeze(pred)

    mask = Image.fromarray((pred * 255).astype("uint8"), mode="L")
    mask = mask.resize(img.size, Image.LANCZOS)
    return mask


def _find_subject_bbox(alpha_img: Image.Image, threshold: int = 20):
    """Alfa-kanal (shaffoflik) xaritasidan odam figurasi joylashgan
    to'rtburchak sohani (bounding box) topadi."""
    bbox = alpha_img.point(lambda p: 255 if p > threshold else 0).getbbox()
    return bbox


def process_3x4(image_bytes: bytes) -> bytes:
    """Kirish: xom rasm baytlari (JPEG/PNG/HEIC va h.k.).
    Chiqish: OQ fonli, 3x4 sm hujjat o'lchamiga moslashtirilgan JPEG bayt."""
    original = Image.open(io.BytesIO(image_bytes))

    # JPEG uchun "draft mode" - rasmni to'liq (masalan 50 MP) dekodlab,
    # keyin kichraytirish o'rniga, dekodlashning o'zida kerakli o'lchamga
    # yaqinlashtirib oladi. Bu telefon kamerasidan kelgan yuqori sifatli
    # (masalan 48-50 MP) suratlarni ham xotira sarfini keskin kamaytirib
    # (~60%) qayta ishlash imkonini beradi.
    original.draft("RGB", (MAX_INPUT_DIM, MAX_INPUT_DIM))
    original = original.convert("RGB")

    # Telefon kamerasi rasmlari ko'pincha EXIF "Orientation" metama'lumoti bilan
    # keladi (piksellar aslida burilmagan, faqat "buni ekranda 90/180 gradus
    # burib ko'rsat" degan ko'rsatma bor). Buni hisobga olmasak, model rasmni
    # noto'g'ri burchakda tahlil qiladi va natija burilib/qiyshayib chiqadi.
    original = ImageOps.exif_transpose(original)

    # Qolgan formatlar (PNG, WEBP va h.k.) uchun draft mode ishlamaydi,
    # shuning uchun oddiy kichraytirish bilan yakunlaymiz
    if max(original.size) > MAX_INPUT_DIM:
        original.thumbnail((MAX_INPUT_DIM, MAX_INPUT_DIM), Image.LANCZOS)

    # 1-qadam: odam figurasi maskasini hisoblash va uni alfa-kanal sifatida qo'llash
    mask = _predict_mask(original)
    removed = original.convert("RGBA")
    removed.putalpha(mask)

    # 2-qadam: odam figurasi joylashgan sohani aniqlash
    bbox = _find_subject_bbox(mask)
    if bbox is None:
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
        needed_h = crop_w / TARGET_RATIO
        extra = (needed_h - crop_h) / 2
        crop_top -= extra
        crop_bottom += extra
    else:
        needed_w = crop_h * TARGET_RATIO
        extra = (needed_w - crop_w) / 2
        crop_left -= extra
        crop_right += extra

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
    result = out_buf.read()

    del original, mask, removed, cropped, white_bg, composed, final
    import gc
    gc.collect()

    return result
