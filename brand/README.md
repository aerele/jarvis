# Jarvis brand assets

The Jarvis mark, exported for use outside the app: docs, slides, README files,
websites, social avatars, app-store listings, and designer handoff.

Everything here is generated. Do not hand-edit files in `svg/` or `png/`.

## Regenerating

```bash
python brand/generate.py            # write the pack
python brand/generate.py --check    # verify the committed pack is current
```

No extra dependencies. Pillow ships with Frappe, and the glyph is a
straight-line polygon, so no SVG rasteriser is involved.

To change the brand, edit the constants at the top of `generate.py`, re-run it,
and make the matching change in `frontend/src/main.css` and
`frontend/src/components/JarvisMark.vue`. `jarvis/tests/test_brand_assets.py`
fails if those three ever disagree.

## Variants

| File | Use |
|---|---|
| `jarvis-mark-tile` | White spark on the gradient square. The app-icon look. Default choice on a white or light page. |
| `jarvis-mark` | Gradient spark, transparent background. For docs, slides, and anywhere the tile would look boxed-in. |
| `jarvis-mark-mono-black` | One colour, for print, faxes, watermarks, and light backgrounds that clash with the gradient. |
| `jarvis-mark-mono-white` | One colour, for dark backgrounds, photography, and coloured panels. |

SVGs are in `svg/`. Use them wherever the target accepts SVG, since they stay
crisp at any size. PNGs are in `png/` at 16, 32, 64, 128, 256, 512, and 1024,
for tools that reject SVG.

## Colours

| Token | Hex | Role |
|---|---|---|
| `--brand-1` | `#6e8bff` | Gradient start, top-left |
| `--brand-2` | `#8b5cf6` | Gradient end, bottom-right |

The gradient is `linear-gradient(135deg, #6e8bff, #8b5cf6)`, defined once in
`frontend/src/main.css` as `--brand-grad`.

## Clear space and minimum size

Leave clear space on all four sides equal to at least 25 percent of the mark's
width. Nothing else sits inside that margin.

Do not place the mark below 16px. Below that the spark's points break up. Use
the tile variant rather than the transparent one at small sizes, since the
solid background holds the shape together.

## Misuse

Do not do any of the following:

- Recolour the mark, or apply the gradient to anything other than the spark
- Rotate, flip, skew, or stretch it, or change its proportions
- Add shadows, glows, outlines, or bevels
- Put the gradient tile on a coloured or busy background. Use a mono variant
- Reconstruct the spark by hand. Use these files
- Pair it with a "Jarvis" wordmark of your own. No approved wordmark exists yet

## App icons

The five PWA icons in `jarvis/public/manifest/` are a separate set, and they
currently predate the colours above. `manifest_icons.py` renders them from this
same definition:

```bash
python brand/manifest_icons.py             # compare the live icons and report, always exits 0
python brand/manifest_icons.py --check     # same, but exits 1 on drift, for CI
python brand/manifest_icons.py --write     # regenerate them
```

A bare run reports without failing, because the live icons are known to have
drifted and a red exit would block everyone. Use `--check` only once the icons
have been regenerated and you want CI to hold them there.

`--write` changes the tab icon and home-screen icon for every customer, so it
is deliberately opt-in and is not part of the normal generate step. Bump the
`?v=` query string in `pwa/index.html` when you do it, or browsers will keep
serving the cached icon indefinitely.
