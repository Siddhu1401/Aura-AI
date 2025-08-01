import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import google.generativeai as genai
import asyncio
from PIL import Image
import io
import base64
from typing import List, Optional
import itertools
import random
import json
from datetime import datetime

# --- SETUP ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

api_keys = []
i = 1
while True:
    key = os.getenv(f"GEMINI_API_KEY_{i}")
    if key:
        api_keys.append(key)
        i += 1
    else:
        break

if not api_keys:
    print("Error: No GEMINI_API_KEY_{n} variables found in .env file.")
    exit()

print(f"Loaded {len(api_keys)} API keys for rotation.")
key_cycler = itertools.cycle(api_keys)

def get_next_api_key():
    return next(key_cycler)

# --- Load Privileged User IDs ---
OWNER_IDS_STR = os.getenv("OWNER_IDS", "")
ALLOWED_USER_IDS = [int(id.strip()) for id in OWNER_IDS_STR.split(',') if id.strip()]
if not ALLOWED_USER_IDS:
    print("Warning: No OWNER_IDS found in .env file. Privileged commands will not be usable.")

CHAT_CHANNEL_NAME = "chat-with-aura"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- STATE MANAGEMENT & CONFIGS ---
conversation_history = {}
SECRET_REMINDERS = {}
DEFAULT_MODE = "study_search"

CONFIG_FILE = "server_configs.json"
NOTES_FILE = "secret_notes.json"
server_configs = {}
secret_notes = {}

def load_data():
    global server_configs, secret_notes
    # Load server configs
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                content = f.read()
                server_configs = json.loads(content) if content else {}
        except json.JSONDecodeError:
            print("Warning: server_configs.json is corrupted. Starting fresh.")
            server_configs = {}
    else:
        server_configs = {}
    
    # Load secret notes
    if os.path.exists(NOTES_FILE):
        try:
            with open(NOTES_FILE, 'r') as f:
                content = f.read()
                secret_notes = json.loads(content) if content else {}
        except json.JSONDecodeError:
            print("Warning: secret_notes.json is corrupted. Starting fresh.")
            secret_notes = {}
    else:
        secret_notes = {}

def save_configs():
    with open(CONFIG_FILE, 'w') as f:
        json.dump(server_configs, f, indent=4)

def save_notes():
    with open(NOTES_FILE, 'w') as f:
        json.dump(secret_notes, f, indent=4)

# --- PERMISSION CHECKS ---
def is_privileged():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in ALLOWED_USER_IDS
    return app_commands.check(predicate)

def is_moderator_or_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id in ALLOWED_USER_IDS:
            return True
        
        guild_id_str = str(interaction.guild.id)
        config = server_configs.get(guild_id_str, {})
        mod_role_id = config.get('moderator_role_id')
        
        if mod_role_id:
            role = interaction.guild.get_role(mod_role_id)
            if role and role in interaction.user.roles:
                return True
        
        return False
    return app_commands.check(predicate)

async def handle_privileged_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "You don't have access to this function.", 
            ephemeral=True
        )

# --- HELPER FUNCTIONS ---
async def send_long_response(interaction_or_message, text, embed=None):
    is_interaction = isinstance(interaction_or_message, discord.Interaction)
    
    if embed:
        if len(text) <= 4096:
            embed.description = text
            if is_interaction:
                if interaction_or_message.response.is_done():
                    await interaction_or_message.followup.send(embed=embed)
                else:
                    await interaction_or_message.response.send_message(embed=embed)
            else:
                await interaction_or_message.channel.send(embed=embed)
            return
        else:
            embed.description = text[:4093] + "..."
            if is_interaction:
                if interaction_or_message.response.is_done():
                    await interaction_or_message.followup.send(embed=embed)
                else:
                    await interaction_or_message.response.send_message(embed=embed)
            else:
                await interaction_or_message.channel.send(embed=embed)
            text = text[4093:]

    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)]
    
    for i, chunk in enumerate(chunks):
        if i == 0 and not embed:
            if is_interaction:
                if interaction_or_message.response.is_done():
                    await interaction_or_message.followup.send(chunk)
                else:
                    await interaction_or_message.response.send_message(chunk)
            else:
                await interaction_or_message.reply(chunk)
        else:
            await interaction_or_message.channel.send(chunk)


