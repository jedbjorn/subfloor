"""Emit a byte corpus to stdout (raw tty) once a go-sentinel exists, then idle.
usage: emitter.py <corpus-file> <go-file>"""
import os
import sys
import time
import tty

corpus, go = sys.argv[1], sys.argv[2]
while not os.path.exists(go):
    time.sleep(0.02)
tty.setraw(1)
data = open(corpus, "rb").read()
while data:
    n = os.write(1, data)
    data = data[n:]
time.sleep(3600)
