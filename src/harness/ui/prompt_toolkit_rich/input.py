from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import DummyHistory, FileHistory
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.widgets import Button, Dialog, Label, RadioList
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from harness.api.types import ToolDefinition
from harness.model_selection import MODEL_PICKER_EXIT, ModelOption


class PromptToolkitInput:
    """Input adapter implemented with prompt-toolkit."""

    _PASTE_GRACE = 0.08

    def __init__(self, history_file: Path) -> None:
        self._reading = False
        self.last_input_was_multiline_paste = False
        self._pending_enter: asyncio.TimerHandle | None = None
        self._burst_timer: asyncio.TimerHandle | None = None
        self._input_processor: Any = None
        bindings = KeyBindings()
        reading = Condition(lambda: self._reading)

        @bindings.add("enter", filter=reading, eager=True)
        @bindings.add("c-j", filter=reading, eager=True)
        @bindings.add("escape", "enter", filter=reading, eager=True)
        def enter(event: Any) -> None:
            if self._input_processor is None:
                self._input_processor = event.key_processor
                self._input_processor.before_key_press += self._continue_input
            previous = event.previous_key_sequence
            paired_lf = (
                event.key_sequence[-1].key == Keys.ControlJ
                and len(previous) == 1
                and previous[0].key == Keys.ControlM
            )
            if self._burst_timer is not None:
                if not paired_lf:
                    event.current_buffer.insert_text(" ")
            else:
                self._pending_enter = asyncio.get_running_loop().call_later(
                    self._PASTE_GRACE, self._submit
                )

        @bindings.add(Keys.BracketedPaste, filter=reading)
        def paste(event: Any) -> None:
            text = event.data.replace("\r\n", "\n").replace("\r", "\n")
            self.last_input_was_multiline_paste |= "\n" in text
            event.current_buffer.insert_text(text.replace("\n", " "))

        self.session: Any = PromptSession(
            history=FileHistory(str(history_file)),
            enable_history_search=True,
            key_bindings=bindings,
        )

    def _continue_input(self, _processor: Any) -> None:
        # Another key before pending Enter settles is an unbracketed input
        # burst. Keep its newlines as text, including a trailing newline.
        if self._pending_enter is not None:
            self._pending_enter.cancel()
            self._pending_enter = None
            self.session.default_buffer.insert_text(" ")
            self.last_input_was_multiline_paste = True
            self._restart_burst_timer()
        elif self._burst_timer is not None:
            self._restart_burst_timer()

    def _restart_burst_timer(self) -> None:
        if self._burst_timer is not None:
            self._burst_timer.cancel()
        self._burst_timer = asyncio.get_running_loop().call_later(
            self._PASTE_GRACE, self._finish_burst
        )

    def _finish_burst(self) -> None:
        self._burst_timer = None

    def _submit(self) -> None:
        self._pending_enter = None
        buffer = self.session.default_buffer
        history = buffer.history
        # A flattened slash-leading document must not become an executable
        # command on a later history recall, when paste provenance is absent.
        if self.last_input_was_multiline_paste and buffer.text.lstrip().startswith("/"):
            buffer.history = DummyHistory()
        try:
            buffer.validate_and_handle()
        finally:
            buffer.history = history

    async def read(self) -> str:
        self._reading = True
        self.last_input_was_multiline_paste = False
        try:
            with patch_stdout():
                return await self.session.prompt_async("❯ ")
        finally:
            self._reading = False
            if self._input_processor is not None:
                self._input_processor.before_key_press -= self._continue_input
                self._input_processor = None
            for timer in (self._pending_enter, self._burst_timer):
                if timer is not None:
                    timer.cancel()
            self._pending_enter = self._burst_timer = None

    async def approve(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        details = Text(f"{tool.description}\n\nArguments:\n")
        details.append(json.dumps(arguments, indent=2, sort_keys=True, ensure_ascii=False))
        prompt = (
            "Archive invalid artifact and prepare replacement? [y/N] "
            if tool.name == "recreate_invalid_artifact"
            else f"Approve {tool.name}? [y/N] "
        )
        # Rich emits ANSI styles; preserve them through prompt-toolkit's proxy.
        with patch_stdout(raw=True):
            Console().print(
                Panel(details, title=Text(f"Approval · {tool.name}"), border_style="yellow")
            )
            answer = await self.session.prompt_async(prompt)
        return answer.strip().lower() in {"y", "yes"}

    async def prompt_secret(self, variable: str) -> str:
        with patch_stdout():
            secret_session: Any = PromptSession(history=DummyHistory())
            return await secret_session.prompt_async(
                f"Enter {variable} (kept only for this session): ", is_password=True
            )

    async def pick_model(
        self,
        options: Sequence[ModelOption],
        current: str | None = None,
    ) -> str | None:
        values = [(option.key, self._model_option_label(option)) for option in options]
        radio_list = RadioList(values=values, default=current)

        def select_model() -> None:
            radio_list._handle_enter()
            get_app().exit(result=radio_list.current_value)

        def cancel() -> None:
            get_app().exit(result=None)

        dialog = Dialog(
            title="Models",
            body=HSplit(
                [
                    Label(
                        text="Select a model (↑/↓ to move, Enter to select, Esc to cancel)",
                        dont_extend_height=True,
                    ),
                    radio_list,
                ],
                padding=1,
            ),
            buttons=[
                Button(text="Select", handler=select_model),
                Button(text="Cancel", handler=cancel),
            ],
            with_background=True,
        )
        bindings = KeyBindings()
        bindings.add("tab")(focus_next)
        bindings.add("s-tab")(focus_previous)

        @bindings.add("escape", eager=True, is_global=True)
        def cancel_from_dialog(_event: Any) -> None:
            cancel()

        @bindings.add(
            "enter",
            eager=True,
            is_global=True,
            filter=Condition(lambda: get_app().layout.current_control is radio_list.control),
        )
        def select_model_from_list(_event: Any) -> None:
            select_model()

        app: Application[str | None] = Application(
            layout=Layout(dialog),
            key_bindings=merge_key_bindings([load_key_bindings(), bindings]),
            mouse_support=True,
            full_screen=True,
        )
        return await app.run_async()

    @staticmethod
    def _model_option_label(option: ModelOption) -> str:
        if option.key == MODEL_PICKER_EXIT:
            return option.name
        state = "[ready]" if option.available else f"[unavailable: {option.reason}]"
        return f"{state} {option.key} — {option.name}"
