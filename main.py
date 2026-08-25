import os
import re
import time
import asyncio
import aiohttp
import logging
from collections import defaultdict, deque
from datetime import timedelta, datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite

TOKEN = os.getenv("TOKEN")
profanity = os.getenv("profanity")
profanityURL = "https://api.api-ninjas.com/v1/profanityfilter"


DATABASE = "bot.db"

SPAM_MESSAGES = 6
SPAM_WINDOW = 7

MENTION_LIMIT = 5
CAPS_PERCENT = 0.75
CAPS_MINIMUM_LENGTH = 12

DEFAULT_WARN_LIMIT = 3

URL_REGEX = re.compile(
    r"(https?://|www\.)[^\s]+",
    re.IGNORECASE
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s"
)

logger = logging.getLogger("private-bot")

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.presences = True

class PrivateModerationBot(commands.Bot):

    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )

        self.db = None
        self.http = None
        self.profanity_cache = {}
        self.profanity_cache_ttl = 300
        self.config_cache = {}
        self.word_cache = {}


        self.spam_tracker = defaultdict(deque)

        self.warn_cooldowns = defaultdict(dict)

        self.afk_users = {}

        self.reminders = {}

        self.automod_cooldowns = {}

    async def setup_hook(self):

        self.db = await aiosqlite.connect(DATABASE)
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3)
        )

        await self.db.executescript("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            modlog_channel INTEGER,
            automod_enabled INTEGER DEFAULT 1,
            anti_link INTEGER DEFAULT 0,
            anti_caps INTEGER DEFAULT 1,
            anti_spam INTEGER DEFAULT 1,
            anti_mentions INTEGER DEFAULT 1,
            warn_limit INTEGER DEFAULT 3
        );

        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            timestamp INTEGER
        );

        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            moderator_id INTEGER,
            action TEXT,
            reason TEXT,
            timestamp INTEGER
        );

        CREATE TABLE IF NOT EXISTS banned_words (
            guild_id INTEGER,
            word TEXT,
            UNIQUE(guild_id, word)
        );

        CREATE TABLE IF NOT EXISTS afk (
            guild_id INTEGER,
            user_id INTEGER,
            reason TEXT,
            timestamp INTEGER,
            PRIMARY KEY(guild_id, user_id)
        );
        """)

        await self.db.commit()

        await self.tree.sync()

        logger.info("Slash commands synced.")

    async def close(self):
        if self.http:
            await self.http.close()

        if self.db:
            await self.db.close()

        await super().close()

bot = PrivateModerationBot()

async def get_config(guild_id: int):

    cursor = await bot.db.execute(
        "SELECT * FROM guild_config WHERE guild_id = ?",
        (guild_id,)
    )

    row = await cursor.fetchone()

    if row:
        return row

    await bot.db.execute(
        """
        INSERT INTO guild_config
        (guild_id, modlog_channel, automod_enabled,
         anti_link, anti_caps, anti_spam,
         anti_mentions, warn_limit)
        VALUES (?, NULL, 1, 0, 1, 1, 1, 3)
        """,
        (guild_id,)
    )

    await bot.db.commit()

    cursor = await bot.db.execute(
        "SELECT * FROM guild_config WHERE guild_id = ?",
        (guild_id,)
    )

    return await cursor.fetchone()

async def set_config(guild_id: int, column: str, value):

    allowed = {
        "modlog_channel",
        "automod_enabled",
        "anti_link",
        "anti_caps",
        "anti_spam",
        "anti_mentions",
        "warn_limit"
    }

    if column not in allowed:
        raise ValueError("Invalid configuration field.")

    await bot.db.execute(
        f"""
        INSERT INTO guild_config (guild_id, {column})
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET {column} = excluded.{column}
        """,
        (guild_id, value)
    )

    await bot.db.commit()

async def create_case(
    guild_id: int,
    user_id: int,
    moderator_id: int,
    action: str,
    reason: str
):

    cursor = await bot.db.execute(
        """
        INSERT INTO cases
        (guild_id, user_id, moderator_id, action, reason, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            user_id,
            moderator_id,
            action,
            reason,
            int(time.time())
        )
    )

    await bot.db.commit()

    return cursor.lastrowid

