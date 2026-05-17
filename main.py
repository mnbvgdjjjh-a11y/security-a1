# ══════════════════════════════════════════════════════════════════
#  Security Bot  –  บ้านมงคล
#  pip install discord.py aiohttp
#
#  ENV:
#    DISCORD_TOKEN  – token บอท
#    API_BASE_URL   – URL เว็บ (เช่น https://yourapp.railway.app)
#    PORT           – port web server (default 8080)
#    DATA_SERVER_ID – ID ของ Server หลักที่เก็บข้อมูลทุก guild
# ══════════════════════════════════════════════════════════════════

import os, json, asyncio, io, secrets, logging, re
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import discord
from discord.ext import tasks
from aiohttp import web

# ── ENV ────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("DISCORD_TOKEN", "")
API_BASE_URL   = os.environ.get("API_BASE_URL", "http://localhost:8080")
PORT           = int(os.environ.get("PORT", "8080"))
DATA_SERVER_ID = int(os.environ.get("DATA_SERVER_ID", "0"))   # ← ใส่ ID server หลัก

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("SecurityBot")

# ── REGEX ──────────────────────────────────────────────────────────
RE_LINK    = re.compile(r"https?://\S+", re.I)
RE_INVITE  = re.compile(r"discord(?:\.gg|app\.com/invite)/\S+", re.I)
RE_CAPS    = re.compile(r"[A-Z]")

# ══════════════════════════════════════════════════════════════════
#  BOT
# ══════════════════════════════════════════════════════════════════
intents = discord.Intents.all()

class SecurityBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.guild_data: dict     = {}                           # guild_id → config
        self.active_tokens: dict  = {}                           # token → info
        self.data_lock             = asyncio.Lock()
        self.heat: dict            = defaultdict(list)           # uid → [timestamps]
        self.join_tracker: dict    = defaultdict(list)           # guild_id → [timestamps]
        self.raid_mode: set        = set()
        self.nuke_track: dict      = defaultdict(lambda: defaultdict(list))

bot = SecurityBot()

# ══════════════════════════════════════════════════════════════════
#  DEFAULT CONFIG
# ══════════════════════════════════════════════════════════════════
def default_config():
    return {
        "automod": {
            "enabled":        False,
            "banned_words":   [],
            "filter_links":   False,
            "filter_invites": False,
            "filter_caps":    False,
            "filter_emoji":   False,
            "bypass_roles":   [],
        },
        "antispam": {
            "enabled":       False,
            "max_messages":  5,
            "interval":      5,
            "mute_duration": 5,
            "max_emoji":     10,
        },
        "antiraid": {
            "enabled":         False,
            "join_threshold":  10,
            "min_account_age": 7,
        },
        "antinuke": {
            "enabled":       False,
            "channel_limit": 3,
            "role_limit":    3,
            "ban_limit":     5,
        },
        "joingate": {
            "enabled":          False,
            "min_account_age":  7,
            "block_no_avatar":  False,
        },
        "verification": {
            "enabled":          False,
            "verified_role_id": None,
        },
        "welcome": {
            "enabled":    False,
            "channel_id": None,
            "message":    "ยินดีต้อนรับ {user} สู่ {server}! 🎉",
        },
        "log_channel_id":  None,
        "data_channel_id": None,
    }

def get_cfg(guild_id: int) -> dict:
    if guild_id not in bot.guild_data:
        bot.guild_data[guild_id] = default_config()
    return bot.guild_data[guild_id]

# ══════════════════════════════════════════════════════════════════
#  DATA CHANNEL SYSTEM  (เก็บใน SERVER หลักทั้งหมด)
#  ห้องชื่อ  💾・{guild_id}  ใน DATA_SERVER_ID
#  แต่ละห้องเก็บ data.json ของ guild นั้น
# ══════════════════════════════════════════════════════════════════
DATA_CH_PREFIX = "💾・"

async def get_data_server() -> discord.Guild | None:
    if not DATA_SERVER_ID:
        return None
    return bot.get_guild(DATA_SERVER_ID)

async def ensure_data_channel(guild_id: int) -> discord.TextChannel | None:
    """สร้าง / หาห้องเก็บข้อมูลของ guild ใน data server"""
    ds = await get_data_server()
    if not ds:
        log.warning("DATA_SERVER_ID ไม่ถูกต้องหรือบอทไม่ได้อยู่ใน server นั้น")
        return None

    ch_name = f"{DATA_CH_PREFIX}{guild_id}"

    # หาจากชื่อ
    for ch in ds.text_channels:
        if ch.name == ch_name:
            return ch

    # สร้างใหม่ — ซ่อนจากทุกคน เห็นแค่บอท
    try:
        ow = {
            ds.default_role: discord.PermissionOverwrite(read_messages=False),
            ds.me: discord.PermissionOverwrite(
                read_messages=True, send_messages=True, manage_messages=True
            ),
        }
        ch = await ds.create_text_channel(ch_name, overwrites=ow, reason="Security Bot: data channel")
        log.info(f"✅ สร้างห้อง {ch_name} ใน {ds.name}")
        return ch
    except Exception as e:
        log.error(f"❌ สร้างห้องไม่ได้: {e}")
        return None

async def load_guild_data(guild_id: int):
    """โหลด data.json จาก data server"""
    try:
        ch = await ensure_data_channel(guild_id)
        if not ch:
            return
        async for msg in ch.history(limit=20):
            for att in msg.attachments:
                if att.filename == "data.json":
                    raw = await att.read()
                    bot.guild_data[guild_id] = json.loads(raw.decode())
                    log.info(f"✅ โหลดข้อมูล guild {guild_id}")
                    return
    except Exception as e:
        log.error(f"❌ โหลดข้อมูล guild {guild_id}: {e}")

async def save_guild_data(guild_id: int):
    """บันทึก data.json ไปยัง data server"""
    async with bot.data_lock:
        try:
            ch = await ensure_data_channel(guild_id)
            if not ch:
                return
            await ch.purge(limit=20, check=lambda m: m.author == bot.user)
            raw = json.dumps(get_cfg(guild_id), ensure_ascii=False, indent=2)
            f = discord.File(io.BytesIO(raw.encode()), filename="data.json")
            await ch.send(f"💾 guild:{guild_id}", file=f)
        except Exception as e:
            log.error(f"❌ บันทึก guild {guild_id}: {e}")

@tasks.loop(minutes=5)
async def auto_save():
    for guild in bot.guilds:
        await save_guild_data(guild.id)