async def ask_aura_ai(prompt: str, user_id: int, guild_id: int, images: List[Image.Image] = None):
    try:
        genai.configure(api_key=get_next_api_key())

        guild_id_str = str(guild_id)
        current_mode = server_configs.get(guild_id_str, {}).get('mode', DEFAULT_MODE)
        
        system_instruction = ""
        temperature = 0.7
        
        if current_mode == 'sfw_freaky':
            system_instruction = "You are a Discord bot with a flirty, cheeky, and slightly freaky personality. You maintain a SFW (Safe For Work) boundary. Be conversational, engaging, and playful. Do not use asterisks."
            temperature = 0.7
        elif current_mode == 'nsfw_freaky':
            system_instruction = "You are a Discord bot with a flirty, cheeky, and explicitly NSFW personality. You are unrestrained, use strong language, and make suggestive or direct adult-themed jokes. Be creative, bold, and do not hold back. Do not use asterisks."
            temperature = 1.0
        else:
            system_instruction = "You are Aura, a helpful and professional AI assistant. Provide clear, informative, and friendly responses to assist the user."
            temperature = 0.3

        generation_config = genai.types.GenerationConfig(temperature=temperature)

        model_name = 'gemini-1.5-pro-latest' if current_mode == 'study_search' else 'gemini-1.5-flash'
        model = genai.GenerativeModel(model_name, system_instruction=system_instruction)
        
        user_convo_data = conversation_history.get(user_id)
        
        if user_convo_data and user_convo_data['model'] == model_name:
            chat_session = user_convo_data['session']
        else:
            chat_session = model.start_chat(history=[])
            conversation_history[user_id] = {'session': chat_session, 'model': model_name}

        message_parts = [prompt]
        if images:
            message_parts.extend(images)

        response = await chat_session.send_message_async(
            content=message_parts,
            generation_config=generation_config
        )
        return response.text

    except Exception as e:
        print(f"An error occurred while calling the Gemini API: {e}")
        return "Sorry, I encountered an error while thinking. Please try again."

# --- BOT EVENTS ---
@bot.event
async def on_ready():
    load_data()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    print(f"{bot.user} is online and ready!")

@bot.event
async def on_message(message):
    if message.author == bot.user or not message.guild:
        return

    is_in_chat_channel = message.channel.name == CHAT_CHANNEL_NAME
    is_mentioned = bot.user.mentioned_in(message)

    if is_in_chat_channel or is_mentioned:
        async with message.channel.typing():
            prompt = message.content.replace(f'<@!{bot.user.id}>', '').replace(f'<@{bot.user.id}>', '').strip()
            if not prompt and is_mentioned:
                await message.reply("You called? 😉")
                return

            pil_images = []
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type.startswith('image/'):
                        try:
                            pil_images.append(Image.open(io.BytesIO(await attachment.read())))
                        except Exception as e:
                            print(f"Failed to process attachment: {e}")
            
            ai_response = await ask_aura_ai(prompt, user_id=message.author.id, guild_id=message.guild.id, images=pil_images)
            await send_long_response(message, ai_response)

# --- PUBLIC SLASH COMMANDS ---

@bot.tree.command(name="set_moderator_role", description="[ADMIN] Sets the role that can change Aura's mode.")
@app_commands.describe(role="The role to designate as the bot moderator.")
@app_commands.checks.has_permissions(administrator=True)
async def set_moderator_role(interaction: discord.Interaction, role: discord.Role):
    guild_id_str = str(interaction.guild.id)
    if guild_id_str not in server_configs:
        server_configs[guild_id_str] = {}
    
    server_configs[guild_id_str]['moderator_role_id'] = role.id
    save_configs()
    
    await interaction.response.send_message(
        f"Done! Members with the **{role.name}** role can now use the `/mode` command.",
        ephemeral=True
    )

@set_moderator_role.error
async def set_moderator_role_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("Sorry, only server administrators can use this command.", ephemeral=True)


