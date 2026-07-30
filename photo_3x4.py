"""
3x4 rasm tayyorlovchi modul.
Fon olib tashlash uchun remove.bg tashqi API'sidan foydalanadi (REMOVEBG_API_KEY
muhit o'zgaruvchisi orqali). Bunga o'tilgan sabab: mahalliy 'rembg'/'onnxruntime'
Render bepul rejasining 512MB RAM chegarasidan oshib, servisni OOM bilan
qulatib yuborardi. Tashqi API bilan server juda yengil (faqat Pillow) ishlaydi.

Qo'shimcha (mahalliy, bepul):
  - Yuz/rasm tiniqligini oshirish (sharpen + kontrast)
  - Rasmiy kostyum-yoqa siymosini yelka-ko'krak qismiga qo'yish (chizilgan overlay,
    generativ AI emas - shuning uchun pulsiz va tez ishlaydi)
"""
import io
import os

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

# 3x4 sm hujjat o'lchami, 300 DPI da piksel hisobida
TARGET_W = 354   # 3 sm
TARGET_H = 472   # 4 sm

REMOVEBG_API_KEY = os.getenv("REMOVEBG_API_KEY", "")
REMOVEBG_URL = "https://api.remove.bg/v1.0/removebg"


def _remove_background(image_bytes: bytes) -> Image.Image:
    """remove.bg API orqali fonni olib tashlaydi. RGBA (shaffof fon) qaytaradi."""
    if not REMOVEBG_API_KEY:
        raise RuntimeError(
            "REMOVEBG_API_KEY sozlanmagan. Render'da Environment bo'limiga qo'shing."
        )

    response = requests.post(
        REMOVEBG_URL,
        files={"image_file": ("photo.jpg", image_bytes, "image/jpeg")},
        data={"size": "auto"},
        headers={"X-Api-Key": REMOVEBG_API_KEY},
        timeout=30,
    )

    if response.status_code != requests.codes.ok:
        raise RuntimeError(
            f"remove.bg xatosi ({response.status_code}): {response.text[:200]}"
        )

    return Image.open(io.BytesIO(response.content)).convert("RGBA")


def _find_subject_bbox(rgba_image: Image.Image, alpha_threshold: int = 30):
    """Shaffof bo'lmagan (subyekt) qismning chegaralarini topadi."""
    alpha = np.array(rgba_image.split()[-1])
    mask = alpha > alpha_threshold
    if not mask.any():
        w, h = rgba_image.size
        return 0, 0, w, h
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _enhance_photo(img: Image.Image) -> Image.Image:
    """Yuz/rasm tiniqligini oshiradi - keskinlik, kontrast, biroz to'yinganlik."""
    img = img.filter(ImageFilter.UnsharpMask(radius=1.6, percent=110, threshold=2))
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Color(img).enhance(1.06)
    img = ImageEnhance.Brightness(img).enhance(1.02)
    return img


def _build_suit_overlay(width: int, height: int) -> Image.Image:
    """Rasmiy kostyum + yoqa + galstuk siymosini chizadi (RGBA, shaffof fon).
    Pastki ~42% qismda joylashadi, tepa chetida yumshoq o'tish (feather) bor -
    shunda haqiqiy bo'yin bilan tabiiy qo'shilib ketadi.
    """
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    JACKET = (35, 40, 52, 255)      # to'q ko'k-kulrang kostyum
    SHIRT = (250, 250, 252, 255)    # oq ko'ylak
    TIE = (60, 30, 34, 255)         # to'q bordo galstuk

    top_y = int(height * 0.60)      # yelka chizig'i boshlanishi
    neck_y = int(height * 0.55)     # yoqa uchi (bo'yinga eng yaqin nuqta)
    cx = width / 2

    # Kostyum (yelkalardan pastga qarab kengayadigan trapetsiya, ikki tomondan)
    jacket_poly = [
        (cx - width * 0.06, neck_y),
        (cx - width * 0.50, top_y + height * 0.10),
        (cx - width * 0.55, height),
        (cx + width * 0.55, height),
        (cx + width * 0.50, top_y + height * 0.10),
        (cx + width * 0.06, neck_y),
        (cx, neck_y + height * 0.09),
    ]
    draw.polygon(jacket_poly, fill=JACKET)

    # Ko'ylak/yoqa (V shaklida, kostyum ustida markazda)
    shirt_poly = [
        (cx - width * 0.10, neck_y),
        (cx - width * 0.16, top_y + height * 0.16),
        (cx, top_y + height * 0.30),
        (cx + width * 0.16, top_y + height * 0.16),
        (cx + width * 0.10, neck_y),
    ]
    draw.polygon(shirt_poly, fill=SHIRT)

    # Yoqa uchlari (ikki uchburchak)
    draw.polygon([
        (cx - width * 0.10, neck_y),
        (cx - width * 0.03, neck_y),
        (cx - width * 0.14, top_y + height * 0.20),
    ], fill=JACKET)
    draw.polygon([
        (cx + width * 0.10, neck_y),
        (cx + width * 0.03, neck_y),
        (cx + width * 0.14, top_y + height * 0.20),
    ], fill=JACKET)

    # Galstuk
    draw.polygon([
        (cx - width * 0.028, top_y + height * 0.14),
        (cx + width * 0.028, top_y + height * 0.14),
        (cx + width * 0.045, height),
        (cx - width * 0.045, height),
    ], fill=TIE)

    # Tepa chetida yumshoq o'tish (feather) - alpha gradient
    feather_h = int(height * 0.10)
    alpha = np.array(overlay.split()[-1]).astype(np.float32)
    for i in range(feather_h):
        y = neck_y - feather_h + i
        if 0 <= y < height:
            factor = i / feather_h
            alpha[y, :] *= factor
    overlay.putalpha(Image.fromarray(alpha.astype(np.uint8)))

    return overlay


