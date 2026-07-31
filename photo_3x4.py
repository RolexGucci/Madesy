"""
3x4 rasm tayyorlovchi modul.
Fon olib tashlash uchun remove.bg tashqi API'sidan foydalanadi (REMOVEBG_API_KEY
muhit o'zgaruvchisi orqali). Bunga o'tilgan sabab: mahalliy 'rembg'/'onnxruntime'
Render bepul rejasining 512MB RAM chegarasidan oshib, servisni OOM bilan
qulatib yuborardi. Tashqi API bilan server juda yengil (faqat Pillow) ishlaydi.

Qo'shimcha (mahalliy, bepul):
  - Yuz/rasm tiniqligini oshirish (sharpen + kontrast)
  - Haqiqiy kostyum surati (assets/suit_male.png yoki suit_female.png, fon
    shaffof) ni rasm kengligiga moslab kattalashtirib, bo'yin chizig'iga
    joylashtirish - har bir odamning tana/yelka kengligiga avtomatik moslashadi
"""
import io
import os
from collections import deque

import numpy as np
import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

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


def _matte_checkerboard_background(img: Image.Image) -> Image.Image:
    """Ba'zi tayyor shablon PNG'larda haqiqiy shaffoflik yo'q - saytdan olingan
    "checkerboard" (shaxmat) fon rasmga chizib qo'yilgan bo'ladi. Bu funksiya
    rasm burchaklaridan boshlab, och rangdagi (checkerboard/oq fon) piksellarni
    "suv toshqini" (flood fill) usulida aniqlaydi va faqat ularni shaffof qiladi -
    kostyum ichidagi (masalan oq ko'ylak) alohida ajratilgan yorug' joylarga
    tegmaydi, chunki ular to'q kostyum bilan o'ralgan va tashqi chegaraga
    ulanmagan.
    """
    rgb = np.array(img.convert("RGB"), dtype=np.int16)
    h, w, _ = rgb.shape
    is_light = (rgb[:, :, 0] >= 200) & (rgb[:, :, 1] >= 200) & (rgb[:, :, 2] >= 200)

    visited = np.zeros((h, w), dtype=bool)
    dq = deque()

    def _seed(y, x):
        if is_light[y, x] and not visited[y, x]:
            visited[y, x] = True
            dq.append((y, x))

    for x in range(w):
        _seed(0, x)
        _seed(h - 1, x)
    for y in range(h):
        _seed(y, 0)
        _seed(y, w - 1)

    while dq:
        y, x = dq.popleft()
        if y > 0:
            _seed(y - 1, x)
        if y < h - 1:
            _seed(y + 1, x)
        if x > 0:
            _seed(y, x - 1)
        if x < w - 1:
            _seed(y, x + 1)

    alpha = np.where(visited, 0, 255).astype(np.uint8)
    rgba = img.convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha))
    return rgba


ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_suit_cache = {}


def _load_suit_template(gender: str) -> Image.Image:
    """Kostyum shablonini (PNG) diskdan yuklaydi, checkerboard fonini shaffof
    qilib, keshda saqlaydi."""
    key = "male" if gender == "male" else "female"
    if key not in _suit_cache:
        path = os.path.join(ASSETS_DIR, f"suit_{key}.png")
        raw = Image.open(path)
        _suit_cache[key] = _matte_checkerboard_background(raw)
    return _suit_cache[key]


def _apply_suit_overlay(final_rgb: Image.Image, gender: str) -> Image.Image:
    """Haqiqiy kostyum surati (PNG)ni rasm kengligiga moslab, bo'yin chizig'iga
    joylashtiradi. Har bir odam uchun rasm kengligi bir xil (TARGET_W) bo'lgani
    sababli, shablon ham xuddi shu kenglikka moslab kattalashtiriladi - shu orqali
    har xil tana/yelka kengligiga avtomatik moslashadi.
    """
    template = _load_suit_template(gender)
    width, height = final_rgb.size

    scale = width / template.width
    new_h = max(1, int(template.height * scale))
    resized = template.resize((width, new_h), Image.LANCZOS)

    # Shablonning tepasida bo'sh (fon) joy bo'lishi mumkin - kiyimning haqiqiy
    # boshlanish nuqtasini (birinchi shaffof bo'lmagan qator) topamiz
    alpha_arr = np.array(resized.split()[-1])
    rows_with_content = np.where(alpha_arr.max(axis=1) > 10)[0]
    content_top = int(rows_with_content[0]) if len(rows_with_content) else 0
    content = resized.crop((0, content_top, width, new_h))
    content_h = content.height

    # Yoqa boshlanishi taxminan bo'yin chizig'ida (kalibrlangan nisbat)
    neck_y = int(height * 0.56)
    needed_h = height - neck_y

    if content_h < needed_h:
        # Kiyim canvas tagigacha yetmasa, oxirgi qatorni pastga cho'zib to'ldiramiz
        last_row = content.crop((0, content_h - 1, width, content_h))
        extra = last_row.resize((width, needed_h - content_h))
        extended = Image.new("RGBA", (width, needed_h), (0, 0, 0, 0))
        extended.paste(content, (0, 0))
        extended.paste(extra, (0, content_h))
        content = extended
    elif content_h > needed_h:
        content = content.crop((0, 0, width, needed_h))

    final_rgba = final_rgb.convert("RGBA")
    final_rgba.alpha_composite(content, (0, neck_y))
    return final_rgba.convert("RGB")


def process_3x4_photo(input_bytes: bytes, gender: str = None) -> bytes:
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
    input_img = ImageOps.exif_transpose(input_img)  # telefon kamerasi "aylantirish" belgisini tuzatadi
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

    if gender in ("male", "female"):
        print(f"[photo_3x4] kostyum qo'shilmoqda, gender={gender}", flush=True)
        final_rgb = _apply_suit_overlay(final_rgb, gender)
    else:
        print(f"[photo_3x4] kostyum QO'SHILMADI, gender={gender!r}", flush=True)

    out_buf = io.BytesIO()
    final_rgb.save(out_buf, format="JPEG", quality=95)
    out_buf.seek(0)
    return out_buf.read()
