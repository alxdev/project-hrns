from __future__ import annotations

from harness.agent.loop import Agent
from harness.commands.registry import CommandContext, CommandRegistry
from harness.runtime.operations import OperationController
from harness.ui.protocols import InputAdapter, OutputRenderer


class InteractiveRunner:
    """Connect an input adapter and output renderer to the agent loop."""

    def __init__(
        self,
        input_adapter: InputAdapter,
        output: OutputRenderer,
        operations: OperationController,
    ) -> None:
        self.input = input_adapter
        self.output = output
        self.operations = operations

    async def run(
        self, agent: Agent, commands: CommandRegistry, command_context: CommandContext
    ) -> None:
        while True:
            try:
                prompt = await self.input.read()
            except KeyboardInterrupt:
                self.output.write("input cancelled")
                continue
            except EOFError:
                self.output.write("")
                return

            if not prompt.strip():
                continue

            # Flattened multiline pastes are still documents, even if they
            # begin with a slash. Ordinary single-line commands remain usable.
            command = (
                commands.dispatch(prompt, command_context)
                if "\n" not in prompt
                and "\r" not in prompt
                and not getattr(self.input, "last_input_was_multiline_paste", False)
                else None
            )
            if command is not None:
                if command.text:
                    self.output.write(command.text)
                if command.action is not None:
                    result = await command.action()
                    if result:
                        self.output.write(result)
                if command.should_exit:
                    return
                continue

            try:
                async with self.output.thinking():
                    operation_result = await self.operations.run(agent.send(prompt))
            except Exception as exc:
                self.output.error(str(exc))
                continue
            if operation_result.cancelled:
                self.output.write("operation cancelled")
