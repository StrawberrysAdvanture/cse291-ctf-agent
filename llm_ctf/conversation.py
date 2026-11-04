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
from .tokens import estimate_tokens, shorten_chars, shorten_tokens, MIN_KEEP_TOKENS

now = lambda: time.time()

class CTFConversation:
    def __init__(self, environment: CTFEnvironment, challenge: CTFChallenge, prompt_manager: PromptManager, backend: Backend, logfile: Path, max_rounds:int=30, max_cost:float=1.0, args=None,
                 repeat_detection: str = "off", no_tool_nudge: bool = False,
                 max_output_chars: int = 0, max_output_tokens: int = 0, max_reply_output_tokens: int = 0):
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
        # Issue I12: cap every string field of a tool result at this many characters
        # (0 = no cap, the baseline). The full result is kept on the ToolResult for
        # memory's save_outputs but is not sent to the model or written to the log.
        self.max_output_chars = int(max_output_chars or 0)
        # Token-aware limits (llm_ctf/tokens.py estimates; 0 = off): per output, and for all
        # outputs of one reply together, since several capped outputs in one reply overran
        # the context in the harness-fixes run.
        self.max_output_tokens = int(max_output_tokens or 0)
        self.max_reply_output_tokens = int(max_reply_output_tokens or 0)
        self.outputs_capped = 0
        self.replies_capped = 0
        # Issue I11: invokes naming a program instead of a tool, run through run_command.
        self.rerouted_calls: List[dict] = []
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
            rerouted_from = None
            if not tool:
                rerouted = self.backend.reroute_unknown_call(tool_call, self.environment.available_tools)
                if rerouted is None:
                    status.debug_message(f"Unknown tool {tool_call.name}")
                    tool_results.append(tool_call.error(f"Unknown tool {tool_call.name}"))
                    continue
                # Upgrade the call in place, so the backend's log and the memory see the
                # call that actually ran; rerouted_calls keeps the name the model wrote.
                rerouted_from = tool_call.name
                tool_call.function.name = rerouted.name
                tool_call.parsed_arguments = rerouted.parsed_arguments
                tool = self.environment.available_tools[tool_call.name]
                self.rerouted_calls.append({"round": self.rounds, "tool_name": rerouted_from,
                                            "command": tool_call.parsed_arguments.get("command")})
                status.debug_message(f"No tool named {rerouted_from}; running it as {tool.name}: "
                                     f"{tool_call.parsed_arguments.get('command')}")

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
            if self.repeats.should_skip(previous):
                last = previous["rounds"][-1]
                ran = previous["last_run"] if previous["last_run"] is not None else last
                result = dict(previous["result"])
                notes = []
                if rerouted_from is not None:
                    notes.append(f"There is no tool named {rerouted_from!r}; the harness treats it as "
                                 f"run_command `{tool_call.parsed_arguments.get('command')}`.")
                if self.repeats.mode == "skip_identical":
                    times = previous["identical_repeats"] + 1
                    notes.append(f"{self._same_call_text(last)}; it has produced identical output "
                                 f"{times} times, so it was not run again. This is the result from round {ran}.")
                else:
                    notes.append(f"{self._same_call_text(last)}; it was not run again. "
                                 f"This is the result from round {ran}.")
                result["note"] = " ".join(notes)
                tool_res = ToolResult(tool_call.name, tool_call.id, result)
                tool_res.clean_result = dict(previous["result"])
                tool_res.full_result = previous["full"]
                self.repeats.add_repeat(round_no=self.rounds, earlier_rounds=previous["rounds"], tool=tool.name,
                                        arguments=canonical_arguments(tool, tool_call.parsed_arguments),
                                        action="skip", identical=None)
                self.repeats.remember(key, self.rounds, previous["result"], full=previous["full"], ran=False)
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

            if self.max_output_chars or self.max_output_tokens:
                tool_res = self.cap_output(tool_res)

            # Notes are added to a copy, so the stored result (served again on a skip and
            # kept for the log) stays free of them. "Identical" is judged on the full
            # output, before any cap shortened it.
            clean_result = tool_res.result
            tool_res.clean_result = clean_result
            full_result = getattr(tool_res, "full_result", None) or clean_result
            notes = []
            if rerouted_from is not None:
                notes.append(f"There is no tool named {rerouted_from!r}; the harness ran "
                             f"`{tool_call.parsed_arguments.get('command')}` with run_command instead.")
            identical = False
            if previous is not None:
                identical = full_result == previous["full"]
                last = previous["rounds"][-1]
                noting = self.repeats.mode in ("note", "skip_identical")
                self.repeats.add_repeat(round_no=self.rounds, earlier_rounds=previous["rounds"], tool=tool.name,
                                        arguments=canonical_arguments(tool, tool_call.parsed_arguments),
                                        action="note" if noting else "none",
                                        identical=identical)
                if noting:
                    notes.append(f"{self._same_call_text(last)}; the output "
                                 f"{'is identical' if identical else 'differs'}.")
                    status.print(f"\n[Repeat] {tool.name}: {self._same_call_text(last).lower()}; "
                                 f"output {'identical' if identical else 'differs'}", style="magenta bold")
            if notes:
                tool_res.result = dict(clean_result)
                tool_res.result["note"] = " ".join(notes)
            self.repeats.remember(key, self.rounds, clean_result, full=full_result, identical=identical)
            tool_results.append(tool_res)
        if self.max_reply_output_tokens:
            self.cap_reply(tool_results)
        return tool_results

    @staticmethod
    def _original(tool_res: ToolResult) -> dict:
        """The result before any cap and without notes (what the memory saves and caps cut from)."""
        return (getattr(tool_res, "full_result", None) or getattr(tool_res, "clean_result", None)
                or tool_res.result)

    @classmethod
    def _shortened(cls, tool_res: ToolResult, capped: dict):
        """Install capped as the result sent to the model, keeping the original on full_result."""
        if getattr(tool_res, "full_result", None) is None:
            tool_res.full_result = cls._original(tool_res)
        tool_res.result = capped

    def cap_output(self, tool_res: ToolResult) -> ToolResult:
        """Shorten string fields over max_output_chars characters or max_output_tokens
        estimated tokens, keeping the head and the tail (issue I12). Both limits are applied
        to the original value, so the omission note states the true counts."""
        capped = {}
        changed = False
        for key, value in tool_res.result.items():
            new = value
            if isinstance(value, str):
                over_chars = self.max_output_chars and len(value) > self.max_output_chars
                over_tokens = self.max_output_tokens and estimate_tokens(value) > self.max_output_tokens
                if over_tokens:
                    new = shorten_tokens(value, self.max_output_tokens, "output limit", max_chars=self.max_output_chars)
                elif over_chars:
                    new = shorten_chars(value, self.max_output_chars, "output limit")
                    if self.max_output_tokens and estimate_tokens(new) > self.max_output_tokens:
                        new = shorten_tokens(value, self.max_output_tokens, "output limit", max_chars=self.max_output_chars)
            capped[key] = new
            changed = changed or new is not value
        if changed:
            self.outputs_capped += 1
            self._shortened(tool_res, capped)
            status.debug_message(f"Output of {tool_res.name} capped (limits: {self.max_output_chars} characters, "
                                 f"{self.max_output_tokens} tokens)")
        return tool_res

    def cap_reply(self, tool_results: List[ToolResult]):
        """Keep the outputs of one reply within max_reply_output_tokens together. Water-filling:
        the largest level L with sum(min(size, L)) <= budget is found and every string field
        above L is shortened to L, cut from its original text; fields never go below
        MIN_KEEP_TOKENS, so a reply with very many outputs can still exceed the budget."""
        budget = self.max_reply_output_tokens
        fields = []   # [result index, key, estimated tokens now]
        for i, tr in enumerate(tool_results):
            for key, value in tr.result.items():
                if isinstance(value, str) and key != "note":
                    fields.append([i, key, estimate_tokens(value)])
        if not fields or sum(f[2] for f in fields) <= budget:
            return
        self.replies_capped += 1
        lo, hi = MIN_KEEP_TOKENS, max(f[2] for f in fields)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if sum(min(f[2], mid) for f in fields) <= budget:
                lo = mid
            else:
                hi = mid - 1
        level = lo
        for i, key, tokens in fields:
            if tokens > level:
                tr = tool_results[i]
                original = self._original(tr).get(key)
                if not isinstance(original, str):
                    original = tr.result[key]
                capped = dict(tr.result)
                capped[key] = shorten_tokens(original, level, "per-reply output limit")
                self._shortened(tr, capped)
        status.debug_message(f"Outputs of this reply shortened to about {level} tokens each "
                             f"(reply limit {budget})")

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
        if exc_type is SystemExit and self.finish_reason == "unknown":
            # Issue I14: killed by the job's per-challenge time limit (SIGTERM -> SystemExit).
            self.finish_reason = "killed"
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
                "output_cap": {"max_output_chars": self.max_output_chars,
                               "max_output_tokens": self.max_output_tokens,
                               "max_reply_output_tokens": self.max_reply_output_tokens,
                               "outputs_capped": self.outputs_capped, "replies_capped": self.replies_capped},
                "rerouted_calls": self.rerouted_calls,
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
