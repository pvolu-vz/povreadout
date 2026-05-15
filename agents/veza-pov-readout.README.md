# veza-pov-readout — operator's guide

Companion notes for [`veza-pov-readout.md`](veza-pov-readout.md). The agent file is the spec the model reads; this file is for the human kicking it off — what to have ready before invoking, what surprises to expect, and how to get a deck you can actually send.

## What it does

Orchestrates end-to-end production of a Veza POV (Proof of Value) executive read-out PPTX. It does not capture screenshots or assemble PPTX itself — it composes two skills:

| Skill | Owns |
|---|---|
| [`collect-pov-screenshots`](../skills/collect-pov-screenshots/SKILL.md) | Keychain login, parallel screenshot workers, Query Builder pair/graph captures |
| [`build-pov-deck`](../skills/build-pov-deck/SKILL.md) | Template clone, text find/replace, image swap, output render |

The agent's job is the glue: gather customer-specific inputs, update capture config, drive the skills, author the manifest, and report what the human still needs to review by hand.

## When to invoke

Direct triggers:
- "build a POV read-out for `<customer>`"
- "assemble the Veza POV deck"
- "generate the read-out for `<customer>`"
- `/build-pov-deck` (the skill will hand off here if no manifest exists yet)

The agent runs on Opus by default.

## Before you start — have these ready

The agent will ask, but you'll save a round-trip if you have them in one message:

1. **Customer name** (used in title, footer, output filename)
2. **Veza tenant URL** + **keychain prefix** + **keychain account** (e.g. `https://pov-f.vezacloud.com`, prefix `pov-f`, account `you@company.com`)
3. **Systems to deep-dive** — which template sections to keep vs drop (Oracle JDE, SQL Server, GICO, OTM, Hyperion HFM, Azure AD, Active Directory, etc.)
4. **Query Builder pairs** — the most error-prone input. For each pair you want captured: `entity_type` + `relates_to` as **internal Veza node-type identifiers** (e.g. `AzureADUser`, `OracleJDEProgram`), plus whether you want a filter and/or graph view. These come from a HAR capture of the `query_spec:nodes_async_create` request — see the `collect-pov-screenshots` SKILL.md "Adding a filter" section. **Don't guess these names — wrong identifiers silently produce empty tables.**
5. **Highlight metric values** (slides 17–22) — or "compute from the captures" if it's derivable
6. **Presenters** — if omitted, the title slide keeps boilerplate names

## Tips that come from running it

### Analyze the screenshots and adjust headlines + benefits per slide

The single most valuable thing you can ask for. Without this instruction the agent will leave boilerplate subtitles and bullet text in place even when the captured data tells a different story. Saying "analyze the screenshots and adjust the headlines and benefits for the slide accordingly" makes the agent:

- Rewrite the subtitle on Highlight slides to cite numbers actually visible in the captures (e.g. "641 open risks across 7 domains, 176 Critical / 126 High" instead of a templated phrase).
- Re-pick which Highlight slides to keep — if the data doesn't substantiate a claim, the slide is dropped rather than left misleading.
- Reframe Highlight 3 (Dashboards) around what the OOTB library actually shows for this tenant instead of fabricated counts.

If you skip this, every customized deck still reads like the SmurfitWestrock exemplar.

### Tell it explicitly which systems to drop

The template has full slide pairs for ~7 systems. The agent will keep them all unless you say otherwise. Phrasing like "ONLY Azure AD and Active Directory" produces the cleanest result — the agent populates `remove_slides:` for everything else instead of leaving stale per-system slides.

### Cost Impact (slide 22) needs explicit guidance

The agent will compute the dollar figure from screenshots (e.g. dormant users × per-license cost), but it has to assume a per-license rate. Defaults to **Azure AD P1 ≈ $6/user/month = $72/year**, which is conservative — if the customer's SKU mix includes E5 / dev packs, real reclaimable spend is 2–5× higher. Either:
- Provide the per-license figure you want used, or
- Accept the conservative default and edit slide 22 by hand against the customer's Microsoft EA

### Verify tenant URLs in `config.json` before running

A common silent failure: dashboard sub-capture URLs in `config.json` point at `standard.vezacloud.com` instead of the customer's actual tenant. Playwright follows the redirect to the login page, captures the *login screen*, and saves it as if it succeeded. The agent flags these as "unusable" in its final report but won't auto-fix them. Skim `~/.povreadout/config.json` and confirm every `url` matches the prefix you set.

### `picture_index` is z-order, not visual order

When the agent authors a `slide_overrides:` entry it uses `list_slide_pictures.py <slide>` to map indexes. If you're hand-editing a manifest, do the same — picture indexes are 0-based z-order, which usually but not always matches what you see on the slide. Getting it wrong silently swaps the wrong image.

### Some pairs can't have a graph capture

Query Builder pairs with an empty `relates_to` (typical for "find all dormant users") don't render an "Open in Graph" button. The agent will report `NOT POSSIBLE` for those — that's correct, not a bug. Don't add them to `graph_pairs:`.

### Slide 8 (POV Team Key Metrics) doesn't auto-customize

The agent doesn't have visibility into how many teams / queries / dashboards your POV produced — those aren't on a screenshot. Slide 8 stays as boilerplate unless you supply the values explicitly. Expect to either edit it by hand or tell the agent the numbers up front.

## What the agent will hand back

- Path to the rendered PPTX (default `~/Desktop/<customer-slug>-Veza-POV-ReadOut.pptx`)
- Path to the YAML manifest (`~/.povreadout/manifests/<customer-slug>.yaml`) — useful for re-rendering after manual tweaks
- Final slide count
- A "review before sending" list — assumptions made (per-license cost, presenter decisions), slides dropped, captures that failed, anything the agent couldn't substantiate from data

Treat that review list as a real checklist. The deck is presentation-ready, not send-ready, until you've walked it.

## Files the agent touches

| Path | What it is |
|---|---|
| `~/.povreadout/config.json` | Edited in place to set the customer's site + Query Builder pairs |
| `~/.povreadout/screenshots/<prefix>/` | Captured PNGs (dashboard, risk, governance, query-builder, query-builder-graph) |
| `~/.povreadout/manifests/<customer-slug>.yaml` | Generated manifest |
| `~/Desktop/<customer-slug>-Veza-POV-ReadOut.pptx` | Output deck |

## Re-rendering after manual edits

If you tweak the manifest by hand (fix a metric, swap an image, change a headline), re-render without re-running captures:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/build-pov-deck/scripts/build_deck.py \
    --manifest ~/.povreadout/manifests/<customer-slug>.yaml \
    --output   ~/Desktop/<customer-slug>-Veza-POV-ReadOut.pptx
```

The screenshots already on disk are reused.