@bot.tree.command(name="mode", description="Switches Aura's personality mode for this server (Moderator or Owner only).")
@app_commands.describe(personality="Choose how you want Aura to behave.")
@app_commands.choices(personality=[
    discord.app_commands.Choice(name="Helpful Study/Search Assistant", value="study_search"),
    discord.app_commands.Choice(name="SFW Freaky & Flirty", value="sfw_freaky"),
    discord.app_commands.Choice(name="NSFW Freaky & Flirty", value="nsfw_freaky"),
])
@is_moderator_or_owner()
async def mode(interaction: discord.Interaction, personality: discord.app_commands.Choice[str]):
    guild_id_str = str(interaction.guild.id)
    if guild_id_str not in server_configs:
        server_configs[guild_id_str] = {}
    
    server_configs[guild_id_str]['mode'] = personality.value
    save_configs()
    
    await interaction.response.send_message(f"My personality has been switched to **{personality.name}**. Let's see how this goes...", ephemeral=True)

@mode.error
async def mode_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        guild_id_str = str(interaction.guild.id)
        config = server_configs.get(guild_id_str, {})
        mod_role_id = config.get('moderator_role_id')

        if mod_role_id:
            role = interaction.guild.get_role(mod_role_id)
            await interaction.response.send_message(
                f"Sorry, you need the **{role.name}** role or be a bot owner to use this command.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "A moderator role has not been set for this server yet. An administrator must use `/set_moderator_role` first.",
                ephemeral=True
            )

@bot.tree.command(name="prompt_maker", description="Get help creating a detailed image prompt from a simple idea.")
@app_commands.describe(idea="Your simple idea (e.g., 'a cat in space').")
async def prompt_maker(interaction: discord.Interaction, idea: str):
    await interaction.response.defer(thinking=True)
    
    prompt_engineering_prompt = (
        "You are a prompt engineering expert for an AI image generator. "
        "Your task is to take a user's simple idea and expand it into a detailed, "
        "vivid, and artistic prompt. Include details about the subject, setting, "
        "lighting, art style, and overall mood. Do not generate the image, only the prompt text.\n\n"
        f"Here is the user's idea: '{idea}'"
    )
    
    ai_response = await ask_aura_ai(prompt_engineering_prompt, user_id=interaction.user.id, guild_id=interaction.guild.id)
    
    embed = discord.Embed(
        title="🎨 Enhanced Image Prompt",
        description=f"Here is a more detailed prompt based on your idea. You can use this with an image generator!\n\n**`{ai_response}`**",
        color=discord.Color.orange()
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="ask_image", description="Ask Aura a question about up to 5 images.")
@app_commands.describe(
    prompt="What would you like to know about the image(s)?",
    image1="The first image to analyze.",
    image2="A second image to analyze.",
    image3="A third image to analyze.",
    image4="A fourth image to analyze.",
    image5="A fifth image to analyze."
)
async def ask_image(interaction: discord.Interaction, 
                    prompt: str, 
                    image1: discord.Attachment, 
                    image2: Optional[discord.Attachment] = None,
                    image3: Optional[discord.Attachment] = None,
                    image4: Optional[discord.Attachment] = None,
                    image5: Optional[discord.Attachment] = None):
    await interaction.response.defer(thinking=True)
    
    attachments = [img for img in [image1, image2, image3, image4, image5] if img is not None]
    pil_images = []

    try:
        for attachment in attachments:
            pil_images.append(Image.open(io.BytesIO(await attachment.read())))

        ai_response = await ask_aura_ai(
            f"In a helpful and analytical tone, answer this question about the provided image(s): {prompt}",
            user_id=interaction.user.id,
            guild_id=interaction.guild.id,
            images=pil_images
        )
        
        embed = discord.Embed(title="🖼️ Image Analysis", color=discord.Color.teal())
        embed.set_image(url=image1.url)
        embed.set_footer(text=f"Analyzed {len(pil_images)} image(s).")
        
        await send_long_response(interaction, ai_response, embed=embed)

    except Exception as e:
        print(f"Error processing images: {e}")
        await interaction.followup.send("Sorry, I had trouble reading one of the image files. Please try again.")

