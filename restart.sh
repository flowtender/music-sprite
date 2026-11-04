git pull
sudo venv/bin/pip install .
sudo systemctl daemon-reload
sudo systemctl enable --now music-sprite
journalctl -u music-sprite -f