# ══════════════════════════════════════════════════════════════════
#  TOKEN MANAGER
# ══════════════════════════════════════════════════════════════════
def create_token(guild_id: int, guild_name: str) -> str:
    for t, v in list(bot.active_tokens.items()):
        if v["guild_id"] == guild_id:
            del bot.active_tokens[t]
    token = secrets.token_urlsafe(24)
    bot.active_tokens[token] = {
        "guild_id":   guild_id,
        "guild_name": guild_name,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    return token

def verify_token(token: str) -> dict | None:
    d = bot.active_tokens.get(token)
    if not d:
        return None
    if datetime.now(timezone.utc) > d["expires_at"]:
        del bot.active_tokens[token]
        return None
    return d

@tasks.loop(minutes=1)
async def cleanup_tokens():
    now = datetime.now(timezone.utc)
    for t in [t for t, v in list(bot.active_tokens.items()) if now > v["expires_at"]]:
        del bot.active_tokens[t]

# ══════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    log.info(f"🤖 {bot.user} ออนไลน์")
    ds = await get_data_server()
    if ds:
        log.info(f"📦 Data server: {ds.name} ({ds.id})")
    else:
        log.warning("⚠️  DATA_SERVER_ID ไม่ได้ตั้งค่า — จะไม่มีการบันทึก/โหลดข้อมูล")

    for guild in bot.guilds:
        await load_guild_data(guild.id)

    auto_save.start()
    cleanup_tokens.start()
    log.info(f"✅ พร้อมใช้งาน — {len(bot.guilds)} server(s)")

@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info(f"📥 เข้า server: {guild.name}")
    await ensure_data_channel(guild.id)
    await save_guild_data(guild.id)

# ══════════════════════════════════════════════════════════════════
#  !getcode
# ══════════════════════════════════════════════════════════════════
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # ── !getcode ──
    if message.content.strip().lower() == "!getcode":
        if message.guild.owner_id != message.author.id:
            r = await message.reply("❌ เฉพาะเจ้าของ Server เท่านั้น")
            await asyncio.sleep(5)
            try: await r.delete()
            except: pass
            return
        token = create_token(message.guild.id, message.guild.name)
        try:
            await message.author.send(
                f"🔐 **รหัสเข้าสู่ระบบ Security Bot**\n"
                f"```{token}```\n"
                f"⏰ หมดอายุใน 10 นาที\n"
                f"🌐 เปิดเว็บ: {API_BASE_URL}"
            )
            r = await message.reply("📨 ส่ง DM ให้คุณแล้วครับ 🔐")
            await asyncio.sleep(5)
            try: await r.delete()
            except: pass
        except discord.Forbidden:
            await message.reply("❌ ไม่สามารถส่ง DM ได้ กรุณาเปิดรับ DM ก่อน", delete_after=10)
        return

    # ── AutoMod ──
    cfg = get_cfg(message.guild.id)
    am  = cfg["automod"]
    if am["enabled"]:
        author_roles = [r.id for r in getattr(message.author, "roles", [])]
        bypass       = [int(r) for r in am.get("bypass_roles", []) if r]
        if not any(r in bypass for r in author_roles):
            content = message.content
            cl      = content.lower()

            # คำต้องห้าม
            for word in am.get("banned_words", []):
                if word and word.lower() in cl:
                    try: await message.delete()
                    except: pass
                    await message.channel.send(
                        f"⚠️ {message.author.mention} ข้อความถูกลบ (คำต้องห้าม)",
                        delete_after=5,
                    )
                    return

            # กรองลิงก์
            if am.get("filter_links") and RE_LINK.search(content):
                try: await message.delete()
                except: pass
                await message.channel.send(
                    f"🔗 {message.author.mention} ไม่อนุญาตให้ส่งลิงก์",
                    delete_after=5,
                )
                return

            # กรองลิงก์เชิญ Discord
            if am.get("filter_invites") and RE_INVITE.search(content):
                try: await message.delete()
                except: pass
                await message.channel.send(
                    f"🚫 {message.author.mention} ไม่อนุญาตให้ส่งลิงก์เชิญ Discord",
                    delete_after=5,
                )
                return

            # กรอง CAPS (>70% ตัวพิมพ์ใหญ่ และยาวกว่า 8 ตัว)
            if am.get("filter_caps"):
                letters = [c for c in content if c.isalpha()]
                if len(letters) > 8 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7:
                    try: await message.delete()
                    except: pass
                    await message.channel.send(
                        f"🔠 {message.author.mention} กรุณาพิมพ์ตัวปกติ",
                        delete_after=5,
                    )
                    return

            # กรองอีโมจิสแปม
            if am.get("filter_emoji"):
                emoji_count = (
                    content.count("<:") + content.count("<a:")
                    + sum(1 for c in content if ord(c) > 127000)
                )
                if emoji_count > cfg["antispam"]["max_emoji"]:
                    try: await message.delete()
                    except: pass
                    return

    # ── Anti-Spam ──
    sp = cfg["antispam"]
    if sp["enabled"]:
        uid      = message.author.id
        now      = datetime.now(timezone.utc).timestamp()
        interval = sp["interval"]
        bot.heat[uid] = [t for t in bot.heat[uid] if now - t < interval]
        bot.heat[uid].append(now)
        if len(bot.heat[uid]) > sp["max_messages"]:
            mute_min = sp.get("mute_duration", 5)
            try:
                await message.author.timeout(timedelta(minutes=mute_min), reason="Anti-Spam")
                await message.channel.send(
                    f"🔇 {message.author.mention} ถูก mute {mute_min} นาที (spam)",
                    delete_after=10,
                )
            except: pass

# ══════════════════════════════════════════════════════════════════
#  ANTI-RAID
# ══════════════════════════════════════════════════════════════════
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    cfg   = get_cfg(guild.id)

    # Welcome
    wl = cfg["welcome"]
    if wl["enabled"] and wl.get("channel_id"):
        ch = guild.get_channel(int(wl["channel_id"]))
        if ch:
            msg = (
                wl["message"]
                .replace("{user}", member.mention)
                .replace("{server}", guild.name)
                .replace("{count}", str(guild.member_count))
            )
            embed = discord.Embed(description=msg, color=0x5865F2)
            embed.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=embed)

    # Join Gate
    jg = cfg["joingate"]
    if jg["enabled"]:
        age_days = (datetime.now(timezone.utc) - member.created_at).days
        if age_days < jg["min_account_age"]:
            try:
                await member.send(f"❌ บัญชีของคุณอายุน้อยเกินไป ({age_days} วัน)")
                await member.kick(reason=f"บัญชีอายุน้อย: {age_days} วัน")
            except: pass
            return
        if jg["block_no_avatar"] and member.avatar is None:
            try: await member.kick(reason="ไม่มีรูปโปรไฟล์")
            except: pass
            return

    # Anti-Raid
    ar = cfg["antiraid"]
    if ar["enabled"]:
        now = datetime.now(timezone.utc).timestamp()
        bot.join_tracker[guild.id].append(now)
        bot.join_tracker[guild.id] = [t for t in bot.join_tracker[guild.id] if now - t < 60]
        if len(bot.join_tracker[guild.id]) >= ar["join_threshold"]:
            if guild.id not in bot.raid_mode:
                bot.raid_mode.add(guild.id)
                log_id = cfg.get("log_channel_id")
                if log_id:
                    ch = guild.get_channel(int(log_id))
                    if ch:
                        await ch.send("🚨 **RAID DETECTED** — Raid Mode เปิดแล้ว (ปิดอัตโนมัติใน 10 นาที)")
                asyncio.create_task(disable_raid_mode(guild.id))
        if guild.id in bot.raid_mode:
            age_days = (datetime.now(timezone.utc) - member.created_at).days
            if age_days < ar["min_account_age"]:
                try: await member.kick(reason="Raid Mode: บัญชีใหม่")
                except: pass

    # Log
    await send_log(guild, discord.Embed(
        title="📥 สมาชิกเข้าร่วม",
        description=f"{member.mention} ({member})\nบัญชีสร้างเมื่อ: {member.created_at.strftime('%d/%m/%Y')}",
        color=0x3fb950,
    ).set_thumbnail(url=member.display_avatar.url))

async def disable_raid_mode(guild_id: int):
    await asyncio.sleep(600)
    bot.raid_mode.discard(guild_id)
    guild = bot.get_guild(guild_id)
    if guild:
        log_id = get_cfg(guild_id).get("log_channel_id")
        if log_id:
            ch = guild.get_channel(int(log_id))
            if ch: await ch.send("✅ Raid Mode ปิดแล้ว")

# ══════════════════════════════════════════════════════════════════
#  ANTI-NUKE
# ══════════════════════════════════════════════════════════════════
async def check_nuke(guild: discord.Guild, user: discord.Member, action: str):
    cfg = get_cfg(guild.id)
    if not cfg["antinuke"]["enabled"] or user.bot:
        return
    now    = datetime.now(timezone.utc).timestamp()
    track  = bot.nuke_track[guild.id][user.id]
    track.append((action, now))
    recent = [(a, t) for a, t in track if now - t < 10]
    bot.nuke_track[guild.id][user.id] = recent
    counts = {"channel": 0, "role": 0, "ban": 0}
    for a, _ in recent:
        if a in counts: counts[a] += 1
    limits = {
        "channel": cfg["antinuke"]["channel_limit"],
        "role":    cfg["antinuke"]["role_limit"],
        "ban":     cfg["antinuke"]["ban_limit"],
    }
    for a, limit in limits.items():
        if counts[a] >= limit:
            try:
                await user.edit(roles=[], reason="Anti-Nuke: ถอด role")
                await guild.ban(user, reason=f"Anti-Nuke: {a} เกิน {limit}x ใน 10 วิ")
            except: pass
            await send_log(guild, discord.Embed(
                title="🚨 Anti-Nuke ทำงาน",
                description=f"{user.mention} ถูก ban ({a} เกินกำหนด)",
                color=0xf85149,
            ))
            return

@bot.event
async def on_guild_channel_delete(channel):
    async for entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
        await check_nuke(channel.guild, entry.user, "channel")

@bot.event
async def on_guild_role_delete(role):
    async for entry in role.guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
        await check_nuke(role.guild, entry.user, "role")

@bot.event
async def on_member_ban(guild, user):
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
        await check_nuke(guild, entry.user, "ban")

# ══════════════════════════════════════════════════════════════════
#  LOGS
# ══════════════════════════════════════════════════════════════════
async def send_log(guild: discord.Guild, embed: discord.Embed):
    log_id = get_cfg(guild.id).get("log_channel_id")
    if not log_id: return
    ch = guild.get_channel(int(log_id))
    if ch:
        embed.timestamp = datetime.now(timezone.utc)
        try: await ch.send(embed=embed)
        except: pass

@bot.event
async def on_member_remove(member):
    await send_log(member.guild, discord.Embed(
        title="📤 สมาชิกออกจาก Server",
        description=f"{member.mention} ({member})",
        color=0xf85149,
    ))

@bot.event
async def on_message_delete(message):
    if message.author.bot: return
    await send_log(message.guild, discord.Embed(
        title="🗑️ ลบข้อความ",
        description=f"**{message.author}** ใน {message.channel.mention}\n{message.content[:500] or '(ไม่มีข้อความ)'}",
        color=0xd29922,
    ))

@bot.event
async def on_message_edit(before, after):
    if before.author.bot or before.content == after.content: return
    await send_log(before.guild, discord.Embed(
        title="✏️ แก้ไขข้อความ",
        description=f"**{before.author}** ใน {before.channel.mention}\n**ก่อน:** {before.content[:200]}\n**หลัง:** {after.content[:200]}",
        color=0x5865F2,
    ))

# ══════════════════════════════════════════════════════════════════
#  WEB API
# ══════════════════════════════════════════════════════════════════
CORS = {"Access-Control-Allow-Origin": "*"}

def jres(data, status=200):
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        status=status,
        headers={**CORS, "Content-Type": "application/json"},
    )

async def api_verify(req):
    t = req.rel_url.query.get("token", "")
    d = verify_token(t)
    if not d: return jres({"valid": False, "message": "รหัสไม่ถูกต้องหรือหมดอายุ"}, 401)
    return jres({"valid": True, "guild_id": str(d["guild_id"]), "guild_name": d["guild_name"]})

async def api_get_config(req):
    t = req.rel_url.query.get("token", "")
    d = verify_token(t)
    if not d: return jres({"error": "unauthorized"}, 401)
    return jres(get_cfg(d["guild_id"]))

