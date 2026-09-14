# Slides

`deck.pdf` — a walkthrough of the MAG ↔ OpenAI Agents API adapter: the two
questions (feasibility + the ask), what MAG serves live, the Agents *SDK* vs
Agents *API* crux, the architecture, what's built and proven, the two paths, the
one ask to OpenAI, and how to get a MAG token and run.

## Rebuild

Requires XeLaTeX (TeX Live) with the `metropolis` beamer theme and `fontawesome5`.

```bash
xelatex deck.tex && xelatex deck.tex   # twice, to resolve references
```

Sources: `deck.tex` (+ `preamble.tex`, `titlepage.tex`, `acknowledgement.tex`),
the `architecture.pdf`/`.svg` diagram, and `logos/`.
