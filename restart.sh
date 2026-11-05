git pull
sudo venv/bin/pip install .
sudo systemctl daemon-reload
sudo systemctl restart music-sprite
journalctl -u music-sprite -f