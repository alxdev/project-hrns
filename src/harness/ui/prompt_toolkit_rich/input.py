from __future__ import annotations

import json
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
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.widgets import Button, Dialog, Label, RadioList

from harness.api.types import ToolDefinition


class PromptToolkitInput:
    """Input adapter implemented with prompt-toolkit."""

    def __init__(self, history_file: Path) -> None:
        self.session: Any = PromptSession(
            history=FileHistory(str(history_file)),
            enable_history_search=True,
        )

    async def read(self) -> str:
        with patch_stdout():
            return await self.session.prompt_async("❯ ")

    async def approve(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        prompt = f"Approve {tool.name} {json.dumps(arguments, sort_keys=True)}? [y/N] "
        with patch_stdout():
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
        values: list[tuple[str, str]],
        current: str | None = None,
    ) -> str | None:
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
