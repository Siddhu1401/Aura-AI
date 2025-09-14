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
import numpy as np
import aiohttp
import yt_dlp
from collections import deque
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import time
import logging

# --- SETUP ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s',
                    handlers=[
                        logging.FileHandler("bot_crash.log"),
                        logging.StreamHandler()
                    ])

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


# --- Global State & Theme Colors ---
SONG_QUEUES = {}
NOW_PLAYING_MESSAGES = {}
GUILD_VOLUMES = {}
THEME_COLOR_BLUE = discord.Color.from_rgb(52, 152, 219) 
THEME_COLOR_YELLOW = discord.Color.from_rgb(241, 196, 15) 

# --- Spotify and YouTube-DL Setup ---
if SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET:
    spotify = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET))
else:
    spotify = None
    print("Spotify credentials not found. Spotify integration will be disabled.")

async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))

def get_spotify_tracks(query):
    if not spotify or "spotify.com" not in query:
        return []
    songs = []
    try:
        if "track" in query:
            track = spotify.track(query)
            songs.append(f"{track['name']} {track['artists'][0]['name']}")
        elif "playlist" in query:
            results = spotify.playlist_tracks(query)
            for item in results.get('items', []):
                track = item.get('track')
                if track:
                    songs.append(f"{track['name']} {track['artists'][0]['name']}")
        elif "album" in query:
            results = spotify.album_tracks(query)
            for track in results.get('items', []):
                if track:
                    songs.append(f"{track['name']} {track['artists'][0]['name']}")
    except Exception as e:
        print(f"Error fetching Spotify data: {e}")
        return []
    return songs

