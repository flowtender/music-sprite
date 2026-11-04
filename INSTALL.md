# 1. Get the code
sudo git clone git@github.com:flowtender/music-sprite.git /opt/music-sprite
cd /opt/music-sprite

# 2. Venv and dependencies (yt-dlp, discord.py, mutagen and httpx all come from pyproject.toml)
sudo python3 -m venv venv
sudo venv/bin/pip install .

# 3. Service user, with write access to your music folder
sudo useradd --system --no-create-home --shell /usr/sbin/nologin music-sprite
sudo chown -R music-sprite: /srv/music        # or use your actual MUSIC_ROOT

# 4. Config (fill in DISCORD_TOKEN, channel/user IDs, guild ID, MUSIC_ROOT, ...)
sudo cp music-sprite.env.example /etc/music-sprite.env
sudo chmod 600 /etc/music-sprite.env
sudo nano /etc/music-sprite.env

# 5. Install and start
sudo cp music-sprite.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now music-sprite

# 6. Watch it start
journalctl -u music-sprite -f
