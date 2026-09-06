# Telegram Media Downloader Bot

Bu bot Telegramga yuborilgan ommaviy media linklarini yuklab, foydalanuvchiga qaytaradi.

Qo'llab-quvvatlanadigan manbalar: YouTube, Instagram, TikTok, Facebook, Pinterest va `yt-dlp` qo'llab-quvvatlaydigan ko'p boshqa ommaviy saytlar.

## Muhim eslatma

Botdan faqat o'zingizga tegishli yoki yuklab olishga ruxsat berilgan kontent uchun foydalaning. Bot private kontent, DRM himoyasi yoki kirish cheklovlarini aylanib o'tish uchun mo'ljallanmagan.

## Ishga tushirish

1. Python 3.11 yoki yangiroq versiyani o'rnating.
2. `ffmpeg` o'rnating. YouTube kabi saytlarda video va audioni birlashtirish uchun kerak bo'lishi mumkin.
3. Dependency fayllarni o'rnating:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

4. `.env.example` faylidan `.env` fayl yarating va BotFather bergan tokenni yozing:

```env
BOT_TOKEN=123456789:YOUR_REAL_TOKEN
ADMIN_IDS=123456789
MAX_FILE_MB=45
MAX_DAILY_DOWNLOADS=5
```

5. Botni ishga tushiring:

```powershell
python bot.py
```

## Sozlamalar

`MAX_FILE_MB` - Telegramga yuboriladigan faylning maksimal hajmi. Standart qiymat: `45`.

`MAX_CONCURRENT_DOWNLOADS` - bir vaqtda nechta yuklashga ruxsat berilishi. Standart qiymat: `2`.

`MAX_DAILY_DOWNLOADS` - oddiy foydalanuvchi kuniga nechta media yuklay olishi. Standart qiymat: `5`. `0` berilsa limit o'chadi.

`ADMIN_IDS` - admin Telegram ID raqamlari. Bir nechta admin bo'lsa vergul bilan yozing: `123,456`.

`DOWNLOAD_TIMEOUT_SECONDS` - bitta yuklash uchun vaqt chegarasi. Standart qiymat: `300`.

`LIMIT_TIMEZONE` - kunlik limit qaysi vaqt zonasi bo'yicha yangilanishi. Standart qiymat: `Asia/Tashkent`.

## Foydalanish

Telegramda botga `/start` yuboring, keyin bitta media link yuboring. Bot linkdan videoni yuklab, fayl sifatida qaytaradi.

## Admin komandalar

Avval Telegramda botga `/id` yuboring va chiqqan raqamni Railway `ADMIN_IDS` variable ichiga qo'ying.

`/admin` - admin panel.

`/stats` - bugungi statistika.

`/users` - oxirgi foydalanuvchilar.

`/setlimit 5` - kunlik limitni o'zgartirish. `0` = limitsiz.

`/resetlimit` - bugungi ishlatilgan limitlarni nol qilish.

## Hosting

Hostingga qo'yish uchun [DEPLOY.md](DEPLOY.md) faylini ko'ring. Loyiha `Dockerfile` bilan tayyorlangan, shuning uchun Railway kabi servislar uni avtomatik build qila oladi.
