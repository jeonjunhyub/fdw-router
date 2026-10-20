# -*- coding: utf-8 -*-
"""마크다운 표 열 수 무결성 검사 (정규식 수술 후 확인용)."""
import re
import sys

SEP = re.compile(r'^\|[\s:\-|]+\|?$')
BS = chr(92)


def ncol(s):
    s = s.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]
    n, esc = 1, False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == BS:
            esc = True
        elif ch == '|':
            n += 1
    return n


for p in sys.argv[1:]:
    lines = open(p, encoding='utf-8').read().split('\n')
    bad, ntab, i = [], 0, 0
    while i < len(lines):
        if lines[i].strip().startswith('|') and i + 1 < len(lines) and SEP.match(lines[i + 1].strip()):
            ntab += 1
            n = ncol(lines[i])
            i += 2
            while i < len(lines) and lines[i].strip().startswith('|'):
                if ncol(lines[i]) != n:
                    bad.append((i + 1, n, ncol(lines[i]), lines[i].strip()[:75]))
                i += 1
            continue
        i += 1
    print(f'{p}: 표 {ntab}개', '— 정상' if not bad else f'— 불일치 {len(bad)}건')
    for b in bad:
        print(f'   {b[0]}행 기대 {b[1]} 실제 {b[2]}: {b[3]}')
