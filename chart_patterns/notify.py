# -*- coding: utf-8 -*-
"""
스캔 결과를 텔레그램으로 전송 (기본: 본인 채팅 TELEGRAM_MY_CHAT_ID).

  python -m chart_patterns.notify                       # 스캔 후 전송
  python -m chart_patterns.notify --json output/chart_patterns.json --html output/chart_patterns.html
  python -m chart_patterns.notify --chat-id <id> --top 15

의존성: requests 만 사용 (python-telegram-bot 불필요).
"""
from __future__ import annotations

import argparse
import html as _html
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

import requests

from . import utf8_stdout

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.telegram.org/bot{token}/{method}"


def _env(name: str) -> Optional[str]:
    val = os.environ.get(name)
    if val:
        return val
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    return os.environ.get(name)


def summarize(results: List[dict], top: int = 12) -> str:
    """텔레그램 HTML 파스모드용 요약 (4096자 제한 고려)."""
    lines = ["<b>📊 차트패턴 스크리너</b>",
             "<i>Bulkowski Encyclopedia of Chart Patterns (3rd ed.) 규칙 기반</i>", ""]
    for res in results:
        label = "🇰🇷 국장" if res["market"] == "KR" else "🇺🇸 미장"
        lines.append(
            f"<b>{label}</b>  {res['universe']:,}종목 · 국면 {res['regime']} · "
            f"최근 {res['recent']}봉 · <b>{len(res['hits']):,}건</b>")
        for r in res["hits"][:top]:
            arrow = "▲" if r["direction"] == "up" else "▼"
            name = _html.escape((r["name"] or r["code"])[:16])
            tail = ""
            if r.get("back_inside"):
                tail = " ⚠회귀"
            elif r.get("to_trigger_pct") is not None:
                tail = f" (트리거 {r['to_trigger_pct']:+.0f}%)"
            up = (f"{r['upside_pct']:+.0f}%"
                  if r.get("upside_pct") is not None else "-")
            lines.append(
                f"<code>{r['score']:4.0f}</code> {arrow} <b>{name}</b> "
                f"{_html.escape(r['pattern_kr'])} · 여력 {up}{tail}")
        lines.append("")
    lines.append("<i>규칙 탐지는 경계 사례에서 사람 눈과 갈립니다. "
                 "첨부 HTML로 형태 확인 후 판단하세요.</i>")
    text = "\n".join(lines)
    return text[:4000]


HERE = Path(__file__).resolve().parent

# 모듈 zip 에 넣을 것 / 뺄 것
PACK_SUFFIX = (".py", ".json", ".csv", ".txt", ".md")
PACK_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
PACK_SKIP_NAMES = {".env"}


def build_module_zip(dest: Optional[Path] = None) -> Path:
    """chart_patterns 모듈을 다른 PC로 옮길 수 있는 zip 으로 묶는다.

    시세 캐시(ohlcv.db)는 제외한다 — 새 PC에서 API 로 재생성하는 게 맞다.
    book_patterns.json / us_universe.csv 는 포함 (이게 있어야 PDF 없이 돈다).
    """
    import zipfile
    dest = dest or (HERE.parent / "output" / "chart_patterns_module.zip")
    dest.parent.mkdir(parents=True, exist_ok=True)

    files = []
    for p in sorted(HERE.rglob("*")):
        if not p.is_file():
            continue
        if any(part in PACK_SKIP_DIRS for part in p.parts):
            continue
        if p.name in PACK_SKIP_NAMES or p.suffix.lower() not in PACK_SUFFIX:
            continue
        files.append(p)

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            z.write(p, Path("chart_patterns") / p.relative_to(HERE))
        z.writestr("chart_patterns/SETUP.txt", SETUP_TXT)
    return dest


SETUP_TXT = """\
차트패턴 스크리너 — 새 PC 설치 순서
====================================
0) 이 zip 을 프로젝트 루트에 풀면 chart_patterns/ 폴더가 생깁니다.
   (텔레그램 봇 레포와 같은 위치에 두면 us_market.db 도 자동 인식)

1) pip install -r chart_patterns/requirements.txt

2) 국장을 쓰려면 프로젝트 루트 .env 에 서비스키 추가:
      DART_API_KEY=<data.go.kr 서비스키>
   (미장만 쓸 거면 생략 가능)

3) python -m chart_patterns.doctor
      → 부족한 항목과 실행할 명령을 찍어줍니다.

4) python -m chart_patterns.ohlcv --market ALL --days 400
      국장 ~10분 / 미장 ~1분. 시세 DB(ohlcv.db)가 여기서 생성됩니다.

5) python -m chart_patterns.scan --market ALL --recent 10 --min-score 62
      HTML 리포트까지: --html output/chart_patterns.html

주의
 - ohlcv.db 는 이 zip 에 없습니다(용량). 4번에서 재생성됩니다.
 - book_patterns.json 은 저작권 있는 책에서 추출한 내용입니다.
   공개 저장소·외부 배포 금지.
 - 자세한 내용은 chart_patterns/README.md 참고.
"""