# --- UI Modals and Views ---
class VolumeModal(discord.ui.Modal, title="Set Volume"):
    volume_input = discord.ui.TextInput(label="Volume Level (1-100)", placeholder="e.g., 50 for 50% volume", min_length=1, max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.source:
            return await interaction.response.send_message("Not playing anything!", ephemeral=True)
        try:
            new_volume = int(self.volume_input.value)
            if not 1 <= new_volume <= 100:
                raise ValueError()
            voice_client.source.volume = new_volume / 100.0
            GUILD_VOLUMES[str(interaction.guild_id)] = new_volume / 100.0
            await interaction.response.send_message(f"🔊 Volume set to **{new_volume}%**.", ephemeral=True)
        except (ValueError, TypeError):
            await interaction.response.send_message("Invalid input. Please enter a number between 1 and 100.", ephemeral=True)

class MusicControls(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance

    @discord.ui.button(label="❚❚ Pause", style=discord.ButtonStyle.secondary, custom_id="pause_resume", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if not voice_client: return await interaction.response.send_message("I'm not in a voice channel!", ephemeral=True)
        if voice_client.is_playing():
            voice_client.pause()
            button.label, button.style = "▶ Resume", discord.ButtonStyle.success
        elif voice_client.is_paused():
            voice_client.resume()
            button.label, button.style = "❚❚ Pause", discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.primary, custom_id="skip", row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()
            await interaction.response.send_message("Skipped!", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        guild_id = str(interaction.guild_id)
        if guild_id in SONG_QUEUES: SONG_QUEUES[guild_id].clear()
        if voice_client and voice_client.is_connected():
            voice_client.stop()
            await voice_client.disconnect()
            await interaction.response.send_message("Stopped and left the channel.", ephemeral=True)
            if guild_id in NOW_PLAYING_MESSAGES and NOW_PLAYING_MESSAGES[guild_id]:
                try: await NOW_PLAYING_MESSAGES[guild_id].delete()
                except discord.NotFound: pass
                NOW_PLAYING_MESSAGES[guild_id] = None

    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.primary, custom_id="shuffle", row=1)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = SONG_QUEUES.get(str(interaction.guild_id))
        if queue and len(queue) > 1:
            random.shuffle(queue)
            await interaction.response.send_message("Queue shuffled!", ephemeral=True)
        else:
            await interaction.response.send_message("Not enough songs to shuffle.", ephemeral=True)

    @discord.ui.button(label="📜 Queue", style=discord.ButtonStyle.secondary, custom_id="queue", row=1)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = SONG_QUEUES.get(str(interaction.guild_id))
        if not queue:
            return await interaction.response.send_message("The queue is empty.", ephemeral=True)
        embed = discord.Embed(title="🎶 Song Queue", color=THEME_COLOR_BLUE)
        for i, song in enumerate(list(queue)[:10]):
            embed.add_field(name=f"{i+1}. {song['title']}", value="", inline=False)
        if len(queue) > 10:
            embed.set_footer(text=f"...and {len(queue)-10} more.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔊 Volume", style=discord.ButtonStyle.secondary, custom_id="volume", row=1)
    async def volume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VolumeModal())

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


# --- Game Storage ---
active_connect_four_games = {}
active_hangman_games = {}
active_word_ladder_games = {}
active_tictactoe_games = {}
active_anagram_games = {}
active_guess_the_number_games = {}


# --- Word Loading Logic ---
WL_VALID_WORDS = set()
WL_PAIRS = []
HM_EASY_WORDS, HM_MEDIUM_WORDS, HM_HARD_WORDS = [], [], []

try:
    with open('words.json', 'r') as f:
        data = json.load(f)
        ladder_words = [word.upper() for word in data.get('ladder_words', []) if len(word) == 4]
        WL_VALID_WORDS = set(ladder_words)
        if len(ladder_words) > 1:
            for _ in range(20):
                WL_PAIRS.append(tuple(random.sample(ladder_words, 2)))

        hangman_words = data.get('hangman_words', [])
        for word in hangman_words:
            l = len(word)
            if l <= 4: HM_EASY_WORDS.append(word.upper())
            elif l <= 6: HM_MEDIUM_WORDS.append(word.upper())
            else: HM_HARD_WORDS.append(word.upper())
        print("Successfully loaded words from words.json")
except FileNotFoundError:
    print("ERROR: words.json not found.")
except (json.JSONDecodeError, KeyError):
    print("ERROR: words.json is not formatted correctly.")


# --- Game Logic (Grouped by Game) ---

# --- Word Ladder Logic ---
def wl_get_word_pair():
    return random.choice(WL_PAIRS) if WL_PAIRS else ("WORD", "GAME")
def wl_is_valid_move(current, next_w, difficulty="hard"):
    if next_w.upper() not in WL_VALID_WORDS or len(current) != len(next_w): return False
    diff = sum(1 for c1, c2 in zip(current, next_w) if c1 != c2)
    return diff == 1 if difficulty == "hard" else 1 <= diff <= 2
def wl_format_ladder(ladder): return " → ".join(ladder) if ladder else "No words yet."

# --- Connect Four Logic ---
C4_ROWS, C4_COLS, C4_EMPTY, C4_P1, C4_P2 = 6, 7, "⚪", "🔴", "🟡"
def c4_create_board(): return np.full((C4_ROWS, C4_COLS), C4_EMPTY)
def c4_drop_piece(b, r, c, p): b[r][c] = p
def c4_is_valid_location(b, c): return b[0][c] == C4_EMPTY
def c4_get_next_open_row(b, c):
    for r in range(C4_ROWS - 1, -1, -1):
        if b[r][c] == C4_EMPTY: return r
    return None
def c4_check_win(b, p):
    for c in range(C4_COLS - 3):
        for r in range(C4_ROWS):
            if all(b[r][c+i] == p for i in range(4)): return True
    for c in range(C4_COLS):
        for r in range(C4_ROWS - 3):
            if all(b[r+i][c] == p for i in range(4)): return True
    for c in range(C4_COLS - 3):
        for r in range(C4_ROWS - 3):
            if all(b[r+i][c+i] == p for i in range(4)): return True
    for c in range(C4_COLS - 3):
        for r in range(3, C4_ROWS):
            if all(b[r-i][c+i] == p for i in range(4)): return True
    return False
def c4_format_board(b):
    h = "".join([f"{i+1}\u20e3" for i in range(C4_COLS)]) + "\n"
    return h + "\n".join(["".join(r) for r in b])

# --- Hangman Logic ---
HANGMAN_PICS = ['```\n  +---+\n  |   |\n      |\n      |\n      |\n      |\n=========\n```', '```\n  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n=========\n```', '```\n  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n=========\n```', '```\n  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n=========\n```', '```\n  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n=========\n```', '```\n  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n=========\n```', '```\n  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n=========\n```']
def hm_get_random_word(difficulty="medium"):
    if difficulty == "easy" and HM_EASY_WORDS: return random.choice(HM_EASY_WORDS)
    if difficulty == "hard" and HM_HARD_WORDS: return random.choice(HM_HARD_WORDS)
    return random.choice(HM_MEDIUM_WORDS) if HM_MEDIUM_WORDS else "PUZZLE"
def hm_format_display(w, g): return "".join([f" {l} " if l in g else " __ " for l in w])

# --- Tic-Tac-Toe Logic ---
TTT_EMPTY, TTT_P1, TTT_P2 = "➖", "❌", "⭕"
def ttt_check_win(b, p):
    wins = [[(0,0),(0,1),(0,2)],[(1,0),(1,1),(1,2)],[(2,0),(2,1),(2,2)],[(0,0),(1,0),(2,0)],[(0,1),(1,1),(2,1)],[(0,2),(1,2),(2,2)],[(0,0),(1,1),(2,2)],[(0,2),(1,1),(2,0)]]
    for w in wins:
        if all(b[r][c] == p for r,c in w): return True
    return False

# --- Anagrams Logic ---
def get_anagram_word(d="medium"): return hm_get_random_word(d)
def scramble_word(w):
    s = list(w); random.shuffle(s); scrambled = "".join(s)
    return scramble_word(w) if scrambled == w else scrambled


# --- Guess the Number Logic ---
def gtn_generate_number(): return random.randint(1, 100)

# --- Discord UI Views ---
class C4GameView(discord.ui.View):
    def __init__(self, gs):
        super().__init__(timeout=300); self.game_state = gs
        for i in range(C4_COLS): self.add_item(C4ColumnButton(str(i+1), i))
    async def interaction_check(self, i):
        if i.user.id != self.game_state["players"][self.game_state["turn_index"]].id:
            await i.response.send_message("It's not your turn!", ephemeral=True); return False
        return True
    async def handle_win(self, i, w):
        for item in self.children: item.disabled = True
        e = i.message.embeds[0]; e.description = f"**🎉 {w.mention} wins! 🎉**\n\n{c4_format_board(self.game_state['board'])}"; e.color = discord.Color.green()
        await i.response.edit_message(embed=e, view=self); del active_connect_four_games[i.message.id]
    async def handle_draw(self, i):
        for item in self.children: item.disabled = True
        e = i.message.embeds[0]; e.description = f"**🤝 It's a draw! 🤝**\n\n{c4_format_board(self.game_state['board'])}"; e.color = discord.Color.gold()
        await i.response.edit_message(embed=e, view=self); del active_connect_four_games[i.message.id]
class C4ColumnButton(discord.ui.Button):
    def __init__(self, l, c):
        super().__init__(style=discord.ButtonStyle.secondary, label=l); self.column = c
    async def callback(self, i):
        gs, b = self.view.game_state, self.view.game_state["board"]
        if not c4_is_valid_location(b, self.column): await i.response.send_message("This column is full!", ephemeral=True); return
        r, p = c4_get_next_open_row(b, self.column), gs["pieces"][gs["turn_index"]]
        c4_drop_piece(b, r, self.column, p)
        if c4_check_win(b, p): await self.view.handle_win(i, i.user); return
        if C4_EMPTY not in b: await self.view.handle_draw(i); return
        gs["turn_index"] = 1 - gs["turn_index"]
        e, np = i.message.embeds[0], gs["players"][gs["turn_index"]]
        e.description = f"{c4_format_board(b)}\n\nIt's **{np.mention}'s** turn ({gs['pieces'][gs['turn_index']]})"
        await i.response.edit_message(embed=e, view=self.view)
class C4ChallengeView(discord.ui.View):
    def __init__(self, ch, op):
        super().__init__(timeout=60); self.challenger, self.opponent = ch, op
    async def interaction_check(self, i):
        if i.user.id != self.opponent.id: await i.response.send_message("This challenge is not for you.", ephemeral=True); return False
        return True
    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, i, b):
        gs = {"board": c4_create_board(), "players": [self.challenger, self.opponent], "pieces": [C4_P1, C4_P2], "turn_index": 0}
        e = discord.Embed(title=f"Connect Four: {self.challenger.display_name} vs. {self.opponent.display_name}", description=f"{c4_format_board(gs['board'])}\n\nIt's **{self.challenger.mention}'s** turn ({C4_P1})", color=discord.Color.blue())
        await i.response.edit_message(content="Challenge accepted!", embed=e, view=C4GameView(gs))
        msg = await i.original_response(); active_connect_four_games[msg.id] = gs; self.stop()
    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, i, b): await i.response.edit_message(content=f"{self.opponent.mention} declined.", view=None); self.stop()
