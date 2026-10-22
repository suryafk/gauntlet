# Gauntlet pitch deck

`Gauntlet-pitch.pptx` (editable, with speaker notes) and `Gauntlet-pitch.pdf`: 14 core slides (about 5 minutes) and a 3-slide technical appendix.

Every number and screenshot comes from the running app:

```bash
../.venv/bin/python capture.py
```

drives the real app (mock mode, `MOCK_SEED=672`) through the flagship journey and saves 2x screenshots and the run data to `shots/`.

```bash
npm install
```

```bash
NODE_PATH="$PWD/node_modules" PPTX_SKILL=<path to the pptx skill> node build_deck.js
```

builds the deck from `shots/data.json` and the screenshots. Without `PPTX_SKILL` the deck is still written, just without the theme colours applied to PowerPoint's palette.