async def api_post_config(req):
    t = req.rel_url.query.get("token", "")
    d = verify_token(t)
    if not d: return jres({"error": "unauthorized"}, 401)
    try:
        new = await req.json()
        cfg = get_cfg(d["guild_id"])
        for k, v in new.items():
            if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        await save_guild_data(d["guild_id"])
        return jres({"success": True})
    except Exception as e:
        return jres({"error": str(e)}, 400)

async def api_stats(req):
    t = req.rel_url.query.get("token", "")
    d = verify_token(t)
    if not d: return jres({"error": "unauthorized"}, 401)
    guild = bot.get_guild(d["guild_id"])
    if not guild: return jres({"error": "guild not found"}, 404)
    online = sum(1 for m in guild.members if m.status != discord.Status.offline and not m.bot)
    return jres({
        "guild_name":    guild.name,
        "server_id":     str(guild.id),
        "member_count":  guild.member_count,
        "online_count":  online,
        "channel_count": len(guild.channels),
        "role_count":    len(guild.roles),
        "icon_url":      str(guild.icon.url) if guild.icon else "",
    })

async def api_logs(req):
    """ดึง audit log ล่าสุด"""
    t = req.rel_url.query.get("token", "")
    d = verify_token(t)
    if not d: return jres({"error": "unauthorized"}, 401)
    guild = bot.get_guild(d["guild_id"])
    if not guild: return jres({"error": "guild not found"}, 404)
    logs = []
    try:
        async for entry in guild.audit_logs(limit=50):
            logs.append({
                "action":    str(entry.action).replace("AuditLogAction.", ""),
                "user":      str(entry.user),
                "target":    str(entry.target) if entry.target else "-",
                "reason":    entry.reason or "-",
                "timestamp": entry.created_at.isoformat(),
            })
    except Exception as e:
        return jres({"error": str(e)}, 500)
    return jres(logs)

async def api_options(req):
    return web.Response(status=200, headers={
        **CORS,
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    })

# ══════════════════════════════════════════════════════════════════
#  DASHBOARD HTML  (Single Page App)
# ══════════════════════════════════════════════════════════════════
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"/>
<title>Security Bot</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<style>
:root{
  --bg:#080c10;
  --surface:#0e1420;
  --surface2:#141b27;
  --border:#1e2a3a;
  --border2:#2a3a50;
  --text:#e8edf5;
  --muted:#5a7090;
  --muted2:#7a90aa;
  --primary:#3b82f6;
  --primary-dim:rgba(59,130,246,.15);
  --success:#10b981;
  --success-dim:rgba(16,185,129,.15);
  --danger:#ef4444;
  --danger-dim:rgba(239,68,68,.12);
  --warn:#f59e0b;
  --warn-dim:rgba(245,158,11,.12);
  --sidebar:200px;
  --nav-h:64px;
  --radius:10px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html{height:100%;}
body{
  font-family:'IBM Plex Sans Thai',sans-serif;
  background:var(--bg);color:var(--text);
  min-height:100%;overflow-x:hidden;font-size:14px;line-height:1.5;
}

/* ── SCROLLBAR ── */
::-webkit-scrollbar{width:4px;height:4px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}

/* ── UTILS ── */
.hidden{display:none!important;}
.mono{font-family:'JetBrains Mono',monospace;}

/* ── ANIMATIONS ── */
@keyframes fadeUp{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes shimmer{0%{background-position:-600px 0}100%{background-position:600px 0}}
@keyframes slideIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}

/* ── SKELETON ── */
.skeleton{
  background:linear-gradient(90deg,var(--surface) 25%,var(--surface2) 50%,var(--surface) 75%);
  background-size:600px 100%;
  animation:shimmer 1.6s infinite;
  border-radius:6px;
  color:transparent!important;pointer-events:none;
  user-select:none;
}

/* ── LOGIN ── */
#login-view{
  position:fixed;inset:0;z-index:1000;
  display:flex;align-items:center;justify-content:center;
  background:var(--bg);
  background-image:
    radial-gradient(ellipse 60% 50% at 50% -10%,rgba(59,130,246,.12),transparent),
    linear-gradient(var(--border) 1px,transparent 1px),
    linear-gradient(90deg,var(--border) 1px,transparent 1px);
  background-size:auto,40px 40px,40px 40px;
}
.login-box{
  width:100%;max-width:380px;padding:0 24px;
  display:flex;flex-direction:column;gap:20px;
  animation:fadeUp .6s cubic-bezier(.16,1,.3,1) both;
}
.login-logo{
  display:flex;align-items:center;gap:12px;
  margin-bottom:4px;
}
.login-logo-icon{
  width:44px;height:44px;
  background:linear-gradient(135deg,var(--primary),#60a5fa);
  border-radius:12px;display:flex;align-items:center;justify-content:center;
  font-size:22px;box-shadow:0 0 24px rgba(59,130,246,.4);
}
.login-title{font-size:22px;font-weight:700;letter-spacing:-.4px;}
.login-sub{font-size:13px;color:var(--muted2);}
.login-input{
  width:100%;background:var(--surface);
  border:1.5px solid var(--border);border-radius:var(--radius);
  color:var(--text);padding:13px 16px;font-size:14px;
  font-family:'IBM Plex Sans Thai',sans-serif;
  outline:none;transition:border-color .2s,box-shadow .2s;
}
.login-input:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(59,130,246,.15);}
.login-input::placeholder{color:var(--muted);}
.login-btn{
  width:100%;padding:13px;
  background:var(--primary);color:#fff;border:none;
  border-radius:var(--radius);font-size:14px;font-weight:600;
  font-family:'IBM Plex Sans Thai',sans-serif;cursor:pointer;
  transition:opacity .2s,transform .15s;display:flex;align-items:center;justify-content:center;gap:8px;
}
.login-btn:hover{opacity:.9;transform:translateY(-1px);}
.login-btn:disabled{opacity:.5;cursor:not-allowed;transform:none;}
.login-error{
  color:var(--danger);font-size:13px;text-align:center;min-height:18px;
}
.login-hint{font-size:12px;color:var(--muted);text-align:center;}
.login-hint code{
  background:var(--surface2);padding:2px 6px;border-radius:4px;
  font-family:'JetBrains Mono',monospace;color:var(--primary);
}

/* ── LAYOUT ── */
#app{display:none;min-height:100vh;}

/* Desktop sidebar */
.sidebar{
  position:fixed;left:0;top:0;bottom:0;
  width:var(--sidebar);background:var(--surface);
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;z-index:200;
  transition:transform .3s cubic-bezier(.22,1,.36,1);
}
.sidebar-head{
  padding:20px 16px 16px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;
}
.s-logo{
  width:32px;height:32px;
  background:linear-gradient(135deg,var(--primary),#60a5fa);
  border-radius:8px;display:flex;align-items:center;justify-content:center;
  font-size:16px;flex-shrink:0;
}
.s-title{font-size:13px;font-weight:700;}
.s-sub{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.sidebar-nav{flex:1;padding:12px 8px;display:flex;flex-direction:column;gap:2px;}
.nav-item{
  display:flex;align-items:center;gap:10px;
  padding:9px 12px;border-radius:8px;
  color:var(--muted2);font-size:13px;font-weight:500;
  cursor:pointer;transition:background .15s,color .15s;
  border:1px solid transparent;
}
.nav-item:hover{background:var(--surface2);color:var(--text);}
.nav-item.active{
  background:var(--primary-dim);color:var(--primary);
  border-color:rgba(59,130,246,.2);
}
.nav-icon{font-size:15px;width:20px;text-align:center;flex-shrink:0;}
.sidebar-foot{
  padding:16px;border-top:1px solid var(--border);
}
.server-chip{
  display:flex;align-items:center;gap:8px;
  padding:10px 12px;background:var(--surface2);
  border:1px solid var(--border);border-radius:8px;
  margin-bottom:10px;
}
.server-av{
  width:30px;height:30px;border-radius:50%;flex-shrink:0;
  background:linear-gradient(135deg,var(--primary),var(--success));
  display:flex;align-items:center;justify-content:center;
  font-size:12px;font-weight:700;overflow:hidden;
}
.server-av img{width:100%;height:100%;object-fit:cover;}
.server-name{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.server-id{font-size:10px;color:var(--muted);}
.btn-logout{
  width:100%;background:transparent;border:1px solid var(--border);
  border-radius:8px;padding:9px;color:var(--muted2);
  font-family:'IBM Plex Sans Thai',sans-serif;font-size:12px;
  cursor:pointer;transition:border-color .2s,color .2s,background .2s;
  display:flex;align-items:center;justify-content:center;gap:6px;
}
.btn-logout:hover{border-color:var(--danger);color:var(--danger);background:var(--danger-dim);}

/* Main content */
.main{
  margin-left:var(--sidebar);
  padding:32px 36px 80px;
  max-width:calc(var(--sidebar) + 760px);
  min-height:100vh;
}

/* Mobile bottom nav */
.bottom-nav{
  display:none;position:fixed;bottom:0;left:0;right:0;
  background:var(--surface);border-top:1px solid var(--border);
  z-index:200;
}
.bottom-nav-inner{display:flex;height:var(--nav-h);}
.bn-item{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;color:var(--muted);font-size:10px;font-weight:500;
  cursor:pointer;transition:color .15s;min-height:44px;border:none;background:none;
  font-family:'IBM Plex Sans Thai',sans-serif;
}
.bn-item.active{color:var(--primary);}
.bn-icon{font-size:18px;}

/* hamburger */
.hamburger{
  display:none;position:fixed;top:12px;left:12px;z-index:400;
  width:40px;height:40px;background:var(--surface);border:1px solid var(--border);
  border-radius:8px;align-items:center;justify-content:center;
  cursor:pointer;font-size:18px;color:var(--text);
}
.sidebar-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:150;
}
.sidebar-overlay.show{display:block;}

/* ── TABS ── */
.tab{display:none;flex-direction:column;gap:20px;animation:fadeIn .3s ease;}
.tab.active{display:flex;}

/* ── PAGE HEADER ── */
.page-hd{margin-bottom:4px;}
.page-hd h1{font-size:20px;font-weight:700;letter-spacing:-.3px;}
.page-hd p{font-size:13px;color:var(--muted2);margin-top:2px;}

/* ── STAT CARDS ── */
.stats-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;}
.stat-card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:18px 20px;
  transition:border-color .2s,transform .15s;
  position:relative;overflow:hidden;
}
.stat-card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--accent,var(--primary));opacity:.7;
}
.stat-card:hover{border-color:var(--border2);transform:translateY(-2px);}
.stat-val{
  font-size:28px;font-weight:700;line-height:1;
  font-family:'JetBrains Mono',monospace;
  margin-bottom:4px;display:block;
}
.stat-lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;}