async def add_warning(
    guild_id: int,
    user_id: int,
    moderator_id: int,
    reason: str
):

    await bot.db.execute(
        """
        INSERT INTO warnings
        (guild_id, user_id, moderator_id, reason, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            user_id,
            moderator_id,
            reason,
            int(time.time())
        )
    )

    await bot.db.commit()

async def get_warning_count(guild_id: int, user_id: int):

    cursor = await bot.db.execute(
        """
        SELECT COUNT(*)
        FROM warnings
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id)
    )

    row = await cursor.fetchone()

    return row[0]

def moderator():
    async def predicate(interaction: discord.Interaction):

        if not interaction.guild:
            raise app_commands.CheckFailure(
                "This command can only be used inside a server."
            )

        if not interaction.user.guild_permissions.moderate_members:
            raise app_commands.CheckFailure(
                "You need the Moderate Members permission."
            )

        return True

    return app_commands.check(predicate)

def administrator():
    async def predicate(interaction: discord.Interaction):

        if not interaction.guild:
            raise app_commands.CheckFailure(
                "This command can only be used inside a server."
            )

        if not interaction.user.guild_permissions.administrator:
            raise app_commands.CheckFailure(
                "Administrator permission required."
            )

        return True

    return app_commands.check(predicate)

async def modlog(
    guild: discord.Guild,
    title: str,
    description: str,
    color=discord.Color.orange()
):

    config = await get_config(guild.id)

    channel_id = config[1]

    if not channel_id:
        return

    channel = guild.get_channel(channel_id)

    if not channel:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_footer(
        text=f"{guild.name} • Moderation Log"
    )

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass

@bot.tree.command(name="warn", description="Warn a member.")
@app_commands.describe(
    member="Member to warn",
    reason="Reason for warning"
)
@moderator()
async def warn(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str
):

    if member == interaction.user:
        return await interaction.response.send_message(
            "You cannot warn yourself.",
            ephemeral=True
        )

    await add_warning(
        interaction.guild.id,
        member.id,
        interaction.user.id,
        reason
    )

    count = await get_warning_count(
        interaction.guild.id,
        member.id
    )

    case = await create_case(
        interaction.guild.id,
        member.id,
        interaction.user.id,
        "WARN",
        reason
    )

    await modlog(
        interaction.guild,
        f" Warning | Case #{case}",
        (
            f"**User:** {member.mention} (`{member.id}`)\n"
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Total warnings:** `{count}`"
        )
    )

    try:
        await member.send(
            f"You were warned in **{interaction.guild.name}**.\n"
            f"Reason: {reason}\n"
            f"Warnings: {count}"
        )
    except discord.HTTPException:
        pass

    await interaction.response.send_message(
        f"Warned {member.mention}. They now have `{count}` warning(s)."
    )

    config = await get_config(interaction.guild.id)
    warn_limit = config[7]

    if count >= warn_limit:

        try:
            await member.timeout(
                timedelta(minutes=30),
                reason=f"Automatic punishment: {count} warnings"
            )

            await modlog(
                interaction.guild,
                " Automatic Timeout",
                (
                    f"{member.mention} reached "
                    f"`{warn_limit}` warnings and was timed out for 30 minutes."
                ),
                discord.Color.red()
            )

        except discord.HTTPException:
            pass