def send(token: str, chat_id: str, text: str,
         doc: Optional[Path] = None) -> bool:
    ok = True
    res = requests.post(API.format(token=token, method="sendMessage"),
                        data={"chat_id": chat_id, "text": text,
                              "parse_mode": "HTML",
                              "disable_web_page_preview": True}, timeout=30)
    if not res.ok or not res.json().get("ok"):
        print(f"메시지 전송 실패: {res.text[:300]}", file=sys.stderr)
        ok = False
    else:
        print("메시지 전송 완료")

    if doc:
        ok = send_file(token, chat_id, doc) and ok
    return ok


def send_file(token: str, chat_id: str, doc: Path,
              caption: Optional[str] = None) -> bool:
    if not doc.exists():
        print(f"파일 없음: {doc}", file=sys.stderr)
        return False
    mime = ("application/zip" if doc.suffix == ".zip"
            else "text/html" if doc.suffix in (".html", ".htm")
            else "application/octet-stream")
    cap = caption or f"{doc.name} ({doc.stat().st_size / 1e3:.0f}KB)"
    with doc.open("rb") as f:
        res = requests.post(
            API.format(token=token, method="sendDocument"),
            data={"chat_id": chat_id, "caption": cap[:1000]},
            files={"document": (doc.name, f, mime)}, timeout=180)
    if not res.ok or not res.json().get("ok"):
        print(f"파일 전송 실패: {res.text[:300]}", file=sys.stderr)
        return False
    print(f"파일 전송 완료: {doc.name} ({doc.stat().st_size / 1e3:.0f}KB)")
    return True


def _send_module(a) -> int:
    """모듈 zip 을 만들어 전송."""
    import zipfile
    z = build_module_zip()
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
    print(f"zip 생성: {z}  ({z.stat().st_size / 1e3:.0f}KB, {len(names)}개 파일)")
    for n in names:
        print(f"   {n}")
    if a.zip_only or a.dry_run:
        return 0

    token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = a.chat_id or _env("TELEGRAM_MY_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_MY_CHAT_ID 가 필요합니다 (.env)",
              file=sys.stderr)
        return 1

    caption = ("차트패턴 스크리너 모듈 (chart_patterns)\n"
               "압축 풀고 → pip install -r chart_patterns/requirements.txt\n"
               "→ python -m chart_patterns.doctor\n"
               "자세한 순서는 zip 안 SETUP.txt / README.md")
    return 0 if send_file(token, chat_id, z, caption) else 1


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="기존 스캔 JSON (없으면 새로 스캔)")
    ap.add_argument("--html", default="", help="첨부할 HTML 리포트")
    ap.add_argument("--chat-id", default="", help="기본: TELEGRAM_MY_CHAT_ID")
    ap.add_argument("--market", choices=["KR", "US", "ALL"], default="ALL")
    ap.add_argument("--recent", type=int, default=10)
    ap.add_argument("--min-score", type=float, default=62.0)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true", help="전송 없이 본문만 출력")
    ap.add_argument("--module", action="store_true",
                    help="스캔 결과 대신 모듈 소스 zip 을 전송 (다른 PC 이식용)")
    ap.add_argument("--zip-only", action="store_true",
                    help="--module 과 함께: 전송 없이 zip 만 생성")
    a = ap.parse_args()

    if a.module:
        return _send_module(a)

    if a.json and Path(a.json).exists():
        results = json.loads(Path(a.json).read_text(encoding="utf-8"))
    else:
        from .scan import scan_market
        markets = ["KR", "US"] if a.market == "ALL" else [a.market]
        results = []
        for m in markets:
            res = scan_market(m, recent=a.recent, min_score=a.min_score)
            if not res.get("error"):
                results.append(res)

    text = summarize(results, a.top)
    if a.dry_run:
        print(text)
        return 0

    token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = a.chat_id or _env("TELEGRAM_MY_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_MY_CHAT_ID 가 필요합니다 (.env)",
              file=sys.stderr)
        return 1
    doc = Path(a.html) if a.html else None
    return 0 if send(token, chat_id, text, doc) else 1


if __name__ == "__main__":
    raise SystemExit(main())
