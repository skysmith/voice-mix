#!/usr/bin/env python3
"""VoiceMix desktop GUI (Tkinter) powered by the existing engine classes."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from midi_bridge import probe_output_port, resolve_output_port_name, safe_list_output_ports
from main import (
    ActionResolver,
    JsonLogger,
    LLMTranslator,
    MappingConfig,
    MidiOut,
    PresetStore,
    SchemaValidationError,
    StateStore,
    TargetConfig,
    apply_messages_to_snapshot,
    run_preset_apply,
    run_undo,
    snapshot_from_state,
    validate_action_doc,
)


class VoiceMixGuiController:
    def __init__(
        self,
        mapping_path: Path,
        targets_path: Path,
        state_path: Path,
        preset_dir: Path,
        log_path: Path,
        llm_model: str,
        start_target: str,
        start_bank: str,
        dry_run: bool,
        midi_port: str | None = None,
    ):
        self.mapping = MappingConfig(mapping_path)
        if midi_port:
            self.mapping.port_name = midi_port

        self.targets = TargetConfig(targets_path, fallback_channel=self.mapping.channel, mapping=self.mapping)
        self.state = StateStore(state_path)
        self.presets = PresetStore(preset_dir)
        self.logger = JsonLogger(log_path)
        self.translator = LLMTranslator(llm_model, self.mapping)
        self.resolver = ActionResolver(self.mapping, self.state)

        default_target = next(iter(self.targets.targets))
        self.current_target = self.targets.resolve_target(start_target) or default_target
        self.current_bank = self.mapping.resolve_bank_name(start_bank, fallback=next(iter(self.mapping.banks))) or next(iter(self.mapping.banks))

        preferred = self.targets.targets[self.current_target].default_bank
        if preferred:
            self.current_bank = preferred

        self.current_channel = self.targets.targets[self.current_target].channel
        self.current_snapshots: dict[str, dict[str, dict[str, int]]] = {}
        self.dry_run = dry_run
        self.midi: MidiOut | None = None

        if not self.dry_run:
            try:
                self.midi = MidiOut(self.mapping.port_name)
            except Exception:
                self.dry_run = True
                self.midi = None

        self.logger.log(
            "startup_gui",
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "target": self.current_target,
                "bank": self.current_bank,
                "channel": self.current_channel,
                "dry_run": self.dry_run,
                "port": self.mapping.port_name,
                "llm_model": llm_model,
            },
        )

    def _snapshot_for_target(self, target: str) -> dict[str, dict[str, int]]:
        if target not in self.current_snapshots:
            self.current_snapshots[target] = snapshot_from_state(self.mapping, self.state, target=target)
        return self.current_snapshots[target]

    def available_targets(self) -> list[str]:
        return list(self.targets.targets.keys())

    def available_banks(self) -> list[str]:
        return list(self.mapping.banks.keys())

    def available_ports(self) -> list[str]:
        return safe_list_output_ports()

    def set_port(self, port_name: str) -> str:
        port_name = port_name.strip()
        if not port_name:
            raise ValueError("port name cannot be empty")
        ok, detail = probe_output_port(port_name)
        if not ok:
            raise ValueError(f"port probe failed for '{port_name}': {detail}")
        resolved = resolve_output_port_name(port_name)
        self.mapping.port_name = resolved
        if not self.dry_run:
            self.midi = MidiOut(self.mapping.port_name)
        self.logger.log("midi_port", {"port": self.mapping.port_name, "source": "gui"})
        return f"MIDI port set to: {self.mapping.port_name}"

    def available_params(self, bank: str | None = None) -> list[str]:
        target_bank = bank or self.current_bank
        if target_bank not in self.mapping.banks:
            return []
        return list(self.mapping.banks[target_bank].keys())

    def set_target(self, target_name: str) -> str:
        resolved = self.targets.resolve_target(target_name)
        if not resolved:
            raise ValueError(f"Unknown target: {target_name}")
        self.current_target = resolved
        self.current_channel = self.targets.targets[self.current_target].channel
        preferred = self.targets.targets[self.current_target].default_bank
        if preferred:
            self.current_bank = preferred
        self._snapshot_for_target(self.current_target)
        self.logger.log("target", {"target": self.current_target, "channel": self.current_channel, "source": "gui"})
        return f"active target set to: {self.current_target} (ch {self.current_channel})"

    def set_bank(self, bank_name: str) -> str:
        resolved = self.mapping.resolve_bank_name(bank_name)
        if not resolved:
            raise ValueError(f"Unknown bank: {bank_name}")
        self.current_bank = resolved
        self.logger.log("bank", {"bank": self.current_bank, "source": "gui"})
        return f"active bank set to: {self.current_bank}"

    def set_mode(self, dry_run: bool) -> str:
        if not dry_run and self.midi is None:
            ok, detail = probe_output_port(self.mapping.port_name)
            if not ok:
                raise RuntimeError(f"MIDI port '{self.mapping.port_name}' is not ready: {detail}")
            self.midi = MidiOut(self.mapping.port_name)
        self.dry_run = dry_run
        self.logger.log("mode", {"dry_run": self.dry_run, "source": "gui"})
        return "Mode set to DRY (preview only)." if dry_run else "Mode set to APPLY (sending MIDI)."

    def status_text(self) -> str:
        mode = "DRY" if self.dry_run else "APPLY"
        lines = [
            f"mode: {mode}",
            f"midi_port: {self.mapping.port_name} ({'connected' if self.midi is not None else 'not connected'})",
            f"channel: {self.current_channel}",
            f"active_target: {self.current_target}",
            f"active_bank: {self.current_bank}",
            "targets:",
            self.targets.describe(),
            "banks:",
        ]
        for bank, params in self.mapping.banks.items():
            lines.append(f"- {bank}: {', '.join(sorted(params.keys()))}")
        return "\n".join(lines)

    def learn_text(self, full: bool = False) -> str:
        lines: list[str] = [f"learn: target '{self.current_target}' bank '{self.current_bank}'"]
        if full:
            lines.append("Format: param | cc | current | default | min..max | step")
        else:
            lines.append("Format: param | cc | current | default | step")
        for param, cfg in self.mapping.banks[self.current_bank].items():
            current = self.state.get_current(self.current_target, self.current_bank, param, cfg.default)
            if full:
                lines.append(
                    f"- {param} | CC{cfg.cc} | {current} | {cfg.default} | {cfg.minimum}..{cfg.maximum} | {cfg.step:.2f}"
                )
            else:
                lines.append(f"- {param} | CC{cfg.cc} | {current} | {cfg.default} | {cfg.step:.2f}")
        return "\n".join(lines)

    def add_target(self, name: str, channel: int, aliases_csv: str, default_bank: str | None) -> str:
        aliases = [a.strip() for a in aliases_csv.split(",") if a.strip()]
        spec = self.targets.add_target(name, channel, aliases, default_bank=default_bank)
        self.logger.log(
            "target_add",
            {
                "target": spec.name,
                "channel": spec.channel,
                "aliases": spec.aliases,
                "default_bank": spec.default_bank,
                "source": "gui",
            },
        )
        return f"Added target: {spec.name} (ch {spec.channel}) aliases=[{', '.join(spec.aliases) if spec.aliases else '-'}]"

    def channel_verify(self, cc: int = 119, start_channel: int = 1, end_channel: int = 16) -> list[str]:
        if start_channel > end_channel:
            start_channel, end_channel = end_channel, start_channel
        start_channel = max(1, min(16, start_channel))
        end_channel = max(1, min(16, end_channel))
        cc = max(0, min(127, cc))

        pattern = [0, 32, 64, 96, 127, 0]
        lines: list[str] = [f"Channel verify: CC{cc}, channels {start_channel}..{end_channel}"]
        events: list[dict[str, Any]] = []
        for ch in range(start_channel, end_channel + 1):
            lines.append(f"- channel {ch}")
            for value in pattern:
                if self.dry_run or self.midi is None:
                    lines.append(f"  DRY CC{cc}={value} ch{ch}")
                else:
                    self.midi.send_cc(cc, value, ch)
                    lines.append(f"  sent CC{cc}={value} ch{ch}")
                events.append({"cc": cc, "value": value, "channel": ch, "dry_run": self.dry_run})
        self.logger.log("channel_verify_gui", {"cc": cc, "start_channel": start_channel, "end_channel": end_channel, "events": events})
        return lines

    def pulse_param_for_learn(
        self,
        target_name: str,
        channel: int,
        bank: str,
        param: str,
        aliases_csv: str = "",
    ) -> list[str]:
        aliases = [a.strip() for a in aliases_csv.split(",") if a.strip()]
        spec = self.targets.upsert_target(
            name=target_name,
            channel=channel,
            aliases=aliases,
            default_bank=bank,
            keep_existing_aliases=True,
        )
        self.current_target = spec.name
        self.current_channel = spec.channel
        self.current_bank = bank
        self._snapshot_for_target(self.current_target)

        if bank not in self.mapping.banks or param not in self.mapping.banks[bank]:
            raise ValueError("unknown bank/param for learn pulse")
        cc = self.mapping.banks[bank][param].cc

        pattern = [0, 64, 127, 64, 0]
        lines: list[str] = [
            f"Learn pulse target={self.current_target} ch={self.current_channel} bank={bank} param={param} CC{cc}",
            "In Logic: arm Learn, select destination control, then watch pulses.",
        ]
        events: list[dict[str, Any]] = []
        for value in pattern:
            if self.dry_run or self.midi is None:
                lines.append(f"DRY CC{cc}={value} ch{self.current_channel}")
            else:
                self.midi.send_cc(cc, value, self.current_channel)
                lines.append(f"sent CC{cc}={value} ch{self.current_channel}")
            events.append({"cc": cc, "value": value, "channel": self.current_channel, "dry_run": self.dry_run})
        self.logger.log(
            "logic_learn_pulse",
            {
                "target": self.current_target,
                "channel": self.current_channel,
                "bank": bank,
                "param": param,
                "cc": cc,
                "events": events,
                "source": "gui",
            },
        )
        return lines

    def list_presets(self) -> list[str]:
        return [p.stem for p in self.presets.list_presets()]

    def save_preset(self, name: str, use_current_snapshot: bool) -> str:
        if use_current_snapshot:
            path = self.presets.save_values(name, self._snapshot_for_target(self.current_target))
            self.logger.log("preset_save_current", {"name": name, "file": path.name, "target": self.current_target, "source": "gui"})
            return f"Preset saved from current session snapshot: {path.name}"
        path = self.presets.save(name, self.mapping, self.state, target=self.current_target)
        self.logger.log("preset_save", {"name": name, "file": path.name, "target": self.current_target, "source": "gui"})
        return f"Preset saved: {path.name}"

    def show_preset(self, name: str) -> str:
        doc = self.presets.load(name)
        values = doc.get("values", {})
        lines = [f"preset: {doc.get('name', name)}", f"saved_at: {doc.get('saved_at', 'unknown')}"]
        if isinstance(values, dict):
            for bank, params in values.items():
                lines.append(f"- {bank}")
                if isinstance(params, dict):
                    for param, value in params.items():
                        lines.append(f"  {param}: {value}")
        return "\n".join(lines)

    def apply_preset(self, name: str) -> str:
        messages = run_preset_apply(
            name,
            self.presets,
            self.mapping,
            self.state,
            self.midi,
            self.dry_run,
            self.logger,
            target=self.current_target,
            channel=self.current_channel,
        )
        if messages:
            apply_messages_to_snapshot(self._snapshot_for_target(self.current_target), messages)
            return f"Applied preset '{name}' ({len(messages)} message(s))."
        return f"Preset '{name}' produced no mappable messages."

    def undo(self) -> str:
        run_undo(self.state, self.midi, self.dry_run, self.logger)
        self.current_snapshots[self.current_target] = snapshot_from_state(self.mapping, self.state, target=self.current_target)
        return "Undo processed."

    def process_text(self, transcript: str) -> list[str]:
        out: list[str] = []
        detected = self.targets.detect_target_from_text(transcript, self.current_target)
        if detected != self.current_target:
            self.current_target = detected
            self.current_channel = self.targets.targets[self.current_target].channel
            preferred = self.targets.targets[self.current_target].default_bank
            if preferred:
                self.current_bank = preferred
            out.append(f"auto-target: {self.current_target} (ch {self.current_channel})")

        self.logger.log("transcript", {"text": transcript, "target": self.current_target, "bank": self.current_bank, "source": "gui"})

        action_doc = self.translator.translate(transcript, self.current_bank, self.current_target)
        validate_action_doc(action_doc)
        self.logger.log("actions", action_doc)

        messages = self.resolver.resolve(action_doc, self.current_bank, target=self.current_target, channel=self.current_channel)
        if not messages:
            out.append("No mapped actions.")
            return out

        emitted: list[dict[str, Any]] = []
        for msg in messages:
            if self.dry_run or self.midi is None:
                out.append(f"DRY [{msg.target}] {msg.bank}.{msg.param} CC{msg.cc} {msg.previous_value}->{msg.value} ch{msg.channel}")
            else:
                self.midi.send_cc(msg.cc, msg.value, msg.channel)
                out.append(f"sent [{msg.target}] {msg.bank}.{msg.param} CC{msg.cc} {msg.previous_value}->{msg.value} ch{msg.channel}")

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

        self.logger.log("midi", {"dry_run": self.dry_run, "messages": emitted, "source": "gui"})
        apply_messages_to_snapshot(self._snapshot_for_target(self.current_target), messages)
        if not self.dry_run:
            self.state.apply_batch(messages, target=self.current_target)

        return out


class VoiceMixGuiApp(tk.Tk):
    def __init__(self, controller: VoiceMixGuiController):
        super().__init__()
        self.controller = controller
        self.title("VoiceMix GUI")
        self.geometry("1120x760")

        self.target_var = tk.StringVar(value=self.controller.current_target)
        self.bank_var = tk.StringVar(value=self.controller.current_bank)
        current_params = self.controller.available_params(self.controller.current_bank)
        self.param_var = tk.StringVar(value=current_params[0] if current_params else "")
        self.port_var = tk.StringVar(value=self.controller.mapping.port_name)
        self.mode_var = tk.StringVar(value="DRY" if self.controller.dry_run else "APPLY")
        self.preset_var = tk.StringVar()

        self._build_ui()
        self._log("VoiceMix GUI ready.")
        self._log(self.controller.status_text())
        self.refresh_presets()

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Target:").pack(side=tk.LEFT)
        self.target_combo = ttk.Combobox(top, state="readonly", textvariable=self.target_var, values=self.controller.available_targets(), width=18)
        self.target_combo.pack(side=tk.LEFT, padx=(4, 8))
        self.target_combo.bind("<<ComboboxSelected>>", self.on_target_change)

        ttk.Label(top, text="Bank:").pack(side=tk.LEFT)
        self.bank_combo = ttk.Combobox(top, state="readonly", textvariable=self.bank_var, values=self.controller.available_banks(), width=14)
        self.bank_combo.pack(side=tk.LEFT, padx=(4, 8))
        self.bank_combo.bind("<<ComboboxSelected>>", self.on_bank_change)

        current_params = self.controller.available_params(self.bank_var.get().strip() or self.controller.current_bank)
        if current_params and not self.param_var.get():
            self.param_var.set(current_params[0])
        ttk.Label(top, text="Param:").pack(side=tk.LEFT)
        self.param_combo = ttk.Combobox(top, state="readonly", textvariable=self.param_var, values=current_params, width=18)
        self.param_combo.pack(side=tk.LEFT, padx=(4, 8))

        ttk.Label(top, text="Port:").pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=22)
        self.port_combo.pack(side=tk.LEFT, padx=(4, 4))
        self.port_combo.bind("<<ComboboxSelected>>", self.on_port_change)
        ttk.Button(top, text="Refresh Ports", command=self.refresh_ports).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(top, text="Mode:").pack(side=tk.LEFT)
        self.mode_label = ttk.Label(top, textvariable=self.mode_var, width=8)
        self.mode_label.pack(side=tk.LEFT, padx=(4, 4))
        ttk.Button(top, text="DRY", command=lambda: self.set_mode(True)).pack(side=tk.LEFT)
        ttk.Button(top, text="APPLY", command=lambda: self.set_mode(False)).pack(side=tk.LEFT, padx=(4, 16))

        ttk.Button(top, text="Undo", command=self.on_undo).pack(side=tk.LEFT)
        ttk.Button(top, text="Status", command=self.on_status).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(top, text="Learn", command=lambda: self.on_learn(False)).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(top, text="Learn Full", command=lambda: self.on_learn(True)).pack(side=tk.LEFT, padx=(4, 0))

        target_row = ttk.Frame(self, padding=(8, 0, 8, 8))
        target_row.pack(fill=tk.X)
        ttk.Button(target_row, text="Add Target", command=self.on_add_target).pack(side=tk.LEFT)
        ttk.Button(target_row, text="Import Targets", command=self.on_import_targets).pack(side=tk.LEFT, padx=(6, 0))

        learn_row = ttk.Frame(self, padding=(8, 0, 8, 8))
        learn_row.pack(fill=tk.X)
        ttk.Label(learn_row, text="Logic Learn:").pack(side=tk.LEFT)
        ttk.Button(learn_row, text="Session Setup Wizard", command=self.on_session_setup_wizard).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(learn_row, text="Verify All (1-16)", command=self.on_verify_all_channels).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(learn_row, text="Verify Current", command=self.on_verify_current_channel).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(learn_row, text="Learn Bind (Selected Param)", command=self.on_learn_bind).pack(side=tk.LEFT, padx=(6, 0))

        preset_row = ttk.Frame(self, padding=(8, 0, 8, 8))
        preset_row.pack(fill=tk.X)
        ttk.Label(preset_row, text="Preset:").pack(side=tk.LEFT)
        self.preset_combo = ttk.Combobox(preset_row, textvariable=self.preset_var, width=24)
        self.preset_combo.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(preset_row, text="Refresh", command=self.refresh_presets).pack(side=tk.LEFT)
        ttk.Button(preset_row, text="Save", command=lambda: self.on_preset_save(False)).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(preset_row, text="Save Current", command=lambda: self.on_preset_save(True)).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(preset_row, text="Apply", command=self.on_preset_apply).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(preset_row, text="Show", command=self.on_preset_show).pack(side=tk.LEFT, padx=(4, 0))

        input_row = ttk.Frame(self, padding=(8, 0, 8, 8))
        input_row.pack(fill=tk.X)
        ttk.Label(input_row, text="Instruction:").pack(side=tk.LEFT)
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(input_row, textvariable=self.input_var)
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        self.input_entry.bind("<Return>", self.on_send)
        ttk.Button(input_row, text="Send", command=self.on_send).pack(side=tk.LEFT)

        log_frame = ttk.Frame(self, padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log = tk.Text(log_frame, wrap=tk.WORD)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.configure(yscrollcommand=scroll.set)

    def _log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.insert(tk.END, f"[{stamp}] {text}\n")
        self.log.see(tk.END)

    def on_send(self, _event=None) -> None:
        text = self.input_var.get().strip()
        if not text:
            return
        self.input_var.set("")
        self._log(f"> {text}")
        try:
            lines = self.controller.process_text(text)
            self.target_var.set(self.controller.current_target)
            self.bank_var.set(self.controller.current_bank)
            for line in lines:
                self._log(line)
        except SchemaValidationError as e:
            self._log(f"Invalid action JSON: {e}")
        except Exception as e:
            self._log(f"Error: {e}")

    def on_target_change(self, _event=None) -> None:
        target = self.target_var.get().strip()
        if not target:
            return
        try:
            msg = self.controller.set_target(target)
            self.bank_var.set(self.controller.current_bank)
            params = self.controller.available_params(self.controller.current_bank)
            self.param_combo.configure(values=params)
            if params:
                self.param_var.set(params[0])
            self._log(msg)
        except Exception as e:
            self._log(f"Target error: {e}")

    def on_bank_change(self, _event=None) -> None:
        bank = self.bank_var.get().strip()
        if not bank:
            return
        try:
            self._log(self.controller.set_bank(bank))
            params = self.controller.available_params(bank)
            self.param_combo.configure(values=params)
            if params:
                self.param_var.set(params[0])
        except Exception as e:
            self._log(f"Bank error: {e}")

    def refresh_ports(self) -> None:
        ports = self.controller.available_ports()
        current = self.port_var.get().strip()
        if current and current not in ports:
            ports = [current, *ports]
        self.port_combo.configure(values=ports)
        self._log(f"Detected MIDI ports: {', '.join(ports) if ports else '(none)'}")

    def on_port_change(self, _event=None) -> None:
        self._apply_port_from_ui()

    def _apply_port_from_ui(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            return
        try:
            self._log(self.controller.set_port(port))
        except Exception as e:
            self._log(f"Port error: {e}")

    def set_mode(self, dry: bool) -> None:
        try:
            self._apply_port_from_ui()
            msg = self.controller.set_mode(dry)
            self.mode_var.set("DRY" if self.controller.dry_run else "APPLY")
            self._log(msg)
        except Exception as e:
            self._log(f"Mode error: {e}")

    def on_undo(self) -> None:
        try:
            self._log(self.controller.undo())
        except Exception as e:
            self._log(f"Undo error: {e}")

    def on_status(self) -> None:
        self._log(self.controller.status_text())

    def on_learn(self, full: bool) -> None:
        self._log(self.controller.learn_text(full=full))

    def on_add_target(self) -> None:
        name = simpledialog.askstring("Add Target", "Target name (e.g. guitar_3):", parent=self)
        if not name:
            return
        channel = simpledialog.askinteger("Add Target", "MIDI channel (1-16):", parent=self, minvalue=1, maxvalue=16)
        if channel is None:
            return
        aliases = simpledialog.askstring("Add Target", "Aliases CSV (optional):", parent=self) or ""
        default_bank = simpledialog.askstring("Add Target", "Default bank (optional):", parent=self)
        default_bank = default_bank.strip() if default_bank else None

        try:
            msg = self.controller.add_target(name, channel, aliases, default_bank)
            self.target_combo.configure(values=self.controller.available_targets())
            self._log(msg)
        except Exception as e:
            self._log(f"Add target error: {e}")

    def on_import_targets(self) -> None:
        start_channel = simpledialog.askinteger(
            "Import Targets",
            "Starting MIDI channel (1-16):",
            parent=self,
            minvalue=1,
            maxvalue=16,
            initialvalue=1,
        )
        if start_channel is None:
            return

        next_channel = start_channel
        count = 0
        self._log("Target import mode: select a track in Logic, then enter its name in the prompt.")
        while True:
            name = simpledialog.askstring(
                "Import Targets",
                "Select a track in Logic, then enter track name (Cancel/blank to finish):",
                parent=self,
            )
            if not name or not name.strip():
                break
            name = name.strip()

            channel = simpledialog.askinteger(
                "Import Targets",
                f"Channel for '{name}' (1-16):",
                parent=self,
                minvalue=1,
                maxvalue=16,
                initialvalue=next_channel,
            )
            if channel is None:
                break

            aliases = simpledialog.askstring(
                "Import Targets",
                "Aliases CSV (optional):",
                parent=self,
            ) or ""
            default_bank = simpledialog.askstring(
                "Import Targets",
                "Default bank (optional, Enter for plugin1):",
                parent=self,
            )
            default_bank = default_bank.strip() if default_bank else "plugin1"

            try:
                msg = self.controller.add_target(name, channel, aliases, default_bank)
                self._log(msg)
                count += 1
                if channel < 16:
                    next_channel = channel + 1
            except Exception as e:
                self._log(f"Import target error: {e}")
                continue

            again = messagebox.askyesno("Import Targets", "Import another track?", parent=self)
            if not again:
                break

        self.target_combo.configure(values=self.controller.available_targets())
        self._log(f"Import complete. Added {count} target(s).")

    def on_session_setup_wizard(self) -> None:
        self._log("Session Setup Wizard started.")
        self.refresh_ports()
        current = self.port_var.get().strip()
        suggested = current
        ports = self.controller.available_ports()
        if not suggested and ports:
            suggested = ports[0]
        selected = simpledialog.askstring(
            "Session Setup",
            "Step 1/4: Enter or select MIDI output port name:",
            parent=self,
            initialvalue=suggested,
        )
        if not selected or not selected.strip():
            self._log("Session Setup canceled at port selection.")
            return
        self.port_var.set(selected.strip())
        self._apply_port_from_ui()

        run_import = messagebox.askyesno(
            "Session Setup",
            "Step 2/4: Import tracks/targets now?",
            parent=self,
        )
        if run_import:
            self.on_import_targets()

        verify_choice = messagebox.askyesnocancel(
            "Session Setup",
            "Step 3/4: Run channel verify?\nYes = verify current channel\nNo = verify all channels\nCancel = skip",
            parent=self,
        )
        if verify_choice is True:
            self.on_verify_current_channel()
        elif verify_choice is False:
            self.on_verify_all_channels()

        run_bind = messagebox.askyesno(
            "Session Setup",
            "Step 4/4: Run Learn Bind for selected param now?",
            parent=self,
        )
        if run_bind:
            self.on_learn_bind()
        self._log("Session Setup Wizard complete.")

    def on_verify_all_channels(self) -> None:
        if self.controller.dry_run:
            proceed = messagebox.askyesno("VoiceMix", "You are in DRY mode. Run dry preview anyway?", parent=self)
            if not proceed:
                return
        try:
            lines = self.controller.channel_verify(cc=119, start_channel=1, end_channel=16)
            for line in lines:
                self._log(line)
        except Exception as e:
            self._log(f"Verify error: {e}")

    def on_verify_current_channel(self) -> None:
        if self.controller.dry_run:
            proceed = messagebox.askyesno("VoiceMix", "You are in DRY mode. Run dry preview anyway?", parent=self)
            if not proceed:
                return
        try:
            ch = self.controller.current_channel
            lines = self.controller.channel_verify(cc=119, start_channel=ch, end_channel=ch)
            for line in lines:
                self._log(line)
        except Exception as e:
            self._log(f"Verify error: {e}")

    def on_learn_bind(self) -> None:
        target_name = self.target_var.get().strip() or self.controller.current_target
        bank = self.bank_var.get().strip() or self.controller.current_bank
        param = self.param_var.get().strip()
        if not param:
            self._log("Learn bind error: no parameter selected.")
            return

        channel = simpledialog.askinteger(
            "Logic Learn Bind",
            f"Channel for target '{target_name}' (1-16):",
            parent=self,
            minvalue=1,
            maxvalue=16,
            initialvalue=self.controller.current_channel,
        )
        if channel is None:
            return
        aliases = simpledialog.askstring(
            "Logic Learn Bind",
            "Optional aliases CSV for this target:",
            parent=self,
        ) or ""

        if self.controller.dry_run:
            proceed = messagebox.askyesno(
                "VoiceMix",
                "You are in DRY mode. Run dry learn preview only?\n(Choose No and switch to APPLY to send real MIDI.)",
                parent=self,
            )
            if not proceed:
                return

        try:
            lines = self.controller.pulse_param_for_learn(
                target_name=target_name,
                channel=channel,
                bank=bank,
                param=param,
                aliases_csv=aliases,
            )
            self.target_combo.configure(values=self.controller.available_targets())
            self.target_var.set(self.controller.current_target)
            self.bank_var.set(self.controller.current_bank)
            for line in lines:
                self._log(line)
        except Exception as e:
            self._log(f"Learn bind error: {e}")

    def refresh_presets(self) -> None:
        names = self.controller.list_presets()
        self.preset_combo.configure(values=names)

    def on_preset_save(self, current: bool) -> None:
        name = self.preset_var.get().strip()
        if not name:
            name = simpledialog.askstring("Preset Name", "Enter preset name:", parent=self) or ""
        name = name.strip()
        if not name:
            return
        try:
            self._log(self.controller.save_preset(name, use_current_snapshot=current))
            self.refresh_presets()
            self.preset_var.set(name)
        except Exception as e:
            self._log(f"Preset save error: {e}")

    def on_preset_apply(self) -> None:
        name = self.preset_var.get().strip()
        if not name:
            messagebox.showinfo("VoiceMix", "Select or type a preset name first.")
            return
        try:
            self._log(self.controller.apply_preset(name))
        except Exception as e:
            self._log(f"Preset apply error: {e}")

    def on_preset_show(self) -> None:
        name = self.preset_var.get().strip()
        if not name:
            messagebox.showinfo("VoiceMix", "Select or type a preset name first.")
            return
        try:
            text = self.controller.show_preset(name)
            self._log(text)
            messagebox.showinfo(f"Preset: {name}", text)
        except Exception as e:
            self._log(f"Preset show error: {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VoiceMix GUI")
    parser.add_argument("--mapping", default="mapping.yaml")
    parser.add_argument("--targets", default="targets.yaml")
    parser.add_argument("--state", default=".voicemix/state.json")
    parser.add_argument("--preset-dir", default=".voicemix/presets")
    parser.add_argument("--log", default="logs/voicemix-gui.jsonl")
    parser.add_argument("--llm-model", default="gpt-4o-mini")
    parser.add_argument("--port", default=None)
    parser.add_argument("--bank", default="plugin1")
    parser.add_argument("--target", default="")
    parser.add_argument("--dry", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    controller = VoiceMixGuiController(
        mapping_path=Path(args.mapping),
        targets_path=Path(args.targets),
        state_path=Path(args.state),
        preset_dir=Path(args.preset_dir),
        log_path=Path(args.log),
        llm_model=args.llm_model,
        start_target=args.target,
        start_bank=args.bank,
        dry_run=args.dry,
        midi_port=args.port,
    )
    app = VoiceMixGuiApp(controller)
    app.mainloop()


if __name__ == "__main__":
    main()