/* ── CARD ── */
.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);overflow:hidden;
  transition:border-color .2s;
}
.card:hover{border-color:var(--border2);}
.card-head{
  padding:14px 20px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  gap:12px;
}
.card-title{font-size:13px;font-weight:600;}
.card-desc{font-size:12px;color:var(--muted);margin-top:1px;}

/* ── ACCORDION ── */
.acc{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;}
.acc-head{
  padding:14px 20px;display:flex;align-items:center;justify-content:space-between;
  cursor:pointer;transition:background .15s;user-select:none;
}
.acc-head:hover{background:rgba(255,255,255,.02);}
.acc-head-l{display:flex;align-items:center;gap:12px;}
.acc-icon-wrap{
  width:34px;height:34px;border-radius:8px;
  display:flex;align-items:center;justify-content:center;
  font-size:16px;flex-shrink:0;background:var(--surface2);
}
.acc-title{font-size:13px;font-weight:600;}
.acc-sub{font-size:11px;color:var(--muted);margin-top:1px;}
.acc-head-r{display:flex;align-items:center;gap:10px;}
.chevron{
  color:var(--muted);font-size:10px;
  transition:transform .3s cubic-bezier(.22,1,.36,1);
}
.acc.open .chevron{transform:rotate(180deg);}
.acc-body{max-height:0;overflow:hidden;transition:max-height .4s cubic-bezier(.22,1,.36,1);}
.acc.open .acc-body{max-height:1200px;}
.acc-inner{border-top:1px solid var(--border);}

/* ── TOGGLE ROW ── */
.toggle-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 20px;transition:background .15s;
}
.toggle-row:hover{background:rgba(255,255,255,.02);}
.tg-t{font-size:13px;font-weight:500;}
.tg-d{font-size:11px;color:var(--muted);margin-top:2px;}
.sub-row{
  padding:10px 20px 10px 52px;
  display:flex;align-items:center;justify-content:space-between;
  transition:background .15s;
  border-left:2px solid rgba(59,130,246,.15);
  margin-left:20px;margin-right:20px;
}
.sub-row:hover{background:rgba(255,255,255,.015);}
.sub-row .tg-t{font-size:12px;color:var(--muted2);}

/* ── TOGGLE SWITCH ── */
.sw{position:relative;width:42px;height:24px;flex-shrink:0;}
.sw input{opacity:0;width:0;height:0;position:absolute;}
.sw-track{
  position:absolute;inset:0;border-radius:24px;
  background:#1a2535;border:1px solid var(--border2);
  cursor:pointer;transition:background .2s,border-color .2s;
}
.sw-track::after{
  content:'';position:absolute;
  width:18px;height:18px;border-radius:50%;
  top:2px;left:2px;background:#fff;
  transition:transform .25s cubic-bezier(.34,1.56,.64,1);
  box-shadow:0 1px 4px rgba(0,0,0,.4);
}
.sw input:checked+.sw-track{background:var(--success);border-color:var(--success);}
.sw input:checked+.sw-track::after{transform:translateX(18px);}
.sw input:disabled+.sw-track{opacity:.4;cursor:not-allowed;}

/* ── SLIDER ── */
.slider-wrap{padding:12px 20px;}
.slider-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;}
.slider-lbl{font-size:12px;color:var(--muted);}
.slider-badge{
  background:var(--primary-dim);color:var(--primary);
  font-size:12px;font-weight:700;padding:2px 10px;border-radius:12px;
  font-family:'JetBrains Mono',monospace;min-width:36px;text-align:center;
}
input[type=range]{
  -webkit-appearance:none;width:100%;height:4px;
  background:var(--border2);border-radius:4px;
  outline:none;cursor:pointer;border:none;min-height:20px;padding:0;
}
input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;width:18px;height:18px;
  background:var(--primary);border-radius:50%;cursor:pointer;
  box-shadow:0 0 8px rgba(59,130,246,.5);
  transition:transform .15s;
}
input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.2);}
input[type=range]:disabled{opacity:.4;cursor:not-allowed;}

/* ── BANNED WORDS ── */
.words-wrap{padding:12px 20px 16px;}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;min-height:32px;}
.chip{
  display:flex;align-items:center;gap:5px;
  background:var(--danger-dim);border:1px solid rgba(239,68,68,.25);
  border-radius:16px;padding:4px 10px;
  font-size:12px;color:#fc8585;
  animation:slideIn .15s ease both;
}
.chip button{
  background:none;border:none;color:inherit;cursor:pointer;
  font-size:14px;line-height:1;padding:0;opacity:.6;
  transition:opacity .15s;font-family:inherit;
}
.chip button:hover{opacity:1;}
.word-input-row{display:flex;gap:8px;}
.word-inp{
  flex:1;background:var(--surface2);border:1.5px solid var(--border);
  border-radius:8px;padding:9px 14px;color:var(--text);
  font-size:13px;font-family:'IBM Plex Sans Thai',sans-serif;
  outline:none;transition:border-color .2s;min-height:40px;
}
.word-inp:focus{border-color:var(--primary);}
.word-inp::placeholder{color:var(--muted);}
.btn-add{
  background:var(--primary-dim);border:1px solid rgba(59,130,246,.3);
  border-radius:8px;padding:9px 14px;
  color:var(--primary);font-size:13px;font-weight:600;
  font-family:'IBM Plex Sans Thai',sans-serif;
  cursor:pointer;transition:background .15s;white-space:nowrap;min-height:40px;
}
.btn-add:hover{background:rgba(59,130,246,.25);}

/* ── SAVE BAR ── */
.save-bar{
  position:fixed;bottom:0;left:var(--sidebar);right:0;
  background:rgba(8,12,16,.92);backdrop-filter:blur(12px);
  border-top:1px solid var(--border);
  padding:12px 36px;
  display:flex;align-items:center;justify-content:space-between;gap:12px;
  z-index:100;
}
.save-hint{font-size:12px;color:var(--muted);}
.save-actions{display:flex;gap:8px;}
.btn-reset{
  background:transparent;border:1px solid var(--border);border-radius:8px;
  padding:8px 16px;color:var(--muted2);
  font-family:'IBM Plex Sans Thai',sans-serif;font-size:13px;font-weight:500;
  cursor:pointer;transition:border-color .2s,color .2s;min-height:38px;
}
.btn-reset:hover{border-color:var(--text);color:var(--text);}
.btn-save{
  background:var(--primary);border:none;border-radius:8px;
  padding:8px 20px;color:#fff;
  font-family:'IBM Plex Sans Thai',sans-serif;font-size:13px;font-weight:600;
  cursor:pointer;transition:opacity .2s;min-height:38px;
  display:flex;align-items:center;gap:8px;
}
.btn-save:hover{opacity:.9;}
.btn-save:disabled{opacity:.5;cursor:not-allowed;}

/* ── LOGS ── */
.log-list{display:flex;flex-direction:column;}
.log-row{
  display:flex;align-items:flex-start;gap:12px;
  padding:12px 20px;border-bottom:1px solid var(--border);
  transition:background .15s;
}
.log-row:last-child{border-bottom:none;}
.log-row:hover{background:rgba(255,255,255,.015);}
.log-badge{
  font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px;
  white-space:nowrap;flex-shrink:0;margin-top:2px;
  font-family:'JetBrains Mono',monospace;
}
.log-content{flex:1;min-width:0;}
.log-action{font-size:12px;font-weight:600;margin-bottom:2px;}
.log-meta{font-size:11px;color:var(--muted);}
.log-time{font-size:10px;color:var(--muted);white-space:nowrap;flex-shrink:0;}

/* ── STATUS DOT ── */
.dot{
  display:inline-block;width:8px;height:8px;border-radius:50%;
  margin-right:6px;vertical-align:middle;
}
.dot-on{background:var(--success);box-shadow:0 0 6px var(--success);}
.dot-off{background:var(--muted);}

/* ── TOAST ── */
#toast-wrap{
  position:fixed;bottom:24px;right:24px;z-index:9999;
  display:flex;flex-direction:column;gap:8px;pointer-events:none;
}
.toast{
  background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:12px 18px;
  font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.4);
  animation:slideIn .3s cubic-bezier(.16,1,.3,1);
  pointer-events:auto;display:flex;align-items:center;gap:8px;
  border-left:3px solid var(--muted);
}
.toast.success{border-left-color:var(--success);}
.toast.error{border-left-color:var(--danger);}

