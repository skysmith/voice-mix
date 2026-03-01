# VoiceMix

VoiceMix is a local macOS tool for controlling Logic Pro with natural language.

It translates text into strict action JSON, resolves those actions through your YAML mapping, and emits MIDI CC messages to an IAC bus (default: `VoiceMix`).

## License

MIT. See [LICENSE](LICENSE).

## What It Includes

- CLI REPL (`main.py`)
- Desktop GUI (`gui.py`)
- AI + heuristic translation for mix intent
- Bank/parameter mapping via `mapping.yaml`
- Track targeting via `targets.yaml`
- Dry/apply workflow, undo, learn/status helpers
- Presets (`save`, `save-current`, `apply`, `show`)

## Requirements

- macOS (for Logic + IAC driver workflow)
- Python 3.10+
- Logic Pro (or any MIDI-mappable host)
- IAC Driver enabled in Audio MIDI Setup

## Quickstart

```bash
cd /Users/sky/.openclaw/workspace/voice-mix
./setup.sh
source .venv/bin/activate
```

Optional AI key (recommended):

```bash
cp .env.example .env
# edit .env and set OPENAI_API_KEY=...
source .env
```

Run CLI dry mode:

```bash
python3 main.py --dry --targets targets.yaml
```

Run GUI dry mode:

```bash
python3 gui.py --dry --targets targets.yaml
```

## Logic + IAC Setup

1. Open `Audio MIDI Setup` -> `Window` -> `Show MIDI Studio`.
2. Open `IAC Driver`, enable `Device is online`.
3. Create (or rename) a bus/port to `VoiceMix`.
4. In Logic, map plugin/track controls to the CC values in `mapping.yaml`.
5. Start VoiceMix in dry mode first, then switch to apply mode.

## Core Usage

### CLI

```bash
python3 main.py --dry
```

Common commands:

- `/dry`
- `/apply`
- `/undo`
- `/status`
- `/learn`
- `/learn full`
- `/bank [name]`
- `/target [name]`
- `/target list`
- `/target add <name> <channel> [aliases_csv] [default_bank]`
- `/preset save <name>`
- `/preset save-current <name>`
- `/preset list`
- `/preset show <name>`
- `/preset apply <name>`

Natural examples:

- `guitar 1 is sounding too chungy`
- `lead vox is too harsh`
- `bass feels too boomy, make it clearer`

### GUI

```bash
python3 gui.py --dry
```

GUI includes:

- target + bank selectors
- DRY/APPLY mode controls
- text instruction input + live log
- undo/status/learn controls
- preset save/save-current/apply/show
- target add dialog (persists to `targets.yaml`)

## Customization

### 1) Macro/CC Mapping (`mapping.yaml`)

Define:

- MIDI port + default channel
- banks/params
- CC number, min/max/default, and step size
- bank aliases

This is the control surface contract for VoiceMix.

### 2) Track Targets (`targets.yaml`)

Define:

- target name (`lead_vox`, `guitar_1`, etc.)
- aliases (phrases users might type)
- MIDI channel per target
- optional `default_bank`

Phrase parsing can auto-switch target when an alias appears.

### 3) Environment (`.env`)

- `OPENAI_API_KEY` enables AI translation.
- Without key, VoiceMix falls back to local heuristics.

## Project Files

- `main.py`: CLI app + core runtime
- `gui.py`: Tkinter GUI
- `mapping.yaml`: bank/CC map
- `targets.yaml`: target aliases/channels
- `setup.sh`: one-shot local setup
- `VoiceMix.command`: desktop launcher for CLI
- `VoiceMix-GUI.command`: desktop launcher for GUI

Runtime outputs:

- `logs/`
- `.voicemix/state.json`
- `.voicemix/presets/*.yaml`

## Troubleshooting

### MIDI port not found (`VoiceMix`)

- Confirm IAC Driver is online.
- Confirm bus name matches `mapping.yaml`.
- Run:

```bash
python3 main.py --list-ports
```

### Commands parse but nothing changes in Logic

- Verify Logic learn/mapping is bound to the same CC numbers in `mapping.yaml`.
- Confirm target channel in `targets.yaml` matches your routing assumptions.
- Use dry mode first to verify expected messages.

### `OPENAI_API_KEY` missing

- App still runs with heuristic translation.
- Add key in `.env` for better language coverage.

### Dependency errors

Run setup again:

```bash
./setup.sh
```

## Open Source Notes

This repo is ready to share now.

For friendlier distribution later, shipping a signed standalone macOS app bundle is the next step, but current `.command` launchers are usually enough for technical users.
