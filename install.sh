#!/data/data/com.termux/files/usr/bin/bash
# One-time setup. Run from inside the project folder:  bash install.sh
set -e
cd "$(dirname "$0")"

echo "[1/4] Installing packages..."
pkg update -y
pkg install -y python termux-api ffmpeg curl
pkg install -y llama-cpp || echo "    (llama-cpp not available, skipping local LLM; everything else works)"

echo "[2/4] API key..."
if [ ! -f .env ]; then
  cp .env.example .env
  read -r -p "    Chariot API key (press Enter to skip, uses Android voice): " KEY
  [ -n "$KEY" ] && sed -i "s|^CHARIOT_API_KEY=.*|CHARIOT_API_KEY=$KEY|" .env
  chmod 600 .env
fi

echo "[3/4] Optional local LLM (Qwen2.5 1.5B, ~1 GB) for open-ended chat"
mkdir -p models
if ! ls models/*.gguf >/dev/null 2>&1; then
  read -r -p "    Download it now? [y/N] " ANS
  if [ "$ANS" = "y" ] || [ "$ANS" = "Y" ]; then
    curl -L -o models/qwen2.5-1.5b-instruct-q4_k_m.gguf \
      "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"
  fi
fi

echo "[4/4] Auto-start on boot (needs the Termux:Boot app)"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/jarvis.sh" <<BOOT
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
bash "$PWD/start.sh" --no-open
BOOT
chmod +x "$HOME/.termux/boot/jarvis.sh" start.sh stop.sh

echo
echo "Done. Next: allow permissions (see README), then run:  bash start.sh"