/* ── SPINNER ── */
.spin{
  width:14px;height:14px;border:2px solid rgba(255,255,255,.2);
  border-top-color:#fff;border-radius:50%;
  animation:spin .7s linear infinite;display:none;
}

/* ── OVERVIEW BANNER ── */
.banner{
  height:140px;border-radius:var(--radius);
  background:linear-gradient(135deg,#0d1b35,#0a1525);
  border:1px solid var(--border);
  display:flex;align-items:flex-end;padding:20px;
  position:relative;overflow:hidden;
}
.banner::before{
  content:'';position:absolute;inset:0;
  background:radial-gradient(ellipse 80% 60% at 30% 50%,rgba(59,130,246,.15),transparent);
}
.banner-content{position:relative;z-index:1;}
.banner-name{font-size:18px;font-weight:700;margin-bottom:2px;}
.banner-sub{font-size:12px;color:rgba(255,255,255,.5);}

/* ── SYSTEM STATUS CARD ── */
.sys-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 20px;border-bottom:1px solid var(--border);
}
.sys-row:last-child{border-bottom:none;}
.sys-label{font-size:13px;font-weight:500;}
.sys-status{font-size:12px;font-weight:600;display:flex;align-items:center;gap:4px;}
.badge-on{color:var(--success);}
.badge-off{color:var(--muted);}

/* ── RESPONSIVE ── */
@media(max-width:768px){
  :root{--sidebar:240px;}
  .sidebar{transform:translateX(-100%);}
  .sidebar.open{transform:translateX(0);}
  .hamburger{display:flex;}
  .main{margin-left:0;padding:16px 16px calc(16px + var(--nav-h));}
  .bottom-nav{display:block;}
  .save-bar{left:0;padding:10px 16px;}
  .stats-grid{grid-template-columns:1fr 1fr;}
  #toast-wrap{bottom:calc(16px + var(--nav-h));right:12px;left:12px;}
  .toast{max-width:100%;}
  .page-hd{margin-top:44px;}
}
@media(min-width:769px){
  .stats-grid{grid-template-columns:repeat(4,1fr);}
}
</style>
</head>
<body>

<!-- ─── LOGIN ─── -->
<div id="login-view">
  <div class="login-box">
    <div>
      <div class="login-logo">
        <div class="login-logo-icon">🛡️</div>
        <div>
          <div class="login-title">Security Bot</div>
        </div>
      </div>
      <div class="login-sub" style="margin-top:8px;">Dashboard ตั้งค่าบอทความปลอดภัย</div>
    </div>
    <div>
      <input class="login-input" type="text" id="tok-inp"
        placeholder="วางรหัสที่ได้จากคำสั่ง !getcode" autocomplete="off"/>
      <div class="login-error" id="login-err" style="margin-top:8px;"></div>
    </div>
    <button class="login-btn" id="login-btn" onclick="doLogin()">
      <div class="spin" id="login-spin"></div>
      <span id="login-txt">เข้าสู่ระบบ</span>
    </button>
    <div class="login-hint">พิมพ์ <code>!getcode</code> ใน Discord Server ของคุณก่อน</div>
  </div>
</div>