@bot.tree.command(name="warnings", description="View a member's warnings.")
@app_commands.describe(member="Member")
@moderator()
async def warnings(
    interaction: discord.Interaction,
    member: discord.Member
):

    cursor = await bot.db.execute(
        """
        SELECT moderator_id, reason, timestamp
        FROM warnings
        WHERE guild_id = ? AND user_id = ?
        ORDER BY timestamp DESC
        LIMIT 15
        """,
        (interaction.guild.id, member.id)
    )

    rows = await cursor.fetchall()

    if not rows:
        return await interaction.response.send_message(
            f"{member.mention} has no warnings.",
            ephemeral=True
        )

    embed = discord.Embed(
        title=f"Warnings — {member}",
        color=discord.Color.orange()
    )

    for moderator_id, reason, timestamp in rows:

        moderator_user = interaction.guild.get_member(moderator_id)

        name = (
            moderator_user.mention
            if moderator_user
            else f"<@{moderator_id}>"
        )

        embed.add_field(
            name=f"<t:{timestamp}:R>",
            value=f"**Moderator:** {name}\n**Reason:** {reason}",
            inline=False
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

@bot.tree.command(name="kick", description="Kick a member.")
@app_commands.describe(
    member="Member to kick",
    reason="Reason"
)
@moderator()
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided"
):

    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "That member has an equal or higher role than you.",
            ephemeral=True
        )

    case = await create_case(
        interaction.guild.id,
        member.id,
        interaction.user.id,
        "KICK",
        reason
    )

    try:
        await member.kick(reason=reason)

    except discord.Forbidden:
        return await interaction.response.send_message(
            "I don't have permission to kick that member.",
            ephemeral=True
        )

    await modlog(
        interaction.guild,
        f" Member Kicked | Case #{case}",
        (
            f"**User:** `{member}` (`{member.id}`)\n"
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Reason:** {reason}"
        ),
        discord.Color.red()
    )

    await interaction.response.send_message(
        f"Kicked `{member}`."
    )

@bot.tree.command(name="ban", description="Ban a member.")
@app_commands.describe(
    member="Member to ban",
    reason="Reason",
    delete_days="Delete message history (0-7 days)"
)
@moderator()
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
    delete_days: app_commands.Range[int, 0, 7] = 0
):

    if member == interaction.user:
        return await interaction.response.send_message(
            "You cannot ban yourself.",
            ephemeral=True
        )

    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "That member has an equal or higher role than you.",
            ephemeral=True
        )

    case = await create_case(
        interaction.guild.id,
        member.id,
        interaction.user.id,
        "BAN",
        reason
    )

    try:
        await member.ban(
            reason=reason,
            delete_message_days=delete_days
        )

    except discord.Forbidden:
        return await interaction.response.send_message(
            "I don't have permission to ban that member.",
            ephemeral=True
        )

    await modlog(
        interaction.guild,
        f" Member Banned | Case #{case}",
        (
            f"**User:** `{member}` (`{member.id}`)\n"
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Reason:** {reason}"
        ),
        discord.Color.red()
    )

    await interaction.response.send_message(
        f"Banned `{member}`."
    )

@bot.tree.command(name="unban", description="Unban a user.")
@app_commands.describe(user_id="User ID")
@moderator()
async def unban(
    interaction: discord.Interaction,
    user_id: str
):

    try:
        uid = int(user_id)
    except ValueError:
        return await interaction.response.send_message(
            "Invalid user ID.",
            ephemeral=True
        )

    try:
        user = await bot.fetch_user(uid)
        await interaction.guild.unban(user)

    except discord.NotFound:
        return await interaction.response.send_message(
            "That user isn't banned.",
            ephemeral=True
        )

    except discord.Forbidden:
        return await interaction.response.send_message(
            "I don't have permission to unban users.",
            ephemeral=True
        )

    await create_case(
        interaction.guild.id,
        uid,
        interaction.user.id,
        "UNBAN",
        "Manual unban"
    )

    await interaction.response.send_message(
        f"Unbanned `{user}`."
    )

