# -*- coding: utf-8 -*-
"""
덕장 차트 → GitHub Pages 배포
=============================

한 번 실행하면:
  1) build_static.py 로 docs/ 에 최신 데이터 생성 (KRX 로그인 → pykrx)
  2) git commit + push → GitHub Pages 자동 반영

공개 URL: https://windfos2-dot.github.io/deokjang-chart/

사용법
------
    python publish_chart.py                # 전종목 재빌드 후 배포
    python publish_chart.py --no-build     # 재빌드 없이 docs/ 현재 상태만 배포
    python publish_chart.py --kr-limit 300 # 상위 300종목만 (빠른 테스트)
    python publish_chart.py --keep-history # 데이터 커밋을 쌓는다 (기본은 덮어쓰기)

왜 기본이 '덮어쓰기(squash)' 인가
---------------------------------
종목당 JSON 이 ~35KB 라 전종목이면 한 번에 ~92MB 다. 갱신할 때마다 새 커밋을
쌓으면 저장소가 하루 92MB 씩 불어나 금방 못 쓰게 된다. 그래서 직전 데이터
커밋을 amend 로 덮어쓰고 force-with-lease 로 밀어 저장소 크기를 일정하게 유지한다.
코드 커밋은 건드리지 않는다 — 덮어쓰는 대상은 아래 DATA_MSG 로 시작하는 커밋뿐이다.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
PY = HERE / ".venv" / "Scripts" / "python.exe"      # 윈도우 venv
if not PY.is_file():
    PY = HERE / ".venv" / "bin" / "python"          # macOS/리눅스
if not PY.is_file():
    PY = Path(sys.executable)

BRANCH = "master"
DATA_MSG = "데이터 갱신"
SITE_URL = "https://windfos2-dot.github.io/deokjang-chart/"


def run(cmd, cwd=HERE, check=True):
    print(f"$ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd))
    if check and r.returncode != 0:
        raise SystemExit(f"실패(exit {r.returncode}): {' '.join(str(c) for c in cmd)}")
    return r.returncode


def out(cmd, cwd=HERE):
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd),
                       capture_output=True, text=True, encoding="utf-8")
    return (r.stdout or "").strip()


def main() -> int:
    argv = sys.argv[1:]
    build = "--no-build" not in argv
    keep_history = "--keep-history" in argv

    build_args = []
    for flag in ("--kr-limit", "--us-limit"):
        if flag in argv:
            build_args += [flag, argv[argv.index(flag) + 1]]
    for flag in ("--skip-us", "--skip-kr"):
        if flag in argv:
            build_args.append(flag)

    if build:
        print("① 데이터 빌드 …")
        run([PY, HERE / "build_static.py", *build_args])

    if not (HERE / "docs" / "index.json").is_file():
        raise SystemExit("docs/index.json 이 없습니다 — build_static.py 가 실패했습니다.")

    print("② 변경 확인 …")
    run(["git", "add", "-A"])
    if run(["git", "diff", "--cached", "--quiet"], check=False) == 0:
        print("   변경 없음 — 배포 스킵")
        return 0

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    last = out(["git", "log", "-1", "--format=%s"])
    amend = (not keep_history) and last.startswith(DATA_MSG)

    print("③ 커밋 …")
    if amend:
        print(f"   직전 데이터 커밋 덮어쓰기: {last!r}")
        run(["git", "commit", "-q", "--amend", "-m", f"{DATA_MSG} {ts}"])
    else:
        run(["git", "commit", "-q", "-m", f"{DATA_MSG} {ts}"])

    print("④ 푸시 …")
    if amend:
        run(["git", "push", "--force-with-lease", "origin", BRANCH])
    else:
        run(["git", "push", "origin", BRANCH])

    print(f"\n✓ 배포 완료 → {SITE_URL}  (반영까지 1~2분)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
