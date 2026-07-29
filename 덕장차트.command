#!/bin/bash
# 덕장 차트 — 맥에서 더블클릭으로 실행
#
# Finder 에서 이 파일을 더블클릭하면 터미널이 열리고 서버가 뜬 뒤
# 브라우저가 자동으로 열린다. 전종목(한국 2,700여 + 미국 6,900여)을
# 그때그때 조회하므로 파일로 내보낸 버전과 달리 데이터 제한이 없다.
#
# 끄려면 이 터미널 창에서 Control+C.

cd "$(dirname "$0")" || exit 1
PORT=8010
URL="http://127.0.0.1:$PORT/api/chart/ui"

printf '\033[1;36m'
cat <<'BANNER'
  덕장 차트
BANNER
printf '\033[0m\n'

# 이미 떠 있으면 브라우저만 열고 끝낸다 (중복 실행 방지)
if curl -s --max-time 2 "http://127.0.0.1:$PORT/api/chart/health" >/dev/null 2>&1; then
  echo "이미 실행 중입니다 → $URL"
  open "$URL"
  echo
  echo "이 창은 닫아도 됩니다."
  read -r -p "Enter 를 누르면 닫힙니다..." _
  exit 0
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "❌ 최초 1회 설치가 필요합니다. 아래를 복사해 터미널에 붙여넣으세요:"
  echo
  echo "   cd \"$(pwd)\" && python3.12 -m venv .venv && ./.venv/bin/python -m pip install -r requirements.txt yfinance"
  echo
  read -r -p "Enter 를 누르면 닫힙니다..." _
  exit 1
fi

echo "서버를 시작합니다 (포트 $PORT)…"
# uvicorn 실행파일은 shebang 에 절대경로가 박혀 폴더를 옮기면 깨진다.
# python -m 방식은 경로 이동에 영향받지 않는다.
./.venv/bin/python -m uvicorn chart_router:app --port "$PORT" --log-level warning &
SRV=$!

for _ in $(seq 1 60); do
  if curl -s --max-time 2 "http://127.0.0.1:$PORT/api/chart/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl -s --max-time 2 "http://127.0.0.1:$PORT/api/chart/health" >/dev/null 2>&1; then
  echo "❌ 서버가 응답하지 않습니다. 위 로그를 확인하세요."
  read -r -p "Enter 를 누르면 닫힙니다..." _
  kill $SRV 2>/dev/null
  exit 1
fi

echo "✅ 준비 완료 → $URL"
echo
echo "   검색창에 종목명·코드·티커를 입력하세요 (예: 삼성전자, 005930, NVDA)"
echo "   전종목 조회 가능 · 기간 1~20년 · 일/주/월봉"
echo
echo "   끄려면 이 창에서 Control+C"
echo
open "$URL"

# Control+C 로 종료할 때 서버도 같이 정리
trap 'echo; echo "종료합니다…"; kill $SRV 2>/dev/null; exit 0' INT TERM
wait $SRV
