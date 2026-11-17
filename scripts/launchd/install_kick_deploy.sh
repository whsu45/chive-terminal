#!/bin/bash
# 在這台 Mac 裝一個 launchd 排程：每天 06:15、06:45、07:20、08:00、08:30 跑 scripts/kick_deploy.py，
# 網站還缺當天的夜盤三大法人、期交所又已經公布時，才觸發一次 deploy.yml（細節見 kick_deploy.py 開頭說明）。
# Mac 睡眠時錯過的時間，醒來後會補跑一次；關機就不會跑（GitHub 自己的排程照舊在）。
# 需要已登入的 gh（gh auth login，要有 workflow 權限）。紀錄寫在 ~/Library/Logs/chive-terminal-kick-deploy.log。
#
#   安裝／更新：bash scripts/launchd/install_kick_deploy.sh
#   馬上試跑：  launchctl kickstart gui/$(id -u)/com.chive-terminal.kick-deploy
#   移除：      bash scripts/launchd/install_kick_deploy.sh --uninstall
set -euo pipefail

LABEL=com.chive-terminal.kick-deploy
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/chive-terminal-kick-deploy.log"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已移除 $LABEL"
  exit 0
fi

PY="$(command -v python3)" || { echo "找不到 python3"; exit 1; }
GH="$(command -v gh)" || { echo "找不到 gh，請先安裝並 gh auth login"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh 還沒登入，請先 gh auth login"; exit 1; }
if [[ "$(date +%z)" != "+0800" ]]; then
  echo "注意：這台 Mac 不是台灣時區（$(date +%Z)），launchd 用本機時間，06:15 等時間會對不上"
fi

TIMES=(06:15 06:45 07:20 08:00 08:30)
INTERVALS=""
for t in "${TIMES[@]}"; do
  h=$((10#${t%:*})); m=$((10#${t#*:}))
  INTERVALS+="    <dict><key>Hour</key><integer>$h</integer><key>Minute</key><integer>$m</integer></dict>"$'\n'
done

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$REPO/scripts/kick_deploy.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$GH"):$(dirname "$PY"):/usr/bin:/bin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
$INTERVALS  </array>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "已安裝 $LABEL：每天 ${TIMES[*]} 檢查網站的夜盤三大法人，缺了才觸發 deploy.yml"
echo "紀錄：$LOG"
