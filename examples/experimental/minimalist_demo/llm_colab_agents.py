from typing import AsyncIterator
from aact import Message, NodeFactory, Node
from aact.messages import Text, Tick, DataModel, DataModelFactory, Zero
from sotopia.agents.llm_agent import ainput
from sotopia.experimental.agents import BaseAgent
from aact.nodes import PrintNode

from sotopia.generation_utils import agenerate
from sotopia.generation_utils.generate import StrOutputParser
from sotopia.messages.message_classes import ActionType

from pydantic import Field
import logging
from rich.logging import RichHandler
import json

import sys

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
logging.basicConfig(
    level=logging.WARNING,
    format=FORMAT,
    datefmt="[%X]",
    handlers=[RichHandler()],
)

from typing import Optional


@DataModelFactory.register("agent_action")
class AgentAction(DataModel):
    agent_name: str = Field(description="the name of the agent")
    action_type: ActionType = Field(
        description="whether to speak at this turn or choose to not do anything"
    )
    argument: str = Field(
        description="the utterance if choose to speak, the expression or gesture if choose non-verbal communication, or the physical action if choose action"
    )
    path: Optional[str] = Field(description="path of file")

    def to_natural_language(self) -> str:
        match self.action_type:
            case "none":
                return "did nothing"
            case "speak":
                return f'said: "{self.argument}"'
            case "thought":
                return f'thought: "{self.argument}"'
            case "browse":
                return f'browsed: "{self.argument}"'
            case "run":
                return f'ran: "{self.argument}"'
            case "read":
                return f'read: "{self.argument}"'
            case "write":
                return f'wrote: "{self.argument}"'
            case "non-verbal communication":
                return f"[{self.action_type}] {self.argument}"
            case "action":
                return f"[{self.action_type}] {self.argument}"
            case "leave":
                return "left the conversation"


def _format_message_history(message_history: list[tuple[str, str]]) -> str:
    ## TODO: akhatua Fix the mapping of action to be gramatically correct
    return "\n".join(
        (f"{speaker} {action} {message}")
        for speaker, action, message in message_history
    )


