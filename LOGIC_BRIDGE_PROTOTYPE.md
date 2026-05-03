# Logic Bridge Prototype (Phase 2/3)

Goal: reduce setup friction by discovering/aligning tracks and mappings from Logic with minimal manual work.

## Phase 2 (Near-Term)

1. Add optional macOS Accessibility (UI scripting) read-only probe:
- detect visible track names from Logic track list
- present candidates for import into `targets.yaml`
- never auto-overwrite without confirmation

2. Add guided parameter bind helper:
- user chooses target + bank param in VoiceMix
- VoiceMix emits deterministic pulse on target channel
- Logic in Learn mode binds selected destination
- save bind checklist/log for repeatability

## Phase 3 (Deeper Integration)

1. Investigate Logic control-surface integration route:
- custom control surface script / MIDI remote profile
- track focus awareness + metadata handoff

2. Build a lightweight bridge service:
- source of truth for current project tracks
- syncs aliases/channels to `targets.yaml`
- API for GUI (`discover tracks`, `bind`, `verify`)

## Constraints

- Logic does not expose a simple public API for full project track metadata in this use case.
- UI scripting can break across Logic/macOS updates; must be treated as best-effort.
- Keep CLI/GUI fallback path fully functional without bridge.

## Exit Criteria

- Session start from zero to first mapped target in <2 minutes.
- Channel/target verification in one click.
- New track onboarding without manual YAML edits.
