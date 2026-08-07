# -*- coding: utf-8 -*-
"""Bulkowski 차트패턴 스크리너 (국장/미장)."""
import sys


def utf8_stdout() -> None:
    """윈도우 cp949 콘솔에서도 한글/기호가 깨지지 않게 강제 (CLI 진입점용).

    다른 PC로 옮겼을 때 PYTHONIOENCODING 을 안 걸어도 되도록.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
