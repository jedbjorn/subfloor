"""Raw-mode byte sink: copies stdin to a file until N bytes arrive.
usage: reader.py <outfile> <total-bytes> [--echo]
--echo: write one '.' to stdout per chunk (drives broker composer clean)."""
import os
import sys
import tty

out_path, total = sys.argv[1], int(sys.argv[2])
echo = "--echo" in sys.argv
tty.setraw(0)
open(out_path + ".ready", "w").close()
got = 0
with open(out_path, "wb") as fh:
    while got < total:
        data = os.read(0, 65536)
        if not data:
            break
        fh.write(data)
        fh.flush()
        got += len(data)
        if echo:
            os.write(1, b".")
