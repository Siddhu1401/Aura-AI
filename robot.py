# Aura AI Bot - A Personal Companion (v2 - Stable)
# This script uses discord.py and the Google Gemini API for multimodal chat.

import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import google.generativeai as genai
import asyncio
from PIL import Image
import io

# --- SETUP ---
# Load environment variables from a .env file
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- CONFIGURATION ---
# Set the name of the channel where the bot will actively chat
CHAT_CHANNEL_NAME = "chat-with-aura"

# Configure the Gemini API
try:
    genai.configure(api_key=GEMINI_API_KEY)
except Exception as e:
    print(f"Error configuring Gemini API: {e}\nMake sure you have a valid GEMINI_API_KEY in your .env file.")
    exit()

# Set up the bot with necessary intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- STATE MANAGEMENT ---
# Dictionary to store conversation history for the chat feature
CONVERSATION_HISTORY = {}

# --- HELPER FUNCTIONS ---

async def send_long_response(interaction_or_message, text):
    """
    Sends a response, splitting it into multiple messages if it's too long.
    Works for both slash command interactions and regular message replies.
    """
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)]
    
    # Check if this is an interaction (slash command) or a message (chat)
    is_interaction = isinstance(interaction_or_message, discord.Interaction)
    
    for i, chunk in enumerate(chunks):
        if i == 0:
            # Send the first chunk as a followup or reply
            if is_interaction:
                await interaction_or_message.followup.send(chunk)
            else:
                await interaction_or_message.reply(chunk)
        else:
            # Send subsequent chunks to the same channel
            await interaction_or_message.channel.send(chunk)

async def ask_aura_ai(prompt: str, image: Image.Image = None, user_id: int = None):
    """
    Sends a prompt to the Gemini API and returns the response.
    Maintains conversation history if a user_id is provided.
    """
    print(f"Sending prompt to AI for user {user_id}: '{prompt[:50]}...'")
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        history = CONVERSATION_HISTORY.get(user_id, [])
        chat = model.start_chat(history=history)

        message_parts = [prompt]
        if image:
            message_parts.append(image)

        loop = asyncio.get_running_loop()
        send_func = lambda: chat.send_message(message_parts)
        
        response = await asyncio.wait_for(loop.run_in_executor(None, send_func), timeout=60.0)
        
        CONVERSATION_HISTORY[user_id] = chat.history
        print("Received response from AI.")
        return response.text

    except asyncio.TimeoutError:
        print("AI response timed out.")
        return "Sorry, I took too long to think and my response timed out. Please try again!"
    except Exception as e:
        print(f"An error occurred while calling the Gemini API: {e}")
        return "Sorry, I encountered an error while thinking. Please try again."

# --- BOT EVENTS ---
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
    """Handles the continuous chat feature."""
    if message.author == bot.user:
        return
    
    if message.channel.name == CHAT_CHANNEL_NAME and bot.user.mentioned_in(message):
        async with message.channel.typing():
            prompt = message.content.replace(f'<@{bot.user.id}>', '').strip()
            user_id = message.author.id
            
            ai_response = await ask_aura_ai(prompt, user_id=user_id)
            
            # Use the helper function to send the response safely
            await send_long_response(message, ai_response)

# --- AI SLASH COMMANDS ---

@bot.tree.command(name="ask_image", description="Ask Aura a question about an image or ask for edits.")
@app_commands.describe(
    image="Upload the image you want to discuss.",
    prompt="What would you like to know or what edits should I suggest?"
)
async def ask_image(interaction: discord.Interaction, image: discord.Attachment, prompt: str):
    await interaction.response.defer(thinking=True)
    try:
        image_data = await image.read()
        img = Image.open(io.BytesIO(image_data))
        ai_response = await ask_aura_ai(prompt, image=img)
        
        embed = discord.Embed(title="🖼️ Image Analysis", color=discord.Color.teal())
        embed.set_image(url=image.url)
        
        # Check if the response is too long for the embed description
        if len(ai_response) <= 4096:
            embed.description = ai_response
            await interaction.followup.send(embed=embed)
        else:
            # If too long, send the embed without a description, then send the text
            await interaction.followup.send(embed=embed)
            await send_long_response(interaction, ai_response)

    except Exception as e:
        print(f"Error processing image: {e}")
        await interaction.followup.send("Sorry, I had trouble reading that image file. Please try another one.")

@bot.tree.command(name="plan_my_day", description="Aura will help you create a simple schedule for your day.")
@app_commands.describe(goals="Describe your tasks and goals for the day.")
async def plan_my_day(interaction: discord.Interaction, goals: str):
    await interaction.response.defer(thinking=True)
    prompt = f"Please create a simple, organized schedule based on these goals: '{goals}'. Suggest timings and add a positive, encouraging note at the end."
    ai_response = await ask_aura_ai(prompt)
    embed = discord.Embed(title="✨ Here's a Plan for Your Day! ✨", description=ai_response, color=discord.Color.purple())
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="summarize", description="Aura will summarize a long piece of text for you.")
@app_commands.describe(text="Paste the text or article link you want to summarize.")
async def summarize(interaction: discord.Interaction, text: str):
    await interaction.response.defer(thinking=True)
    prompt = f"Please provide a concise, easy-to-read summary of the following text or webpage: '{text}'"
    ai_response = await ask_aura_ai(prompt)
    embed = discord.Embed(title="📝 Here's a Summary!", description=ai_response, color=discord.Color.blue())
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="brainstorm", description="Aura will help you brainstorm ideas on any topic.")
@app_commands.describe(topic="What do you need ideas for?")
async def brainstorm(interaction: discord.Interaction, topic: str):
    await interaction.response.defer(thinking=True)
    prompt = f"Please brainstorm a list of creative and interesting ideas for the following topic: '{topic}'"
    ai_response = await ask_aura_ai(prompt)
    embed = discord.Embed(title=f"💡 Ideas for '{topic}'", description=ai_response, color=discord.Color.gold())
    await interaction.followup.send(embed=embed)

# --- RUN THE BOT ---
if DISCORD_TOKEN is None:
    print("Error: DISCORD_BOT_TOKEN not found in .env file.")
else:
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("Error: Improper token has been passed. Please check your DISCORD_BOT_TOKEN.")
