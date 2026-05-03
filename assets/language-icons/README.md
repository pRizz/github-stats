# Language Icon Assets

This directory contains a vendored subset of Devicon SVG assets used by the
generated GitHub stats cards.

- Source: https://github.com/devicons/devicon
- Source commit: `7330accdbc47e2dc0c19789a48533c4a3c50fe58`
- License: MIT
- Variants: original SVGs when available; plain/wordmark variants only where
  Devicon does not publish an original SVG variant at this commit.

Supported language mappings are defined in `language_icons.py`. The subset is
biased toward GitHub Linguist language names with direct Devicon matches.
Unknown or ambiguous names intentionally fall back to the existing colored dot
marker instead of rendering a misleading logo.
