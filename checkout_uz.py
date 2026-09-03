"""
Checkout.uz to'lov agregatori bilan ishlash uchun modul (Madesy / Render / Python).

Bu - Topchiq.uz loyihasida (Vercel / Node.js) yozilgan va real to'lovlar bilan
sinovdan o'tgan checkout.js kutubxonasining Python/aiohttp'ga portlangan versiyasi.

Rasmiy hujjat: https://checkout.uz/api-docs (LLM uchun: https://checkout.uz/llm.txt)

MUHIM FARQ (Vercel vs Render):
Vercel'da funksiyalar 10 soniyada avtomatik uziladi, shuning uchun Topchiq'da
timeout atayin 6 soniya qilingan edi. Render'da HTTP javoblar bir necha
daqiqagacha davom etishi mumkin, shuning uchun bu yerda timeout 20 soniyaga
oshirilgan - lekin baribir cheksiz osilib qolmasligi uchun chegara qo'yilgan.

ENG MUHIM QOIDA: Checkout.uz webhook'ida kriptografik imzo yo'q. Webhook
faqat "tekshirib ko'r" signali sifatida ishlatilishi kerak - haqiqat manbai
har doim shu moduldagi get_payment_status() funksiyasi orqali so'ralishi shart.
Bu qoida bot.py'dagi confirm_checkout_payment() funksiyasida amalga oshirilgan.
"""

import os
import logging

import aiohttp

logger = logging.getLogger(__name__)

CHECKOUT_API_BASE = os.getenv("CHECKOUT_API_BASE", "https://checkout.uz/api/v1")
CHECKOUT_API_KEY = os.getenv("CHECKOUT_API_KEY", "")

# Checkout.uz'ning o'zi belgilagan chegaralar
MIN_AMOUNT = 1000
MAX_AMOUNT = 10_000_000

DEFAULT_TIMEOUT_SEC = 20


class CheckoutError(Exception):
    """Checkout.uz bilan ishlashda yuzaga kelgan har qanday xato uchun."""
    pass


def _safe_int(value) -> int:
    """Checkout.uz ba'zan summani '1000.00' kabi (o'nlik nuqta bilan,
    matn ko'rinishida) qaytaradi - oddiy int() bunday holatda xato beradi.
    Bu funksiya butun son, o'nlik son, matn - barchasini xavfsiz qabul qiladi."""
    if value is None:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


async def _call(path: str, body: dict, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> dict:
    if not CHECKOUT_API_KEY:
        raise CheckoutError("CHECKOUT_API_KEY sozlanmagan (Render -> Environment)")

    url = f"{CHECKOUT_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {CHECKOUT_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=body or {},
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    raise CheckoutError(f"Checkout.uz noto'g'ri javob qaytardi (HTTP {resp.status})")

                if resp.status != 200:
                    msg = (data or {}).get("error") or f"HTTP {resp.status}"
                    if resp.status == 401:
                        raise CheckoutError(
                            "Checkout.uz: API kalit noto'g'ri yoki kassa hali Faol emas"
                        )
                    if resp.status == 403:
                        raise CheckoutError(
                            "Checkout.uz: IP ruxsat etilmagan. Kassa sozlamalarida "
                            "IP Whitelist'ni o'chiring."
                        )
                    raise CheckoutError(f"Checkout.uz: {msg}")

                return data
    except CheckoutError:
        raise
    except aiohttp.ClientError as e:
        raise CheckoutError(f"Checkout.uz'ga ulanib bo'lmadi: {e}")
    except Exception as e:
        logger.exception("Checkout.uz so'rovida kutilmagan xato")
        raise CheckoutError(f"Checkout.uz so'rovida kutilmagan xato: {e}")


async def create_payment(
    amount: int,
    description: str | None = None,
    webhook_url: str | None = None,
    return_url: str | None = None,
) -> dict:
    """Yangi to'lov havolasi (invoys) yaratadi.

    Qaytaradi: {"id": str, "uuid": str|None, "url": str, "amount": int, "lifetime_sec": int}
    """
    if not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
        raise CheckoutError(f"Summa {MIN_AMOUNT}-{MAX_AMOUNT} so'm oralig'ida bo'lishi kerak")

    body = {"amount": amount}
    if description:
        body["description"] = description
    if webhook_url:
        body["webhook_url"] = webhook_url
    if return_url:
        body["return_url"] = return_url

    data = await _call("/create_payment", body)
    p = (data or {}).get("payment") or {}

    if not p.get("_id") or not p.get("_url"):
        raise CheckoutError("Checkout.uz to'lov havolasini qaytarmadi")

    # Hujjatdagi maydon nomi "_lifteme" (ularning o'z imlo xatosi)
    lifetime_sec = 3600
    try:
        lifetime_sec = int((p.get("_lifteme") or {}).get("_second") or 3600)
    except Exception:
        pass

    return {
        "id": str(p["_id"]),
        "uuid": p.get("_uuid"),
        "url": p["_url"],
        "amount": _safe_int(p.get("_amount")) or amount,
        "lifetime_sec": lifetime_sec,
    }


async def get_payment_status(payment_id: str | None = None, payment_uuid: str | None = None) -> dict | None:
    """To'lov holatini Checkout.uz'ning O'ZIDAN so'raydi - bu HAQIQAT MANBAI.

    Webhook'dan kelgan ma'lumotga hech qachon ishonmang - har doim shu
    funksiya orqali tasdiqlang, chunki webhook'da imzo (signature) yo'q.

    Qaytaradi: {"id": str, "status": str, "amount": int, "paid_at": str|None} yoki None
    """
    if payment_uuid:
        body = {"uuid": payment_uuid}
    elif payment_id is not None:
        try:
            body = {"id": int(payment_id)}
        except (TypeError, ValueError):
            body = {"id": payment_id}
    else:
        raise CheckoutError("id yoki uuid berilishi shart")

    try:
        data = await _call("/status_payment", body)
    except CheckoutError as e:
        msg = str(e).lower()
        if "topilmadi" in msg or "not found" in msg or "404" in msg:
            return None
        raise

    d = (data or {}).get("data")
    if not d:
        return None

    return {
        "id": str(d.get("id")),
        "status": d.get("status"),
        "amount": _safe_int(d.get("amount")),
        "paid_at": d.get("paid_at"),
    }
