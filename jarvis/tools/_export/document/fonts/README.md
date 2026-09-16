# Bundled PDF template fonts

The curated font catalog offered to custom PDF templates (see `../font_catalog.py`).
Each family ships as **static Regular + Bold** faces (`<key>-regular.ttf`,
`<key>-bold.ttf`), **subset to Latin** to keep them small. Static weights are
required because the PDF renderer (wkhtmltopdf 0.12.6 / QtWebKit) cannot use
variable fonts, and they are referenced by family name via fontconfig (a base64
`@font-face` blanks that renderer — see the `wkhtmltopdf-custom-fonts` note). The
same files are base64-embedded as `@font-face` for the Chrome preview.

| Key | Family | Category | License |
|-----|--------|----------|---------|
| inter | Inter | sans | SIL OFL 1.1 (`inter-OFL.txt`) |
| lato | Lato | sans | SIL OFL 1.1 (`lato-OFL.txt`) |
| worksans | Work Sans | sans | SIL OFL 1.1 (`worksans-OFL.txt`) |
| lora | Lora | serif | SIL OFL 1.1 (`lora-OFL.txt`) |
| merriweather | Merriweather | serif | SIL OFL 1.1 (`merriweather-OFL.txt`) |
| ptserif | PT Serif | serif | SIL OFL 1.1 (`ptserif-OFL.txt`) |

`sans` and `serif` in the catalog are system-stack defaults with no bundled file.

All fonts are under the SIL Open Font License 1.1; each family's `OFL.txt` (with its
copyright and any Reserved Font Name) is kept alongside the faces, satisfying the
license's requirement to distribute it with the fonts.

## Rebuilding a face

Sources come from the `google/fonts` repo. Variable families are instanced to the
target weight (`fontTools.varLib.instancer`, pinning every non-`wght` axis to its
default), then subset to Latin (`fontTools.subset`), then the name table + OS/2
weight + style bits are set so each Regular/Bold pairs correctly under fontconfig.
Keep faces small (a couple of hundred KB at most) — every referenced face is
embedded into the PDF and the preview payload.