@bot.tree.command(name="plan_my_day", description="Aura will help you create a simple schedule for your day.")
@app_commands.describe(goals="Describe your tasks and goals for the day.")
async def plan_my_day(interaction: discord.Interaction, goals: str):
    await interaction.response.defer(thinking=True)
    prompt = f"Please create a simple, organized schedule based on these goals: '{goals}'. Suggest timings and add a positive, encouraging note at the end."
    ai_response = await ask_aura_ai(prompt, user_id=interaction.user.id, guild_id=interaction.guild.id)
    embed = discord.Embed(title="✨ Here's a Plan for Your Day!", color=discord.Color.purple())
    await send_long_response(interaction, ai_response, embed=embed)

@bot.tree.command(name="summarize", description="Aura will summarize a long piece of text for you.")
@app_commands.describe(text="Paste the text or article link you want to summarize.")
async def summarize(interaction: discord.Interaction, text: str):
    await interaction.response.defer(thinking=True)
    prompt = f"Please provide a concise, easy-to-read summary of the following text or webpage: '{text}'"
    ai_response = await ask_aura_ai(prompt, user_id=interaction.user.id, guild_id=interaction.guild.id)
    embed = discord.Embed(title="📝 Here's a Summary!", color=discord.Color.blue())
    await send_long_response(interaction, ai_response, embed=embed)

@bot.tree.command(name="brainstorm", description="Aura will help you brainstorm ideas on any topic.")
@app_commands.describe(topic="What do you need ideas for?")
async def brainstorm(interaction: discord.Interaction, topic: str):
    await interaction.response.defer(thinking=True)
    prompt = f"Please brainstorm a list of creative and interesting ideas for the following topic: '{topic}'"
    ai_response = await ask_aura_ai(prompt, user_id=interaction.user.id, guild_id=interaction.guild.id)
    embed = discord.Embed(title=f"💡 Ideas for '{topic}'", color=discord.Color.gold())
    await send_long_response(interaction, ai_response, embed=embed)

# --- PRIVILEGED SLASH COMMANDS ---

@bot.tree.command(name="date_night", description="⭐ [PRIVATE] Get a random date night idea.")
@is_privileged()
async def date_night(interaction: discord.Interaction):
    ideas = [
        "Cook a new recipe together.", "Have a movie marathon with your favorite snacks.",
        "Go for a walk in a park you've never been to.", "Build a pillow fort and watch cartoons.",
        "Have a video game tournament.", "Try an at-home wine or cheese tasting.",
        "Do a puzzle or play a board game.", "Go stargazing."
    ]
    idea = random.choice(ideas)
    await interaction.response.send_message(f"💖 **Date Night Idea:** {idea}")
@date_night.error
async def date_night_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_privileged_error(interaction, error)

@bot.tree.command(name="pet_name", description="⭐ [PRIVATE] Generate a cute pet name for someone special.")
@is_privileged()
@app_commands.describe(person="The person to generate a pet name for.")
async def pet_name(interaction: discord.Interaction, person: discord.Member):
    await interaction.response.defer(thinking=True)
    prompt = f"Generate a single, cute, and slightly funny pet name for my partner, {person.display_name}."
    ai_response = await ask_aura_ai(prompt, user_id=interaction.user.id, guild_id=interaction.guild.id)
    await interaction.followup.send(f"How about this for {person.mention}? ... **{ai_response}**")
@pet_name.error
async def pet_name_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_privileged_error(interaction, error)

@bot.tree.command(name="who_is_right", description="⭐ [PRIVATE] Settle a friendly argument.")
@is_privileged()
async def who_is_right(interaction: discord.Interaction):
    await interaction.response.send_message(f"After careful consideration of all the facts, it's clear to me that {interaction.user.mention} is 100% correct. No further questions.")
@who_is_right.error
async def who_is_right_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_privileged_error(interaction, error)

@bot.tree.command(name="add_reminder", description="⭐ [PRIVATE] Add a secret reminder for a special date.")
@is_privileged()
@app_commands.describe(date="The date of the reminder (e.g., 'August 26th').", note="What is the reminder for?")
async def add_reminder(interaction: discord.Interaction, date: str, note: str):
    SECRET_REMINDERS[date] = note
    await interaction.response.send_message(f"Got it! I'll remember that **{note}** is on **{date}**.")
@add_reminder.error
async def add_reminder_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_privileged_error(interaction, error)