@bot.tree.command(name="timeout", description="Timeout a member.")
@app_commands.describe(
    member="Member",
    minutes="Timeout duration",
    reason="Reason"
)
@moderator()
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "No reason provided"
):

    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "That member has an equal or higher role than you.",
            ephemeral=True
        )

    try:
        await member.timeout(
            timedelta(minutes=minutes),
            reason=reason
        )

    except discord.Forbidden:
        return await interaction.response.send_message(
            "I cannot timeout that member.",
            ephemeral=True
        )

    case = await create_case(
        interaction.guild.id,
        member.id,
        interaction.user.id,
        "TIMEOUT",
        reason
    )

    await modlog(
        interaction.guild,
        f"⏱ Timeout | Case #{case}",
        (
            f"**User:** {member.mention}\n"
            f"**Duration:** `{minutes}` minutes\n"
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Reason:** {reason}"
        )
    )

    await interaction.response.send_message(
        f"Timed out {member.mention} for `{minutes}` minutes."
    )

@bot.tree.command(name="untimeout", description="Remove a member's timeout.")
@app_commands.describe(member="Member")
@moderator()
async def untimeout(
    interaction: discord.Interaction,
    member: discord.Member
):

    await member.timeout(None)

    await interaction.response.send_message(
        f"Removed timeout from {member.mention}."
    )

@bot.tree.command(name="purge", description="Delete messages.")
@app_commands.describe(
    amount="Number of messages",
    user="Only delete messages from this user"
)
@moderator()
async def purge(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100],
    user: discord.Member = None
):

    await interaction.response.defer(ephemeral=True)

    def check(message):

        if user:
            return message.author.id == user.id

        return True

    deleted = await interaction.channel.purge(
        limit=amount,
        check=check
    )

    await interaction.followup.send(
        f"Deleted `{len(deleted)}` messages.",
        ephemeral=True
    )

    await modlog(
        interaction.guild,
        " Messages Purged",
        (
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Channel:** {interaction.channel.mention}\n"
            f"**Deleted:** `{len(deleted)}`"
        )
    )

@bot.tree.command(name="slowmode", description="Set channel slowmode.")
@app_commands.describe(seconds="Slowmode seconds")
@moderator()
async def slowmode(
    interaction: discord.Interaction,
    seconds: app_commands.Range[int, 0, 21600]
):

    await interaction.channel.edit(
        slowmode_delay=seconds
    )

    await interaction.response.send_message(
        f"Slowmode set to `{seconds}` seconds."
    )

@bot.tree.command(name="lock", description="Lock the current channel.")
@moderator()
async def lock(interaction: discord.Interaction):

    overwrite = interaction.channel.overwrites_for(
        interaction.guild.default_role
    )

    overwrite.send_messages = False

    await interaction.channel.set_permissions(
        interaction.guild.default_role,
        overwrite=overwrite,
        reason=f"Locked by {interaction.user}"
    )

    await interaction.response.send_message(
        " Channel locked."
    )

@bot.tree.command(name="unlock", description="Unlock the current channel.")
@moderator()
async def unlock(interaction: discord.Interaction):

    overwrite = interaction.channel.overwrites_for(
        interaction.guild.default_role
    )

    overwrite.send_messages = None

    await interaction.channel.set_permissions(
        interaction.guild.default_role,
        overwrite=overwrite,
        reason=f"Unlocked by {interaction.user}"
    )

    await interaction.response.send_message(
        " Channel unlocked."
    )

@bot.tree.command(
    name="lockdown",
    description="Lock every text channel."
)
@administrator()
async def lockdown(interaction: discord.Interaction):

    await interaction.response.defer()

    changed = 0

    for channel in interaction.guild.text_channels:

        try:

            overwrite = channel.overwrites_for(
                interaction.guild.default_role
            )

            overwrite.send_messages = False

            await channel.set_permissions(
                interaction.guild.default_role,
                overwrite=overwrite,
                reason=f"Server lockdown by {interaction.user}"
            )

            changed += 1

        except discord.Forbidden:
            continue

    await interaction.followup.send(
        f" Server lockdown enabled for `{changed}` channels."
    )

