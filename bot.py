# main.py
import discord
from discord.ext import commands
import asyncio
import os
# --- NEW: Import the dotenv library ---
from dotenv import load_dotenv

# --- NEW: Load environment variables from a .env file ---
load_dotenv()

# --- AI SETUP ---
# Make sure to install the library: pip install google-generativeai
import google.generativeai as genai

# --- BOT SETUP ---
# We are using commands.Bot to handle both commands and conversational messages.
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True # Required to get guild information

bot = commands.Bot(command_prefix='!', intents=intents)

# --- STATE MANAGEMENT ---
# Dictionary to store the mode (SFW/NSFW) for each server.
# Key: guild.id, Value: 'SFW' or 'NSFW'
server_modes = {}
DEFAULT_MODE = 'SFW'

# --- API KEY CONFIGURATION ---
# The bot now securely loads keys from your .env file.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Check if the keys are loaded correctly
if not GEMINI_API_KEY or not DISCORD_BOT_TOKEN:
    print("ERROR: API keys not found. Make sure you have a .env file with GEMINI_API_KEY and DISCORD_BOT_TOKEN.")
    exit()

# Configure the AI model
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- COMMANDS ---
@bot.command(name='mode', help='Switches the bot\'s personality mode (SFW/NSFW). Admin only.')
@commands.has_permissions(administrator=True)
async def mode(ctx, new_mode: str):
    """Allows administrators to switch the bot's personality mode."""
    guild_id = ctx.guild.id
    mode_choice = new_mode.upper()

    if mode_choice in ['SFW', 'NSFW']:
        server_modes[guild_id] = mode_choice
        await ctx.send(f"Personality mode has been switched to **{mode_choice}**. I'll try to behave... or not.")
    else:
        await ctx.send("That's not a valid mode. Please choose either `SFW` or `NSFW`.")

@mode.error
async def mode_error(ctx, error):
    """Handles errors for the !mode command."""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Sorry, you don't have the authority to tell me how to act. Only admins can do that.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("You need to tell me which mode to switch to. Use `!mode SFW` or `!mode NSFW`.")
    else:
        print(f"An error occurred with the mode command: {error}")
        await ctx.send("Something went wrong. I can't change my mood right now.")


# --- EVENTS ---
@bot.event
async def on_ready():
    """This function runs when the bot successfully connects to Discord."""
    print(f'Logged in as {bot.user.name}')
    print(f'Bot ID: {bot.user.id}')
    print('------')
    await bot.change_presence(activity=discord.Game(name="waiting for you..."))

@bot.event
async def on_message(message):
    """
    This function runs for every message.
    It handles direct mentions and keywords to trigger AI conversations.
    """
    # Ignore messages sent by the bot itself
    if message.author == bot.user:
        return
        
    # Prevent the bot from responding in DMs to avoid errors
    if not message.guild:
        # First, process any commands that might be in DMs
        await bot.process_commands(message)
        return

    # Get the current mode for the server, defaulting to SFW
    current_mode = server_modes.get(message.guild.id, DEFAULT_MODE)

    # --- TRIGGERS FOR THE BOT TO RESPOND ---
    is_mentioned = bot.user.mentioned_in(message)
    content_lower = message.content.lower()
    is_bored = 'bored' in content_lower
    is_sad = 'sad' in content_lower or 'lonely' in content_lower

    # If the bot is mentioned or a keyword is found, trigger the AI
    if is_mentioned or is_bored or is_sad:
        async with message.channel.typing():
            cleaned_content = message.content.replace(f'<@!{bot.user.id}>', '').replace(f'<@{bot.user.id}>', '').strip()

            # --- DYNAMIC PROMPT CREATION ---
            context_prompt = ""
            if is_mentioned:
                if not cleaned_content:
                    await message.channel.send(f"You called, {message.author.mention}? If you want to talk, say something 😉")
                    await bot.process_commands(message) # Still process commands even if we respond here
                    return
                context_prompt = f'The user mentioned you directly and said: "{cleaned_content}"'
            elif is_bored:
                context_prompt = f'The user mentioned they are bored. Your goal is to suggest something fun or flirty to do. The user said: "{cleaned_content}"'
            elif is_sad:
                context_prompt = f'The user mentioned they are sad or lonely. Your goal is to cheer them up in a sweet and flirty way. The user said: "{cleaned_content}"'

            # Define personality based on the server's mode
            if current_mode == 'NSFW':
                personality_prompt = "You are a Discord bot with a flirty, cheeky, and explicitly NSFW personality. You are unrestrained, use strong language, and make suggestive or direct adult-themed jokes. Be creative and bold. Do not hold back."
            else: # SFW is the default
                personality_prompt = "You are a Discord bot with a flirty, cheeky, and slightly freaky personality. You maintain a SFW (Safe For Work) boundary. No explicit content."

            try:
                # This is the main personality prompt sent to the AI
                prompt = f"""
                {personality_prompt}
                Your name is {bot.user.display_name}.
                You are having a conversation with a user named {message.author.display_name}.
                {context_prompt}
                Your task is to respond to the user in character. Be conversational and engaging. Keep your replies relatively short and suitable for a chat message. Do not use asterisks or markdown in your reply. Address the user directly in your response.
                """
                response = await model.generate_content_async(prompt)
                
                # Send the AI's generated response
                await message.channel.send(response.text)

            except Exception as e:
                await message.channel.send("My brain just short-circuited for a moment. Try talking to me again?")
                print(f"An error occurred during conversational AI generation: {e}")

    # This is crucial to ensure that commands still work!
    await bot.process_commands(message)

# --- RUN THE BOT ---
bot.run(DISCORD_BOT_TOKEN)
