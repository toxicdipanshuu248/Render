# ⚡ Telegram Hosting Bot — Render Free Tier (512 MB)

Telegram se hi apna Render server control karo — terminal commands, file upload,
auto dependency install, background apps — sab kuch. Owner approval + channel join
gate ke saath, sirf tumhari allow-list wale log hi use kar sakte hain.

## ✨ Features

| Feature | Detail |
|---|---|
| 👑 **Owner system** | `OWNER_ID` wala hi owner; baaki sab ko approval chahiye |
| ✅ **Approval gate** | Naya user `/start` kare → owner ko **Approve/Deny button** milta hai |
| 📢 **Channel gate** | Bot channel me **admin** hoga; join karne wale ko hi entry |
| 💻 **Terminal** | Koi bhi command bhejo — hacker-style `root@render:~#` output |
| 📁 **Smart installer** | `requirements.txt` → auto `pip install` · `package.json` → auto `npm install` · `.zip` → auto extract |
| 🚀 **Background apps** | `/run python3 app.py` — 24x7 chalta rahe; `/ps`, `/logs`, `/kill` |
| 🔒 **Safe by design** | Har user apne workspace folder me band; `BOT_TOKEN` user commands me hidden |
| 🟢 **24x7 keep-alive** | Flask server + UptimeRobot ping = free tier sleep nahi karta |

---

## 🚀 Deploy — Step by Step

### 1️⃣ Telegram bot banao
- Telegram me [@BotFather](https://t.me/BotFather) ke paas jao → `/newbot` → naam do → **BOT_TOKEN** mil jayega.

### 2️⃣ Apni Telegram ID nikalo
- [@userinfobot](https://t.me/userinfobot) ko `/start` karo — tumhari numeric ID mil jayegi (ye `OWNER_ID` hai).

### 3️⃣ Channel banao (optional)
- Ek channel banao (jaise `@myhostingchannel`).
- Apne bot ko channel me **Administrator** banao (koi bhi admin right chalegi, e.g. "Post Messages").
- ⚠️ Bot admin nahi hoga to membership check fail hoga aur koi entry nahi milegi.

### 4️⃣ Render pe deploy
**Sabse easy tarika (Blueprint):**
1. In saari files ko ek GitHub repo me push karo.
2. [Render.com](https://render.com) → **New +** → **Blueprint** → repo select karo.
3. `render.yaml` apne aap detect ho jayega.
4. **Credentials pehle se `bot.py` me hard-coded hain** (BOT_TOKEN + OWNER_ID) — isliye
   env vars chhod bhi do to bot chal jayega. Optional vars:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | (already hard-coded — override karna ho to dalo) |
| `OWNER_ID` | (already hard-coded) |
| `CHANNEL_ID` | (optional) `@yourchannel` — ya baad me bot me `/setchannel` se lagao |
| `ALLOWED_USERS` | (optional) `111111,222222` — redeploy ke baad bhi approved rakhne ke liye |

> 🔐 **Zaroori:** GitHub repo **PRIVATE** rakho — usme bot token hard-coded hai.
> Galti se public ho jaye to BotFather se `/revoke` karke naya token banao.

**Manual tarika:** New + → **Web Service** → repo → Environment **Python 3** →
Build: `pip install -r requirements.txt` → Start: `python bot.py` → Plan: **Free**.

> **Node.js/npm bhi chahiye?** (package.json auto-install ke liye) Render ke Python
> image me Node nahi hota. Service ka runtime **Docker** choose karo — repo me `Dockerfile`
> already hai jisme Python + Node dono hain.

### 5️⃣ UptimeRobot se 24x7 active rakho
Render free service **15 minute** koi request na aane to so jaati hai. UptimeRobot use jaga deta hai:
1. [uptimerobot.com](https://uptimerobot.com) pe free account banao.
2. **+ Add New Monitor** → type **HTTP(s)**.
3. URL me apni Render site ka URL daalo (jaise `https://telegram-hosting-bot.onrender.com`).
4. Interval: **5 minutes** → save.
5. Bas — bot 24x7 online rahega. ✅

---

## 📱 Bot commands

**Har user:**
- `/start` — access check + welcome
- `/help` — commands list
- `/id` — apni Telegram ID
- Koi bhi text = terminal command: `ls -la`, `python3 main.py`, `pip install flask` ...
- File bhejo = upload (requirements/package.json/zip auto-setup)
- `cd <folder>` — directory badlo (workspace ke andar hi)
- `/run <cmd>` — background me chalu karo (servers/bots ke liye)
- `/ps` — running background processes
- `/logs <id> [lines]` — process ka output
- `/kill <id>` — process band karo

**Sirf owner:**
- `/approve <user_id>` — user approve (notification pe button bhi aata hai)
- `/unapprove <user_id>` — access wapas lo
- `/users` — approved list
- `/setchannel @channel` — channel gate lagao / badlo
- `/channel` — current channel dekho

---

## ⚠️ Important notes (free tier)

1. **Disk ephemeral hai** — Render restart/redeploy pe `workspaces/` aur `data/` wipe ho
   jaate hain (approved list bhi reset). Permanent approval chahiye to IDs ko
   `ALLOWED_USERS` env var me daal do.
2. **Upload limit ~20 MB** — Telegram bot API ki limit hai. Bade projects zip karke bhejo.
3. **RAM 512 MB** — ek waqt me bhaari builds (npm/bade pip) OOM kar sakti hain; isiliye
   Node heap 256 MB cap hai aur pip cache off hai.
4. **Sirf bharose wale logon ko approve karo** — approved user ko shell milta hai.
   Bot token env se chhupaya hua hai, phir bhi approval apni zimmedari hai.
5. Background apps ke liye sirf Render ka `$PORT` internet se khulta hai — andar ki
   ports (Flask ke alawa) bahar se accessible nahi hoti, lekin bots/scripts aaram se chalte hain.
