# -*- coding: utf-8 -*-
"""
새 PC/서버에서 바로 돌아가는지 점검한다.

  python -m chart_patterns.doctor

패키지·키·데이터 각각을 확인하고, 부족한 건 실행할 명령까지 찍어준다.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
from pathlib import Path

from . import utf8_stdout

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

OK, WARN, BAD = "  OK  ", " 주의 ", " 필요 "

REQUIRED = [("numpy", "numpy"), ("requests", "requests")]
OPTIONAL = [("yfinance", "yfinance (미장 시세)"),
            ("fitz", "PyMuPDF (PDF 재파싱 — book_patterns.json 있으면 불필요)"),
            ("dotenv", "python-dotenv (.env 읽기)"),
            ("pandas", "pandas (yfinance 의존)")]


def _line(tag: str, msg: str, fix: str = "") -> bool:
    print(f"[{tag}] {msg}")
    if fix:
        print(f"        → {fix}")
    return tag == OK


def main() -> int:
    utf8_stdout()
    print(f"chart_patterns 점검  (python {sys.version.split()[0]}, {sys.platform})")
    print(f"  위치: {HERE}")
    print("-" * 66)
    fatal = 0

    # 1) 패키지
    for mod, label in REQUIRED:
        try:
            importlib.import_module(mod)
            _line(OK, f"{label}")
        except ImportError:
            fatal += 1
            _line(BAD, f"{label} 없음",
                  "pip install -r chart_patterns/requirements.txt")
    for mod, label in OPTIONAL:
        try:
            importlib.import_module(mod)
            _line(OK, label)
        except ImportError:
            _line(WARN, f"{label} 없음",
                  "pip install -r chart_patterns/requirements.txt")

    # 2) 책 데이터
    bj = HERE / "book_patterns.json"
    if bj.exists() and bj.stat().st_size > 100_000:
        import json
        n = json.loads(bj.read_text(encoding="utf-8"))["pattern_count"]
        _line(OK, f"book_patterns.json ({n}개 패턴, {bj.stat().st_size/1e3:.0f}KB)")
    else:
        fatal += 1
        _line(BAD, "book_patterns.json 없음 (탐지기 통계·한글명 소스)",
              "PDF 를 구해서: python -m chart_patterns.extract_book <PDF경로>")

    # 3) API 키 (국장)
    key = os.environ.get("DATAGO_KEY") or os.environ.get("DART_API_KEY")
    if not key:
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
            key = os.environ.get("DATAGO_KEY") or os.environ.get("DART_API_KEY")
        except ImportError:
            pass
    if key:
        _line(OK, f"data.go.kr 서비스키 ({ROOT/'.env'} 또는 환경변수)")
    else:
        _line(WARN, "DART_API_KEY 없음 → 국장 수집 불가 (미장은 가능)",
              f"{ROOT/'.env'} 에 DART_API_KEY=... 추가")

    # 4) 미장 유니버스
    if (ROOT / "us_market.db").exists():
        _line(OK, "미장 유니버스: us_market.db")
    elif (HERE / "us_universe.csv").exists():
        _line(OK, "미장 유니버스: us_universe.csv (동봉본)")
    else:
        _line(WARN, "미장 유니버스 없음 → 티커를 직접 넣어야 함")

    # 5) 시세 캐시
    db = HERE / "ohlcv.db"
    ready = []
    if db.exists():
        conn = sqlite3.connect(db)
        for m in ("KR", "US"):
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT code), MIN(dt), MAX(dt) "
                "FROM bars WHERE market=?", (m,)).fetchone()
            if row[0] > 50_000:
                ready.append(m)
                _line(OK, f"{m} 시세 {row[0]:,}행 / {row[1]:,}종목 / {row[2]}~{row[3]}")
            else:
                _line(WARN,
                      f"{m} 시세 {'부족' if row[0] else '없음'} ({row[0]:,}행)",
                      f"python -m chart_patterns.ohlcv --market {m} --days 400")
        conn.close()
    else:
        _line(WARN, "ohlcv.db 없음 (시세 캐시는 API 로 재생성 가능)",
              "python -m chart_patterns.ohlcv --market ALL --days 400")

    print("-" * 66)
    if fatal:
        print(f"필수 항목 {fatal}건 미충족 — 위 → 명령을 먼저 실행하세요.")
        return 1
    if not ready:
        print("코드는 준비됐지만 시세가 없습니다. 먼저 수집하세요:")
        print("  python -m chart_patterns.ohlcv --market ALL --days 400"
              "   (국장 ~10분 / 미장 ~1분)")
        return 0
    print(f"스캔 가능 ({'/'.join(ready)}):")
    print(f"  python -m chart_patterns.scan --market {ready[0]} "
          f"--recent 10 --min-score 62")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