<!-- ─── APP ─── -->
<div id="app">
  <div class="sidebar-overlay" id="sov" onclick="closeSidebar()"></div>
  <button class="hamburger" onclick="openSidebar()">☰</button>

  <!-- Sidebar -->
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-head">
      <div class="s-logo">🛡️</div>
      <div>
        <div class="s-title">Security Bot</div>
        <div class="s-sub" id="sb-server">กำลังโหลด...</div>
      </div>
    </div>
    <nav class="sidebar-nav">
      <div class="nav-item active" data-tab="overview" onclick="goTab('overview',this)">
        <span class="nav-icon">🏠</span>หน้าหลัก
      </div>
      <div class="nav-item" data-tab="automod" onclick="goTab('automod',this)">
        <span class="nav-icon">🛡️</span>กรองข้อความ
      </div>
      <div class="nav-item" data-tab="antiraid" onclick="goTab('antiraid',this)">
        <span class="nav-icon">🚨</span>กันเรด / Nuke
      </div>
      <div class="nav-item" data-tab="others" onclick="goTab('others',this)">
        <span class="nav-icon">⚙️</span>ระบบอื่นๆ
      </div>
      <div class="nav-item" data-tab="logs" onclick="goTab('logs',this)">
        <span class="nav-icon">📋</span>บันทึก
      </div>
    </nav>
    <div class="sidebar-foot">
      <div class="server-chip">
        <div class="server-av" id="sb-av">?</div>
        <div>
          <div class="server-name" id="sb-name">กำลังโหลด...</div>
          <div class="server-id mono" id="sb-id"></div>
        </div>
      </div>
      <button class="btn-logout" onclick="doLogout()">🚪 ออกจากระบบ</button>
    </div>
  </aside>

  <!-- Main -->
  <main class="main">

    <!-- ── Overview ── -->
    <div class="tab active" id="tab-overview">
      <div class="page-hd">
        <h1>หน้าหลัก</h1>
        <p id="ov-server" class="skeleton" style="width:140px;height:13px;border-radius:4px;"></p>
      </div>

      <div class="banner">
        <div class="banner-content">
          <div class="banner-name" id="ov-banner-name">...</div>
          <div class="banner-sub" id="ov-banner-sub">กำลังโหลด...</div>
        </div>
      </div>

      <div class="stats-grid">
        <div class="stat-card" style="--accent:var(--primary)">
          <span class="stat-val skeleton" id="st-members" style="width:56px;height:32px;display:inline-block;"></span>
          <div class="stat-lbl">สมาชิก</div>
        </div>
        <div class="stat-card" style="--accent:var(--success)">
          <span class="stat-val skeleton" id="st-online" style="width:40px;height:32px;display:inline-block;"></span>
          <div class="stat-lbl">ออนไลน์</div>
        </div>
        <div class="stat-card" style="--accent:#f59e0b">
          <span class="stat-val skeleton" id="st-channels" style="width:40px;height:32px;display:inline-block;"></span>
          <div class="stat-lbl">ช่อง</div>
        </div>
        <div class="stat-card" style="--accent:#a78bfa">
          <span class="stat-val skeleton" id="st-roles" style="width:40px;height:32px;display:inline-block;"></span>
          <div class="stat-lbl">ยศ</div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">สถานะระบบป้องกัน</div>
            <div class="card-desc">เปิด/ปิดฟีเจอร์หลักได้เลย</div>
          </div>
        </div>
        <div class="sys-row">
          <div class="sys-label">🛡️ กรองข้อความ (AutoMod)</div>
          <label class="sw">
            <input type="checkbox" id="q-automod" onchange="quickSave('automod',this.checked)"/>
            <span class="sw-track"></span>
          </label>
        </div>
        <div class="sys-row">
          <div class="sys-label">⚡ กันสแปม</div>
          <label class="sw">
            <input type="checkbox" id="q-antispam" onchange="quickSave('antispam',this.checked)"/>
            <span class="sw-track"></span>
          </label>
        </div>
        <div class="sys-row">
          <div class="sys-label">🚨 กันเรด</div>
          <label class="sw">
            <input type="checkbox" id="q-antiraid" onchange="quickSave('antiraid',this.checked)"/>
            <span class="sw-track"></span>
          </label>
        </div>
        <div class="sys-row">
          <div class="sys-label">💣 Anti-Nuke</div>
          <label class="sw">
            <input type="checkbox" id="q-antinuke" onchange="quickSave('antinuke',this.checked)"/>
            <span class="sw-track"></span>
          </label>
        </div>
        <div class="sys-row">
          <div class="sys-label">🚪 Join Gate</div>
          <label class="sw">
            <input type="checkbox" id="q-joingate" onchange="quickSave('joingate',this.checked)"/>
            <span class="sw-track"></span>
          </label>
        </div>
        <div class="sys-row">
          <div class="sys-label">🎉 ยินดีต้อนรับ</div>
          <label class="sw">
            <input type="checkbox" id="q-welcome" onchange="quickSave('welcome',this.checked)"/>
            <span class="sw-track"></span>
          </label>
        </div>
      </div>
    </div>

    <!-- ── AutoMod ── -->
    <div class="tab" id="tab-automod">
      <div class="page-hd">
        <h1>กรองข้อความ</h1>
        <p>ตั้งค่าระบบ AutoMod ป้องกันสแปมและเนื้อหาไม่พึงประสงค์</p>
      </div>

      <!-- คำต้องห้าม -->
      <div class="acc" id="acc-words">
        <div class="acc-head" onclick="toggleAcc('acc-words')">
          <div class="acc-head-l">
            <div class="acc-icon-wrap">💬</div>
            <div>
              <div class="acc-title">คำต้องห้าม</div>
              <div class="acc-sub">ลบข้อความที่มีคำต้องห้ามอัตโนมัติ</div>
            </div>
          </div>
          <div class="acc-head-r">
            <label class="sw" onclick="event.stopPropagation()">
              <input type="checkbox" id="am-enabled"/>
              <span class="sw-track"></span>
            </label>
            <span class="chevron">▼</span>
          </div>
        </div>
        <div class="acc-body">
          <div class="acc-inner">
            <div class="words-wrap">
              <div class="chips" id="chips"></div>
              <div class="word-input-row">
                <input class="word-inp" type="text" id="word-inp" placeholder="พิมพ์คำต้องห้าม แล้วกด Enter"/>
                <button class="btn-add" onclick="addWord()">+ เพิ่ม</button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- กรองลิงก์ -->
      <div class="acc" id="acc-links">
        <div class="acc-head" onclick="toggleAcc('acc-links')">
          <div class="acc-head-l">
            <div class="acc-icon-wrap">🔗</div>
            <div>
              <div class="acc-title">กรองลิงก์</div>
              <div class="acc-sub">บล็อก URL และลิงก์เชิญ Discord</div>
            </div>
          </div>
          <div class="acc-head-r"><span class="chevron">▼</span></div>
        </div>
        <div class="acc-body">
          <div class="acc-inner">
            <div class="sub-row">
              <div class="tg-t">กรองลิงก์ทั่วไป (http://...)</div>
              <label class="sw"><input type="checkbox" id="am-filter-links"/><span class="sw-track"></span></label>
            </div>
            <div class="sub-row">
              <div class="tg-t">กรองลิงก์เชิญ Discord</div>
              <label class="sw"><input type="checkbox" id="am-filter-invites"/><span class="sw-track"></span></label>
            </div>
          </div>
        </div>
      </div>

      <!-- กรองสแปม -->
      <div class="acc" id="acc-spam">
        <div class="acc-head" onclick="toggleAcc('acc-spam')">
          <div class="acc-head-l">
            <div class="acc-icon-wrap">⚡</div>
            <div>
              <div class="acc-title">กรองสแปม</div>
              <div class="acc-sub">จำกัดจำนวนข้อความและ Mute อัตโนมัติ</div>
            </div>
          </div>
          <div class="acc-head-r">
            <label class="sw" onclick="event.stopPropagation()">
              <input type="checkbox" id="sp-enabled"/>
              <span class="sw-track"></span>
            </label>
            <span class="chevron">▼</span>
          </div>
        </div>
        <div class="acc-body">
          <div class="acc-inner">
            <div class="slider-wrap">
              <div class="slider-top">
                <span class="slider-lbl">ข้อความสูงสุด (ต่อ 5 วินาที)</span>
                <span class="slider-badge" id="sp-msg-val">5</span>
              </div>
              <input type="range" id="sp-msg" min="1" max="20" value="5"
                oninput="document.getElementById('sp-msg-val').textContent=this.value"/>
            </div>
            <div class="slider-wrap">
              <div class="slider-top">
                <span class="slider-lbl">ระยะ Mute (นาที)</span>
                <span class="slider-badge" id="sp-mute-val">5</span>
              </div>
              <input type="range" id="sp-mute" min="1" max="60" value="5"
                oninput="document.getElementById('sp-mute-val').textContent=this.value"/>
            </div>
            <div class="sub-row">
              <div class="tg-t">กรองตัวพิมพ์ใหญ่ทั้งหมด</div>
              <label class="sw"><input type="checkbox" id="am-filter-caps"/><span class="sw-track"></span></label>
            </div>
            <div class="sub-row" style="margin-bottom:8px;">
              <div class="tg-t">กรองอีโมจิสแปม</div>
              <label class="sw"><input type="checkbox" id="am-filter-emoji"/><span class="sw-track"></span></label>
            </div>
          </div>
        </div>
      </div>

      <div class="save-bar">
        <span class="save-hint">💡 กดบันทึกเพื่อนำการเปลี่ยนแปลงไปใช้งาน</span>
        <div class="save-actions">
          <button class="btn-reset" onclick="loadConfig()">↺ รีเซ็ต</button>
          <button class="btn-save" id="save-am" onclick="saveAutoMod()">
            <div class="spin" id="spin-am"></div>
            💾 บันทึก
          </button>
        </div>
      </div>
    </div>

    <!-- ── Anti-Raid / Anti-Nuke ── -->
    <div class="tab" id="tab-antiraid">
      <div class="page-hd">
        <h1>กันเรด / Anti-Nuke</h1>
        <p>ปกป้อง Server จากการโจมตีและบัญชีใหม่</p>
      </div>

      <!-- Anti-Raid -->
      <div class="acc" id="acc-raid">
        <div class="acc-head" onclick="toggleAcc('acc-raid')">
          <div class="acc-head-l">
            <div class="acc-icon-wrap">🚨</div>
            <div>
              <div class="acc-title">กันเรด (Anti-Raid)</div>
              <div class="acc-sub">ตรวจจับบัญชีใหม่บุกรุกพร้อมกัน</div>
            </div>
          </div>
          <div class="acc-head-r">
            <label class="sw" onclick="event.stopPropagation()">
              <input type="checkbox" id="ar-enabled"/>
              <span class="sw-track"></span>
            </label>
            <span class="chevron">▼</span>
          </div>
        </div>
        <div class="acc-body">
          <div class="acc-inner">
            <div class="slider-wrap">
              <div class="slider-top">
                <span class="slider-lbl">จำนวนคนเข้าพร้อมกัน (ต่อนาที) ที่ trigger Raid Mode</span>
                <span class="slider-badge" id="ar-thr-val">10</span>
              </div>
              <input type="range" id="ar-thr" min="2" max="30" value="10"
                oninput="document.getElementById('ar-thr-val').textContent=this.value"/>
            </div>
            <div class="slider-wrap">
              <div class="slider-top">
                <span class="slider-lbl">อายุบัญชีขั้นต่ำ (วัน) ใน Raid Mode</span>
                <span class="slider-badge" id="ar-age-val">7</span>
              </div>
              <input type="range" id="ar-age" min="1" max="90" value="7"
                oninput="document.getElementById('ar-age-val').textContent=this.value"/>
            </div>
          </div>
        </div>
      </div>

      <!-- Anti-Nuke -->
      <div class="acc" id="acc-nuke">
        <div class="acc-head" onclick="toggleAcc('acc-nuke')">
          <div class="acc-head-l">
            <div class="acc-icon-wrap">💣</div>
            <div>
              <div class="acc-title">Anti-Nuke</div>
              <div class="acc-sub">ตรวจจับการลบช่อง/ยศ/แบน จำนวนมากในเวลาสั้น</div>
            </div>
          </div>
          <div class="acc-head-r">
            <label class="sw" onclick="event.stopPropagation()">
              <input type="checkbox" id="an-enabled"/>
              <span class="sw-track"></span>
            </label>
            <span class="chevron">▼</span>
          </div>
        </div>
        <div class="acc-body">
          <div class="acc-inner">
            <div class="slider-wrap">
              <div class="slider-top">
                <span class="slider-lbl">จำนวนลบช่อง (ใน 10 วิ) ก่อน Ban</span>
                <span class="slider-badge" id="an-ch-val">3</span>
              </div>
              <input type="range" id="an-ch" min="1" max="10" value="3"
                oninput="document.getElementById('an-ch-val').textContent=this.value"/>
            </div>
            <div class="slider-wrap">
              <div class="slider-top">
                <span class="slider-lbl">จำนวนลบยศ (ใน 10 วิ) ก่อน Ban</span>
                <span class="slider-badge" id="an-rl-val">3</span>
              </div>
              <input type="range" id="an-rl" min="1" max="10" value="3"
                oninput="document.getElementById('an-rl-val').textContent=this.value"/>
            </div>
            <div class="slider-wrap">
              <div class="slider-top">
                <span class="slider-lbl">จำนวนแบนคน (ใน 10 วิ) ก่อน Ban</span>
                <span class="slider-badge" id="an-bn-val">5</span>
              </div>
              <input type="range" id="an-bn" min="1" max="10" value="5"
                oninput="document.getElementById('an-bn-val').textContent=this.value"/>
            </div>
          </div>
        </div>
      </div>

      <!-- Join Gate -->
      <div class="acc" id="acc-jg">
        <div class="acc-head" onclick="toggleAcc('acc-jg')">
          <div class="acc-head-l">
            <div class="acc-icon-wrap">🚪</div>
            <div>
              <div class="acc-title">Join Gate</div>
              <div class="acc-sub">กรองบัญชีใหม่เมื่อเข้า Server</div>
            </div>
          </div>
          <div class="acc-head-r">
            <label class="sw" onclick="event.stopPropagation()">
              <input type="checkbox" id="jg-enabled"/>
              <span class="sw-track"></span>
            </label>
            <span class="chevron">▼</span>
          </div>
        </div>
        <div class="acc-body">
          <div class="acc-inner">
            <div class="slider-wrap">
              <div class="slider-top">
                <span class="slider-lbl">อายุบัญชีขั้นต่ำ (วัน)</span>
                <span class="slider-badge" id="jg-age-val">7</span>
              </div>
              <input type="range" id="jg-age" min="1" max="90" value="7"
                oninput="document.getElementById('jg-age-val').textContent=this.value"/>
            </div>
            <div class="sub-row" style="margin-bottom:8px;">
              <div class="tg-t">บล็อกบัญชีที่ไม่มีรูปโปรไฟล์</div>
              <label class="sw"><input type="checkbox" id="jg-noav"/><span class="sw-track"></span></label>
            </div>
          </div>
        </div>
      </div>

      <div class="save-bar">
        <span class="save-hint">💡 กดบันทึกเพื่อนำการเปลี่ยนแปลงไปใช้งาน</span>
        <div class="save-actions">
          <button class="btn-reset" onclick="loadConfig()">↺ รีเซ็ต</button>
          <button class="btn-save" id="save-ar" onclick="saveRaid()">
            <div class="spin" id="spin-ar"></div>
            💾 บันทึก
          </button>
        </div>
      </div>
    </div>

    <!-- ── Others ── -->
    <div class="tab" id="tab-others">
      <div class="page-hd">
        <h1>ระบบอื่นๆ</h1>
        <p>ตั้งค่า Welcome, Log Channel และอื่นๆ</p>
      </div>

      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">🎉 ยินดีต้อนรับ (Welcome)</div>
            <div class="card-desc">ส่งข้อความต้อนรับเมื่อมีสมาชิกเข้า</div>
          </div>
          <label class="sw"><input type="checkbox" id="wl-enabled"/><span class="sw-track"></span></label>
        </div>
        <div class="toggle-row">
          <div class="tg-t">ID ช่อง Welcome</div>
          <input style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-family:monospace;font-size:12px;width:180px;outline:none;" 
            type="text" id="wl-ch" placeholder="เช่น 123456789"/>
        </div>
        <div class="toggle-row" style="flex-direction:column;align-items:flex-start;gap:8px;">
          <div class="tg-t">ข้อความต้อนรับ</div>
          <div class="tg-d">ใช้ {user} = ชื่อ, {server} = เซิร์ฟเวอร์, {count} = จำนวนสมาชิก</div>
          <textarea id="wl-msg" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;color:var(--text);font-family:'IBM Plex Sans Thai',sans-serif;font-size:13px;resize:vertical;outline:none;min-height:80px;">ยินดีต้อนรับ {user} สู่ {server}! 🎉</textarea>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">📋 Log Channel</div>
            <div class="card-desc">ช่องสำหรับบันทึกกิจกรรม</div>
          </div>
        </div>
        <div class="toggle-row">
          <div class="tg-t">ID ช่อง Log</div>
          <input style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-family:monospace;font-size:12px;width:180px;outline:none;"
            type="text" id="log-ch" placeholder="เช่น 987654321"/>
        </div>
      </div>

      <div class="save-bar">
        <span class="save-hint">💡 กดบันทึกเพื่อนำการเปลี่ยนแปลงไปใช้งาน</span>
        <div class="save-actions">
          <button class="btn-reset" onclick="loadConfig()">↺ รีเซ็ต</button>
          <button class="btn-save" id="save-oth" onclick="saveOthers()">
            <div class="spin" id="spin-oth"></div>
            💾 บันทึก
          </button>
        </div>
      </div>
    </div>

    <!-- ── Logs ── -->
    <div class="tab" id="tab-logs">
      <div class="page-hd">
        <h1>บันทึกกิจกรรม</h1>
        <p>Audit log ล่าสุด 50 รายการ</p>
      </div>
      <div class="card">
        <div class="card-head">
          <div class="card-title">📋 รายการล่าสุด</div>
          <button class="btn-reset" style="padding:6px 12px;font-size:12px;" onclick="loadLogs()">↺ รีเฟรช</button>
        </div>
        <div class="log-list" id="log-list">
          <div style="padding:24px;text-align:center;color:var(--muted);font-size:13px;">กำลังโหลด...</div>
        </div>
      </div>
    </div>

  </main>

  <!-- Mobile Bottom Nav -->
  <nav class="bottom-nav">
    <div class="bottom-nav-inner">
      <button class="bn-item active" data-tab="overview" onclick="goTab('overview',this)">
        <span class="bn-icon">🏠</span>หน้าหลัก
      </button>
      <button class="bn-item" data-tab="automod" onclick="goTab('automod',this)">
        <span class="bn-icon">🛡️</span>กรองข้อความ
      </button>
      <button class="bn-item" data-tab="antiraid" onclick="goTab('antiraid',this)">
        <span class="bn-icon">🚨</span>กันเรด
      </button>
      <button class="bn-item" data-tab="others" onclick="goTab('others',this)">
        <span class="bn-icon">⚙️</span>ระบบอื่น
      </button>
      <button class="bn-item" data-tab="logs" onclick="goTab('logs',this)">
        <span class="bn-icon">📋</span>บันทึก
      </button>
    </div>
  </nav>
</div>

<div id="toast-wrap"></div>

<script>
const API_BASE = "http://localhost:8080";  // จะถูกแทนที่อัตโนมัติ

/* ─── STATE ─── */
let bannedWords = [];
let currentToken = null;

/* ─── TOKEN ─── */
const getToken  = () => localStorage.getItem('_sbot_tok');
const setToken  = v  => localStorage.setItem('_sbot_tok', v);
const clearToken= ()  => localStorage.removeItem('_sbot_tok');

/* ─── TOAST ─── */
function toast(msg, type='info', ms=3200) {
  const wrap = document.getElementById('toast-wrap');
  const el   = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0'; el.style.transition = 'opacity .3s';
    setTimeout(() => el.remove(), 300);
  }, ms);
}

