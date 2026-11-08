#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")" || exit 1
if [ -f jarvis.pid ]; then kill "$(cat jarvis.pid)" 2>/dev/null; rm -f jarvis.pid; fi
pkill -f "python3 server.py" 2>/dev/null
termux-microphone-record -q >/dev/null 2>&1
echo "Jarvis stopped."