class HangmanView(discord.ui.View):
    def __init__(self, gs):
        super().__init__(timeout=300); self.game_state = gs
        for i, l in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            if i // 5 >= 5: break
            self.add_item(HangmanLetterButton(l, i // 5))
    async def interaction_check(self, i):
        if i.user.id != self.game_state["player"].id: await i.response.send_message("This is not your game!", ephemeral=True); return False
        return True
    async def update_game(self, i):
        gs = self.game_state; wd = hm_format_display(gs["word"], gs["guessed"]); dr = HANGMAN_PICS[gs["wrong_guesses"]]
        e = i.message.embeds[0]; e.description = f"{dr}\n\nThe word has **{len(gs['word'])}** letters.\n\n**Word:**{wd}\n\n**Guessed:** {' '.join(sorted(list(gs['guessed'])))}"
        if " __ " not in wd:
            e.color, e.title = discord.Color.green(), "🎉 You Win! 🎉"
            for item in self.children: item.disabled = True
            del active_hangman_games[i.message.id]
        elif gs["wrong_guesses"] >= len(HANGMAN_PICS) - 1:
            e.color, e.title = discord.Color.red(), "💀 You Lost! 💀"; e.description = f"{dr}\n\nThe word was: **{gs['word']}**"
            for item in self.children: item.disabled = True
            del active_hangman_games[i.message.id]
        await i.response.edit_message(embed=e, view=self)
class HangmanLetterButton(discord.ui.Button):
    def __init__(self, l, r):
        super().__init__(style=discord.ButtonStyle.secondary, label=l, row=r); self.letter = l
    async def callback(self, i):
        gs = self.view.game_state; self.disabled = True; gs["guessed"].add(self.letter)
        if self.letter not in gs["word"]: gs["wrong_guesses"] += 1
        await self.view.update_game(i)
class WordLadderView(discord.ui.View):
    def __init__(self, gs):
        super().__init__(timeout=300); self.game_state = gs
    async def interaction_check(self, i):
        if i.user.id not in [p.id for p in self.game_state["players"]]: await i.response.send_message("This is not your game!", ephemeral=True); return False
        return True
    @discord.ui.button(label="Make a Move", style=discord.ButtonStyle.primary)
    async def make_move_button(self, i, b): await i.response.send_modal(WordLadderInputModal(self.game_state))
class WordLadderInputModal(discord.ui.Modal, title="Submit Your Next Word"):
    def __init__(self, gs):
        super().__init__(); self.game_state = gs
        self.next_word = discord.ui.TextInput(label="Your Word", placeholder="Enter the next word...", min_length=len(gs["start_word"]), max_length=len(gs["start_word"]))
        self.add_item(self.next_word)
    async def on_submit(self, i):
        pi = 0 if len(self.game_state["players"]) == 1 or i.user.id == self.game_state["players"][0].id else 1
        pl, cw, nwi = self.game_state["ladders"][pi], self.game_state["ladders"][pi][-1], self.next_word.value.upper()
        if not wl_is_valid_move(cw, nwi, self.game_state["difficulty"]): await i.response.send_message(f"'{nwi}' is not a valid move from '{cw}'.", ephemeral=True); return
        pl.append(nwi)
        e = i.message.embeds[0]
        if len(self.game_state["players"]) == 1:
            e.description = f"**Goal:** `{self.game_state['start_word']}` → `{self.game_state['end_word']}`\n\n**Your Ladder ({len(pl) - 1} points):**\n{wl_format_ladder(pl)}"
        else:
            p1, p2 = self.game_state["players"][0], self.game_state["players"][1]
            e.description = f"**Goal:** `{self.game_state['start_word']}` → `{self.game_state['end_word']}`\n\n**{p1.display_name}'s Ladder ({len(self.game_state['ladders'][0]) - 1} points):**\n{wl_format_ladder(self.game_state['ladders'][0])}\n\n**{p2.display_name}'s Ladder ({len(self.game_state['ladders'][1]) - 1} points):**\n{wl_format_ladder(self.game_state['ladders'][1])}"
        if nwi == self.game_state["end_word"]:
            e.title = f"🎉 {i.user.display_name} Wins! 🎉"; e.color = discord.Color.green()
            await i.response.edit_message(embed=e, view=None); del active_word_ladder_games[i.message.id]
        else: await i.response.edit_message(embed=e)
class WLChallengeView(discord.ui.View):
    def __init__(self, ch, op, d):
        super().__init__(timeout=60); self.challenger, self.opponent, self.difficulty = ch, op, d
    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, i, b):
        s, e = wl_get_word_pair()
        gs = {"players": [self.challenger, self.opponent], "start_word": s, "end_word": e, "ladders": [[s], [s]], "difficulty": self.difficulty}
        p1n, p2n = self.challenger.display_name, self.opponent.display_name
        em = discord.Embed(title=f"Word Ladder: {p1n} vs. {p2n}", color=discord.Color.blue(), description=f"**Goal:** `{s}` → `{e}`\n\n**{p1n}'s Ladder (0 points):**\n{wl_format_ladder([s])}\n\n**{p2n}'s Ladder (0 points):**\n{wl_format_ladder([s])}")
        await i.response.edit_message(content="Challenge accepted!", embed=em, view=WordLadderView(gs))
        msg = await i.original_response(); active_word_ladder_games[msg.id] = gs; self.stop()
    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, i, b): await i.response.edit_message(content=f"{self.opponent.mention} declined.", view=None); self.stop()