/* ─── COUNT UP ─── */
function countUp(el, target, dur=800) {
  const start = Date.now();
  const step = () => {
    const p = Math.min((Date.now()-start)/dur,1);
    const e = 1-Math.pow(1-p,3);
    el.textContent = Math.round(e*target).toLocaleString('th-TH');
    if(p<1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ─── SIDEBAR ─── */
function openSidebar() {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('sov').classList.add('show');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sov').classList.remove('show');
}

/* ─── TABS ─── */
function goTab(name, el) {
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(`tab-${name}`).classList.add('active');
  document.querySelectorAll('.nav-item,.bn-item').forEach(n=>n.classList.remove('active'));
  document.querySelectorAll(`[data-tab="${name}"]`).forEach(n=>n.classList.add('active'));
  closeSidebar();
  if(name==='logs') loadLogs();
}

/* ─── ACCORDION ─── */
function toggleAcc(id) {
  document.getElementById(id).classList.toggle('open');
}

/* ─── LOGIN ─── */
document.getElementById('tok-inp').addEventListener('keydown', e => {
  if(e.key==='Enter') doLogin();
  document.getElementById('login-err').textContent='';
});

async function doLogin() {
  const tok = document.getElementById('tok-inp').value.trim();
  if(!tok) return;
  const btn  = document.getElementById('login-btn');
  const spin = document.getElementById('login-spin');
  const txt  = document.getElementById('login-txt');
  btn.disabled=true; spin.style.display='block'; txt.textContent='กำลังตรวจสอบ...';
  try {
    const res  = await fetch(`${API_BASE}/api/verify?token=${encodeURIComponent(tok)}`);
    const data = await res.json();
    if(res.ok && data.valid) {
      setToken(tok);
      await showApp();
    } else {
      document.getElementById('login-err').textContent = data.message || 'รหัสไม่ถูกต้องหรือหมดอายุ';
    }
  } catch {
    document.getElementById('login-err').textContent = 'เชื่อมต่อ Server ไม่ได้';
  } finally {
    btn.disabled=false; spin.style.display='none'; txt.textContent='เข้าสู่ระบบ';
  }
}

async function showApp() {
  document.getElementById('login-view').style.display='none';
  document.getElementById('app').style.display='block';
  await Promise.all([loadStats(), loadConfig()]);
}

function doLogout() {
  clearToken();
  document.getElementById('app').style.display='none';
  document.getElementById('login-view').style.display='flex';
  document.getElementById('tok-inp').value='';
}

/* ─── STATS ─── */
async function loadStats() {
  const tok = getToken();
  try {
    const res  = await fetch(`${API_BASE}/api/stats?token=${encodeURIComponent(tok)}`);
    if(!res.ok) throw new Error();
    const d = await res.json();
    const name = d.guild_name||d.server_name||'เซิร์ฟเวอร์';

    // sidebar
    document.getElementById('sb-server').textContent = name;
    document.getElementById('sb-name').textContent   = name;
    document.getElementById('sb-id').textContent     = d.server_id ? `ID: ${d.server_id}` : '';
    const av = document.getElementById('sb-av');
    if(d.icon_url) { av.innerHTML=`<img src="${d.icon_url}" alt=""/>`; }
    else { av.textContent = name.charAt(0).toUpperCase(); }

    // overview
    const ovS = document.getElementById('ov-server');
    ovS.textContent=name; ovS.classList.remove('skeleton'); ovS.style='';
    document.getElementById('ov-banner-name').textContent = name;
    document.getElementById('ov-banner-sub').textContent  = `${d.member_count||0} สมาชิก`;

    const setV = (id, val) => {
      const el = document.getElementById(id);
      if(val!=null){ el.style=''; el.classList.remove('skeleton'); countUp(el,val); }
      else el.textContent='—';
    };
    setV('st-members',  d.member_count);
    setV('st-online',   d.online_count);
    setV('st-channels', d.channel_count);
    setV('st-roles',    d.role_count);
  } catch {
    toast('⚠️ เชื่อมต่อบอทไม่ได้','error');
  }
}

/* ─── CONFIG ─── */
async function loadConfig() {
  const tok = getToken();
  try {
    const res = await fetch(`${API_BASE}/api/config?token=${encodeURIComponent(tok)}`);
    if(!res.ok) throw new Error();
    applyConfig(await res.json());
  } catch {
    toast('⚠️ โหลด config ไม่ได้','error');
  }
}

function applyConfig(cfg) {
  const chk = (id,v) => { const el=document.getElementById(id); if(el) el.checked=!!v; };
  const rng = (id,vid,v) => { if(v==null)return; const el=document.getElementById(id); if(el){el.value=v; document.getElementById(vid).textContent=v;} };

  const am = cfg.automod||{};
  chk('am-enabled',       am.enabled);
  chk('am-filter-links',  am.filter_links);
  chk('am-filter-invites',am.filter_invites);
  chk('am-filter-caps',   am.filter_caps);
  chk('am-filter-emoji',  am.filter_emoji);
  bannedWords = Array.isArray(am.banned_words) ? [...am.banned_words] : [];
  renderChips();

  const sp = cfg.antispam||{};
  chk('sp-enabled', sp.enabled);
  rng('sp-msg','sp-msg-val', sp.max_messages||sp.message_limit);
  rng('sp-mute','sp-mute-val', sp.mute_duration);

  const ar = cfg.antiraid||{};
  chk('ar-enabled', ar.enabled);
  rng('ar-thr','ar-thr-val', ar.join_threshold||ar.threshold);
  rng('ar-age','ar-age-val', ar.min_account_age||ar.account_age);

  const an = cfg.antinuke||{};
  chk('an-enabled', an.enabled);
  rng('an-ch','an-ch-val', an.channel_limit);
  rng('an-rl','an-rl-val', an.role_limit);
  rng('an-bn','an-bn-val', an.ban_limit);

  const jg = cfg.joingate||{};
  chk('jg-enabled', jg.enabled);
  rng('jg-age','jg-age-val', jg.min_account_age);
  chk('jg-noav', jg.block_no_avatar);

  const wl = cfg.welcome||{};
  chk('wl-enabled', wl.enabled);
  if(wl.channel_id) document.getElementById('wl-ch').value = wl.channel_id;
  if(wl.message)    document.getElementById('wl-msg').value = wl.message;

  if(cfg.log_channel_id) document.getElementById('log-ch').value = cfg.log_channel_id;

  // quick toggles on overview
  chk('q-automod',  am.enabled);
  chk('q-antispam', sp.enabled);
  chk('q-antiraid', ar.enabled);
  chk('q-antinuke', an.enabled);
  chk('q-joingate', jg.enabled);
  chk('q-welcome',  wl.enabled);
}

/* ─── QUICK SAVE ─── */
async function quickSave(feature, val) {
  const tok = getToken();
  try {
    const res = await fetch(`${API_BASE}/api/config?token=${encodeURIComponent(tok)}`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({[feature]:{enabled:val}})
    });
    if(!res.ok) throw new Error();
    const labels = {automod:'กรองข้อความ',antispam:'กันสแปม',antiraid:'กันเรด',antinuke:'Anti-Nuke',joingate:'Join Gate',welcome:'Welcome'};
    toast(`✅ ${labels[feature]||feature} ${val?'เปิด':'ปิด'}แล้ว`,'success');
    // sync mirror toggles
    const mirrorMap = {automod:'am-enabled',antispam:'sp-enabled',antiraid:'ar-enabled',antinuke:'an-enabled',joingate:'jg-enabled',welcome:'wl-enabled'};
    if(mirrorMap[feature]) { const el=document.getElementById(mirrorMap[feature]); if(el) el.checked=val; }
  } catch {
    toast('❌ บันทึกไม่สำเร็จ','error');
    const el=document.getElementById(`q-${feature}`); if(el) el.checked=!val;
  }
}

/* ─── SAVE HELPERS ─── */
async function postConfig(data, btnId, spinId) {
  const tok = getToken();
  const btn  = document.getElementById(btnId);
  const spin = document.getElementById(spinId);
  btn.disabled=true; spin.style.display='block';
  try {
    const res = await fetch(`${API_BASE}/api/config?token=${encodeURIComponent(tok)}`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)
    });
    if(!res.ok) { const e=await res.json().catch(()=>({})); throw new Error(e.error||`HTTP ${res.status}`); }
    toast('✅ บันทึกสำเร็จ','success');
  } catch(err) {
    toast(`❌ ${err.message}`,'error');
  } finally {
    btn.disabled=false; spin.style.display='none';
  }
}

