# Gauntlet demo video

A 2:10, 1080p walkthrough of the real app, built with [Remotion](https://www.remotion.dev) and narrated with an ElevenLabs voice.

| Piece | Source |
|---|---|
| Footage | `record.py` drives the actual running app (mock mode, `MOCK_SEED=672`) through the flagship journey in headless Chromium and records it: Overview → 30-call run → failing line → patch → compare → evidence → Review |
| Narration | `narration.json`, voiced by ElevenLabs (`eleven_v4`, voice "Naresh – The Education Narrator"); one MP3 per scene in `public/audio/` |
| Timeline | `build_timeline.py` sizes every scene to its narration, cuts footage at the scene marks the recorder logged, and times the captions |
| Video | `src/Demo.tsx`: an animated intro and outro, footage with scene labels, a badge saying which parts are mock (scripted calls) and which are real (the Gemini-simulated call on the evidence page), burned-in captions |

## Rebuild

```bash
npm install
```

```bash
../.venv/bin/python record.py
```

```bash
python3 build_timeline.py
```

```bash
npm run render
```

The output is `out/gauntlet-demo.mp4`. `npm run studio` opens Remotion Studio to preview and tweak. `record.py` starts its own server on port 8790 with a throwaway database, so it doesn't touch your data.

To change the narration, edit `narration.json`, regenerate the scene's MP3 with ElevenLabs into `public/audio/<scene id>.mp3`, then run `record.py` (scenes hold for as long as their narration) and `build_timeline.py` again.

The footage (`public/footage/`) and renders (`out/`) are not committed; the steps above recreate them.