class TTTGameView(discord.ui.View):
    def __init__(self, gs):
        super().__init__(timeout=300); self.game_state = gs
        for r in range(3):
            for c in range(3): self.add_item(TTTSquareButton(r, c))
    async def interaction_check(self, i):
        if i.user.id != self.game_state["players"][self.game_state["turn_index"]].id: await i.response.send_message("It's not your turn!", ephemeral=True); return False
        return True
    async def handle_win(self, i, w):
        for item in self.children: item.disabled = True
        e = i.message.embeds[0]; e.description = f"**🎉 {w.mention} wins! 🎉**"; e.color = discord.Color.green()
        await i.response.edit_message(embed=e, view=self); del active_tictactoe_games[i.message.id]
    async def handle_draw(self, i):
        for item in self.children: item.disabled = True
        e = i.message.embeds[0]; e.description = "**🤝 It's a draw! 🤝**"; e.color = discord.Color.gold()
        await i.response.edit_message(embed=e, view=self); del active_tictactoe_games[i.message.id]
class TTTSquareButton(discord.ui.Button):
    def __init__(self, r, c):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=r); self.row, self.col = r, c
    async def callback(self, i):
        gs = self.view.game_state; pp = gs["pieces"][gs["turn_index"]]
        gs["board"][self.row][self.col] = pp; self.label = pp; self.style = discord.ButtonStyle.success if pp == TTT_P1 else discord.ButtonStyle.danger; self.disabled = True
        if ttt_check_win(gs["board"], pp): await self.view.handle_win(i, i.user); return
        if all(cell != TTT_EMPTY for row in gs["board"] for cell in row): await self.view.handle_draw(i); return
        gs["turn_index"] = 1 - gs["turn_index"]
        e, np = i.message.embeds[0], gs["players"][gs["turn_index"]]
        e.description = f"It's **{np.mention}'s** turn ({gs['pieces'][gs['turn_index']]})"
        await i.response.edit_message(embed=e, view=self.view)
