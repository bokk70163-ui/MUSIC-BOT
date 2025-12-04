import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import asyncio
import subprocess
import shutil
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from dotenv import load_dotenv

# Load Environment Variables
load_dotenv()

TOKEN = "MTM5MTgwODQ5ODIwNzAzMTQxNw.GN5Int.EugemPMKjfTw_MndsOEV8E-0e1YWCjCs1L9K2c"
SPOTIFY_CLIENT_ID = "672e1ce1011147d5ac207b9c5106bdd7"
SPOTIFY_CLIENT_SECRET = "2407476713ea466cbe3c499e93befdbc"

# Logging Setup
logging.basicConfig(level=logging.INFO)

# Directory Setup
DOWNLOAD_DIR = "./temp_music"
if os.path.exists(DOWNLOAD_DIR):
    shutil.rmtree(DOWNLOAD_DIR) # Clear cache on startup
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Spotify Setup
spotify_client = None

def get_spotify():
    global spotify_client
    if spotify_client is None:
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            return None
        auth_manager = SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET
        )
        spotify_client = spotipy.Spotify(auth_manager=auth_manager)
    return spotify_client

# --- SpotDL Logic (Runs in Executor to avoid blocking bot) ---
def run_spotdl(url: str) -> str | None:
    env = os.environ.copy()
    env["SPOTIPY_CLIENT_ID"] = SPOTIFY_CLIENT_ID or ""
    env["SPOTIPY_CLIENT_SECRET"] = SPOTIFY_CLIENT_SECRET or ""
    
    # Check existing files to identify the new one later
    existing_files = set(os.listdir(DOWNLOAD_DIR)) if os.path.exists(DOWNLOAD_DIR) else set()
    output_template = os.path.join(DOWNLOAD_DIR, "{artist} - {title}.{output-ext}")
    
    try:
        # Using subprocess to run spotdl
        result = subprocess.run(
            [
                "spotdl", "download", url,
                "--output", output_template,
                "--overwrite", "force",
                "--format", "mp3" 
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=DOWNLOAD_DIR
        )
        
        logging.info(f"SpotDL Output: {result.stdout}")
        
        # Find the new file
        new_files = set(os.listdir(DOWNLOAD_DIR)) - existing_files
        mp3_files = [f for f in new_files if f.endswith('.mp3')]
        
        if mp3_files:
            return os.path.join(DOWNLOAD_DIR, mp3_files[0])
        
        # Fallback: check most recent file
        all_mp3s = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith('.mp3')]
        if all_mp3s:
            all_mp3s.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
            return os.path.join(DOWNLOAD_DIR, all_mp3s[0])
            
        return None
    except Exception as e:
        logging.error(f"Error running spotdl: {e}")
        return None

# --- Discord Bot Setup ---
class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        logging.info("Slash commands synced.")

bot = MusicBot()

# --- UI Views (Buttons) ---