@bot.tree.command(
    name="unlockdown",
    description="Unlock every text channel."
)
@administrator()
async def unlockdown(interaction: discord.Interaction):

    await interaction.response.defer()

    changed = 0

    for channel in interaction.guild.text_channels:

        try:

            overwrite = channel.overwrites_for(
                interaction.guild.default_role
            )

            overwrite.send_messages = None

            await channel.set_permissions(
                interaction.guild.default_role,
                overwrite=overwrite,
                reason=f"Server lockdown removed by {interaction.user}"
            )

            changed += 1

        except discord.Forbidden:
            continue

    await interaction.followup.send(
        f" Server unlocked for `{changed}` channels."
    )

@bot.tree.command(name="role-add", description="Add a role.")
@app_commands.describe(
    member="Member",
    role="Role"
)
@moderator()
async def role_add(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role
):

    if role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(
            "I cannot manage that role.",
            ephemeral=True
        )

    await member.add_roles(
        role,
        reason=f"Role added by {interaction.user}"
    )

    await interaction.response.send_message(
        f"Added {role.mention} to {member.mention}."
    )

@bot.tree.command(name="role-remove", description="Remove a role.")
@app_commands.describe(
    member="Member",
    role="Role"
)
@moderator()
async def role_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role
):

    await member.remove_roles(
        role,
        reason=f"Role removed by {interaction.user}"
    )

    await interaction.response.send_message(
        f"Removed {role.mention} from {member.mention}."
    )

@bot.tree.command(
    name="nickname",
    description="Change a member's nickname."
)
@app_commands.describe(
    member="Member",
    nickname="New nickname"
)
@moderator()
async def nickname(
    interaction: discord.Interaction,
    member: discord.Member,
    nickname: str
):

    await member.edit(
        nick=nickname,
        reason=f"Nickname changed by {interaction.user}"
    )

    await interaction.response.send_message(
        f"Nickname changed for {member.mention}."
    )

@bot.tree.command(name="ping", description="Check bot latency.")
async def ping(interaction: discord.Interaction):

    await interaction.response.send_message(
        f" `{round(bot.latency * 1000)}ms`"
    )

@bot.tree.command(name="userinfo", description="Show user information.")
@app_commands.describe(member="Member")
async def userinfo(
    interaction: discord.Interaction,
    member: discord.Member = None
):

    member = member or interaction.user

    embed = discord.Embed(
        title=f"User Information — {member}",
        color=member.color
    )

    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name="ID",
        value=f"`{member.id}`"
    )

    embed.add_field(
        name="Account Created",
        value=f"<t:{int(member.created_at.timestamp())}:R>"
    )

    if member.joined_at:
        embed.add_field(
            name="Joined Server",
            value=f"<t:{int(member.joined_at.timestamp())}:R>"
        )

    embed.add_field(
        name="Top Role",
        value=member.top_role.mention
    )

    embed.add_field(
        name="Roles",
        value=str(max(len(member.roles) - 1, 0))
    )

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="avatar", description="Show a user's avatar.")
@app_commands.describe(member="Member")
async def avatar(
    interaction: discord.Interaction,
    member: discord.Member = None
):

    member = member or interaction.user

    embed = discord.Embed(
        title=f"{member.display_name}'s Avatar"
    )

    embed.set_image(url=member.display_avatar.url)

    await interaction.response.send_message(
        embed=embed
    )

