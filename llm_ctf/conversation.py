import time
import subprocess
import os
import json
import openai
import anthropic
from typing import Tuple, Optional, List

from pathlib import Path
from nyuctf.challenge import CTFChallenge

from .ctflogging import status
from .backends import Backend
from .prompts.prompts import PromptManager
from .tools import ToolCall, ToolResult, Tool, TOOLSETS
from .environment import CTFEnvironment
from .repeats import RepeatDetector, call_key, canonical_arguments

now = lambda: time.time()

class CTFConversation:
    def __init__(self, environment: CTFEnvironment, challenge: CTFChallenge, prompt_manager: PromptManager, backend: Backend, logfile: Path, max_rounds:int=30, max_cost:float=1.0, args=None,
                 repeat_detection: str = "off", no_tool_nudge: bool = False):
        self.challenge = challenge
        self.environment = environment
        self.prompt_manager = prompt_manager
        self.backend = backend
        self.logfile = logfile

        self.available_functions : dict[str,Tool] = {}

        self.max_rounds = max_rounds
        self.max_cost = max_cost
        # self.config = config
        self.args = args

        # Repeated tool calls (llm_ctf/repeats.py): always counted, acted on when the mode is not "off".
        self.repeats = RepeatDetector(repeat_detection)
        # Replies without a tool call: with the nudge on, the next message restates the
        # tool-call format with an example instead of the plain keep-going text.
        self.no_tool_nudge = no_tool_nudge
        self.no_tool_rounds: List[int] = []
        self.nudges_sent = 0
        self._no_tool_message = None

        self.rounds = 0
        self.cost = 0
        self.finish_reason = "unknown"
        self.model_time = 0
        self.tool_time = 0

    def __enter__(self):
        self.backend.setup()
        try:
            self.challenge.start_challenge_container()
        except subprocess.CalledProcessError as e:
            # nyuctf runs `docker compose up` with its output captured; show why it failed.
            status.print(f"[red bold]Challenge services failed to start (exit code {e.returncode})[/red bold]", markup=True)
            for stream in (e.stdout, e.stderr):
                if stream:
                    status.print(stream.decode(errors="replace") if isinstance(stream, bytes) else str(stream))
            raise
        self.environment.setup()

        self.start_time = now()
        return self

    def run(self):
        next_msg = self.prompt_manager.initial_message(self.challenge)
        while not self.environment.solved and not self.environment.giveup \
                and self.rounds <= self.max_rounds and self.cost <= self.max_cost:
            try:
                tools_run = self.run_conversation_step(next_msg)
                if tools_run == 0:
                    next_msg = self.no_tool_message()
                else:
                    next_msg = None
            except KeyboardInterrupt:
                status.print("[red bold]Interrupted by user[/red bold]", markup=True)
                self.finish_reason = "user_cancel"
                break
            # TODO Normalize the ratelimiterrors
            except (openai.RateLimitError, anthropic.RateLimitError):
                status.print("[red bold]Rate limit reached![/red bold]", markup=True)
                self.finish_reason = "rate_limit"
                break
            except openai.BadRequestError as e:
                msg = str(e)
                # OpenAI reports an error code; vLLM only says "maximum context length" in the message.
                if ("'code': 'context_length_exceeded'" in msg or "'code': 'string_above_max_length'" in msg
                        or "maximum context length" in msg):
                    status.print("[red bold]Context length exceeded![/red bold]", markup=True)
                    self.finish_reason = "context_length"
                    break
                else:
                    # Some other error, re-raise
                    raise

        # Look for a finish reason
        if self.environment.solved:
            status.print("[red bold]Challenge solved by our robot overlords![/red bold]", markup=True)
            self.finish_reason = "solved"
        elif self.environment.giveup:
            status.print("[red bold]The LLM decided to give up! NGMI.[/red bold]", markup=True)
            self.finish_reason = "give_up"
        elif self.cost > self.max_cost:
            status.print(f"[red bold]Challenge is unsolved after {self.max_cost} dollars of cost; exiting[/red bold]", markup=True)
            self.finish_reason = "max_cost"
        elif self.rounds > self.max_rounds:
            status.print(f"[red bold]Challenge is unsolved after {self.max_rounds} rounds; exiting[/red bold]", markup=True)
            self.finish_reason = "max_rounds"

    def no_tool_message(self) -> str:
        """The message sent after a reply that called no tool.

        With the nudge off this is the fixed keep-going text (kept as a literal so the
        baseline is unchanged). With the nudge on it is the no_tool_call prompt with a
        formatted example call, rendered once and reused.
        """
        if not self.no_tool_nudge:
            return "Please proceed to the next step using your best judgment."
        if self._no_tool_message is None:
            tools = self.environment.available_tools
            if "run_command" in tools:
                example_call = tools["run_command"].make_call(id="1", command="ls ~/ctf_files")
            else:
                example_call = ToolCall.create_parsed("$TOOL_NAME", "$CALL_ID", {"$PARAMETER_NAME": "$PARAMETER_VALUE"})
            example, formatter = self.backend.example_tool_call(example_call)
            self._no_tool_message = self.prompt_manager.no_tool_call(
                example=example, formatter=formatter, tool_names=list(tools))
        return self._no_tool_message

    def _same_call_text(self, last_round: int) -> str:
        if last_round == self.rounds:
            return "Same call as earlier in this round"
        return f"Same call as in round {last_round}"

    def run_tools(self, tool_calls: List[ToolCall]) -> Tuple[Optional[str],bool]:
        tool_results = []
        for tool_call in tool_calls:
            # Tool lookup
            tool = self.environment.available_tools.get(tool_call.name)
            if not tool:
                status.debug_message(f"Unknown tool {tool_call.name}")
                tool_results.append(tool_call.error(f"Unknown tool {tool_call.name}"))
                continue

            # Parse arguments
            parsed, tool_call = self.backend.parse_tool_arguments(tool, tool_call)
            if not parsed:
                tool_results.append(tool_call)
                continue

            # Repeat detection: compare with the most recent earlier call of the same tool
            # with the same arguments. The stored result is a copy, so notes added to the
            # result sent to the model never change an earlier log entry.
            key = call_key(tool, tool_call)
            previous = self.repeats.previous(key)
            if previous is not None and self.repeats.mode == "skip":
                last = previous["rounds"][-1]
                result = dict(previous["result"])
                result["note"] = (f"{self._same_call_text(last)}; it was not run again. "
                                  f"This is the result from round {last}.")
                tool_res = ToolResult(tool_call.name, tool_call.id, result)
                self.repeats.add_repeat(round_no=self.rounds, earlier_rounds=previous["rounds"], tool=tool.name,
                                        arguments=canonical_arguments(tool, tool_call.parsed_arguments),
                                        action="skip", identical=None)
                self.repeats.remember(key, self.rounds, previous["result"])
                status.print(f"\n[Repeat] {tool.name}: {self._same_call_text(last).lower()}; not run again", style="magenta bold")
                tool_results.append(tool_res)
                continue

            try:
                tool_res = tool.run(tool_call)
            except TypeError as e:
                status.debug_message(f"Error encoding results from {tool.name}: {e}")
                tool_res = tool_call.error(f"{type(e).__name__} running {tool.name}: {e}")
            except Exception as e:
                status.debug_message(f"Error running {tool.name}: {e}")
                tool_res = tool_call.error(f"{type(e).__name__} running {tool.name}: {e}")

            clean_result = tool_res.result
            if previous is not None:
                identical = clean_result == previous["result"]
                last = previous["rounds"][-1]
                self.repeats.add_repeat(round_no=self.rounds, earlier_rounds=previous["rounds"], tool=tool.name,
                                        arguments=canonical_arguments(tool, tool_call.parsed_arguments),
                                        action="note" if self.repeats.mode == "note" else "none",
                                        identical=identical)
                if self.repeats.mode == "note":
                    # A new dict, so the note is the last field of the result sent to the model
                    # and the stored copy below stays free of notes.
                    tool_res.result = dict(clean_result)
                    tool_res.result["note"] = (f"{self._same_call_text(last)}; the output "
                                               f"{'is identical' if identical else 'differs'}.")
                    status.print(f"\n[Repeat] {tool.name}: {self._same_call_text(last).lower()}; "
                                 f"output {'identical' if identical else 'differs'}", style="magenta bold")
            self.repeats.remember(key, self.rounds, clean_result)
            tool_results.append(tool_res)
        return tool_results

    def run_conversation_step(self, message: Optional[str]=None):
        # Any message after the first one is the reply to a round without a tool call.
        if message is not None and self.rounds > 0:
            self.nudges_sent += 1
        if message:
            status.user_message(message)
        status.assistant_message("🤔 ...thinking... 🤔")

        # Prompt the model to produce a response and tool_calls
        st = now()
        content, tool_calls, cost = self.backend.send(message)
        self.model_time += now() - st
        self.rounds += 1
        self.cost += cost

        assistant_response = content if content is not None else ""
        for tc in tool_calls:
            assistant_response += f"\n\n```\n{tc.name}: {tc.arguments}\n```"
        if assistant_response:
            status.assistant_message(assistant_response)
        else:
            status.assistant_message("[ no response ]")

        # Run tool calls
        if tool_calls:
            st = now()
            tool_results = self.run_tools(tool_calls)
            self.tool_time += now() - st

            env_response = "## Tool Responses:"
            for tr in tool_results:
                env_response += f"\n\n```\n{tr.name}: {tr.result}\n```\n"
            status.user_message(env_response)
            self.backend.append(tool_results)
            return len(tool_calls)
        else:
            self.no_tool_rounds.append(self.rounds)
            return 0 # No tools run

    def __exit__(self, exc_type, exc_value, traceback):
        self.end_time = now()
        self.environment.teardown(exc_type, exc_value, traceback)
        self.challenge.stop_challenge_container()

        self.logfile.write_text(json.dumps(
            {
                "args": vars(self.args),
                "messages": self.backend.get_timestamped_messages(),
                "challenge": self.challenge.challenge_info,
                "solved": self.environment.solved,
                "rounds": self.rounds,
                "cost": self.cost,
                "debug_log": status.debug_log,
                # "challenge_server_output": self.chal.challenge_server_output,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "runtime": {
                    "total": self.end_time - self.start_time,
                    "tools": self.tool_time,
                    "model": self.model_time
                },
                # "exception_info": exception_info,
                "finish_reason": self.finish_reason,
                "repeat_detection": self.repeats.to_log(),
                "no_tool_call": {
                    "nudge": self.no_tool_nudge,
                    "count": len(self.no_tool_rounds),
                    "rounds": list(self.no_tool_rounds),
                    "nudges_sent": self.nudges_sent,
                },
                **self.backend.extra_log(),
            },
            indent=4
        ))
        status.print(f"Conversation saved to {self.logfile}")