def process_3x4_photo(input_bytes: bytes, add_suit: bool = True) -> bytes:
    """
    Selfie rasmni qabul qiladi, quyidagilarni bajaradi:
      1. Fonni olib tashlaydi (AI, rembg/u2net)
      2. Oq fon qo'yadi
      3. Subyektni markazga olib, 3x4 nisbatga (bosh ustidan yetarli joy bilan) keladi
      4. 354x472 piksel (3x4 sm, 300 DPI) o'lchamiga keltiradi
      5. Rasm tiniqligini oshiradi (keskinlik, kontrast)
      6. Ixtiyoriy: yelka-ko'krak qismiga rasmiy kostyum siymosini qo'yadi
    Natijani JPEG bayt sifatida qaytaradi.
    """
    input_img = Image.open(io.BytesIO(input_bytes)).convert("RGB")
    # Katta rasmlarda API sekinroq/qimmatroq ishlaydi - hisoblash uchun cheklaymiz
    input_img.thumbnail((1200, 1200), Image.LANCZOS)

    resized_buf = io.BytesIO()
    input_img.save(resized_buf, format="JPEG", quality=95)

    removed = _remove_background(resized_buf.getvalue())  # RGBA, fon shaffof

    x0, y0, x1, y1 = _find_subject_bbox(removed)
    subj_w = x1 - x0
    subj_h = y1 - y0

    # Boshning tepasidan biroz joy, pastda yelkalar ko'rinishi uchun kengroq kadr
    top_pad = subj_h * 0.35
    bottom_pad = subj_h * 0.55
    side_pad_ratio = 0.5

    crop_h = subj_h + top_pad + bottom_pad
    crop_w = crop_h * (TARGET_W / TARGET_H)

    if crop_w < subj_w * (1 + side_pad_ratio):
        crop_w = subj_w * (1 + side_pad_ratio)
        crop_h = crop_w * (TARGET_H / TARGET_W)
        top_pad = crop_h * 0.28
        bottom_pad = crop_h - subj_h - top_pad

    cx = (x0 + x1) / 2
    crop_x0 = cx - crop_w / 2
    crop_y0 = y0 - top_pad
    crop_x1 = crop_x0 + crop_w
    crop_y1 = crop_y0 + crop_h

    # Oq fonli katta "canvas" - agar crop rasm chegarasidan chiqib ketsa ham muammo bo'lmasin
    canvas_pad = int(max(crop_w, crop_h))
    canvas = Image.new("RGBA", (removed.width + 2 * canvas_pad, removed.height + 2 * canvas_pad), (255, 255, 255, 255))
    canvas.paste(removed, (canvas_pad, canvas_pad), removed)

    final_crop = canvas.crop((
        int(crop_x0 + canvas_pad),
        int(crop_y0 + canvas_pad),
        int(crop_x1 + canvas_pad),
        int(crop_y1 + canvas_pad),
    ))

    final_rgb = Image.new("RGB", final_crop.size, (255, 255, 255))
    final_rgb.paste(final_crop, (0, 0), final_crop)

    final_rgb = final_rgb.resize((TARGET_W, TARGET_H), Image.LANCZOS)

    final_rgb = _enhance_photo(final_rgb)

    if add_suit:
        suit = _build_suit_overlay(TARGET_W, TARGET_H)
        final_rgba = final_rgb.convert("RGBA")
        final_rgba = Image.alpha_composite(final_rgba, suit)
        final_rgb = final_rgba.convert("RGB")

    out_buf = io.BytesIO()
    final_rgb.save(out_buf, format="JPEG", quality=95)
    out_buf.seek(0)
    return out_buf.read()
