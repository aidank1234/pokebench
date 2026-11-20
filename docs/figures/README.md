# Result figures

The four figures are ordered by importance in the main README:

1. `pokebench-score.svg`: the combined score.
2. `pokebench-results.svg`: win rate and mean terminal HP depletion.
3. `pokebench-progress.svg`: all terminal HP-depletion values and their means.
4. `pokebench-fixtures.svg`: all 180 game outcomes.

They use only the published episode and system tables. To regenerate:

```sh
python -m pip install -r requirements-figures.txt
python scripts/render_figures.py
```

Use `--output-dir PATH` to render elsewhere, or `--png` to also create 3,200-pixel PNGs. The renderer uses Avenir Next when available and otherwise DejaVu Sans, so font outlines can differ across hosts. Scores and chart geometry use the same public inputs. Finished SVGs embed their logos and outline their text.

[Logo sources](sources.json) and the [icon license](logos/lobe-icons-license.txt) are included. See [methodology](../METHODOLOGY.md) for score definitions and [results](../RESULTS.md) for data fields.
