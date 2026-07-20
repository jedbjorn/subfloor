# Records docs/demo.gif — the README demo of `./sc enter`.
#
# Re-record from the repo root:  vhs docs/demo.tape
# Needs: vhs + ttyd + ffmpeg on PATH, the fork's sandbox container up,
# and a warm analytics sweep (run any boot once first) so the hidden
# wait below covers the pre-session sweep comfortably.
Output "docs/demo.gif"
Set Shell "bash"
Set FontSize 16
Set Width 1000
Set Height 650
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 60ms

Type "./sc enter"
Enter
Sleep 3.5s

# Username prompt
Type "Jed"
Enter
Sleep 3s

# Shell picker — Cartographer
Type "1"
Enter
Sleep 2.5s

# Harness picker — claude
Type "1"
Enter
Sleep 1s

# The pre-session analytics sweep + boot happen here; skip the wait and
# land on the finished frame: boot summary, Review GUI panel, harness up.
Hide
Sleep 30s
Show
Sleep 8s
