# Real footage and video exports

Read when recording product behavior, editing demonstrations, or exporting the deck as video.

## Capture an honest demonstration

Use the actual product, authorized access, and a suitable demonstration dataset. Respect restrictions on resets, writes, or live customer data; do not infer those permissions from a request to make a deck. A capture can use fictional data in the real application, but identify that distinction. Label conceptual illustrations or staged mockups as such.

Plan a small, understandable task whose real workflow supports the claim. Show enough context to establish what was asked, what the product did, and what changed. For agent or conversational demos, retain at least one complete thinking-animation cycle where present, the result, a meaningful follow-up, and actual tool receipts when those are part of the capability being demonstrated. These can form a coherent sequence across gallery clips; every excerpt need not repeat the whole interaction.

Keep captures natural: readable UI, deliberate pointer movements, enough time to understand an action, and no frantic scrolling. Do not replace live responses, inject simulated receipts, or conceal a failed action behind an apparent success. Shorten idle waits when useful, but keep the relationship between request and result clear. Use honest edit seams or an editing disclosure where acceleration or cuts might otherwise imply real-time performance. Never present edited latency as a measured benchmark.

Standard desktop videos should share a frame geometry and preserve the important UI. Portrait conversation captures can keep their native aspect ratio. If a genuine setup modal appears outside that crop, reframe the actual capture to show it, then return to the conversation; do not recreate the modal. Prefer contain/letterbox to distorted scaling or a crop that removes evidence. An enlarge/fullscreen control can make detailed footage usable during questions.

Create useful local posters. Confirm the source and encoded dimensions, duration, playback, and readability; the presence of an MP4 file is not proof of a usable clip. Keep enough provenance in the delivery notes to distinguish real UI, fictional data, edits, and any unavailable behavior without exposing private session details.

## Plan an export timeline

Choose the export mode from the brief. A full presentation preserves the explanatory story and requested media coverage; a short promotional or buzz edit selects highlights to fit its runtime and tempo. Neither should inherit a fixed slide duration without considering the content.

An interactive deck contains more than its initial slides. Inventory **every slide and substate**: diagram stages, gallery clips, and alternate demos. Map each to its export segment or an explicit omission. A complete export covers them; an abridged edit may use short gallery highlights. If every slide is requested, preserve that coverage even in a short edit. Do not silently record the first item in each gallery and call it complete.

Record, for each segment:

| Field | Purpose |
| --- | --- |
| Slide and substate | Unambiguous entry point and ordering |
| Entry/reveal timing | Initial delay, build duration, and gaps |
| Dwell | Time to read and understand the settled state |
| Media | Selected clip, actual duration, and any approved excerpt |
| Audio | Source audio, narration, music, or intentional silence |
| Transition | Advance trigger and required final hold |

Account for autoplay delay and media duration; a fixed short slide dwell can cut a clip off before its useful result. Some reading overlaps motion or footage, so do not mechanically add every duration. Inspect the resulting pace. Hold important final states long enough to interpret them.

For a strict runtime cap, budget the timeline before rendering. Maintain a steady visual tempo with deliberate cuts and holds while keeping source product playback at 1× unless another speed is explicitly requested and disclosed. Select meaningful real snippets rather than speeding an entire interaction beyond comprehension. Preserve authentic thinking, result, follow-up, and receipts across the selected sequence as appropriate to its claim; do not imply a highlight reel contains a full unedited run. Keep a source-to-edit mapping so omitted context and coverage remain reviewable.

Resolve sound and narration preferences from the brief. Do not fabricate the user's voice or imply that silent output contains narration. A requested spoken version needs supplied or authorized narration and timing that follows it. Intentional silence is valid; report it clearly. Use music only when requested or otherwise within the established scope and rights.

## Capture motion, then encode

Record actual rendered frames and media over time. A slideshow of static screenshots is not an export of an animated, video-rich deck. Browser screencapture or a validated deterministic frame pipeline can work; verify that embedded videos advance and SVG animation phases are represented. Keep presenter notes, browser chrome, debug UI, and unintended pointer activity out of the audience capture.

Use a consistent output canvas and frame rate. A common compatibility target is 1920×1080, H.264 in MP4, `yuv420p`, and fast-start metadata; adapt aspect ratio, dimensions, and rate to the requested destination. Do not claim that transcoding a low-resolution recording creates new detail. An FFmpeg encoding stage can use `-c:v libx264 -pix_fmt yuv420p -movflags +faststart`; choose audio flags according to actual audio rather than inserting a misleading empty track.

Inspect metadata and seek through the encoded result, including opening, stage transitions, every gallery item, embedded-video endings, and closing hold. Verify the expected total duration, frame dimensions, codec, sound choice, and absence of blank/stalled sections. Sample motion at multiple timestamps; one thumbnail cannot validate playback. Reopen the final file with a normal player before delivery.
