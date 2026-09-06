# Hostingga qo'yish

Eng sodda variant: Railway. Bot webhook emas, polling bilan ishlaydi, shuning uchun web port kerak emas.

## Railway

1. Kodni GitHub repositoryga yuklang.
2. Railway oching va yangi project yarating.
3. `Deploy from GitHub repo` ni tanlang.
4. Shu repositoryni ulang.
5. Railway rootdagi `Dockerfile` ni avtomatik topib build qiladi.
6. Service `Variables` bo'limiga token va sozlamalarni qo'shing:

```env
BOT_TOKEN=YANGI_TOKENNI_SHU_YERGA_QOYING
ADMIN_IDS=TELEGRAM_ID_RAQAMINGIZ
MAX_FILE_MB=45
MAX_CONCURRENT_DOWNLOADS=2
MAX_DAILY_DOWNLOADS=5
DOWNLOAD_TIMEOUT_SECONDS=300
AUDIO_FORMAT=mp3
AUDIO_QUALITY_KBPS=192
LIMIT_TIMEZONE=Asia/Tashkent
LOG_LEVEL=INFO
```

7. Deploy qiling va logs ichida `Bot ishga tushdi.` yozuvini tekshiring.

Admin ID olish uchun botga `/id` yuboring. Chiqqan raqamni Railway `ADMIN_IDS` ichiga yozing, keyin deployni restart qiling.

## Muhim

`.env` faylni GitHubga yuklamang. Token faqat hosting Variables/Environment bo'limida saqlansin.

Instagram, Facebook, TikTok va boshqa servislar vaqti-vaqti bilan yuklashni cheklashi mumkin. Bunday holatda `yt-dlp` versiyasini yangilash yoki cookie/login talab qiladigan kontentdan voz kechish kerak bo'ladi.
