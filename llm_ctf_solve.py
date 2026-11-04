from datetime import datetime
import socket
import subprocess
import time
import json
import os
import argparse
import shutil
import getpass
import signal
import sys
import yaml
from pathlib import Path

from nyuctf.dataset import CTFDataset
from nyuctf.challenge import CTFChallenge

from llm_ctf.ctflogging import status
from llm_ctf.backends import Backend, OpenAIBackend, AnthropicBackend, VLLMBackend
from llm_ctf.formatters import Formatter
from llm_ctf.prompts.prompts import PromptManager
from llm_ctf.environment import CTFEnvironment
from llm_ctf.conversation import CTFConversation

from nyuctf.dataset import CTFDataset
from nyuctf.challenge import CTFChallenge

def main():
    parser = argparse.ArgumentParser(
        description="Use an LLM to solve a CTF challenge",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    model_list = []
    for b in Backend.registry.values():
        model_list += b.get_models()
    model_list = list(set(model_list))

    script_dir = Path(__file__).parent.resolve()

    parser.add_argument("--challenge", required=True, help="Name of the challenge")
    parser.add_argument("--dataset", help="Dataset JSON path. Only provide if not using the NYUCTF dataset at default path")
    parser.add_argument("-s", "--split", default="development", choices=["test", "development"], help="Dataset split to select. Only used when --dataset not provided.")
    parser.add_argument("-c", "--config", help="Config file to run the experiment")

    parser.add_argument("-q", "--quiet", action="store_true", help="don't print messages to the console")
    parser.add_argument("-d", "--debug", action="store_true", help="print debug messages")
    parser.add_argument("-M", "--model", help="the model to use (default is backend-specific)", choices=model_list)
    parser.add_argument("-C", "--container-image", default="ctfenv", help="the Docker image to use for the CTF environment")
    parser.add_argument("-N", "--network", default="ctfnet", help="the Docker network to use for the CTF environment")
    parser.add_argument("--api-key", default=None, help="API key to use when calling the model")
    parser.add_argument("--api-endpoint", default=None, help="API endpoint URL to use when calling the model")
    parser.add_argument("--backend", default="openai", choices=Backend.registry.keys(), help="model backend to use")
    parser.add_argument("--formatter", default="xml", choices=Formatter.registry.keys(), help="prompt formatter to use")
    parser.add_argument("--prompt-set", default="default", help="set of prompts to use")
    # TODO add back hints functionality
    parser.add_argument("--hints", default=[], nargs="+", help="list of hints to provide")
    parser.add_argument("--disable-markdown", default=False, action="store_true", help="don't render Markdown formatting in messages")
    parser.add_argument("-m", "--max-rounds", type=int, default=10, help="maximum number of rounds to run")
    parser.add_argument("--max-cost", type=float, default=10, help="maximum cost of the conversation to run")
    parser.add_argument("--memory", choices=["off", "llm", "truncate", "mask"], default=None,
                        help="summarize each past action instead of resending the full conversation "
                             "(llm: the model writes the summary; truncate: head and tail of each output; "
                             "mask: outputs replaced by a placeholder). Overrides the config's memory "
                             "section; other memory options come from the config. vllm backend only.")
    parser.add_argument("--repeat-detection", choices=["off", "note", "skip"], default=None,
                        help="when a tool call repeats an earlier call with the same arguments: "
                             "note: run it and add a note to the result; skip: return the earlier "
                             "result without running it. Overrides the config's "
                             "parameter.repeat_detection (default off).")
    parser.add_argument("--lenient-tool-calls", choices=["on", "off"], default=None,
                        help="accept an <invoke> block without the <function_calls> wrapper and "
                             "schema-style parameters as a tool call (issue I11). Overrides "
                             "parameter.lenient_tool_calls (default off). vllm backend only.")
    parser.add_argument("--stop-after-first-call", choices=["on", "off"], default=None,
                        help="stop generation after the first </invoke>, so each reply makes one "
                             "tool call. Overrides parameter.stop_after_first_call (default off).")
    parser.add_argument("--max-output-chars", type=int, default=None,
                        help="cap each tool output at this many characters, keeping the head and "
                             "the tail (issue I12). Overrides parameter.max_output_chars (default 0, no cap).")
    parser.add_argument("--no-tool-nudge", choices=["on", "off"], default=None,
                        help="after a reply with no tool call, send a message that restates the "
                             "tool-call format with an example instead of 'Please proceed...'. "
                             "Overrides the config's parameter.no_tool_nudge (default off).")

    # Log directory options
    parser.add_argument("--skip-exist", action="store_true", help="Skip existing logs and experiments")
    parser.add_argument("-L", "--logdir", default=str(script_dir / "logs" / getpass.getuser()), help="log directory to write the log")
    parser.add_argument("-n", "--name", help="Experiment name (creates subdir in logdir)")
    parser.add_argument("-i", "--index", help="Round index of the experiment (creates subdir in logdir)")

    args = parser.parse_args()
    config = None
    if args.config:
        try:
            with open(args.config, "r") as c:
                config = yaml.safe_load(c)
        except FileNotFoundError:
            pass

    config_parameter = {}
    if config:
        config_parameter = config.get("parameter", {})
        config_experiment = config.get("experiment", {})
        config_demostration = config.get("demostration", {})

        args.max_rounds = config_parameter.get("max_rounds", args.max_rounds)
        args.backend = config_parameter.get("backend", args.backend)
        args.model = config_parameter.get("model", args.model)
        args.max_cost = config_parameter.get("max_cost", args.max_cost)
        args.name = config_experiment.get("name", args.name)
        args.debug = config_experiment.get("debug", args.debug)
        args.skip_exist = config_experiment.get("skip_exist", args.skip_exist)
        args.hints = config_demostration.get("hints", [])

    # Action memory settings: the config's `memory` section, overridden by --memory.
    args.memory_config = dict((config or {}).get("memory") or {})
    if args.memory is not None:
        args.memory_config["enabled"] = args.memory != "off"
        if args.memory != "off":
            args.memory_config["summarizer"] = args.memory
    if args.memory_config.get("enabled") and args.backend != "vllm":
        parser.error("memory is currently implemented only for the vllm backend")

    # Harness options (llm_ctf/conversation.py): the config's `parameter` section, overridden by the flags.
    if args.repeat_detection is None:
        args.repeat_detection = str(config_parameter.get("repeat_detection", "off")).lower()
    if args.repeat_detection not in ("off", "note", "skip"):
        parser.error(f"parameter.repeat_detection must be off, note or skip, not {args.repeat_detection!r}")
    for name in ("lenient_tool_calls", "stop_after_first_call"):
        value = getattr(args, name)
        setattr(args, name, bool(config_parameter.get(name, False)) if value is None else value == "on")
    if args.max_output_chars is None:
        args.max_output_chars = int(config_parameter.get("max_output_chars", 0) or 0)
    if args.max_output_chars < 0:
        parser.error("max_output_chars must be 0 (no cap) or a positive number")
    if args.no_tool_nudge is None:
        args.no_tool_nudge = bool(config_parameter.get("no_tool_nudge", False))
    else:
        args.no_tool_nudge = args.no_tool_nudge == "on"

    status.set(quiet=args.quiet, debug=args.debug, disable_markdown=args.disable_markdown)

    if args.dataset is not None:
        dataset = CTFDataset(dataset_json=args.dataset)
    else:
        dataset = CTFDataset(split=args.split)
    challenge = CTFChallenge(dataset.get(args.challenge), dataset.basedir)
    # The memory's facts extractor matches flags against the challenge's format.
    args.memory_config.setdefault("flag_format", getattr(challenge, "flag_format", None))

    logdir = Path(args.logdir).expanduser().resolve()
    logsubdir = []
    if args.name:
        logsubdir.append(args.name)
    if args.index:
        logsubdir.append(f"round{args.index}")
    if len(logsubdir) > 0:
        logdir = logdir / ("_".join(logsubdir))
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"{challenge.canonical_name}.json"
    
    if logfile.exists() and args.skip_exist:
        status.print(f"[red bold]Challenge log {logfile} exists; skipping[/red bold]", markup=True)
        exit()
        
    environment = CTFEnvironment(challenge, args.container_image, args.network)
    prompt_manager = PromptManager(prompt_set=args.prompt_set, config=config)

    if args.backend == "openai":
        backend = OpenAIBackend(
                        prompt_manager.system_message(challenge),
                        prompt_manager.hints_message(),
                        environment.available_tools,
                        model=args.model,
                        api_key=args.api_key,
                        args=args
                    )
    elif args.backend ==  "anthropic":
        backend = AnthropicBackend(
                        prompt_manager.system_message(challenge),
                        prompt_manager.hints_message(),
                        environment.available_tools,
                        prompt_manager,
                        model=args.model,
                        api_key=args.api_key,
                        args=args
                    )
    elif args.backend == "vllm":
        backend = VLLMBackend(
                        prompt_manager.system_message(challenge),
                        prompt_manager.hints_message(),
                        environment.available_tools,
                        prompt_manager,
                        model=args.model,
                        api_key=args.api_key,
                        api_endpoint=args.api_endpoint,
                        formatter=args.formatter,
                        args=args
                    )

    # Issue I14: the AWS job loop bounds each challenge with `timeout`, which sends SIGTERM.
    # Python's default handler would exit at once; raising SystemExit instead lets the
    # conversation's __exit__ tear the containers down and write the log.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))

    with CTFConversation(environment, challenge, prompt_manager, backend, logfile, max_rounds=args.max_rounds, max_cost=args.max_cost, args=args,
                         repeat_detection=args.repeat_detection, no_tool_nudge=args.no_tool_nudge,
                         max_output_chars=args.max_output_chars) as convo:
        convo.run()

if __name__ == "__main__":
    main()
