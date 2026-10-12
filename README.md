# Frame AI - Social backend

Frontend (Vercel app) jo plan Drive mein likhta hai (`Frame AI/Social/Days/<date>.json`, `social_config.json`, `Brand/brand.json`),
yeh backend usay GitHub Actions par parh kar YouTube / Facebook / Instagram / TikTok par upload karta hai.

## Files (repo ki root mein, `gemini_client.py` ke saath rakho)
| File | Kaam |
|---|---|
| `social_runner.py` | Manager: din ki file parho, time chuno, upload, result wapas likho |
| `social_drive.py` | Drive/Sheets (wahi refresh token jo video runner use karta hai) |
| `social_media.py` | ffmpeg: logo, lower third, intro/outro/end card, reels, subtitles |
| `social_thumb.py` | Thumbnail: playlist template par title likhna |
| `social_ai.py` | Gemini: tags, hashtags, reel cuts, transcription (`gemini_client.Gemini` ke zariye, rotation wahi) |
| `social_platforms.py` | YouTube Data API + Buffer API |
| `.github/workflows/social-upload.yml` | Har 3 ghante chalta hai |
| `requirements-social.txt` | pip packages |
| `frontend_patch/drive.js` | 1 line: YouTube subtitles ke liye scope |

## Setup
1. Secrets: `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (pehle se). Naya: `GOOGLE_CLIENT_ID` (Secret ya Variable, dono chalte hain).
2. Frontend mein `src/drive.js` ki `connectYouTube` line badlo (`frontend_patch/drive.js`), redeploy, Social > Accounts mein YouTube dobara connect karo.
   (Iske baghair video upload hoga, sirf subtitles nahi lagenge; backend khud banner mein bata dega.)
3. Actions > "Frame AI - Social upload" > Run workflow. Pehli baar `dry_run` tick karke dekho kya due hai.

## Kaise chalta hai
- Sirf **aaj** ki date ke items jate hain (frontend purane ko "Missed" dikhata hai). Time `auto` hota hai: backend 10:00-22:00 ke beech har platform ke items barabar phailata hai (`limits.window_start/end` social_config.json mein badal sakte ho). Item agli run mein jata hai jab uska time guzar jaye.
- **Complete video** (YouTube, Facebook): logo (brand.json ki jagah par), lower third, intro, outro, end card, thumbnail. Ek baar banta hai, dono platforms use karte hain.
- **Reels**: ek baar cut hoti hain (blur / crop / pad, "Part n/N" label, reel logo), Drive `Social/Cache` mein save. Agli iteration/agle din wahan se uthti hain, dobara render nahi hoti. Sab platforms ek hi reel use karte hain. Video ke saare items upload ho jayen to cache delete.
- **Cut mode** "backend choose kare" (`auto`): Gemini transcript + audio ke real pauses dekh kar jumlon ke beech cut chunta hai. Warna equal / chapters / manual.
- **Tags/hashtags**: frontend ne jo likha wo pehle. Khali ho to Gemini YouTube tags aur hashtags banata hai (ek call har video/part ke liye, sab platforms ke kaam aati hai). `social_config.json` mein `"ai": {"hook": true}` se reels par ek hook line bhi lag sakti hai.
- **Subtitles** (YouTube complete video): sirf tayyar `Output/<naam>.srt` upload hoti hai (intro ki lambai ke hisaab se shift). File na mile to video bina subtitles ke jati hai, backend khud subtitle nahi banata.
- Jab backend ko aap se kuch chahiye (token expire, Buffer key/channel missing) to `backend_requests.json` mein likhta hai: frontend ka banner dikhata hai.
- Video ki playlist row `Completed` -> `Uploaded` jab uske saare planned items chale jayen.

## Dhyan do
- **YouTube quota**: default 10,000 units/din. Upload 1,600, thumbnail 50, subtitles 400. Backend units gin kar rukta hai.
- **YouTube audit**: 2020 ke baad bane Google Cloud projects ki API videos jab tak audit na ho **private** lock rehti hain. Production se pehle audit form bharo.
- **Buffer** file upload nahi leta, public URL leta hai. Backend Drive file ko post ke waqt "anyone with link" karta hai aur 3 ghante baad wapas private kar deta hai.
- Reels par subtitles burn nahi hote (abhi). Urdu "Part n/N" label Latin mein hai.
- Thumbnail ka font Google Fonts se utarta hai (Actions par internet hai).
