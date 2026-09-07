# Real terminal capture from the dedicated disposable dos-app installation.
# Run from the Subfloor source checkout:
# vhs docs/demo.tape
# Requires a running exact-ref sandbox, its fresh Demo roster, an available
# CART1 shell, and existing Claude host authentication. See the capture record
# in maintainer/public-documentation-refresh.md. Never simulate boot output.
Output "docs/demo.gif"
Set Shell "bash"
Set FontSize 16
Set Width 1100
Set Height 1000
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 60ms

Hide
Type "source ~/.bashrc; cd /tmp/subfloor-docs-f73-dos-app; unset SC_API_BASE SC_API_TOKEN; export PS1='$ '; clear"
Enter
Sleep 1s
Show
Type "subfloor enter"
Enter
Sleep 4s
Type "Demo"
Enter
Sleep 4s
Screenshot docs/images/cli-picker.png

# Fresh roster: Admin is 1, Cartographer is 2.
Type "2"
Enter
Sleep 3s
# Harness menu: Claude is 1. Its installed account must already be signed in.
Type "1"
Enter
Hide
Sleep 35s
Show
Sleep 8s