class TTTChallengeView(discord.ui.View):
    def __init__(self, ch, op):
        super().__init__(timeout=60); self.challenger, self.opponent = ch, op
    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, i, b):
        gs = {"board": [[TTT_EMPTY for _ in range(3)] for _ in range(3)], "players": [self.challenger, self.opponent], "pieces": [TTT_P1, TTT_P2], "turn_index": 0}
        e = discord.Embed(title=f"Tic-Tac-Toe: {self.challenger.display_name} vs {self.opponent.display_name}", description=f"It's **{self.challenger.mention}'s** turn ({TTT_P1})", color=discord.Color.blue())
        await i.response.edit_message(content="Challenge accepted!", embed=e, view=TTTGameView(gs))
        msg = await i.original_response(); active_tictactoe_games[msg.id] = gs; self.stop()
    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, i, b): await i.response.edit_message(content=f"{self.opponent.mention} declined.", view=None); self.stop()
class AnagramView(discord.ui.View):
    def __init__(self, gs):
        super().__init__(timeout=120); self.game_state = gs
    @discord.ui.button(label="Guess the Word", style=discord.ButtonStyle.primary)
    async def guess_button(self, i, b): await i.response.send_modal(AnagramInputModal(self.game_state, self))
class AnagramInputModal(discord.ui.Modal, title="Unscramble the Word"):
    def __init__(self, gs, pv):
        super().__init__(); self.game_state, self.parent_view = gs, pv
        self.guess_input = discord.ui.TextInput(label="Your Guess", placeholder="Type the unscrambled word here...")
        self.add_item(self.guess_input)
    async def on_submit(self, i):
        guess = self.guess_input.value.upper(); cw = self.game_state["word"]
        if guess == cw:
            e = i.message.embeds[0]; e.title = f"🎉 {i.user.display_name} Solved It! 🎉"
            e.description = f"The scrambled word was `{self.game_state['scrambled']}`.\n\nThe correct word was **{cw}**!"
            e.color = discord.Color.green()
            for item in self.parent_view.children: item.disabled = True
            await i.response.edit_message(embed=e, view=self.parent_view); del active_anagram_games[i.message.id]
        else: await i.response.send_message(f"Sorry, '{guess}' is not the correct word. Try again!", ephemeral=True)