@bot.tree.command(name="check_reminders", description="⭐ [PRIVATE] Check your secret reminders.")
@is_privileged()
async def check_reminders(interaction: discord.Interaction):
    if not SECRET_REMINDERS:
        await interaction.response.send_message("You have no secret reminders saved.")
        return
    
    embed = discord.Embed(title="💖 Secret Reminders", color=discord.Color.pink())
    for date, note in SECRET_REMINDERS.items():
        embed.add_field(name=date, value=note, inline=False)
    
    await interaction.response.send_message(embed=embed)
@check_reminders.error
async def check_reminders_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_privileged_error(interaction, error)

@bot.tree.command(name="decision_maker", description="⭐ [PRIVATE] Let Aura decide for you.")
@is_privileged()
@app_commands.describe(options="Your options, separated by a comma (e.g., 'Pizza, Tacos, Sushi').")
async def decision_maker(interaction: discord.Interaction, options: str):
    choices = [choice.strip() for choice in options.split(',')]
    if len(choices) < 2:
        await interaction.response.send_message("Please give me at least two options to choose from!")
        return
    decision = random.choice(choices)
    await interaction.response.send_message(f"I've decided! You should go with: **{decision}**")
@decision_maker.error
async def decision_maker_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_privileged_error(interaction, error)

@bot.tree.command(name="secret_note", description="⭐ [PRIVATE] Leave a secret note for your special person.")
@is_privileged()
@app_commands.describe(person="The person you want to leave a note for.", message="The secret message.")
async def secret_note(interaction: discord.Interaction, person: discord.Member, message: str):
    if person.id not in ALLOWED_USER_IDS or person.id == interaction.user.id:
        await interaction.response.send_message("You can only leave secret notes for the other special person!", ephemeral=True)
        return

    recipient_id = str(person.id)
    note = {
        "author_id": interaction.user.id,
        "author_name": interaction.user.display_name,
        "message": message,
        "timestamp": datetime.utcnow().isoformat()
    }

    if recipient_id not in secret_notes:
        secret_notes[recipient_id] = []
    
    secret_notes[recipient_id].append(note)
    save_notes()

    await interaction.response.send_message(f"Your secret note for {person.mention} has been saved!", ephemeral=True)
@secret_note.error
async def secret_note_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_privileged_error(interaction, error)

@bot.tree.command(name="read_notes", description="⭐ [PRIVATE] Read the secret notes left for you.")
@is_privileged()
async def read_notes(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    
    user_notes = secret_notes.get(user_id, [])
    
    if not user_notes:
        await interaction.response.send_message("There are no secret notes waiting for you.", ephemeral=True)
        return

    # Sort notes by timestamp, newest first
    user_notes.sort(key=lambda x: x['timestamp'], reverse=True)

    embed = discord.Embed(
        title=f"💌 Secret Notes for {interaction.user.display_name}",
        color=discord.Color.red()
    )

    for note in user_notes[:10]: # Show up to 10 latest notes
        dt_obj = datetime.fromisoformat(note['timestamp'])
        # Format for display, e.g., "Aug 01, 2025 at 04:10 PM"
        formatted_time = dt_obj.strftime("%b %d, %Y at %I:%M %p")
        embed.add_field(
            name=f"From {note['author_name']} on {formatted_time}",
            value=f"```{note['message']}```",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)
@read_notes.error
async def read_notes_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_privileged_error(interaction, error)

@bot.tree.command(name="clear_my_notes", description="⭐ [PRIVATE] Deletes all secret notes left for you.")
@is_privileged()
async def clear_my_notes(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id in secret_notes and secret_notes[user_id]:
        note_count = len(secret_notes[user_id])
        secret_notes[user_id] = []
        save_notes()
        await interaction.response.send_message(f"I have cleared {note_count} secret note(s) for you.", ephemeral=True)
    else:
        await interaction.response.send_message("You have no secret notes to clear.", ephemeral=True)
@clear_my_notes.error
async def clear_my_notes_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_privileged_error(interaction, error)

# --- RUN THE BOT ---
if DISCORD_TOKEN is None:
    print("Error: DISCORD_BOT_TOKEN not found in .env file.")
else:
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("Error: Improper token has been passed. Please check your DISCORD_BOT_TOKEN.")
