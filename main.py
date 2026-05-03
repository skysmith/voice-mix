#!/usr/bin/env python3
"""VoiceMix terminal REPL: text -> LLM JSON actions -> MIDI CC."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from midi_bridge import midi_health, resolve_output_port_name, safe_list_output_ports

try:
    import mido
except ModuleNotFoundError:
    mido = None


ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actions"],
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["bank", "param", "mode", "amount"],
                "properties": {
                    "bank": {"type": "string"},
                    "param": {"type": "string"},
                    "mode": {"type": "string", "enum": ["delta", "absolute"]},
                    "amount": {"type": "number"},
                    "reason": {"type": "string"},
                },
            },
        },
        "notes": {"type": "string"},
    },
}


class SchemaValidationError(Exception):
    pass


def validate_action_doc(doc: dict[str, Any]) -> None:
    if not isinstance(doc, dict):
        raise SchemaValidationError("top-level value must be an object")
    if "actions" not in doc:
        raise SchemaValidationError("missing required field: actions")
    if not isinstance(doc["actions"], list):
        raise SchemaValidationError("actions must be an array")
    for i, action in enumerate(doc["actions"]):
        if not isinstance(action, dict):
            raise SchemaValidationError(f"actions[{i}] must be an object")
        required = {"bank", "param", "mode", "amount"}
        missing = [k for k in required if k not in action]
        if missing:
            raise SchemaValidationError(f"actions[{i}] missing fields: {', '.join(missing)}")
        if not isinstance(action["bank"], str):
            raise SchemaValidationError(f"actions[{i}].bank must be string")
        if not isinstance(action["param"], str):
            raise SchemaValidationError(f"actions[{i}].param must be string")
        if action["mode"] not in {"delta", "absolute"}:
            raise SchemaValidationError(f"actions[{i}].mode must be delta|absolute")
        if not isinstance(action["amount"], (int, float)):
            raise SchemaValidationError(f"actions[{i}].amount must be number")


@dataclass
class ParamConfig:
    cc: int
    minimum: int = 0
    maximum: int = 127
    default: int = 64
    step: float = 0.04


@dataclass
class MidiMessageSpec:
    target: str
    bank: str
    param: str
    cc: int
    value: int
    channel: int
    previous_value: int


@dataclass
class TargetSpec:
    name: str
    aliases: list[str]
    channel: int
    default_bank: str | None = None


class JsonLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "type": event_type,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


class MappingConfig:
    def __init__(self, mapping_path: Path):
        with mapping_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        midi = data.get("midi", {})
        self.port_name = str(midi.get("port_name", "VoiceMix"))
        self.channel = int(midi.get("channel", 1))

        self.banks: dict[str, dict[str, ParamConfig]] = {}
        raw_banks = data.get("banks", {})
        for bank_name, params in raw_banks.items():
            bank_map: dict[str, ParamConfig] = {}
            for param_name, cfg in params.items():
                bank_map[param_name] = ParamConfig(
                    cc=int(cfg["cc"]),
                    minimum=int(cfg.get("min", 0)),
                    maximum=int(cfg.get("max", 127)),
                    default=int(cfg.get("default", 64)),
                    step=float(cfg.get("step", 0.04)),
                )
            self.banks[bank_name] = bank_map
        self.bank_aliases: dict[str, str] = {}
        raw_aliases = data.get("bank_aliases", {}) or {}
        for alias, bank in raw_aliases.items():
            alias_key = str(alias).strip().lower()
            bank_name = str(bank).strip()
            if alias_key and bank_name in self.banks:
                self.bank_aliases[alias_key] = bank_name

        # Always include canonical names and simple short-hands.
        for bank_name in self.banks:
            canonical = bank_name.lower()
            self.bank_aliases[canonical] = bank_name
            self.bank_aliases[canonical.replace("_", "")] = bank_name
            if bank_name.endswith(("1", "2", "3", "4")):
                self.bank_aliases[bank_name[-1]] = bank_name

    def resolve_bank_name(self, name: str, fallback: str | None = None) -> str | None:
        key = name.strip().lower()
        if not key:
            return fallback
        if key in self.bank_aliases:
            return self.bank_aliases[key]
        return fallback

    def detect_bank_from_text(self, text: str, fallback: str) -> str:
        lowered = text.lower()
        for alias, bank in self.bank_aliases.items():
            if f" {alias} " in f" {lowered} ":
                return bank
        return fallback

    def aliases_for_bank(self, bank: str) -> list[str]:
        out = [alias for alias, target in self.bank_aliases.items() if target == bank and alias != bank]
        return sorted(out)

    def prompt_context(self) -> str:
        lines: list[str] = []
        for bank, params in self.banks.items():
            joined = ", ".join(sorted(params.keys()))
            aliases = self.aliases_for_bank(bank)
            if aliases:
                lines.append(f"- {bank} (aliases: {', '.join(aliases)}): {joined}")
            else:
                lines.append(f"- {bank}: {joined}")
        return "\n".join(lines)


class TargetConfig:
    def __init__(self, targets_path: Path, fallback_channel: int, mapping: MappingConfig):
        self.path = targets_path
        self.fallback_channel = fallback_channel
        self.mapping = mapping
        self.targets: dict[str, TargetSpec] = {}
        self.alias_index: dict[str, str] = {}

        data: dict[str, Any] = {}
        if targets_path.exists():
            with targets_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
                if isinstance(loaded, dict):
                    data = loaded

        raw_targets = data.get("targets", {}) if isinstance(data, dict) else {}
        if not isinstance(raw_targets, dict):
            raw_targets = {}

        self._load_from_raw(raw_targets)
        self._rebuild_alias_index()

    def _load_from_raw(self, raw_targets: dict[str, Any]) -> None:
        for name, cfg in raw_targets.items():
            if not isinstance(cfg, dict):
                cfg = {}
            target_name = str(name).strip()
            if not target_name:
                continue
            aliases = [str(a).strip().lower() for a in cfg.get("aliases", []) if str(a).strip()]
            channel = int(cfg.get("channel", self.fallback_channel))
            default_bank = cfg.get("default_bank")
            if default_bank is not None:
                default_bank = self.mapping.resolve_bank_name(str(default_bank), fallback=None)
            spec = TargetSpec(
                name=target_name,
                aliases=aliases,
                channel=channel,
                default_bank=default_bank,
            )
            self.targets[target_name] = spec

        if not self.targets:
            self.targets["default"] = TargetSpec(
                name="default",
                aliases=["default"],
                channel=self.fallback_channel,
                default_bank="plugin1",
            )

    def _rebuild_alias_index(self) -> None:
        self.alias_index = {}
        for target_name, spec in self.targets.items():
            keys = [target_name.lower(), target_name.lower().replace("_", " "), target_name.lower().replace("_", "")]
            keys.extend(spec.aliases)
            for key in keys:
                k = key.strip().lower()
                if k:
                    self.alias_index[k] = target_name

    def save(self) -> None:
        payload: dict[str, Any] = {"targets": {}}
        for name, spec in self.targets.items():
            payload["targets"][name] = {
                "aliases": spec.aliases,
                "channel": spec.channel,
            }
            if spec.default_bank:
                payload["targets"][name]["default_bank"] = spec.default_bank
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, sort_keys=False)

    def add_target(self, name: str, channel: int, aliases: list[str], default_bank: str | None = None) -> TargetSpec:
        canonical_name = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip()).strip("_").lower()
        if not canonical_name:
            raise ValueError("target name cannot be empty")
        if channel < 1 or channel > 16:
            raise ValueError("channel must be in 1..16")
        norm_aliases = sorted({a.strip().lower() for a in aliases if a.strip()})
        if default_bank is not None:
            default_bank = self.mapping.resolve_bank_name(default_bank, fallback=None)
            if not default_bank:
                raise ValueError("unknown default bank")
        spec = TargetSpec(
            name=canonical_name,
            aliases=norm_aliases,
            channel=channel,
            default_bank=default_bank,
        )
        self.targets[canonical_name] = spec
        self._rebuild_alias_index()
        self.save()
        return spec

    def upsert_target(
        self,
        name: str,
        channel: int,
        aliases: list[str] | None = None,
        default_bank: str | None = None,
        keep_existing_aliases: bool = True,
    ) -> TargetSpec:
        canonical_name = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip()).strip("_").lower()
        if not canonical_name:
            raise ValueError("target name cannot be empty")
        if channel < 1 or channel > 16:
            raise ValueError("channel must be in 1..16")

        existing = self.targets.get(canonical_name)
        new_aliases = {a.strip().lower() for a in (aliases or []) if a.strip()}
        if keep_existing_aliases and existing is not None:
            new_aliases.update(existing.aliases)
        norm_aliases = sorted(new_aliases)

        resolved_default_bank = default_bank
        if resolved_default_bank is not None:
            resolved_default_bank = self.mapping.resolve_bank_name(resolved_default_bank, fallback=None)
            if not resolved_default_bank:
                raise ValueError("unknown default bank")
        elif existing is not None:
            resolved_default_bank = existing.default_bank

        spec = TargetSpec(
            name=canonical_name,
            aliases=norm_aliases,
            channel=channel,
            default_bank=resolved_default_bank,
        )
        self.targets[canonical_name] = spec
        self._rebuild_alias_index()
        self.save()
        return spec

    def resolve_target(self, name_or_alias: str) -> str | None:
        key = name_or_alias.strip().lower()
        if not key:
            return None
        return self.alias_index.get(key)

    def detect_target_from_text(self, text: str, fallback: str) -> str:
        lowered = f" {text.lower()} "
        for alias, target_name in sorted(self.alias_index.items(), key=lambda kv: len(kv[0]), reverse=True):
            if f" {alias} " in lowered:
                return target_name
        return fallback

    def describe(self) -> str:
        lines: list[str] = []
        for target_name, spec in self.targets.items():
            aliases = ", ".join(spec.aliases) if spec.aliases else "-"
            lines.append(f"- {target_name} (ch {spec.channel}, aliases: {aliases})")
        return "\n".join(lines)


def suggest_aliases_from_name(name: str) -> list[str]:
    base = name.strip().lower()
    if not base:
        return []
    compact = re.sub(r"\s+", "", base)
    underscored = re.sub(r"\s+", "_", base)
    out = [base, compact, underscored]
    if compact.startswith("guitar"):
        out.append(compact.replace("guitar", "gtr"))
    return sorted({a for a in out if a})


def run_target_wizard(targets: TargetConfig, mapping: MappingConfig, logger: JsonLogger) -> list[str]:
    created: list[str] = []
    print("Target wizard. Press Enter on track name to stop.")
    print(f"Available banks: {', '.join(mapping.banks.keys())}")
    while True:
        raw_name = input("Track/target name: ").strip()
        if not raw_name:
            break

        while True:
            raw_channel = input("MIDI channel (1-16): ").strip()
            try:
                channel = int(raw_channel)
            except ValueError:
                print("Channel must be a number in 1..16.")
                continue
            if 1 <= channel <= 16:
                break
            print("Channel must be in 1..16.")

        suggested = suggest_aliases_from_name(raw_name)
        print(f"Suggested aliases: {', '.join(suggested) if suggested else '-'}")
        raw_aliases = input("Aliases CSV (Enter to use suggested): ").strip()
        aliases = [a.strip() for a in raw_aliases.split(",") if a.strip()] if raw_aliases else suggested

        raw_default_bank = input("Default bank (optional): ").strip()
        default_bank = raw_default_bank or None

        try:
            spec = targets.add_target(raw_name, channel, aliases, default_bank=default_bank)
        except Exception as e:
            print(f"Failed to add target: {e}")
            continue

        created.append(spec.name)
        print(f"Added target: {spec.name} (ch {spec.channel}) aliases=[{', '.join(spec.aliases) if spec.aliases else '-'}]")
        logger.log(
            "target_wizard_add",
            {
                "target": spec.name,
                "channel": spec.channel,
                "aliases": spec.aliases,
                "default_bank": spec.default_bank,
                "file": str(targets.path),
            },
        )

        again = input("Add another target? [y/N]: ").strip().lower()
        if again not in {"y", "yes"}:
            break
    return created


def run_target_import(targets: TargetConfig, mapping: MappingConfig, logger: JsonLogger) -> list[str]:
    created: list[str] = []
    print("Target import mode.")
    print("For each step: click a track in Logic, then type the track name exactly as you want it.")
    print("Press Enter on track name to finish.")
    print(f"Available banks: {', '.join(mapping.banks.keys())}")

    raw_start = input("Starting MIDI channel [1]: ").strip()
    try:
        next_channel = int(raw_start) if raw_start else 1
    except ValueError:
        next_channel = 1
    if not (1 <= next_channel <= 16):
        next_channel = 1

    while True:
        raw_name = input("\nSelect track in Logic now, then enter track name: ").strip()
        if not raw_name:
            break

        while True:
            raw_channel = input(f"Channel for '{raw_name}' [{next_channel}]: ").strip()
            try:
                channel = int(raw_channel) if raw_channel else next_channel
            except ValueError:
                print("Channel must be a number in 1..16.")
                continue
            if 1 <= channel <= 16:
                break
            print("Channel must be in 1..16.")

        suggested = suggest_aliases_from_name(raw_name)
        print(f"Suggested aliases: {', '.join(suggested) if suggested else '-'}")
        raw_aliases = input("Aliases CSV (Enter to use suggested): ").strip()
        aliases = [a.strip() for a in raw_aliases.split(",") if a.strip()] if raw_aliases else suggested

        raw_default_bank = input("Default bank [plugin1]: ").strip()
        default_bank = raw_default_bank or "plugin1"

        try:
            spec = targets.add_target(raw_name, channel, aliases, default_bank=default_bank)
        except Exception as e:
            print(f"Failed to add target: {e}")
            continue

        created.append(spec.name)
        print(f"Imported: {spec.name} (ch {spec.channel}) aliases=[{', '.join(spec.aliases) if spec.aliases else '-'}]")
        logger.log(
            "target_import_add",
            {
                "target": spec.name,
                "channel": spec.channel,
                "aliases": spec.aliases,
                "default_bank": spec.default_bank,
                "file": str(targets.path),
            },
        )

        if channel < 16:
            next_channel = channel + 1
        again = input("Import another selected track? [Y/n]: ").strip().lower()
        if again in {"n", "no"}:
            break
    return created


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = {"current": {}, "undo_stack": []}
            self._save()

    def _save(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def get_current(self, target: str, bank: str, param: str, default: int) -> int:
        scoped = f"{target}:{bank}.{param}"
        legacy = f"{bank}.{param}"
        current = self.data.get("current", {})
        if scoped in current:
            return int(current.get(scoped, default))
        return int(current.get(legacy, default))

    def apply_batch(self, batch: list[MidiMessageSpec], target: str) -> None:
        if not batch:
            return
        undo_entry: list[dict[str, Any]] = []
        for msg in batch:
            key = f"{target}:{msg.bank}.{msg.param}"
            self.data["current"][key] = msg.value
            undo_entry.append(
                {
                    "target": target,
                    "bank": msg.bank,
                    "param": msg.param,
                    "cc": msg.cc,
                    "channel": msg.channel,
                    "previous": msg.previous_value,
                    "new": msg.value,
                }
            )
        self.data["undo_stack"].append(undo_entry)
        self.data["undo_stack"] = self.data["undo_stack"][-100:]
        self._save()

    def pop_undo(self) -> list[dict[str, Any]]:
        stack = self.data.get("undo_stack", [])
        if not stack:
            return []
        entry = stack.pop()
        for e in entry:
            target = str(e.get("target", "default"))
            key = f"{target}:{e['bank']}.{e['param']}"
            self.data["current"][key] = int(e["previous"])
        self._save()
        return entry


class PresetStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slug(name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-")
        return slug or "preset"

    def preset_path(self, name: str) -> Path:
        return self.path / f"{self._slug(name)}.yaml"

    def list_presets(self) -> list[Path]:
        return sorted(self.path.glob("*.yaml"))

    def save_values(self, name: str, snapshot: dict[str, dict[str, int]]) -> Path:
        payload = {
            "name": name,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "values": snapshot,
        }
        out = self.preset_path(name)
        with out.open("w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, sort_keys=False)
        return out

    def save(self, name: str, mapping: MappingConfig, state: StateStore, target: str) -> Path:
        snapshot = snapshot_from_state(mapping, state, target=target)
        return self.save_values(name, snapshot)

    def load(self, name: str) -> dict[str, Any]:
        path = self.preset_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Preset not found: {path.name}")
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict) or not isinstance(data.get("values"), dict):
            raise ValueError("Invalid preset format: missing values map")
        return data


class MidiOut:
    def __init__(self, port_name: str):
        if mido is None:
            raise RuntimeError("mido is not installed. Run: pip install -r requirements.txt")
        resolved = resolve_output_port_name(port_name)
        self.port_name = resolved
        self.port = mido.open_output(resolved)

    @staticmethod
    def list_ports() -> list[str]:
        if mido is None:
            raise RuntimeError("mido is not installed. Run: pip install -r requirements.txt")
        return mido.get_output_names()

    def send_cc(self, cc: int, value: int, channel: int) -> None:
        msg = mido.Message("control_change", control=cc, value=value, channel=channel - 1)
        self.port.send(msg)


class LLMTranslator:
    def __init__(self, model: str, mapping: MappingConfig):
        self.model = model
        self.mapping = mapping
        self.client = None
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            try:
                from openai import OpenAI

                self.client = OpenAI(api_key=api_key)
            except Exception:
                self.client = None

    def translate(self, text: str, current_bank: str, current_target: str) -> dict[str, Any]:
        if not self.client:
            return self._heuristic(text, current_bank)

        system = (
            "Translate user mixing requests into strict JSON actions for MIDI control. "
            "The user may give high-level intent (e.g. clearer, warmer, less harsh, more forward) "
            "instead of technical parameter names; infer useful macro moves. "
            "Return ONLY JSON. Use mode='delta' for relative moves and mode='absolute' for direct set targets. "
            "delta amount should be small by default (typically 0.02 to 0.08 unless user asks for a big move). "
            "absolute amount is normalized 0.0 to 1.0. "
            f"Current active bank is '{current_bank}'. If user does not specify bank, use that bank. "
            f"Current active target (track) is '{current_target}'. "
            "Only use these bank/param options:\n"
            f"{self.mapping.prompt_context()}"
        )

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "voicemix_actions",
                        "strict": True,
                        "schema": ACTION_SCHEMA,
                    },
                },
            )
            content = resp.choices[0].message.content or "{}"
            parsed = json.loads(content)
            validate_action_doc(parsed)
            return parsed
        except Exception:
            return self._heuristic(text, current_bank)

    def _heuristic(self, text: str, current_bank: str) -> dict[str, Any]:
        t = text.lower()
        bank = self.mapping.detect_bank_from_text(t, current_bank)
        actions: list[dict[str, Any]] = []

        direction = 1.0
        if any(k in t for k in ["down", "less", "lower", "reduce", "cut"]):
            direction = -1.0

        strength = 0.04
        if any(k in t for k in ["slight", "slightly", "tiny", "a bit"]):
            strength = 0.02
        if any(k in t for k in ["more", "extra", "stronger", "push"]):
            strength = 0.06
        if any(k in t for k in ["much", "lot", "way"]):
            strength = 0.10

        def add(param: str) -> None:
            actions.append(
                {
                    "bank": bank,
                    "param": param,
                    "mode": "delta",
                    "amount": direction * strength,
                    "reason": "heuristic",
                }
            )

        def add_amount(param: str, amount: float) -> None:
            actions.append(
                {
                    "bank": bank,
                    "param": param,
                    "mode": "delta",
                    "amount": amount,
                    "reason": "heuristic_style",
                }
            )

        # Style-first intent mapping so users can talk in outcomes, not engineering terms.
        if any(k in t for k in ["clearer", "clarity", "clean up", "cleaner"]):
            add_amount("highpass_freq", 0.02)
            add_amount("low_mid_cut", 0.04)
            add_amount("presence", 0.03)
        elif any(k in t for k in ["warmer", "warmth", "less bright", "too bright"]):
            add_amount("brightness", -0.03)
            add_amount("presence", -0.02)
            add_amount("highpass_freq", -0.02)
        elif any(k in t for k in ["forward", "in your face", "up front", "closer"]):
            add_amount("presence", 0.04)
            add_amount("compressor_amount", 0.03)
            add_amount("reverb_send", -0.02)
        elif any(k in t for k in ["smoother", "softer", "less harsh", "tame sibilance"]):
            add_amount("brightness", -0.03)
            add_amount("de_esser_amount", 0.04)
            add_amount("presence", -0.02)
        elif any(k in t for k in ["bigger", "more space", "wider", "more depth"]):
            add_amount("reverb_send", 0.04)
            add_amount("delay_send", 0.02)

        if "pan" in t or "left" in t or "right" in t:
            if "left" in t:
                actions.append(
                    {"bank": bank, "param": "pan", "mode": "delta", "amount": -abs(strength), "reason": "heuristic"}
                )
            elif "right" in t:
                actions.append(
                    {"bank": bank, "param": "pan", "mode": "delta", "amount": abs(strength), "reason": "heuristic"}
                )
            else:
                add("pan")
        elif "volume" in t or "gain" in t or "loud" in t:
            add("volume")
        elif "highpass" in t or "hp" in t:
            add("highpass_freq")
        elif "box" in t or "low-mid" in t or "mud" in t or "chunky" in t or "chungy" in t or "chonky" in t:
            add("low_mid_cut")
        elif "bright" in t or "shelf" in t:
            add("brightness")
        elif "presence" in t:
            add("presence")
        elif "compress" in t:
            add("compressor_amount")
        elif "de-ess" in t or "deesser" in t:
            add("de_esser_amount")
        elif "reverb" in t or "verb" in t:
            add("reverb_send")
        elif "delay" in t:
            add("delay_send")

        if not actions:
            actions = [
                {
                    "bank": bank,
                    "param": "presence",
                    "mode": "delta",
                    "amount": 0.0,
                    "reason": "no-op fallback",
                }
            ]

        parsed = {"actions": actions, "notes": "heuristic_fallback"}
        validate_action_doc(parsed)
        return parsed


class ActionResolver:
    def __init__(self, mapping: MappingConfig, state: StateStore):
        self.mapping = mapping
        self.state = state

    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    def resolve(self, action_doc: dict[str, Any], current_bank: str, target: str, channel: int) -> list[MidiMessageSpec]:
        out: list[MidiMessageSpec] = []
        for action in action_doc.get("actions", []):
            bank_raw = str(action.get("bank", "")).strip() or current_bank
            bank = self.mapping.resolve_bank_name(bank_raw, fallback=current_bank) or current_bank
            param = str(action.get("param", "")).strip()
            mode = str(action.get("mode", "delta"))
            amount = float(action.get("amount", 0.0))

            if bank not in self.mapping.banks:
                continue
            if param not in self.mapping.banks[bank]:
                continue

            cfg = self.mapping.banks[bank][param]
            previous = self.state.get_current(target, bank, param, cfg.default)
            span = max(1, cfg.maximum - cfg.minimum)
            prev_norm = (previous - cfg.minimum) / span

            if mode == "absolute":
                target_norm = self._clamp(amount, 0.0, 1.0)
            else:
                delta = self._clamp(amount, -0.2, 0.2)
                if abs(delta) < 1e-6:
                    delta = cfg.step
                target_norm = self._clamp(prev_norm + delta, 0.0, 1.0)

            value = int(round(cfg.minimum + (target_norm * span)))
            out.append(
                MidiMessageSpec(
                    target=target,
                    bank=bank,
                    param=param,
                    cc=cfg.cc,
                    value=value,
                    channel=channel,
                    previous_value=previous,
                )
            )
        return out


def format_msg(msg: MidiMessageSpec) -> str:
    return f"[{msg.target}] {msg.bank}.{msg.param} CC{msg.cc} {msg.previous_value}->{msg.value} ch{msg.channel}"


def run_undo(state: StateStore, midi: MidiOut | None, dry_run: bool, logger: JsonLogger) -> None:
    entry = state.pop_undo()
    if not entry:
        print("Nothing to undo.")
        return

    for event in entry:
        cc = int(event["cc"])
        prev = int(event["previous"])
        ch = int(event["channel"])
        if dry_run or midi is None:
            print(f"DRY undo CC{cc}={prev} ch{ch}")
        else:
            midi.send_cc(cc, prev, ch)
            print(f"sent undo CC{cc}={prev} ch{ch}")

    logger.log("undo", {"count": len(entry), "events": entry})


def run_channel_verify(
    midi: MidiOut | None,
    dry_run: bool,
    logger: JsonLogger,
    cc: int = 119,
    start_channel: int = 1,
    end_channel: int = 16,
    dwell_seconds: float = 0.10,
) -> None:
    if start_channel > end_channel:
        start_channel, end_channel = end_channel, start_channel
    start_channel = max(1, min(16, start_channel))
    end_channel = max(1, min(16, end_channel))
    cc = max(0, min(127, cc))

    pattern = [0, 32, 64, 96, 127, 0]
    events: list[dict[str, Any]] = []
    print(f"Channel verify: CC{cc}, channels {start_channel}..{end_channel}")
    for ch in range(start_channel, end_channel + 1):
        print(f"- channel {ch}")
        for value in pattern:
            if dry_run or midi is None:
                print(f"  DRY CC{cc}={value} ch{ch}")
            else:
                midi.send_cc(cc, value, ch)
                print(f"  sent CC{cc}={value} ch{ch}")
                time.sleep(dwell_seconds)
            events.append({"cc": cc, "value": value, "channel": ch, "dry_run": dry_run})
    logger.log(
        "channel_verify",
        {"cc": cc, "start_channel": start_channel, "end_channel": end_channel, "events": events},
    )


def run_channel_test(
    midi: MidiOut | None,
    dry_run: bool,
    logger: JsonLogger,
    channel: int,
    cc: int = 119,
) -> None:
    run_channel_verify(
        midi=midi,
        dry_run=dry_run,
        logger=logger,
        cc=cc,
        start_channel=channel,
        end_channel=channel,
        dwell_seconds=0.12,
    )


def snapshot_from_state(mapping: MappingConfig, state: StateStore, target: str) -> dict[str, dict[str, int]]:
    snapshot: dict[str, dict[str, int]] = {}
    for bank, params in mapping.banks.items():
        snapshot[bank] = {}
        for param, cfg in params.items():
            snapshot[bank][param] = state.get_current(target, bank, param, cfg.default)
    return snapshot


def apply_messages_to_snapshot(
    snapshot: dict[str, dict[str, int]],
    messages: list[MidiMessageSpec],
) -> None:
    for msg in messages:
        if msg.bank not in snapshot:
            snapshot[msg.bank] = {}
        snapshot[msg.bank][msg.param] = int(msg.value)


def resolve_preset_to_messages(
    preset_doc: dict[str, Any],
    mapping: MappingConfig,
    state: StateStore,
    target: str,
    channel: int,
) -> list[MidiMessageSpec]:
    values = preset_doc.get("values", {})
    out: list[MidiMessageSpec] = []
    if not isinstance(values, dict):
        return out
    for bank, params in values.items():
        if bank not in mapping.banks or not isinstance(params, dict):
            continue
        for param, raw_value in params.items():
            if param not in mapping.banks[bank]:
                continue
            cfg = mapping.banks[bank][param]
            previous = state.get_current(target, bank, param, cfg.default)
            clamped = max(cfg.minimum, min(cfg.maximum, int(raw_value)))
            out.append(
                MidiMessageSpec(
                    target=target,
                    bank=bank,
                    param=param,
                    cc=cfg.cc,
                    value=clamped,
                    channel=channel,
                    previous_value=previous,
                )
            )
    return out


def run_preset_apply(
    preset_name: str,
    presets: PresetStore,
    mapping: MappingConfig,
    state: StateStore,
    midi: MidiOut | None,
    dry_run: bool,
    logger: JsonLogger,
    target: str,
    channel: int,
) -> list[MidiMessageSpec]:
    try:
        doc = presets.load(preset_name)
    except Exception as e:
        print(f"Preset load failed: {e}")
        return []

    messages = resolve_preset_to_messages(doc, mapping, state, target=target, channel=channel)
    if not messages:
        print("Preset has no mappable values.")
        return []

    emitted: list[dict[str, Any]] = []
    for msg in messages:
        if dry_run or midi is None:
            print(f"DRY {format_msg(msg)}")
        else:
            midi.send_cc(msg.cc, msg.value, msg.channel)
            print(f"sent {format_msg(msg)}")
        emitted.append(
            {
                "target": msg.target,
                "bank": msg.bank,
                "param": msg.param,
                "cc": msg.cc,
                "value": msg.value,
                "prev": msg.previous_value,
                "channel": msg.channel,
            }
        )
    logger.log("preset_apply", {"name": preset_name, "dry_run": dry_run, "target": target, "messages": emitted})
    if not dry_run:
        state.apply_batch(messages, target=target)
    return messages


def run_preset_show(preset_name: str, presets: PresetStore) -> None:
    try:
        doc = presets.load(preset_name)
    except Exception as e:
        print(f"Preset load failed: {e}")
        return
    values = doc.get("values", {})
    print(f"preset: {doc.get('name', preset_name)}")
    print(f"saved_at: {doc.get('saved_at', 'unknown')}")
    if not isinstance(values, dict):
        print("No values.")
        return
    for bank, params in values.items():
        if not isinstance(params, dict):
            continue
        print(f"- {bank}")
        for param, value in params.items():
            print(f"  {param}: {value}")


def print_status(
    mapping: MappingConfig,
    targets: TargetConfig,
    current_target: str,
    current_bank: str,
    current_channel: int,
    dry_run: bool,
    midi_ready: bool,
) -> None:
    mode = "DRY" if dry_run else "APPLY"
    print(f"mode: {mode}")
    print(f"midi_port: {mapping.port_name} ({'connected' if midi_ready else 'not connected'})")
    print(f"channel: {current_channel}")
    print(f"active_target: {current_target}")
    print(f"active_bank: {current_bank}")
    print("targets:")
    print(targets.describe())
    print("banks:")
    for bank, params in mapping.banks.items():
        aliases = mapping.aliases_for_bank(bank)
        alias_text = f" (aliases: {', '.join(aliases)})" if aliases else ""
        print(f"- {bank}{alias_text}: {', '.join(sorted(params.keys()))}")


def example_phrase_for_param(param: str) -> str:
    base = param.replace("_", " ")
    if "highpass" in param:
        return "raise highpass slightly"
    if "low_mid" in param:
        return "cut low mids a bit"
    if "bright" in param:
        return "add a touch of brightness"
    if "presence" in param:
        return "add presence slightly"
    if "compress" in param:
        return "add a little compression"
    if "de_esser" in param:
        return "increase de-esser a little"
    if "reverb" in param:
        return "add a little reverb send"
    if "delay" in param:
        return "add delay send slightly"
    if "volume" in param:
        return "bring volume up slightly"
    if "pan" in param:
        return "pan a little left"
    return f"nudge {base} up slightly"


def print_learn(mapping: MappingConfig, state: StateStore, current_target: str, current_bank: str, full: bool = False) -> None:
    print(f"learn: target '{current_target}' bank '{current_bank}'")
    if full:
        print("Format: param | cc | current | default | min..max | step | example")
    else:
        print("Format: param | cc | current | default | step")
    for param, cfg in mapping.banks[current_bank].items():
        current = state.get_current(current_target, current_bank, param, cfg.default)
        if full:
            print(
                f"- {param} | CC{cfg.cc} | {current} | {cfg.default} | "
                f"{cfg.minimum}..{cfg.maximum} | {cfg.step:.2f} | {example_phrase_for_param(param)}"
            )
        else:
            print(
                f"- {param} | CC{cfg.cc} | {current} | {cfg.default} | {cfg.step:.2f}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VoiceMix REPL")
    parser.add_argument("--mapping", default="mapping.yaml")
    parser.add_argument("--state", default=".voicemix/state.json")
    parser.add_argument("--log", default="logs/voicemix.jsonl")
    parser.add_argument("--llm-model", default="gpt-4o-mini")
    parser.add_argument("--port", default=None)
    parser.add_argument("--bank", default="plugin1")
    parser.add_argument("--target", default="")
    parser.add_argument("--targets", default="targets.yaml")
    parser.add_argument("--preset-dir", default=".voicemix/presets")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--list-ports", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mapping = MappingConfig(Path(args.mapping))
    targets = TargetConfig(Path(args.targets), fallback_channel=mapping.channel, mapping=mapping)
    if args.port:
        mapping.port_name = args.port

    if args.list_ports:
        ports = safe_list_output_ports()
        if not ports:
            print("No MIDI output ports detected (or backend unavailable).")
        else:
            for p in ports:
                print(p)
        return

    logger = JsonLogger(Path(args.log))
    state = StateStore(Path(args.state))
    presets = PresetStore(Path(args.preset_dir))
    translator = LLMTranslator(args.llm_model, mapping)
    resolver = ActionResolver(mapping, state)
    default_target = next(iter(targets.targets))
    current_target = targets.resolve_target(args.target) or default_target
    current_snapshot = snapshot_from_state(mapping, state, target=current_target)

    current_bank = mapping.resolve_bank_name(args.bank, fallback=next(iter(mapping.banks))) or next(iter(mapping.banks))
    preferred_bank = targets.targets[current_target].default_bank
    if preferred_bank:
        current_bank = preferred_bank
    current_channel = targets.targets[current_target].channel
    dry_run = bool(args.dry)

    midi: MidiOut | None = None
    if not dry_run:
        try:
            midi = MidiOut(mapping.port_name)
        except Exception as e:
            print(f"MIDI port '{mapping.port_name}' unavailable: {e}")
            print("Switching to DRY mode. Use /apply to retry after opening the port.")
            dry_run = True

    logger.log(
        "startup",
        {
            "midi_port": mapping.port_name,
            "channel": current_channel,
            "target": current_target,
            "bank": current_bank,
            "dry_run": dry_run,
            "llm_model": args.llm_model,
        },
    )

    print(
        "VoiceMix REPL ready. Type text commands or: "
        "/dry /apply /undo /target [name|list|add|wizard|import] /bank [name] /learn [full] "
        "/preset [save|save-current|list|show|apply] /channel [verify|test] /midi health /status /quit"
    )
    print_status(mapping, targets, current_target, current_bank, current_channel, dry_run, midi is not None)

    while True:
        try:
            line = input("voicemix> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if not line:
            continue

        if line.startswith("/"):
            parts = line.split()
            cmd = parts[0].lower()

            if cmd in {"/quit", "/exit"}:
                print("Exiting.")
                break

            if cmd == "/dry":
                dry_run = True
                print("Mode set to DRY (preview only).")
                logger.log("mode", {"dry_run": True})
                continue

            if cmd == "/apply":
                if midi is None:
                    try:
                        midi = MidiOut(mapping.port_name)
                    except Exception as e:
                        print(f"Cannot open MIDI port '{mapping.port_name}': {e}")
                        continue
                dry_run = False
                print("Mode set to APPLY (sending MIDI).")
                logger.log("mode", {"dry_run": False})
                continue

            if cmd == "/undo":
                run_undo(state, midi, dry_run, logger)
                current_snapshot = snapshot_from_state(mapping, state, target=current_target)
                continue

            if cmd == "/target":
                target_args = shlex.split(line)[1:]
                if len(parts) == 1:
                    print(f"active target: {current_target} (ch {current_channel})")
                    continue
                if target_args and target_args[0].lower() == "list":
                    print("Targets:")
                    print(targets.describe())
                    continue
                if target_args and target_args[0].lower() == "wizard":
                    created = run_target_wizard(targets, mapping, logger)
                    if created:
                        print(f"Wizard added {len(created)} target(s): {', '.join(created)}")
                    else:
                        print("Wizard canceled (no targets added).")
                    continue
                if target_args and target_args[0].lower() == "import":
                    created = run_target_import(targets, mapping, logger)
                    if created:
                        print(f"Import added {len(created)} target(s): {', '.join(created)}")
                    else:
                        print("Import finished (no targets added).")
                    continue
                if target_args and target_args[0].lower() == "add":
                    if len(target_args) < 3:
                        print(
                            "Usage: /target add <name> <channel> [aliases_csv] [default_bank]\n"
                            "       /target wizard\n"
                            "       /target import\n"
                            "Example: /target add guitar_3 6 g3,guitar3 plugin1"
                        )
                        continue
                    try:
                        new_name = target_args[1]
                        new_channel = int(target_args[2])
                        aliases_csv = target_args[3] if len(target_args) >= 4 else ""
                        aliases = [a.strip() for a in aliases_csv.split(",") if a.strip()]
                        default_bank = target_args[4] if len(target_args) >= 5 else None
                        created = targets.add_target(new_name, new_channel, aliases, default_bank=default_bank)
                    except Exception as e:
                        print(f"Failed to add target: {e}")
                        continue
                    print(
                        f"Added target: {created.name} (ch {created.channel}) "
                        f"aliases=[{', '.join(created.aliases) if created.aliases else '-'}]"
                    )
                    logger.log(
                        "target_add",
                        {
                            "target": created.name,
                            "channel": created.channel,
                            "aliases": created.aliases,
                            "default_bank": created.default_bank,
                            "file": str(targets.path),
                        },
                    )
                    continue

                candidate = " ".join(target_args)
                resolved_target = targets.resolve_target(candidate)
                if not resolved_target:
                    print(f"Unknown target '{candidate}'. Use /target list, /target add, /target wizard, or /target import")
                    continue
                current_target = resolved_target
                current_channel = targets.targets[current_target].channel
                preferred_bank = targets.targets[current_target].default_bank
                if preferred_bank:
                    current_bank = preferred_bank
                current_snapshot = snapshot_from_state(mapping, state, target=current_target)
                print(f"active target set to: {current_target} (ch {current_channel})")
                logger.log("target", {"target": current_target, "input": candidate, "channel": current_channel})
                continue

            if cmd == "/bank":
                if len(parts) == 1:
                    print(f"active bank: {current_bank}")
                    continue
                candidate = parts[1]
                resolved = mapping.resolve_bank_name(candidate)
                if not resolved:
                    print(f"Unknown bank '{candidate}'. Available: {', '.join(mapping.banks.keys())}")
                    continue
                current_bank = resolved
                print(f"active bank set to: {current_bank}")
                logger.log("bank", {"bank": current_bank, "input": candidate})
                continue

            if cmd == "/learn":
                full = len(parts) > 1 and parts[1].lower() == "full"
                print_learn(mapping, state, current_target, current_bank, full=full)
                continue

            if cmd == "/status":
                print_status(mapping, targets, current_target, current_bank, current_channel, dry_run, midi is not None)
                continue

            if cmd == "/preset":
                if len(parts) < 2:
                    print("Usage: /preset save <name> | /preset save-current <name> | /preset list | /preset show <name> | /preset apply <name>")
                    continue
                sub = parts[1].lower()
                if sub == "list":
                    items = presets.list_presets()
                    if not items:
                        print("No presets yet.")
                    else:
                        print("Presets:")
                        for p in items:
                            print(f"- {p.stem}")
                    continue
                if sub in {"save", "save-current", "show", "apply"} and len(parts) < 3:
                    print(f"Usage: /preset {sub} <name>")
                    continue
                if sub == "save":
                    name = " ".join(parts[2:])
                    path = presets.save(name, mapping, state, target=current_target)
                    if dry_run:
                        print("Note: saved from current state file (last applied values), not unsent dry previews.")
                    print(f"Preset saved: {path.name}")
                    logger.log("preset_save", {"name": name, "file": path.name, "target": current_target})
                    continue
                if sub == "save-current":
                    name = " ".join(parts[2:])
                    path = presets.save_values(name, current_snapshot)
                    print(f"Preset saved from current session snapshot: {path.name}")
                    logger.log("preset_save_current", {"name": name, "file": path.name, "target": current_target})
                    continue
                if sub == "show":
                    run_preset_show(" ".join(parts[2:]), presets)
                    continue
                if sub == "apply":
                    applied = run_preset_apply(
                        " ".join(parts[2:]),
                        presets,
                        mapping,
                        state,
                        midi,
                        dry_run,
                        logger,
                        target=current_target,
                        channel=current_channel,
                    )
                    if applied:
                        apply_messages_to_snapshot(current_snapshot, applied)
                    continue
                print("Usage: /preset save <name> | /preset save-current <name> | /preset list | /preset show <name> | /preset apply <name>")
                continue

            if cmd == "/channel":
                channel_args = shlex.split(line)[1:]
                if not channel_args:
                    print(
                        "Usage: /channel verify [cc] [start-end]\n"
                        "       /channel test <channel> [cc]\n"
                        "Examples: /channel verify 119 1-16 | /channel test 2 119"
                    )
                    continue
                sub = channel_args[0].lower()
                if sub == "verify":
                    cc = 119
                    start_ch = 1
                    end_ch = 16
                    if len(channel_args) >= 2:
                        try:
                            cc = int(channel_args[1])
                        except ValueError:
                            print("CC must be an integer 0..127")
                            continue
                    if len(channel_args) >= 3:
                        span = channel_args[2]
                        if "-" in span:
                            left, right = span.split("-", 1)
                            try:
                                start_ch = int(left)
                                end_ch = int(right)
                            except ValueError:
                                print("Range must be start-end, e.g. 1-16")
                                continue
                        else:
                            try:
                                start_ch = int(span)
                                end_ch = start_ch
                            except ValueError:
                                print("Channel range must be start-end or single channel.")
                                continue
                    run_channel_verify(
                        midi=midi,
                        dry_run=dry_run,
                        logger=logger,
                        cc=cc,
                        start_channel=start_ch,
                        end_channel=end_ch,
                    )
                    continue
                if sub == "test":
                    if len(channel_args) < 2:
                        print("Usage: /channel test <channel> [cc]")
                        continue
                    try:
                        test_ch = int(channel_args[1])
                    except ValueError:
                        print("Channel must be an integer in 1..16")
                        continue
                    test_cc = 119
                    if len(channel_args) >= 3:
                        try:
                            test_cc = int(channel_args[2])
                        except ValueError:
                            print("CC must be an integer 0..127")
                            continue
                    run_channel_test(
                        midi=midi,
                        dry_run=dry_run,
                        logger=logger,
                        channel=test_ch,
                        cc=test_cc,
                    )
                    continue
                print("Usage: /channel verify [cc] [start-end] | /channel test <channel> [cc]")
                continue

            if cmd == "/midi":
                midi_args = shlex.split(line)[1:]
                if midi_args and midi_args[0].lower() == "health":
                    report = midi_health(mapping.port_name)
                    print("MIDI health:")
                    print(json.dumps(report, indent=2))
                    continue
                print("Usage: /midi health")
                continue

            print(
                "Unknown command. Use /dry /apply /undo /target /bank /learn [full] "
                "/preset [save|save-current|list|show|apply] /channel [verify|test] /midi health /status /quit"
            )
            continue

        transcript = line
        detected_target = targets.detect_target_from_text(transcript, current_target)
        if detected_target != current_target:
            current_target = detected_target
            current_channel = targets.targets[current_target].channel
            preferred_bank = targets.targets[current_target].default_bank
            if preferred_bank:
                current_bank = preferred_bank
            current_snapshot = snapshot_from_state(mapping, state, target=current_target)
            print(f"auto-target: {current_target} (ch {current_channel})")
            logger.log("target_auto", {"target": current_target, "text": transcript, "channel": current_channel})

        logger.log("transcript", {"text": transcript, "target": current_target, "bank": current_bank})

        action_doc = translator.translate(transcript, current_bank, current_target)
        try:
            validate_action_doc(action_doc)
        except SchemaValidationError as e:
            print(f"Invalid action JSON: {e}")
            logger.log("actions_invalid", {"error": str(e), "raw": action_doc})
            continue

        logger.log("actions", action_doc)
        messages = resolver.resolve(action_doc, current_bank, target=current_target, channel=current_channel)

        if not messages:
            print("No mapped actions.")
            logger.log("midi", {"messages": []})
            continue

        emitted: list[dict[str, Any]] = []
        for msg in messages:
            if dry_run or midi is None:
                print(f"DRY {format_msg(msg)}")
            else:
                midi.send_cc(msg.cc, msg.value, msg.channel)
                print(f"sent {format_msg(msg)}")

            emitted.append(
                {
                    "target": msg.target,
                    "bank": msg.bank,
                    "param": msg.param,
                    "cc": msg.cc,
                    "value": msg.value,
                    "prev": msg.previous_value,
                    "channel": msg.channel,
                }
            )

        logger.log("midi", {"dry_run": dry_run, "messages": emitted})
        apply_messages_to_snapshot(current_snapshot, messages)
        if not dry_run:
            state.apply_batch(messages, target=current_target)


if __name__ == "__main__":
    main()