class GuessTheNumberView(discord.ui.View):
    def __init__(self, gs):
        super().__init__(timeout=180); self.game_state = gs
    @discord.ui.button(label="Make a Guess", style=discord.ButtonStyle.primary)
    async def make_guess_button(self, i, b): await i.response.send_modal(GuessTheNumberInputModal(self.game_state))
class GuessTheNumberInputModal(discord.ui.Modal, title="Guess The Number"):
    def __init__(self, gs):
        super().__init__(); self.game_state = gs
        self.guess_input = discord.ui.TextInput(label="Your Guess (1-100)", placeholder="Enter a number...")
        self.add_item(self.guess_input)
    async def on_submit(self, i):
        if not self.guess_input.value.isdigit(): await i.response.send_message("That's not a valid number!", ephemeral=True); return
        guess = int(self.guess_input.value); gs = self.game_state; gs["guesses"] += 1; e = i.message.embeds[0]
        if guess == gs["number"]:
            e.title = f"🎉 You Guessed It! 🎉"; e.color = discord.Color.green(); e.description = f"You guessed the number **{gs['number']}** in {gs['guesses']} guesses!"
            await i.response.edit_message(embed=e, view=None); del active_guess_the_number_games[i.message.id]
        else:
            hint = "Higher ⬆️" if guess < gs["number"] else "Lower ⬇️"
            e.description = f"Your last guess was `{guess}`. The number is **{hint}**"
            await i.response.edit_message(embed=e)

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

# --- MUSIC SLASH COMMANDS ---

@bot.tree.command(name="join", description="Join your current voice channel")
async def join_command(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message(embed=discord.Embed(title="❌ Error", description="You must be in a voice channel.", color=discord.Color.red()), ephemeral=True)
    try:
        await interaction.user.voice.channel.connect()
        await interaction.response.send_message(embed=discord.Embed(title="✅ Connected", description=f"Joined `{interaction.user.voice.channel.name}`.", color=THEME_COLOR_YELLOW))
    except Exception as e:
        await interaction.response.send_message(embed=discord.Embed(title="❌ Error", description=str(e), color=discord.Color.red()), ephemeral=True)

@bot.tree.command(name="leave", description="Leave the voice channel")
async def leave_command(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client:
        await voice_client.disconnect()
        SONG_QUEUES.pop(str(interaction.guild_id), None)
        await interaction.response.send_message(embed=discord.Embed(title="👋 Disconnected", color=THEME_COLOR_YELLOW))
    else:
        await interaction.response.send_message(embed=discord.Embed(title="❌ Not Connected", description="I'm not in a voice channel.", color=discord.Color.red()), ephemeral=True)

@bot.tree.command(name="play", description="Play a song or add it to the queue")
@app_commands.describe(song_query="Search query or Spotify URL")
async def play_command(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send(embed=discord.Embed(title="❌ Error", description="You must be in a voice channel.", color=discord.Color.red()))

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client
    if not voice_client: voice_client = await voice_channel.connect()
    elif voice_client.channel != voice_channel: await voice_client.move_to(voice_channel)

    song_queries = get_spotify_tracks(song_query) or [song_query]
    guild_id = str(interaction.guild_id)
    if guild_id not in SONG_QUEUES: SONG_QUEUES[guild_id] = deque()

    added_to_queue = []
    for query in song_queries:
        try:
            ydl_opts = {"format": "bestaudio", "noplaylist": True, "quiet": True, "extract_flat": True, "cookiefile": "cookies.txt"}
            results = await search_ytdlp_async(f"ytsearch1:{query}", ydl_opts)
            if not results or not results.get('entries'): continue
            video_info = results['entries'][0]
            title = video_info.get("title", "Untitled")
            webpage_url = video_info.get("url")
            SONG_QUEUES[guild_id].append({'webpage_url': webpage_url, 'title': title})
            added_to_queue.append(title)
        except Exception as e:
            await interaction.channel.send(embed=discord.Embed(title="❌ Fetch Error", description=f"Could not fetch '{query}'.\n`{e}`", color=discord.Color.red()))

    if not added_to_queue:
        return await interaction.followup.send(embed=discord.Embed(title="❌ No Results", description="Could not find any playable songs.", color=discord.Color.red()))

    first_song_title = added_to_queue[0]
    if voice_client.is_playing() or voice_client.is_paused():
        desc = f"Added **{len(added_to_queue)}** songs." if len(added_to_queue) > 1 else f"**{first_song_title}**"
        await interaction.followup.send(embed=discord.Embed(title="✅ Added to Queue", description=desc, color=THEME_COLOR_BLUE))
    else:
        await interaction.followup.send(embed=discord.Embed(title="🎵 Let's begin!", description=f"Queued up **{first_song_title}**.", color=THEME_COLOR_YELLOW))
        await play_next_song(voice_client, guild_id, interaction.channel)

async def play_next_song(voice_client, guild_id, channel):
    if guild_id in NOW_PLAYING_MESSAGES and NOW_PLAYING_MESSAGES[guild_id]:
        try: await NOW_PLAYING_MESSAGES[guild_id].delete()
        except discord.NotFound: pass
    
    if guild_id in SONG_QUEUES and SONG_QUEUES[guild_id]:
        song_data = SONG_QUEUES[guild_id].popleft()
        title, webpage_url = song_data['title'], song_data['webpage_url']
        try:
            stream_opts = {"format": "bestaudio", "quiet": True, "cookiefile": "cookies.txt"}
            stream_results = await search_ytdlp_async(webpage_url, stream_opts)
            audio_url = stream_results['url']
            ffmpeg_options = {"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5", "options": "-vn"}
            
            guild_volume = GUILD_VOLUMES.get(guild_id, 0.5) # Default to 50%
            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(audio_url, **ffmpeg_options), volume=guild_volume)
            
            voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_id, channel), bot.loop))
            
            embed = discord.Embed(title="🎶 Now Playing", description=f"**{title}**", color=THEME_COLOR_YELLOW)
            NOW_PLAYING_MESSAGES[guild_id] = await channel.send(embed=embed, view=MusicControls(bot))
        except Exception as e:
            await channel.send(embed=discord.Embed(title="❌ Playback Error", description=f"Could not play '{title}'. Skipping.\n`{e}`", color=discord.Color.red()))
            await play_next_song(voice_client, guild_id, channel)
    else:
        await asyncio.sleep(180)
        if voice_client.is_connected() and not voice_client.is_playing():
            await voice_client.disconnect()
            NOW_PLAYING_MESSAGES.pop(guild_id, None)