@NodeFactory.register("llm_agent")
class LLMAgent(BaseAgent[AgentAction | Tick | Text, AgentAction]):
    def __init__(
        self,
        input_text_channels: list[str],
        input_tick_channel: str,
        input_env_channels: list[str],
        output_channel: str,
        query_interval: int,
        agent_name: str,
        goal: str,
        model_name: str,
        redis_url: str,
    ):
        super().__init__(
            [
                (input_text_channel, AgentAction)
                for input_text_channel in input_text_channels
            ]
            + [
                (input_tick_channel, Tick),
            ]
            + [(input_env_channel, Text) for input_env_channel in input_env_channels],
            [(output_channel, AgentAction)],
            redis_url,
        )
        self.output_channel = output_channel
        self.query_interval = query_interval
        self.count_ticks = 0
        self.message_history: list[tuple[str, str]] = []
        self.name = agent_name
        self.model_name = model_name
        self.goal = goal

    async def send(self, message: AgentAction) -> None:
        if message.action_type == "speak":
            await self.r.publish(
                self.output_channel,
                Message[AgentAction](data=message).model_dump_json(),
            )

        elif message.action_type in ("browse", "write", "read", "run"):
            await self.r.publish(
                "Agent:Runtime",
                Message[AgentAction](data=message).model_dump_json(),
            )

    async def aact(self, message: AgentAction | Tick | Text) -> AgentAction:
        print("entered aact: ", message)

        match message:
            case Text(text=text):
                self.message_history.append((self.name, "observation data", text))
                return AgentAction(
                    agent_name=self.name, action_type="none", argument="", path=""
                )
            case Tick():
                self.count_ticks += 1
                if self.count_ticks % self.query_interval == 0:
                    # print("start generate")
                    # print("message_history: ", _format_message_history(self.message_history))
                    # print("agent_name: ", self.name)
                    # print("agent_goal: ", self.goal)
                    agent_action = await agenerate(
                        model_name=self.model_name,
                        template="""Imagine that you are a work colleague of the other persons.
                        Here is the conversation between you and them.\n
                        You are {agent_name} in the conversation.\n
                        {message_history}\nand you plan to {goal}.
                        ## Action
                        What is your next thought or action? Your response must be in JSON format.

                        It must be an object, and it must contain two fields:
                        * `action`, which is one of the actions below
                        * `args`, which is a map of key-value pairs, specifying the arguments for that action

                        You can choose one of these 3 actions:
                        [1] `speak` - you can talk to the other agnets to share information or ask them something. Arguments:
                            * `content` - the message to send to the other agents (should be short)
                        [2] `thought` - make a plan, set a goal, record your thoughts. Arguments:
                            * `content` - the message you send yourself to organize your thoughts (should be short). You cannot think more than 2 turns.
                        [3] `none` - you can choose not to take an action if you are waiting for some data
                        [4] `browse` - opens a web page. Arguments:
                            * `url` - the URL to open, when you browse the web you must wait until you get some infomation back. When you get the information back you must summarize the article and explain the article to the other agents.
                        [5] `read` - reads the content of a file. Arguments:
                            * `path` - the path of the file to read
                        [6] `write` - writes the content to a file. Arguments:
                            * `path` - the path of the file to write
                            * `content` - the content to write to the file
                        [7] `run` - runs a command on the command line in a Linux shell. Arguments:
                            * `command` - the command to run

                        [8] `finish` - if ALL of your goals have been completed or abandoned, and you're absolutely certain that you've completed your task and have tested your work, use the finish action to stop working.

                        You can use the `speak` action to engage with the other agents. Again, you must reply with JSON, and only with JSON.""",
                        input_values={
                            "message_history": _format_message_history(
                                self.message_history
                            ),
                            "goal": self.goal,
                            "agent_name": self.name,
                        },
                        temperature=0.7,
                        output_parser=StrOutputParser(),
                    )
                    agent_action = (
                        agent_action.replace("```", "")
                        .replace("json", "")
                        .strip('"')
                        .strip()
                    )

                    try:
                        data = json.loads(agent_action)
                        action = data["action"]
                        print(data)

                        # Handle different cases based on the action
                        if action == "thought":
                            content = data["args"]["content"]
                            # print(f"Action: {action}")
                            # print(f"Content: {content}")

                            self.message_history.append((self.name, action, content))
                            return AgentAction(
                                agent_name=self.name,
                                action_type="thought",
                                argument=content,
                                path="",
                            )

                        elif action == "speak":
                            content = data["args"]["content"]
                            # print(f"Action: {action}")
                            # print(f"Content: {content}")

                            self.message_history.append((self.name, action, content))
                            return AgentAction(
                                agent_name=self.name,
                                action_type="speak",
                                argument=content,
                                path="",
                            )

                        elif action == "browse":
                            url = data["args"]["url"]

                            self.message_history.append((self.name, action, url))
                            return AgentAction(
                                agent_name=self.name,
                                action_type=action,
                                argument=url,
                                path="",
                            )

                        elif action == "run":
                            command = data["args"]["command"]

                            self.message_history.append((self.name, action, command))
                            return AgentAction(
                                agent_name=self.name,
                                action_type=action,
                                argument=command,
                                path="",
                            )

                        elif action == "write":
                            path = data["args"]["path"]
                            content = data["args"]["content"]

                            self.message_history.append((self.name, action, content))
                            return AgentAction(
                                agent_name=self.name,
                                action_type=action,
                                argument=content,
                                path=path,
                            )

                        elif action == "read":
                            path = data["args"]["path"]

                            self.message_history.append((self.name, action, path))
                            return AgentAction(
                                agent_name=self.name,
                                action_type=action,
                                argument="Nan",
                                path=path,
                            )

                        elif action == "none":
                            # print("Agent did nothing")
                            return AgentAction(
                                agent_name=self.name,
                                action_type="none",
                                argument="",
                                path="",
                            )
                        else:
                            print(f"Unknown action: {action}")
                    except json.JSONDecodeError as e:
                        print(f"Error decoding JSON: {e}")
                else:
                    return AgentAction(
                        agent_name=self.name, action_type="none", argument="", path=""
                    )
            case AgentAction(
                agent_name=agent_name, action_type=action_type, argument=text
            ):
                if action_type == "speak":
                    self.message_history.append((agent_name, action_type, text))
                return AgentAction(
                    agent_name=self.name, action_type="none", argument="", path=""
                )
            case _:
                raise ValueError(f"Unexpected message type: {type(message)}")


