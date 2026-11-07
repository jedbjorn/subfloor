# Verification and delivery

Read before packaging or handing off a deck. Scale checks to implemented features and promised delivery modes; record actual results and unresolved limitations.

## Rendered layout

Inspect each slide and its meaningful substates at the presentation resolution, a smaller desktop viewport, and a phone-sized viewport. Include initial, mid-animation, and completed states. Check headings, useful body copy, SVG labels, footers, controls, and posters for clipping or unreadable scaling. Examine actual pixels as well as overflow measurements: an element can fit mathematically and still be illegible.

Compare standard media frames across slides at the same viewport. Ensure a portrait exception preserves useful detail and that its navigation remains reachable. Check short-height windows, resizing, and fullscreen where supported. Mobile content may scroll; fixed controls must not cover the bottom of the slide. Do not use `overflow:hidden` to disguise content that does not fit.

## Runtime behavior

Exercise direct navigation, slide and substate shortcuts, boundary behavior, hash entry, and focus after hiding an active panel. Repeat shortcuts from a video, tab, button, and editable field; modifier combinations should remain untouched. Check that handlers do not double-advance.

For video: arrive and wait, leave before autoplay, switch clips rapidly, pause before the delay expires, pause during playback, revisit, and hide/show the browser tab. Verify exclusive playback and no late background restart. Test refused autoplay using the native fallback where possible.

For builds: inspect short and long strokes while drawing, final hold, replay, manual interruption, rapid slide changes, hidden-tab pause, and reduced-motion behavior. Print preview should show the intended completed content and useful posters.

For two-window mode: test notes synchronization, substate controls, media controls, duplicate-open behavior, popup blocking guidance, close/reload/disconnect, and messages from the wrong window or wrong channel. Test from the actual local-file delivery path if offline use is promised.

## Offline and archive checks

Audit HTML, CSS, JavaScript, SVG, and nested assets for remote dependencies: fonts, imports, images, media, embeds, analytics, and runtime API calls. Distinguish optional outbound links from resources required to render or operate the deck.

Then perform a real runtime check with network access disabled or blocked and request logging available. Open the final package directly from disk in the intended browser, navigate the whole deck, start all media, and open presenter mode. Confirm missing resources, external requests, and console errors are absent or explicitly explained. A source search alone does not prove offline behavior; a cached resource can hide a missing asset in a normal browsing session.

Package from a clean staging directory. Include the entrypoint, presenter files if used, required assets, licenses, and concise user instructions. Exclude capture-session credentials, private datasets, raw debugging artifacts, and temporary outputs. Use relative paths throughout and preserve filename case.

Check archive integrity, extract into a fresh directory, and test that extracted copy. Verify every referenced local file exists. Report archive size and practical distribution constraints when relevant; do not upload merely to make a large package easier to share.

## Handoff

Provide the exact final artifact locations, how to open and navigate, how to use a second display when applicable, and the sound/content scope of any video export. Explain browser-specific limitations or untested modes concisely. Describe publication as a separate action unless already explicitly authorized. Do not state that an output is tested offline, accessible, or complete beyond the checks actually performed.