@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping_command(interaction: discord.Interaction):
    # Calculate the latency in milliseconds
    latency = round(bot.latency * 1000)
    
    # Create an embed to send as the reply
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"My latency is **{latency}ms**.",
        color=discord.Color.blue() # You can change this color
    )
    
    # Send the embed as a response
    await interaction.response.send_message(embed=embed)            

# --- GAMES SLASH COMMANDS ---

@bot.tree.command(name="connectfour", description="Challenge a player to Connect Four.")
async def connectfour(i, o: discord.Member):
    if o.bot or o.id == i.user.id: return await i.response.send_message("Invalid opponent.", ephemeral=True)
    await i.response.send_message(f"**Connect Four Challenge!**\n\n{i.user.mention} has challenged {o.mention}.", view=C4ChallengeView(i.user, o))

@bot.tree.command(name="hangman", description="Start a game of Hangman.")
@app_commands.describe(difficulty="How long should the word be?")
@app_commands.choices(difficulty=[app_commands.Choice(name="Easy (3-4 letters)", value="easy"), app_commands.Choice(name="Medium (5-6 letters)", value="medium"), app_commands.Choice(name="Hard (7+ letters)", value="hard")])
async def hangman(interaction: discord.Interaction, difficulty: str = "medium"):
    word = hm_get_random_word(difficulty)
    gs = {"word": word, "guessed": set(), "wrong_guesses": 0, "player": interaction.user}
    e = discord.Embed(title=f"Hangman ({difficulty.title()})", description=f"{HANGMAN_PICS[0]}\n\nThe word has **{len(word)}** letters.\n\n**Word:**{hm_format_display(word, set())}\n\n**Guessed:** (None yet)", color=discord.Color.blue())
    await interaction.response.send_message(embed=e, view=HangmanView(gs))
    msg = await interaction.original_response(); active_hangman_games[msg.id] = gs

