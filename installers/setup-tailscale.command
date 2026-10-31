#!/bin/zsh
# blogbot을 어디서든 접속 가능하게 하는 Tailscale 설치·설정 도우미.
# Homebrew가 있으면 자동 설치, 없으면 다운로드 페이지를 열어줌.

set -e
cd "$(dirname "$0")/.."

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Tailscale로 blogbot 어디서든 접속하기"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Tailscale은 본인 계정에 로그인한 기기끼리만 P2P로 연결하는"
echo "사설 VPN입니다. 공인 IP·포트포워딩 없이도 어디서나 접속 가능."
echo ""

# 이미 설치돼 있는지 확인
have_cli=false
if command -v tailscale >/dev/null 2>&1; then have_cli=true; fi
have_app=false
if [ -d "/Applications/Tailscale.app" ]; then have_app=true; fi

if $have_cli || $have_app; then
  echo "✓ Tailscale이 이미 설치돼 있습니다."
else
  echo "▶ Tailscale이 설치돼 있지 않습니다."
  echo ""
  if command -v brew >/dev/null 2>&1; then
    echo "Homebrew 발견. 자동 설치할까요? [y/N]"
    read -k1 ans; echo
    if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
      brew install --cask tailscale-app || brew install --cask tailscale || {
        echo "❌ 자동 설치 실패. Mac App Store에서 'Tailscale' 검색해 설치하세요."
        open "https://apps.apple.com/us/app/tailscale/id1475387142"
        exit 1
      }
    else
      echo "수동 설치: https://tailscale.com/download/mac"
      open "https://tailscale.com/download/mac"
      exit 0
    fi
  else
    echo "설치 페이지를 브라우저에서 열겠습니다."
    open "https://tailscale.com/download/mac"
    echo "설치 후 이 창을 닫고 이 스크립트를 다시 실행하세요."
    exit 0
  fi
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "다음 단계 (직접 하실 일):"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "① Mac에서:"
echo "   메뉴 바 오른쪽 위의 Tailscale 아이콘 클릭 → Sign in"
echo "   Google/Apple/Microsoft 등으로 로그인 (첫 사용자는 계정 생성)"
echo ""
echo "② iPhone/iPad에서:"
echo "   App Store에서 'Tailscale' 검색해 설치 → 열기"
echo "   방금 Mac에서 쓴 같은 계정으로 로그인"
echo "   VPN 프로필 허용 (Face ID/암호 확인)"
echo ""
echo "③ 확인:"
echo "   두 기기 모두 로그인 후, 아래 명령으로 Mac의 Tailscale IP 확인:"
echo ""
if [ -f "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]; then
  echo "   /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4"
else
  echo "   tailscale ip -4"
fi
echo ""
echo "④ blogbot 재시작하면 서버가 자동으로 Tailscale IP를 감지해"
echo "   대시보드 상단에 표시합니다. 폰 Safari에서 그 주소로 접속."
echo ""
echo "⑤ 폰 Safari에서 접속 후:"
echo "   공유 버튼(⬆️) → '홈 화면에 추가' → blogbot 아이콘 완성"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
# 현재 감지 상태 확인
detected=""
if [ -f "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]; then
  detected=$("/Applications/Tailscale.app/Contents/MacOS/Tailscale" ip -4 2>/dev/null | head -1) || true
elif command -v tailscale >/dev/null 2>&1; then
  detected=$(tailscale ip -4 2>/dev/null | head -1) || true
fi
if [ -n "$detected" ]; then
  echo "✓ 현재 Mac의 Tailscale IP: $detected"
  echo "  폰에서 접속할 주소: http://$detected:8765"
else
  echo "ℹ 아직 Tailscale에 로그인 안 된 상태입니다. 위 ①번 단계를 진행하세요."
fi
echo ""
echo "이 창은 아무 키나 눌러 닫으세요."
read -k1
