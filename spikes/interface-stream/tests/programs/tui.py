"""TUI-ish program: alt screen, mouse modes, hidden cursor, self-driving
updates, then parks the cursor and idles.
usage: tui.py <go-file>"""
import os
import sys
import time


def w(data: bytes) -> None:
    while data:
        n = os.write(1, data)
        data = data[n:]


go = sys.argv[1]
while not os.path.exists(go):
    time.sleep(0.02)
w(b"\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[?25l\x1b[2J")
w(b"\x1b[3;5H\x1b[38;5;82mTUI-ALT\x1b[0m")
w(b"\x1b[7;15H\x1b[48;2;10;20;200m  pad  \x1b[0m")
for i in range(4):
    time.sleep(0.4)
    w(f"\x1b[12;8H\x1b[1;3{i % 7}mcount={i}\x1b[0m".encode())
w(b"\x1b[10;20H")  # park cursor
time.sleep(3600)
