# HujjatBot — Telegram bot (test versiya)

Obyektivka (MA'LUMOTNOMA) va 3x4 rasm xizmati.

## Nima ishlayapti hozir

✅ **Obyektivka** — to'liq ishlaydi:
- Xodim / talaba turini tanlash
- Barcha savollar ketma-ket so'raladi
- Mehnat faoliyati — cheksiz qo'shish mumkin
- Yaqin qarindoshlari — cheksiz qo'shish mumkin
- Natija: **Word (.docx)** yoki **PDF** formatda darhol yuboriladi
- Hech qanday tashqi AI API kerak emas — hammasi kod orqali

⏳ **3x4 rasm** — hozircha placeholder (test bosqichida rasm qabul qilinadi, lekin tahrirlanmaydi). Keyingi qadam: remove.bg yoki OpenAI Image API ulash.

---

## O'zingizning kompyuteringizda sinash

### 1. Talab qilinadigan narsalar
- Python 3.10+
- LibreOffice (PDF konvertatsiya uchun) — Ubuntu/Debian: `sudo apt install libreoffice`
- Telegram bot tokeni — [@BotFather](https://t.me/BotFather) dan oling

### 2. O'rnatish
```bash
cd hujjatbot
pip install -r requirements.txt
```

### 3. Bot tokenini kiritish
```bash
export BOT_TOKEN="1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### 4. Ishga tushirish
```bash
python3 bot.py
```

Telegramda botingizni oching, `/start` bosing — tayyor!

---

## Fayllar tuzilishi

```
hujjatbot/
├── bot.py                # Asosiy bot logikasi (aiogram, savol-javob)
├── obyektivka_docx.py    # DOCX hujjat yasovchi modul
├── requirements.txt      # Python kutubxonalari
└── README.md
```

---

## Serverga joylashtirish (hosting)

Kapital sarflamasdan bepul variant — **Render.com**:

1. Kodni GitHub'ga yuklang
2. Render.com da "Background Worker" yarating, GitHub repo'ni ulang
3. Build command: `pip install -r requirements.txt`
4. Start command: `python3 bot.py`
5. Environment Variables bo'limiga `BOT_TOKEN` qo'shing
6. **Muhim:** PDF konvertatsiya uchun LibreOffice kerak — Render'ning standart image'ida yo'q. Buning uchun `Dockerfile` yozib, unga LibreOffice o'rnatish kerak bo'ladi (buyurtma bo'lsa keyingi bosqichda tayyorlab beraman).

Agar PDF kerak bo'lmasa (faqat Word yetarli bo'lsa), LibreOffice kerak emas — bot to'g'ridan-to'g'ri ishlayveradi.

---

## Keyingi qadamlar (kelishilgan reja bo'yicha)

1. ⏳ 3x4 rasm uchun AI API ulash (fon olib tashlash + kiyim/sifat)
2. ⏳ Dizaynni yaxshilash (siz chizib kelayotgan reja asosida)
3. ⏳ Serverga joylashtirish (Render.com yoki boshqa)
