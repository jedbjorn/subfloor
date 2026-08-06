Future work unit 11 context; do not start before its assignment.

PR #1027 established the relay status contract that the Windows skills and documentation must preserve: `running` is the owned/identity-verified tunnel signal; `listening` reports any live socket; `unverified` identifies a live socket without trusted owned state; and `socket` is present when listening. Task #326 must update broker/relay status guidance to teach those distinctions and must retain the public-error ruling that raw primitive output and Python stack traces never enter `error.details`.