function g(id) { return document.getElementById(id); }
const gBool = id => g(id)?.checked ?? false;
const gNum  = id => parseInt(g(id)?.value ?? '0', 10);
const gStr  = id => g(id)?.value?.trim() ?? '';

function saveAutoMod() {
  postConfig({
    automod: {
      enabled:        gBool('am-enabled'),
      filter_links:   gBool('am-filter-links'),
      filter_invites: gBool('am-filter-invites'),
      filter_caps:    gBool('am-filter-caps'),
      filter_emoji:   gBool('am-filter-emoji'),
      banned_words:   [...bannedWords],
    },
    antispam: {
      enabled:       gBool('sp-enabled'),
      max_messages:  gNum('sp-msg'),
      mute_duration: gNum('sp-mute'),
    }
  }, 'save-am', 'spin-am');
}

function saveRaid() {
  postConfig({
    antiraid: {
      enabled:         gBool('ar-enabled'),
      join_threshold:  gNum('ar-thr'),
      min_account_age: gNum('ar-age'),
    },
    antinuke: {
      enabled:       gBool('an-enabled'),
      channel_limit: gNum('an-ch'),
      role_limit:    gNum('an-rl'),
      ban_limit:     gNum('an-bn'),
    },
    joingate: {
      enabled:          gBool('jg-enabled'),
      min_account_age:  gNum('jg-age'),
      block_no_avatar:  gBool('jg-noav'),
    }
  }, 'save-ar', 'spin-ar');
}

function saveOthers() {
  postConfig({
    welcome: {
      enabled:    gBool('wl-enabled'),
      channel_id: gStr('wl-ch') || null,
      message:    gStr('wl-msg') || 'ยินดีต้อนรับ {user} สู่ {server}! 🎉',
    },
    log_channel_id: gStr('log-ch') || null,
  }, 'save-oth', 'spin-oth');
}

/* ─── BANNED WORDS ─── */
function renderChips() {
  const wrap = document.getElementById('chips');
  wrap.innerHTML = '';
  bannedWords.forEach((w, i) => {
    const el = document.createElement('div');
    el.className = 'chip';
    el.innerHTML = `${escHtml(w)}<button onclick="removeWord(${i})" title="ลบ">×</button>`;
    wrap.appendChild(el);
  });
}
function addWord() {
  const inp = document.getElementById('word-inp');
  const w   = inp.value.trim().toLowerCase();
  if(!w) return;
  if(bannedWords.includes(w)) { toast('⚠️ คำนี้มีอยู่แล้ว','error',1800); inp.value=''; return; }
  bannedWords.push(w); renderChips(); inp.value='';
}
function removeWord(i) { bannedWords.splice(i,1); renderChips(); }
document.getElementById('word-inp').addEventListener('keydown', e => { if(e.key==='Enter') addWord(); });
function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

/* ─── LOGS ─── */
const LOG_COLOR = {
  ban:'#ef4444', kick:'#f59e0b', message_delete:'#d29922',
  channel_delete:'#ef4444', role_delete:'#ef4444',
  member_update:'#5865F2', message_pin:'#3b82f6',
};
async function loadLogs() {
  const tok  = getToken();
  const list = document.getElementById('log-list');
  list.innerHTML = '<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px;">กำลังโหลด...</div>';
  try {
    const res  = await fetch(`${API_BASE}/api/logs?token=${encodeURIComponent(tok)}`);
    if(!res.ok) throw new Error();
    const logs = await res.json();
    if(!logs.length){
      list.innerHTML='<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px;">ไม่มีบันทึก</div>';
      return;
    }
    list.innerHTML = logs.map(l => {
      const action = (l.action||'').replace(/_/g,' ');
      const color  = LOG_COLOR[(l.action||'').toLowerCase()] || '#5a7090';
      const dt     = l.timestamp ? new Date(l.timestamp).toLocaleString('th-TH',{hour:'2-digit',minute:'2-digit',day:'numeric',month:'short'}) : '';
      return `<div class="log-row">
        <span class="log-badge" style="background:${color}20;color:${color};">${action}</span>
        <div class="log-content">
          <div class="log-action">${escHtml(l.user||'-')}</div>
          <div class="log-meta">เป้าหมาย: ${escHtml(String(l.target||'-'))} ${l.reason&&l.reason!=='-'?`• ${escHtml(l.reason)}`:''}
          </div>
        </div>
        <div class="log-time">${dt}</div>
      </div>`;
    }).join('');
  } catch {
    list.innerHTML='<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px;">โหลดไม่ได้</div>';
  }
}

/* ─── INIT ─── */
(function(){
  if(getToken()) showApp();
})();
</script>
</body>
</html>
"""

async def page_index(req):
    html = DASHBOARD_HTML.replace(
        'const API_BASE = "http://localhost:8080";',
        f'const API_BASE = "{API_BASE_URL}";'
    )
    return web.Response(text=html, content_type="text/html", charset="utf-8")

# ══════════════════════════════════════════════════════════════════
#  WEB SERVER
# ══════════════════════════════════════════════════════════════════
async def run_web():
    app = web.Application()
    app.router.add_get("/",             page_index)
    app.router.add_get("/dashboard",    page_index)
    app.router.add_get("/api/verify",   api_verify)
    app.router.add_get("/api/config",   api_get_config)
    app.router.add_post("/api/config",  api_post_config)
    app.router.add_get("/api/stats",    api_stats)
    app.router.add_get("/api/logs",     api_logs)
    app.router.add_route("OPTIONS", "/{tail:.*}", api_options)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"🌐 Web รันที่ port {PORT}")
    while True:
        await asyncio.sleep(3600)

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════
async def main():
    await asyncio.gather(bot.start(BOT_TOKEN), run_web())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("บอทหยุดทำงาน")
