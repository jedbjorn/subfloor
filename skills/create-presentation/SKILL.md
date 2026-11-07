---
name: create-presentation
description: Create or refine offline HTML sales and product presentations with interactive slides, animated diagrams, real product footage, presenter notes, and optional video exports. Use for presentation decks, not general websites or documents.
---

# Create a presentation

Build a presentation that works for its audience, speaking context, and delivery format. Preserve the requested visual direction; this skill specifies behavior and production mechanics, not a palette, typography, illustration style, slide count, or fixed narrative.

## Establish the brief

Use supplied source material and existing assets first. Ideally a .md file is supplied outlining the slides and with some user ideas on flow and topic. If no file is provided, suggest one be started, if user prefers, offer to work on it in line in chat. Resolve the audience, desired decision or action, presentation length, delivery environment, and required outputs from context. Ask only about consequential gaps. A live talk, a self-guided deck, and a narrated recording need different amounts of on-screen explanation and different timing.

Separate verified product behavior, conceptual illustrations, and future plans. Write useful, readable copy around the evidence. Keep speaker elaboration in notes; do not turn slides into transcripts. Remove captions or duplicated copy where the request and layout justify it, without treating all captions as clutter. Preserve labels, explanations, credits, and accessibility text that serve a purpose.

Map the story to slides and, where useful, substates: diagram stages, gallery items, and demo tabs. Identify the evidence and audience takeaway for each. Derive dwell time from reading, explanation, motion, and media duration rather than imposing one duration on every slide.

## Build the deck

Use a small local HTML/CSS/JavaScript package when that meets the request. Bundle media, fonts and required licenses; use relative paths. Direct `file://` opening is a delivery constraint when offline double-click use is requested: avoid runtime fetches or module loading that depend on a server. Preserve an existing framework if it meets these constraints.

Create consistent content zones for headings, visuals or media, explanatory copy, and controls. Shared frame components should make standard videos comparable in size. Preserve portrait footage when its proportions make the interaction readable; do not stretch it to match landscape footage. Adapt composition to small screens rather than shrinking every element into an unreadable miniature.

Provide visible navigation, meaningful slide titles, keyboard access, focus indication, and a clear active state. A useful default is Up/Down for slides and Left/Right for substates; honor an established or requested mapping. Keep native media controls and accessible fallbacks. Keep inactive slides and panels out of keyboard navigation.

For keyboard arbitration, video lifecycle, SVG builds, or a separate notes window, read [interaction-and-motion.md](references/interaction-and-motion.md). Use a single state model for audience controls, presenter commands, and any recording automation so they cannot silently disagree.

## Add evidence and optional exports

For product recordings, editing, or video output, read [media-and-export.md](references/media-and-export.md). Capture actual behavior using authorized data and sessions. An edited demonstration may shorten waits, but must not fabricate tool results or imply actions that did not happen.

Make motion explain sequence or relationships. Let the audience see a completed state; default to playing explanatory builds once and holding, with replay or manual stages where useful. Keep initial delay, per-stage duration, and gaps independently configurable. Reduced motion and print must retain the information.

If presenter notes are requested, provide a separate window when the audience screen must remain clean. Include the current slide, relevant notes, next-slide context, and controls suited to the talk. Do not present hidden notes in the audience UI or recording.

## Verify and deliver

Read [verification-and-delivery.md](references/verification-and-delivery.md) before handoff. Inspect actual rendered states and playback, not only source or screenshots of the first slide. Test the final extracted package in its promised offline environment.

Deliver the requested artifact with concise opening and navigation instructions, recording or print limitations, and relevant test evidence. Creating files, preparing an archive, and producing a video do not by themselves authorize publishing, uploading, posting, or sending them. Honor any existing authorization and local-only constraints; do not default to a hosting service.