@bot.tree.command(name="wordladder", description="Start a game of Word Ladder.")
@app_commands.describe(difficulty="Set the game difficulty.", opponent="The user you want to race (optional).")
@app_commands.choices(difficulty=[app_commands.Choice(name="Easy (1 or 2 letter changes)", value="easy"), app_commands.Choice(name="Hard (1 letter change only)", value="hard")])
async def wordladder(interaction: discord.Interaction, difficulty: str, opponent: discord.Member = None):
    if opponent:
        if opponent.bot or opponent.id == interaction.user.id: return await interaction.response.send_message("Invalid opponent.", ephemeral=True)
        await interaction.response.send_message(f"**Word Ladder Challenge!**\n\n{interaction.user.mention} has challenged {opponent.mention} to a race.", view=WLChallengeView(interaction.user, opponent, difficulty))
    else:
        s, e = wl_get_word_pair(); gs = {"players": [interaction.user], "start_word": s, "end_word": e, "ladders": [[s]], "difficulty": difficulty}
        em = discord.Embed(title=f"Word Ladder ({difficulty.title()})", color=discord.Color.blue(), description=f"**Goal:** `{s}` → `{e}`\n\n**Your Ladder (0 points):**\n{wl_format_ladder([s])}")
        await interaction.response.send_message(embed=em, view=WordLadderView(gs))
        msg = await interaction.original_response(); active_word_ladder_games[msg.id] = gs

@bot.tree.command(name="tictactoe", description="Challenge a player to Tic-Tac-Toe.")
async def tictactoe(interaction: discord.Interaction, opponent: discord.Member):
    if opponent.bot or opponent.id == interaction.user.id: return await interaction.response.send_message("Invalid opponent.", ephemeral=True)
    await interaction.response.send_message(f"**Tic-Tac-Toe Challenge!**\n\n{interaction.user.mention} has challenged {opponent.mention}.", view=TTTChallengeView(interaction.user, opponent))

@bot.tree.command(name="anagram", description="Starts a word scramble game.")
@app_commands.describe(difficulty="How long should the word be?")
@app_commands.choices(difficulty=[app_commands.Choice(name="Easy (3-4 letters)", value="easy"), app_commands.Choice(name="Medium (5-6 letters)", value="medium"), app_commands.Choice(name="Hard (7+ letters)", value="hard")])
async def anagram(interaction: discord.Interaction, difficulty: str = "medium"):
    word = get_anagram_word(difficulty)
    scrambled = scramble_word(word)
    gs = {"word": word, "scrambled": scrambled}
    e = discord.Embed(title=" unscramble the word!", description=f"The first person to unscramble this word wins:\n\n# `{scrambled}`", color=discord.Color.blurple())
    e.set_footer(text=f"Difficulty: {difficulty.title()}")
    await interaction.response.send_message(embed=e, view=AnagramView(gs))
    msg = await interaction.original_response(); active_anagram_games[msg.id] = gs


@bot.tree.command(name="guessthenumber", description="Start a game of Guess the Number.")
async def guessthenumber(i):
    gs = {"number": gtn_generate_number(), "guesses": 0, "player": i.user}
    e = discord.Embed(title="Guess the Number (1-100)", description="I'm thinking of a number between 1 and 100. What's your first guess?", color=discord.Color.teal())
    await i.response.send_message(embed=e, view=GuessTheNumberView(gs))
    msg = await i.original_response(); active_guess_the_number_games[msg.id] = gs

@bot.tree.command(name="help", description="Shows the rules for the games.")
async def help(i):
    e = discord.Embed(title="Puzzles Bot Help", description="Here's how to play the available games:", color=discord.Color.purple())
    e.add_field(name="🔴 Connect Four 🟡", value="**Objective:** Be the first to get four discs in a row.\n**How to Play:** Use `/connectfour @user` to challenge someone.", inline=False)
    e.add_field(name="💀 Hangman 💀", value="**Objective:** Guess the secret word before the hangman is drawn.\n**How to Play:** Use `/hangman` and choose a difficulty to start a solo game.", inline=False)
    e.add_field(name="🪜 Word Ladder 🪜", value="**Objective:** Turn the start word into the end word by changing letters.\n**How to Play:** Use `/wordladder` to play solo or add an `@user` to race.", inline=False)
    e.add_field(name="⚔️ Tic-Tac-Toe ⚔️", value="**Objective:** Be the first to get three of your marks in a row.\n**How to Play:** Use `/tictactoe @user` to challenge someone.", inline=False)
    e.add_field(name=" unscramble the word! Anagrams ", value="**Objective:** Be the first to unscramble the jumbled word.\n**How to Play:** Use `/anagram` and choose a difficulty to start a game for the channel.", inline=False)
    e.add_field(name="🔢 Guess the Number 🔢", value="**Objective:** Guess the secret number between 1 and 100.\n**How to Play:** Use `/guessthenumber` to start. The bot will tell you if your guess is higher or lower.", inline=False)
    await i.response.send_message(embed=e, ephemeral=True)


# --- RUN THE BOT ---
if DISCORD_TOKEN is None:
    print("Error: DISCORD_BOT_TOKEN not found in .env file.")
else:
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("Error: Improper token has been passed. Please check your DISCORD_BOT_TOKEN.")
