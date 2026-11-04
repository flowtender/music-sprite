# 1. Get the code
```bash
sudo git clone git@github.com:flowtender/music-sprite.git /opt/music-sprite
cd /opt/music-sprite
```

# 2. Venv and dependencies
(yt-dlp, discord.py, mutagen and httpx all come from pyproject.toml)
```bash
sudo python3 -m venv venv
sudo venv/bin/pip install .
```

Optional but recommended, for audio fingerprint matching: install `fpcalc` (Arch: `chromaprint`, Debian/Ubuntu: `libchromaprint-tools`) and set `ACOUSTID_API_KEY` in step 4.

Optional, to tag what the audio itself reveals (tempo, key, genre guesses, moods, instruments, vocals, danceability, and loudness as ReplayGain): install the `analysis` extra instead (about 300 MB, TensorFlow included) and make sure `ffmpeg` is on the server. The ~35 MB of models download themselves on first use, into `ANALYSIS_MODEL_DIR`.
```bash
sudo venv/bin/pip install '.[analysis]'
```
This runs on the CPU, taking a few seconds per track. Without the extra the review card just says `Analysis: skipped`. To use an NVIDIA GPU instead, see [GPU analysis](#gpu-analysis-optional) at the end.

# 3. Service user
with write access to your music folder
```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin music-sprite
sudo chown -R music-sprite: /srv/music        # or use your actual MUSIC_ROOT
```

# 4. Config
(fill in DISCORD_TOKEN, channel/user IDs, guild ID, MUSIC_ROOT, ...)
```bash
sudo cp music-sprite.env.example /etc/music-sprite.env
sudo chmod 600 /etc/music-sprite.env
sudo nano /etc/music-sprite.env
```

# 5. Install and start
```bash
sudo cp music-sprite.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now music-sprite
```

# 6. Watch it start
```bash
journalctl -u music-sprite -f
```

# GPU analysis (optional)
The analysis models run on the CPU by default and take a few seconds per track; a GPU only speeds up the neural-network part, so the gain is modest (on an RTX 3060 about 2.6 s against 3.4 s per track, after a slower first run while CUDA starts up). The `essentia-tensorflow` wheel is built for CUDA 11.2 and cuDNN 8.1, which still support Pascal cards such as the GTX 1060. It needs the NVIDIA driver plus those two libraries; conda is the easiest way to get them without touching the system:
```bash
sudo conda create -y -p /opt/music-sprite/cuda -c conda-forge cudatoolkit=11.2 cudnn=8.1
sudo systemctl edit music-sprite
```
and in the editor add:
```ini
[Service]
Environment=LD_LIBRARY_PATH=/opt/music-sprite/cuda/lib
```
Then `sudo systemctl restart music-sprite`. The first analysis logs `audio analysis is running on the GPU` or `... on the CPU`. If it says CPU, TensorFlow could not load the libraries: run the service by hand and look for `Could not load dynamic library` (set `TF_CPP_MIN_LOG_LEVEL=0` to see it). Set `ANALYSIS_DEVICE=cpu` to never use the GPU.