@NodeFactory.register("human_agent")
class HumanAgent(BaseAgent[AgentAction | Tick, AgentAction]):
    def __init__(
        self,
        input_text_channels: list[str],
        input_tick_channel: str,
        output_channel: str,
        query_interval: int,
        agent_name: str,
        goal: str,
        model_name: str,
        redis_url: str,
    ):
        super().__init__(
            [
                (input_text_channel, AgentAction)
                for input_text_channel in input_text_channels
            ]
            + [
                (input_tick_channel, Tick),
            ],
            [(output_channel, AgentAction)],
            redis_url,
        )
        self.output_channel = output_channel
        self.query_interval = query_interval
        self.count_ticks = 0
        self.message_history: list[tuple[str, str]] = []
        self.name = agent_name
        self.model_name = model_name
        self.goal = goal

    async def send(self, message: AgentAction) -> None:
        if message.action_type == "speak":
            await self.r.publish(
                self.output_channel,
                Message[AgentAction](data=message).model_dump_json(),
            )

    async def aact(self, message: AgentAction) -> AgentAction:
        match message:
            case Tick():
                self.count_ticks += 1
                if self.count_ticks % self.query_interval == 0:
                    argument = await ainput("Argument: ")

                    return AgentAction(
                        agent_name=self.name, action_type="speak", argument=argument
                    )

            case AgentAction(
                agent_name=agent_name, action_type=action_type, argument=text
            ):
                if action_type == "speak":
                    self.message_history.append((agent_name, text))
                return AgentAction(
                    agent_name=self.name, action_type="none", argument=""
                )
            case _:
                raise ValueError(f"Unexpected message type: {type(message)}")


@NodeFactory.register("scenario_context")
class ScenarioContext(Node[DataModel, Text]):
    def __init__(
        self,
        input_tick_channel: str,
        output_channels: list[str],
        env_scenario: str,
        redis_url: str = "redis://localhost:6379/0",
    ):
        super().__init__(
            input_channel_types=[(input_tick_channel, Tick)],
            output_channel_types=[
                (output_channel, Text) for output_channel in output_channels
            ],
            redis_url=redis_url,
        )
        self.env_scenario = env_scenario
        self.output_channels = output_channels

    async def send_env_scenario(self) -> None:
        # Send env_scenario to all output channels
        for output_channel in self.output_channels:
            await self.r.publish(
                output_channel,
                Message[Text](data=Text(text=self.env_scenario)).model_dump_json(),
            )

    async def event_loop(self) -> None:
        # Directly call send_env_scenario without any interval
        await self.send_env_scenario()

    async def __aenter__(self) -> Self:
        return await super().__aenter__()

    async def event_handler(
        self, _: str, __: Message[Zero]
    ) -> AsyncIterator[tuple[str, Message[Tick]]]:
        raise NotImplementedError("ScenarioContext does not have an event handler.")
        yield "", Message[Text](data=Text(text == self.env_scenario))


@NodeFactory.register("chat_print")
class ChatPrint(PrintNode):
    async def write_to_screen(self) -> None:
        while self.output:
            data_entry = await self.write_queue.get()

            # Parse the JSON data
            data = json.loads(data_entry.model_dump_json())

            # Extract agent_name and argument
            agent_name = data["data"]["agent_name"]
            argument = data["data"]["argument"]

            # Format the output
            output = f"{agent_name} says: {argument}"

            # Write to output
            await self.output.write(output + "\n")
            await self.output.flush()
            self.write_queue.task_done()


# ###################################################################
# import os
# sys.path.append(os.path.abspath("/Users/arpan/Desktop/sotopia/examples/experimental/minimalist_demo/openhands"))
# print(sys.path)


# openhands_path = "/Users/arpan/Desktop/sotopia/examples/experimental/minimalist_demo/openhands"
# print("Contents of openhands directory:", os.listdir(openhands_path))
# from openhands.core.config import (
#     AgentConfig,
#     AppConfig,
#     SandboxConfig,
#     get_llm_config_arg,
#     get_parser,
# )
