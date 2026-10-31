#!/bin/zsh
# 이 스크립트를 더블클릭하면 Blogbot.app과 Blogbot 종료.app을 만든다.
# 결과물은 /Users/blue/Blog/dist/ 에 생기며, Applications 폴더로 드래그하면 된다.

set -e
cd "$(dirname "$0")/.."

mkdir -p dist

echo "▶ Blogbot.app 만드는 중..."
osacompile -o dist/Blogbot.app installers/blogbot.applescript

echo "▶ Blogbot 종료.app 만드는 중..."
osacompile -o "dist/Blogbot 종료.app" installers/blogbot-stop.applescript

echo ""
echo "✓ 완료!"
echo ""
echo "만들어진 파일:"
ls -d dist/*.app 2>/dev/null

echo ""
echo "──────────────────────────────────────────────────"
echo "설치 방법:"
echo "  1) Finder에서 /Users/blue/Blog/dist 폴더 열기"
echo "  2) Blogbot.app 을 Applications 폴더로 드래그"
echo "  3) (선택) Blogbot 종료.app 도 함께 드래그"
echo ""
echo "실행 방법:"
echo "  • Cmd+Space (Spotlight) → 'blogbot' 입력 → Enter"
echo "  • 또는 Launchpad에서 blogbot 아이콘 클릭"
echo "  • 또는 Applications에서 Blogbot.app 더블클릭"
echo "──────────────────────────────────────────────────"
echo ""
echo "이 창은 아무 키나 눌러 닫으세요."
read -k1
