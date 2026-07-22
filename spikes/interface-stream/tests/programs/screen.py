"""Draw a colored curses-like screen once a go-sentinel exists, then idle.
usage: screen.py <go-file>"""
import os
import sys
import time

go = sys.argv[1]
while not os.path.exists(go):
    time.sleep(0.02)
parts = ["\x1b[2J\x1b[H"]
for r in range(1, 8):
    parts.append(f"\x1b[{r * 2};{r * 3}H\x1b[48;5;{r * 30}m\x1b[38;2;255;255;{r * 20}mBLOCK{r}\x1b[0m")
parts.append("\x1b[18;10H\x1b[1;4;7mMIXED⛄\x1b[0m")
parts.append("\x1b[22;5H\x1b[38;5;196;48;5;22mRED-ON-GREEN\x1b[0m")
parts.append("\x1b[23;1HSCREEN-DONE")
data = "".join(parts).encode()
while data:
    n = os.write(1, data)
    data = data[n:]
time.sleep(3600)