# 1. Control View (Volume, Pause, Stop)
class PlayerControlView(discord.ui.View):
    def __init__(self, voice_client):
        super().__init__(timeout=None)
        self.voice_client = voice_client

    @discord.ui.button(label="⏯️ Pause/Resume", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client.is_playing():
            self.voice_client.pause()
            await interaction.response.send_message("Paused.", ephemeral=True)
        elif self.voice_client.is_paused():
            self.voice_client.resume()
            await interaction.response.send_message("Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client.is_connected():
            self.voice_client.stop()
            await interaction.response.send_message("Stopped playback.", ephemeral=True)
            self.stop() # Stop the view

    @discord.ui.button(label="🔊 Vol +", style=discord.ButtonStyle.secondary)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client.source:
            self.voice_client.source.volume = min(self.voice_client.source.volume + 0.1, 2.0)
            await interaction.response.send_message(f"Volume set to {int(self.voice_client.source.volume * 100)}%", ephemeral=True)

    @discord.ui.button(label="🔉 Vol -", style=discord.ButtonStyle.secondary)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client.source:
            self.voice_client.source.volume = max(self.voice_client.source.volume - 0.1, 0.0)
            await interaction.response.send_message(f"Volume set to {int(self.voice_client.source.volume * 100)}%", ephemeral=True)

# 2. Search Result Selection View
class SongSelectView(discord.ui.View):
    def __init__(self, tracks, interaction_original):
        super().__init__(timeout=60)
        self.tracks = tracks
        self.ctx_interaction = interaction_original
        
        # Add a button for each track found
        for i, track in enumerate(tracks):
            track_name = track['name']
            artists = ", ".join([a['name'] for a in track['artists']])
            label = f"{i+1}. {track_name} - {artists}"
            if len(label) > 80: label = label[:77] + "..."
            
            # Using partial to capture the specific track info
            btn = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, custom_id=f"track_{i}")
            btn.callback = self.create_callback(track)
            self.add_item(btn)

    def create_callback(self, track):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            # Disable all buttons after selection
            for child in self.children:
                child.disabled = True
            await self.ctx_interaction.edit_original_response(view=self)
            
            # Trigger download and play
            url = track['external_urls']['spotify']
            await play_logic(interaction, url, track_name=f"{track['name']} - {track['artists'][0]['name']}")
            
        return callback

# --- Core Logic ---

def cleanup_file(file_path):
    """Callback to run after audio finishes to delete the file."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logging.info(f"Deleted file: {file_path}")
    except Exception as e:
        logging.error(f"Error deleting file: {e}")

async def play_logic(interaction: discord.Interaction, url: str, track_name: str = "Unknown Track"):
    """Handles downloading and playing the audio."""
    voice_client = interaction.guild.voice_client
    
    if not voice_client:
         if interaction.user.voice:
            voice_client = await interaction.user.voice.channel.connect()
         else:
            await interaction.followup.send("You are not in a voice channel!")
            return

    await interaction.followup.send(f"⬇️ Downloading: **{track_name}**... Please wait.")

    # Stop current playing if any
    if voice_client.is_playing():
        voice_client.stop()

    loop = asyncio.get_event_loop()
    file_path = await loop.run_in_executor(None, run_spotdl, url)

    if file_path and os.path.exists(file_path):
        # Create Audio Source with Volume Control
        source = discord.FFmpegPCMAudio(file_path)
        volume_source = discord.PCMVolumeTransformer(source, volume=1.0) # Default 100%

        def after_playing(error):
            cleanup_file(file_path)
            if error:
                logging.error(f"Player error: {error}")

        voice_client.play(volume_source, after=after_playing)
        
        view = PlayerControlView(voice_client)
        await interaction.followup.send(f"▶️ Now Playing: **{track_name}**", view=view)
    else:
        await interaction.followup.send("❌ Failed to download song. Please try again.")

# --- Commands ---

@bot.tree.command(name="join", description="Joins your voice channel")
async def join(interaction: discord.Interaction):
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        await channel.connect()
        await interaction.response.send_message(f"Joined {channel.name}")
    else:
        await interaction.response.send_message("You are not in a voice channel.")

@bot.tree.command(name="leave", description="Leaves the voice channel")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("Left the channel.")
    else:
        await interaction.response.send_message("I am not in a voice channel.")

@bot.tree.command(name="play", description="Plays a song from Spotify link or Search query")
@app_commands.describe(query="Spotify Link or Song Name")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer() # Acknowledge interaction to avoid timeout

    # Check Voice State
    if not interaction.user.voice:
        await interaction.followup.send("You need to be in a voice channel first!")
        return
    
    # Auto-join if not connected
    if not interaction.guild.voice_client:
        await interaction.user.voice.channel.connect()

    sp = get_spotify()
    if not sp:
        await interaction.followup.send("Spotify API not configured.")
        return

    # Check if Link or Search
    if "open.spotify.com" in query:
        # Direct Link Mode
        track_id_match = "track" in query # Simple check, can be improved with regex
        if track_id_match:
            try:
                # Ideally fetch metadata here for better UX
                await play_logic(interaction, query, track_name="Spotify Link")
            except Exception as e:
                 await interaction.followup.send(f"Error processing link: {e}")
        else:
            await interaction.followup.send("Currently only Spotify Track links are supported directly.")
    else:
        # Search Mode
        try:
            results = sp.search(q=query, type='track', limit=5)
            tracks = results.get('tracks', {}).get('items', [])

            if not tracks:
                await interaction.followup.send("No songs found.")
                return

            view = SongSelectView(tracks, interaction)
            await interaction.followup.send("🔎 Select a song:", view=view)

        except Exception as e:
            logging.error(f"Search Error: {e}")
            await interaction.followup.send("An error occurred during search.")

if __name__ == "__main__":
    if not TOKEN:
        print("Error: BOT_TOKEN is missing.")
    else:
        bot.run(TOKEN)
