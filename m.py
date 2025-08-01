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

CHAT_CHANNEL_NAME = "chat-with-aura"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

server_modes = {}
conversation_history = {}
DEFAULT_MODE = "study_search"

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

        current_mode = server_modes.get(guild_id, DEFAULT_MODE)
        
        system_instruction = ""
        temperature = 0.7
        
        if current_mode == 'sfw_freaky':
            system_instruction = "You are a Discord bot with a flirty, cheeky, and slightly freaky personality. You maintain a SFW (Safe For Work) boundary. Be conversational, engaging, and playful. Do not use asterisks."
            temperature = 1.0
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

@bot.event
async def on_ready():
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
                await message.reply("You called? �")
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

@bot.tree.command(name="mode", description="Switches Aura's personality mode for this server (Admin only).")
@app_commands.describe(personality="Choose how you want Aura to behave.")
@app_commands.choices(personality=[
    discord.app_commands.Choice(name="Helpful Study/Search Assistant", value="study_search"),
    discord.app_commands.Choice(name="SFW Freaky & Flirty", value="sfw_freaky"),
    discord.app_commands.Choice(name="NSFW Freaky & Flirty", value="nsfw_freaky"),
])
@commands.has_permissions(administrator=True)
async def mode(interaction: discord.Interaction, personality: discord.app_commands.Choice[str]):
    server_modes[interaction.guild_id] = personality.value
    await interaction.response.send_message(f"My personality has been switched to **{personality.name}**. Let's see how this goes...", ephemeral=True)

@mode.error
async def mode_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("Sorry, only Admins have the authority to change my personality.", ephemeral=True)

@bot.tree.command(name="imagine", description="Aura will generate an image based on your prompt.")
@app_commands.describe(prompt="Describe the image you want Aura to create.")
async def imagine(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)
    try:
        genai.configure(api_key=get_next_api_key())
        
        img_model = genai.GenerativeModel('gemini-2.0-flash-preview-image-generation')
        
        image_generation_config = {"response_modalities": ['IMAGE']}
        
        response = await img_model.generate_content_async(
            prompt,
            generation_config=image_generation_config
        )
        
        img_part = next((part for part in response.parts if part.inline_data), None)
        if img_part:
            image_data = base64.b64decode(img_part.inline_data.data)
            image_file = discord.File(io.BytesIO(image_data), filename="imagine.png")
            await interaction.followup.send(f"Here's what I imagined for you, {interaction.user.mention}:", file=image_file)
        else:
            await interaction.followup.send("I tried to imagine, but my thoughts were filtered or I couldn't create an image from that. Try a different prompt!")
    except Exception as e:
        print(f"An error occurred during 'imagine' generation: {e}")
        await interaction.followup.send("Something went wrong in my imagination engine. Please try again later.")

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
        description=f"Here is a more detailed prompt based on your idea. Try using this with the `/imagine` command!\n\n**`{ai_response}`**",
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

if DISCORD_TOKEN is None:
    print("Error: DISCORD_BOT_TOKEN not found in .env file.")
else:
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("Error: Improper token has been passed. Please check your DISCORD_BOT_TOKEN.")
