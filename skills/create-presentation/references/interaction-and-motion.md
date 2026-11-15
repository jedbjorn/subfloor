# Interaction and motion

Read when implementing navigation, galleries, timed playback, animated diagrams, or presenter mode.

## Navigation and focus

Use one transition function to update the active slide, hidden state, counters, progress, navigation selection, title or hash, announcements, and presenter state. Handle malformed or missing hashes safely; local browsers may restrict history updates. Scope substate selection to the current slide rather than assuming one gallery per deck.

For the Up/Down slides and Left/Right substates mapping:

- Handle shortcuts across the audience window, including when controls or a video have focus. A capture-phase listener can arbitrate navigation before nested controls consume the event; ensure it produces only one transition.
- Leave modified combinations alone: Shift, Ctrl, Alt, and Meta. Respect already-handled events and editable targets, including inherited `isContentEditable`, inputs, textareas, and selects. Handle dialog-specific focus before deck navigation where a modal is present.
- Only prevent default and stop propagation for a shortcut the deck actually owns in that state. Preserve browser and native controls otherwise. For example, focused tabs can retain Home/End while the unfocused deck uses them for first/last slide.
- If navigation hides the focused element, move focus to a stable, focusable presentation region. Within a gallery or tab list, update `aria-selected`, `aria-controls`, and roving `tabindex`; manual keyboard selection should move focus to the new control.
- Choose clamped or wrapping boundaries deliberately and apply the same choice to keyboard, buttons, and presenter commands. Disable boundary buttons where clamped.

Use polite announcements for slide or stage changes without narrating every animation frame. Give SVGs meaningful titles/descriptions with unique IDs. Preserve visible focus and clickable controls; keyboard shortcuts are supplemental. Test shortcuts after clicking tabs, playback controls, fullscreen, and empty slide space. Native video fullscreen may be browser-controlled: test available behavior and supply an exit path rather than claiming every key can be intercepted.

## One visible video at a time

A useful live-presentation default is muted, inline playback after a brief arrival delay. Set that delay from the brief and allow manual-only playback when appropriate. Use posters and native controls if autoplay is refused. Do not loop footage by default.

Maintain a single pending autoplay timer. On slide or media-panel change, cancel it, pause outgoing videos, and schedule only the newly visible video. At timer expiry, verify that the same video is still active and the document visible. Recheck after `play()` resolves: navigation can change while playback is being prepared. If it is no longer active, pause it.

Manual play/pause or seeking cancels pending autoplay; do not restart a clip the presenter deliberately paused. On any video `play`, pause other videos and reject playback of hidden media. On document hide, cancel pending autoplay and pause videos. Returning to the tab should not unexpectedly override a manual pause. Derive presenter play/pause labels from actual media events, not optimistic button state.

## Sequential SVG storytelling

Define stages and timing explicitly. For sequential builds:

`start[i + 1] = start[i] + duration[i] + gap[i]`

Keep an initial delay separate. An animation duration is not its start interval: starting every second with a two-second draw creates overlap. Use overlap only when intentional. Synchronize JavaScript stage changes and CSS timing from shared configuration where practical.

For equal durations and gaps, zero-based stage `n` starts at `initialDelay + n * (duration + gap)`. For example, a requested one-second initial delay, one-second animation, and one-second idle gap produces starts at 1, 3, and 5 seconds; three stages finish at 6 seconds. It does not mean starts at 1, 2, and 3. Verify start, midpoint, completion, and idle intervals against the requested values. These numbers illustrate the arithmetic, not a required pace.

Reveal or emphasize the current stage, retain earlier context, then hold the completed visual. Manual stage selection cancels automatic progression immediately. Replay resets the sequence. Cancel timers on slide exit; pause/resume with remaining time when the document is hidden if preserving playback position is useful. Do not accumulate timers on repeated entry. Choose whether re-entry restarts or resumes, and whether manual selection completes earlier stages; apply that choice consistently in controls and presenter commands. Under reduced motion, manual navigation must not hide information that the complete-state fallback is meant to retain.

For stroke drawing, calculate the length of each path or geometry separately with `getTotalLength()` after insertion, then animate its dash offset from that length to zero. Alternatively normalize each supported geometry with `pathLength="1"` and animate numeric dash values from `1` to `0`. Do not apply one large guessed dash length to every stroke: short lines remain invisible and then snap into view. A group has no path length; traverse its drawing elements. Exclude unsupported or zero-length geometry, and verify short and long strokes midway through the build.

Reduced motion should show a useful complete state immediately, not freeze content halfway or leave it transparent. Listen for preference changes during a session. Print must show final geometry, remove motion, reveal intended slides, and replace video with a meaningful poster. If printed gallery coverage matters, decide explicitly whether to print one representative item or expand all items.

## Separate presenter window

Open the notes window from a user gesture. Keep the audience window authoritative and send state snapshots containing current slide, notes, substate, and media status. Presenter controls send an allowlisted command; they should not maintain a competing slide index. Validate payload shape, bounds, and command names.

For direct `file://` use, origins can be opaque. `postMessage(..., '*')` may be necessary for delivery, so authenticate messages with **both** strict `event.source` equality to the known peer window and a fresh, cryptographically random per-opening channel token. Never accept an origin of `"null"` as proof of identity. Pass the token through the opened window's URL fragment and check it in both directions. Use an exact origin when running in a stable HTTP origin where possible. Avoid injecting untrusted note HTML; use plain text or sanitize external content.

Handle ready/state handshake, reuse/focus of an existing notes window, popup rejection with visible retry guidance, and explicit audience shutdown. Ping/pong with a bounded timeout detects reloaded, navigated, or disconnected peers; disable commands and explain how to reopen. A stale window must not control a new deck session. Clean up timers on close and test reloading either window. Notes are hidden from the projected view, not secret from someone who owns the HTML files.

Document extended-display setup: move notes to the presenter display and fullscreen the audience window. Mirroring cannot keep notes private.
