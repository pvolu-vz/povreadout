---
name: build-pov-deck
description: Assemble a customer-specific Veza POV (Proof of Value) executive read-out PowerPoint deck by cloning a template PPTX and applying text replacements and image swaps from a YAML manifest. Use after `collect-pov-screenshots` has produced the screenshot set. Trigger when the user says "build the POV deck", "assemble the read-out", "render the PPT", or names this skill directly.
---

# build-pov-deck

Produce a finished POV read-out PPTX by taking a template deck, replacing text (customer name, dates, presenters, per-slide metrics) and swapping in the screenshots captured by [`collect-pov-screenshots`](../collect-pov-screenshots/SKILL.md).

This skill does not capture data or write narrative — those are upstream. It only renders the final PPTX. The orchestration (asking the user what to capture, producing the manifest, generating findings text) belongs to the `veza-pov-readout` agent.

## Inputs

1. **Template PPTX** — the master deck to clone. Default is [`templates/pov-readout-template.pptx`](templates/pov-readout-template.pptx), which is the SmurfitWestrock read-out from May 2026 used as a structural exemplar. Override with `--template <path>` when a customer has their own.
2. **Manifest** — a YAML file describing every replacement. See [`examples/sections.example.yaml`](examples/sections.example.yaml) for the full schema.
3. **Output path** — where the rendered PPTX is written. Override with `--output <path>`.

## Prerequisites

- `python3` with `python-pptx` and `PyYAML` (`pip install python-pptx pyyaml`).

## Running it

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/build-pov-deck/scripts/build_deck.py \
    --manifest /path/to/sections.yaml \
    --output   /path/to/AcmeCorp-Veza-POV-ReadOut.pptx
```

The script prints one line per applied operation (`slide=N op=image rid=... src=...`, `slide=* op=text find=... count=N`) plus a final `wrote=<output>` line.

## Manifest schema (overview)

```yaml
# Customer-wide find/replace applied to every slide.
text_replacements:
  - find: "SmurfitWestrock"
    replace: "AcmeCorp"
  - find: "May2026"
    replace: "Aug 2026"

# Per-slide overrides. slide_index is 1-based (matches the slide number you see in PowerPoint).
slide_overrides:
  - slide_index: 26                       # "Oracle JDE: Access Graph"
    image_replacements:
      # picture_index is the z-order of pictures on that slide, 0-based.
      # Use scripts/list_slide_pictures.py <slide_index> to inspect.
      - picture_index: 0
        source: ~/.povreadout/screenshots/pov-f/query-builder-graph/jde-user-to-program.png
      - picture_index: 1
        source: ~/.povreadout/screenshots/pov-f/query-builder/jde-detail-crop.png
    text_replacements:
      - find: "Oracle JDE"
        replace: "Oracle JDE Prod"

# Slides to drop entirely (1-based indices). Use sparingly — the template ordering is curated.
remove_slides: [22, 28]
```

Critical rules the assembler enforces:

- `slide_index` is **1-based** so it matches the PowerPoint slide pane numbering. Internally we convert to 0-based.
- `picture_index` is **0-based** in z-order. Run `scripts/list_slide_pictures.py <N>` to see what's on a slide and confirm which picture is which before authoring the manifest.
- Image replacement preserves the original picture's position and dimensions. The new file is rescaled to fit the slot, which is usually what you want for screenshot swaps. If a customer screenshot has wildly different aspect ratio, crop the source first.
- Text replacement is run-level by default (preserves formatting), with a paragraph-level fallback for text split across runs. Both pass through the same `text_replacements` list.

## Finding the right slide/picture indexes

Two helpers ship with the skill:

```bash
# List every slide with its title and image count.
python3 ${CLAUDE_PLUGIN_ROOT}/skills/build-pov-deck/scripts/list_slides.py \
    --template ${CLAUDE_PLUGIN_ROOT}/skills/build-pov-deck/templates/pov-readout-template.pptx

# Inspect pictures on one slide (size, position, current image path).
python3 ${CLAUDE_PLUGIN_ROOT}/skills/build-pov-deck/scripts/list_slide_pictures.py 26 \
    --template ${CLAUDE_PLUGIN_ROOT}/skills/build-pov-deck/templates/pov-readout-template.pptx
```

Use these before adding an entry to the manifest — slide ordering and picture z-order in a customer's edited template can drift from the reference.

## Validation

Before saving, the assembler checks:
- Every `source` image exists and is readable.
- Every `slide_index` is within `[1, total_slides]`.
- Every `picture_index` is within range for its slide.

A failure prints the offending entry and exits non-zero without writing the output file, so you don't ship a half-built deck.

## Conventions in the reference template

The template is laid out with these conventions — manifests usually only touch these slides:

| Slide(s) | Purpose                                  | What to customize                              |
| -------- | ---------------------------------------- | ---------------------------------------------- |
| 1        | Title — customer name + presenters       | Customer name, presenter names, date           |
| 4–6      | Existing gaps / challenges               | Optional: tweak wording per customer           |
| 8        | POV team key metrics                     | Teams count, queries built, etc.               |
| 17–22    | Highlights 1–5 (customer-specific stats) | All metric numbers + supporting screenshots    |
| 23–47    | Per-system deep dives                    | Swap screenshots; tweak value-add bullets      |
| 48       | Azure AD overview                        | Dashboard screenshot                           |

The boilerplate strategy/architecture slides (11–15, 49–56) usually need no per-customer edits.
