"""Continuous numbered byte stream once a go-sentinel exists.
usage: streamer.py <go-file>"""
import os
import sys
import time
import tty

go = sys.argv[1]
while not os.path.exists(go):
    time.sleep(0.02)
tty.setraw(1)
i = 0
while True:
    data = f"LINE {i:06d}\r\n".encode()
    while data:
        n = os.write(1, data)
        data = data[n:]
    i += 1
    time.sleep(0.02)
