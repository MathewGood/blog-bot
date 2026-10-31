#!/bin/zsh
# blogbot 서버를 macOS 로그인 시 자동으로 시작하도록 설정한다 (launchd agent).
# 이 스크립트를 한 번만 실행하면 이후 컴퓨터 켤 때마다 서버가 자동으로 뜬다.
# 해제하려면 아래 uninstall 부분을 실행.

set -e
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
PLIST_LABEL="com.blogbot.web"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

if [ "$1" = "--uninstall" ]; then
  echo "▶ 자동 시작 해제 중..."
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "✓ 해제 완료. 다시 켜려면 --uninstall 옵션 없이 이 스크립트를 실행하세요."
  read -k1; exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PROJECT_DIR}/.venv/bin/python</string>
        <string>-m</string>
        <string>blogbot</string>
        <string>web</string>
        <string>--no-browser</string>
        <string>--lan</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/blogbot-autostart.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/blogbot-autostart.log</string>
</dict>
</plist>
PLIST

# 기존 로드 해제 후 재로드
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo ""
echo "✓ 로그인 자동 시작 설정 완료!"
echo ""
echo "설정된 파일: $PLIST_PATH"
echo "로그: ${PROJECT_DIR}/blogbot-autostart.log"
echo ""
echo "──────────────────────────────────────────────────"
echo "지금부터:"
echo "  • 컴퓨터 켜면 blogbot 서버가 자동으로 실행됩니다"
echo "  • Chrome에서 http://127.0.0.1:8765 즐겨찾기로 저장하세요"
echo "  • 또는 Chrome에서 대시보드 열고 '설치'로 앱 창처럼 사용"
echo ""
echo "해제 방법:"
echo "  이 스크립트를 --uninstall 옵션과 함께 실행:"
echo "  bash '${0}' --uninstall"
echo "──────────────────────────────────────────────────"
echo ""
echo "지금 서버가 응답하는지 확인 중..."
sleep 3
if curl -sf http://127.0.0.1:8765/api/config > /dev/null; then
  echo "✓ 서버가 정상 응답합니다. http://127.0.0.1:8765 를 브라우저에서 열어 보세요."
else
  echo "⚠ 서버가 아직 응답하지 않습니다. 몇 초 더 기다렸다가 확인하세요."
  echo "   로그: tail -f ${PROJECT_DIR}/blogbot-autostart.log"
fi
echo ""
echo "이 창은 아무 키나 눌러 닫으세요."
read -k1
