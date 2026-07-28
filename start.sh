#!/bin/bash
# 덕장 차트 실행 스크립트
# 사용법:  ./start.sh          (기본 8010 포트)
#          ./start.sh 9000     (포트 지정)

cd "$(dirname "$0")" || exit 1
PORT="${1:-8010}"

# 이미 떠 있으면 정리
pkill -f "uvicorn chart_router"  2>/dev/null
sleep 1

if [ ! -x ".venv/bin/python" ]; then
  echo "❌ .venv 가 없습니다. 아래를 먼저 실행하세요:"
  echo "   python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
  exit 1
fi

# uvicorn 실행파일은 shebang 에 절대경로가 박혀 폴더를 옮기면 깨진다.
# python -m 방식은 경로 이동에 영향받지 않는다.
echo "▶ 덕장 차트 시작 (포트 $PORT)"
./.venv/bin/python -m uvicorn chart_router:app --port "$PORT" --log-level warning &
SRV=$!

# 서버가 응답할 때까지 대기
for _ in $(seq 1 40); do
  if curl -s --max-time 2 "http://127.0.0.1:$PORT/api/chart/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

URL="http://127.0.0.1:$PORT/api/chart/ui"
echo "✅ 준비 완료 → $URL"
echo "   (끄려면 이 창에서 Ctrl+C)"
open "$URL" 2>/dev/null

wait $SRV