@bot.tree.command(
    name="serverinfo",
    description="Show server information."
)
async def serverinfo(interaction: discord.Interaction):

    guild = interaction.guild

    embed = discord.Embed(
        title=guild.name,
        color=discord.Color.blurple()
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(
        name="Owner",
        value=f"<@{guild.owner_id}>"
    )

    embed.add_field(
        name="Members",
        value=f"`{guild.member_count}`"
    )

    embed.add_field(
        name="Channels",
        value=f"`{len(guild.channels)}`"
    )

    embed.add_field(
        name="Roles",
        value=f"`{len(guild.roles)}`"
    )

    embed.add_field(
        name="Boosts",
        value=f"`{guild.premium_subscription_count}`"
    )

    embed.add_field(
        name="Created",
        value=f"<t:{int(guild.created_at.timestamp())}:R>"
    )

    await interaction.response.send_message(
        embed=embed
    )

@bot.tree.command(
    name="set-modlog",
    description="Set the moderation log channel."
)
@administrator()
async def set_modlog(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    await set_config(
        interaction.guild.id,
        "modlog_channel",
        channel.id
    )

    await interaction.response.send_message(
        f"Moderation logs will now go to {channel.mention}."
    )

@bot.tree.command(
    name="automod",
    description="Enable or disable automoderation."
)
@app_commands.describe(enabled="Enable automod")
@administrator()
async def automod(
    interaction: discord.Interaction,
    enabled: bool
):

    await set_config(
        interaction.guild.id,
        "automod_enabled",
        int(enabled)
    )

    await interaction.response.send_message(
        f"Automod {'enabled' if enabled else 'disabled'}."
    )

@bot.tree.command(
    name="anti-link",
    description="Enable or disable link filtering."
)
@app_commands.describe(enabled="Enable link filtering")
@administrator()
async def anti_link(
    interaction: discord.Interaction,
    enabled: bool
):

    await set_config(
        interaction.guild.id,
        "anti_link",
        int(enabled)
    )

    await interaction.response.send_message(
        f"Anti-link {'enabled' if enabled else 'disabled'}."
    )

@bot.tree.command(
    name="anti-caps",
    description="Enable or disable caps filtering."
)
@app_commands.describe(enabled="Enable caps filtering")
@administrator()
async def anti_caps(
    interaction: discord.Interaction,
    enabled: bool
):

    await set_config(
        interaction.guild.id,
        "anti_caps",
        int(enabled)
    )

    await interaction.response.send_message(
        f"Anti-caps {'enabled' if enabled else 'disabled'}."
    )

def caps_ratio(content: str):

    letters = [x for x in content if x.isalpha()]

    if len(letters) < CAPS_MINIMUM_LENGTH:
        return 0

    uppercase = sum(
        1 for x in letters if x.isupper()
    )

    return uppercase / len(letters)

async def automod_action(
    message: discord.Message,
    reason: str
):

    guild = message.guild
    member = message.author

    now = time.monotonic()

    key = (guild.id, member.id)

    if key in bot.automod_cooldowns:
        if now - bot.automod_cooldowns[key] < 5:
            return

    bot.automod_cooldowns[key] = now

    try:
        await message.delete()
    except discord.HTTPException:
        pass

    try:
        await member.timeout(
            timedelta(minutes=1),
            reason=f"Automod: {reason}"
        )

    except discord.HTTPException:
        pass

    await modlog(
        guild,
        " AutoMod Action",
        (
            f"**User:** {member.mention}\n"
            f"**Channel:** {message.channel.mention}\n"
            f"**Reason:** {reason}"
        ),
        discord.Color.red()
    )

async def check_profanity(text: str):
    if not PROFANITY_API_KEY or not text.strip():
        return False

    key = text.strip().lower()

    cached = bot.profanity_cache.get(key)
    if cached:
        result, expires_at = cached
        if time.monotonic() < expires_at:
            return result
        bot.profanity_cache.pop(key, None)

    try:
        async with bot.http.get(
            PROFANITY_API_URL,
            headers={"X-Api-Key": PROFANITY_API_KEY},
            params={"text": text[:1000]}
        ) as response:

            if response.status != 200:
                logger.warning(
                    f"Profanity API returned HTTP {response.status}"
                )
                return False

            data = await response.json()
            result = bool(data.get("has_profanity"))

            bot.profanity_cache[key] = (
                result,
                time.monotonic() + bot.profanity_cache_ttl
            )

            return result

    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        logger.warning(f"Profanity API request failed: {error}")
        return False


async def get_custom_words(guild_id: int):
    cached = bot.word_cache.get(guild_id)

    if cached is not None:
        return cached

    cursor = await bot.db.execute(
        """
        SELECT word FROM banned_words
        WHERE guild_id = ?
        """,
        (guild_id,)
    )

    words = {
        row[0].lower()
        for row in await cursor.fetchall()
        if row[0]
    }

    bot.word_cache[guild_id] = words
    return words


def contains_custom_word(content: str, words: set[str]):
    for word in words:
        pattern = rf"\b{re.escape(word)}\b"

        if re.search(pattern, content, re.IGNORECASE):
            return word

    return None


@bot.event
async def on_message(message: discord.Message):

    if message.author.bot:
        return

    if not message.guild:
        return

    guild = message.guild
    member = message.author

    config = await get_config(guild.id)

    automod_enabled = bool(config[2])

    cursor = await bot.db.execute(
        """
        SELECT reason FROM afk
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild.id, member.id)
    )

    afk = await cursor.fetchone()

    if afk:

        await bot.db.execute(
            """
            DELETE FROM afk
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild.id, member.id)
        )

        await bot.db.commit()

        try:
            await message.channel.send(
                f"Welcome back {member.mention}! "
                f"Your AFK status has been removed."
            )
        except discord.HTTPException:
            pass

    for mentioned in message.mentions:

        cursor = await bot.db.execute(
            """
            SELECT reason, timestamp FROM afk
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild.id, mentioned.id)
        )

        afk_user = await cursor.fetchone()

        if afk_user:

            reason = afk_user[0]

            try:
                await message.channel.send(
                    f" {mentioned.display_name} is AFK: "
                    f"**{reason}**"
                )
            except discord.HTTPException:
                pass

    if not automod_enabled:
        return

    if member.guild_permissions.manage_messages:
        return

    content = message.content

    custom_words = await get_custom_words(guild.id)

    matched_word = contains_custom_word(
        content,
        custom_words
    )

    if matched_word:

        await automod_action(
            message,
            f"Server filtered word: `{matched_word}`"
        )

        return

    if await check_profanity(content):

        await automod_action(
            message,
            "Profanity detected"
        )

        return

    if config[3]:

        if URL_REGEX.search(message.content):

            await automod_action(
                message,
                "Unauthorized link"
            )

            return

    if config[4]:

        if caps_ratio(message.content) >= CAPS_PERCENT:

            await automod_action(
                message,
                "Excessive capitalization"
            )

            return

    if config[6]:

        if len(message.mentions) >= MENTION_LIMIT:

            await automod_action(
                message,
                "Mention spam"
            )

            return

    if config[5]:

        key = (guild.id, member.id)

        timestamps = bot.spam_tracker[key]

        now = time.monotonic()

        timestamps.append(now)

        while timestamps and now - timestamps[0] > SPAM_WINDOW:
            timestamps.popleft()

        if len(timestamps) >= SPAM_MESSAGES:

            timestamps.clear()

            await automod_action(
                message,
                "Message spam"
            )

            return

@bot.tree.command(
    name="afk",
    description="Set your AFK status."
)
@app_commands.describe(reason="AFK reason")
async def afk(
    interaction: discord.Interaction,
    reason: str = "AFK"
):

    await bot.db.execute(
        """
        INSERT INTO afk
        (guild_id, user_id, reason, timestamp)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET
            reason = excluded.reason,
            timestamp = excluded.timestamp
        """,
        (
            interaction.guild.id,
            interaction.user.id,
            reason,
            int(time.time())
        )
    )

    await bot.db.commit()

    await interaction.response.send_message(
        f" AFK enabled: **{reason}**"
    )

@bot.tree.command(
    name="filter-add",
    description="Add a word to the server filter."
)
@administrator()
async def filter_add(
    interaction: discord.Interaction,
    word: str
):

    word = word.lower().strip()

    await bot.db.execute(
        """
        INSERT OR IGNORE INTO banned_words
        (guild_id, word)
        VALUES (?, ?)
        """,
        (interaction.guild.id, word)
    )

    await bot.db.commit()

    bot.word_cache.setdefault(
        interaction.guild.id,
        set()
    ).add(word)

    await interaction.response.send_message(
        f"Added `{word}` to the filter."
    )

@bot.tree.command(
    name="filter-remove",
    description="Remove a word from the server filter."
)
@administrator()
async def filter_remove(
    interaction: discord.Interaction,
    word: str
):

    await bot.db.execute(
        """
        DELETE FROM banned_words
        WHERE guild_id = ? AND word = ?
        """,
        (
            interaction.guild.id,
            word.lower()
        )
    )

    await bot.db.commit()

    bot.word_cache.setdefault(
        interaction.guild.id,
        set()
    ).discard(word.lower())

    await interaction.response.send_message(
        f"Removed `{word}` from the filter."
    )

@bot.tree.command(
    name="filter-list",
    description="List filtered words."
)
@administrator()
async def filter_list(
    interaction: discord.Interaction
):

    cursor = await bot.db.execute(
        """
        SELECT word FROM banned_words
        WHERE guild_id = ?
        ORDER BY word
        """,
        (interaction.guild.id,)
    )

    words = [
        row[0]
        for row in await cursor.fetchall()
    ]

    if not words:
        return await interaction.response.send_message(
            "No custom filtered words."
        )

    await interaction.response.send_message(
        "**Filtered words:**\n" +
        ", ".join(f"`{word}`" for word in words),
        ephemeral=True
    )

@bot.tree.command(
    name="cases",
    description="View moderation cases for a user."
)
@moderator()
async def cases(
    interaction: discord.Interaction,
    member: discord.Member
):

    cursor = await bot.db.execute(
        """
        SELECT id, action, reason, moderator_id, timestamp
        FROM cases
        WHERE guild_id = ? AND user_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (
            interaction.guild.id,
            member.id
        )
    )

    rows = await cursor.fetchall()

    if not rows:

        return await interaction.response.send_message(
            "No moderation cases found.",
            ephemeral=True
        )

    embed = discord.Embed(
        title=f"Moderation Cases — {member}",
        color=discord.Color.blurple()
    )

    for case_id, action, reason, moderator_id, timestamp in rows:

        embed.add_field(
            name=f"Case #{case_id} • {action}",
            value=(
                f"**Moderator:** <@{moderator_id}>\n"
                f"**Reason:** {reason}\n"
                f"**Time:** <t:{timestamp}:R>"
            ),
            inline=False
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

@bot.event
async def on_ready():

    logger.info(
        f"Logged in as {bot.user} ({bot.user.id})"
    )

    logger.info(
        f"Connected to {len(bot.guilds)} server(s)"
    )

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="/help • Moderation"
        )
    )

@bot.event
async def on_member_join(member: discord.Member):

    await modlog(
        member.guild,
        " Member Joined",
        (
            f"**User:** {member.mention}\n"
            f"**ID:** `{member.id}`\n"
            f"**Account:** <t:{int(member.created_at.timestamp())}:R>"
        ),
        discord.Color.green()
    )

@bot.event
async def on_member_remove(member: discord.Member):

    await modlog(
        member.guild,
        " Member Left",
        (
            f"**User:** `{member}`\n"
            f"**ID:** `{member.id}`"
        ),
        discord.Color.red()
    )

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    if isinstance(error, app_commands.CheckFailure):

        message = str(error)

        if not message:
            message = "You don't have permission to use this command."

        if interaction.response.is_done():

            await interaction.followup.send(
                f" {message}",
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                f" {message}",
                ephemeral=True
            )

        return

    logger.exception(
        "Unhandled command error",
        exc_info=error
    )

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                " Something went wrong while executing the command.",
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                " Something went wrong while executing the command.",
                ephemeral=True
            )

    except discord.HTTPException:
        pass

if not TOKEN:
    raise RuntimeError(
        "TOKEN environment variable is missing."
    )

bot.run(TOKEN)
