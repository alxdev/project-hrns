from __future__ import annotations

from harness.agent.loop import Agent
from harness.commands.registry import CommandContext, CommandRegistry
from harness.ui.protocols import InputAdapter, OutputRenderer


class InteractiveRunner:
    """Connect an input adapter and output renderer to the agent loop."""

    def __init__(self, input_adapter: InputAdapter, output: OutputRenderer) -> None:
        self.input = input_adapter
        self.output = output

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

            command = commands.dispatch(prompt, command_context)
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
                    await agent.send(prompt)
            except KeyboardInterrupt:
                self.output.write("operation interrupted")
            except Exception as exc:
                self.output.error(str(exc))
