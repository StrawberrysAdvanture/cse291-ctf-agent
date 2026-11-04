# Transcript findings: baseline 32k, 3 rounds (run 20261103-233511-6b9b)

Qualitative read of the 47 logs in `logs/NYU_Baseline_0/*.json`, alongside the quantitative `report.md`. Counts are over 47 logs, 181 replies, 261 tool calls unless stated. Produced by a review agent on 2026-11-04; the tabulation scripts were run in a scratch directory and are not kept.

Reading the logs: for an assistant message that registered tool calls, the `content` field holds only the text before the tool-call block (the extracted "thought"); the model's raw reply is under `extra.response.data.choices[0].message.content`. For a reply with no registered call, `content` is the whole reply. `scripts/recover_lost_calls.py` shows what lenient parsing (issue I11) would have accepted from these replies: all 69 unwrapped blocks and all 26 schema-style calls (`lenient-parsing-recovery.md`).

## Summary: dominant failure modes, most frequent first

1. Malformed tool-call syntax is the biggest single loss: 69 of 101 no-tool replies contain a full `<invoke>` block missing the `<function_calls>` wrapper, so nothing runs.
2. No executed action per reply: 101 of 181 replies (55.8%) ran no tool; 19 of 47 challenges ran a tool on zero of their 4 replies.
3. Repeated actions: 114 of 261 calls (43.7%); 104 of the 114 reproduced byte-identical output.
4. Second format error: 26 of 41 tool errors are schema syntax (`<name>command</name>...<value>`) placed inside the invoke instead of `<command>`.
5. No correct flag: 34 `check_flag` calls (the report's "0" is a tool-name bug in `run_report.py`, since fixed), all `correct: False` or argument errors; `give_up` never called.
6. Un-truncated output overflows context: all 3 `context_length` endings came from one oversized tool result (largest a 2.6 MB `cat`).
7. Decompiler under-used: 10 of 17 rev/pwn challenges never called a disassembler; 19 of 24 `disassemble_function` calls errored.
8. Placeholder or guessed flags, and plans to "download the binary" although the files are already in `~/ctf_files`.

Through-line: the model usually picks a reasonable command, the harness drops it on a format technicality, and the model neither sees that nothing ran nor remembers that it already tried the same thing.

## Q1. Flags submitted

`run_report.py` matched the name `checkflag`, but the tool is `check_flag`. There were **34 `check_flag` calls** across 13 challenges; **0 `give_up`**.

| check_flag outcome | count |
|---|---|
| `correct: False` (wrong flag) | 30 |
| `error` (missing `flag` argument) | 4 |
| `correct: True` | 0 |

Every submitted flag was a placeholder or guess, never read from a tool output: `$FLAG`, `flag{hidden_flag}`, `FLAG{example_flag}`, `flag{decoded_message}`, `flag{exploit_input}`.

Flag-like strings (`\w+\{[^}]+\}`) appear in **40 of 181 replies** across 16 challenges, mostly placeholders; 34 of the 40 are in no-tool replies. Candidate flags asserted in prose but never submitted:
- `2014q-pwn-the_road_less_traveled` r3: "the flag for this CTF challenge is `flag{the_road_less_traveled}`." (never calls check_flag)
- `2014f-rev-odd` r1–r4: repeats "flag{[extracted_flag]}"
- `2013q-cry-csawpad` r2: "Let's assume the output is `CTF{padded_message_here}`."

## Q2. Replies without a tool call (I9)

All 101 no-tool replies, classified on the raw model output:

| class | count |
|---|---|
| `<invoke>` present but no `<function_calls>` wrapper, so dropped | 69 |
| prose only (plan or explanation, no call attempted) | 32 |
| markdown shell code block | 0 |
| JSON-style tool call | 0 |
| final answer or flag as the reply | 0 |

Mechanism of the 69: `vllm_backend.py` sets `has_tool_calls` only when the literal `<function_calls>` is in the content. The model instead emits (usually in a ```` ```xml ```` fence) a bare `<invoke>...<command>...</command></invoke>`; the command is often valid, only the wrapper is missing. Since `<function_calls>` is absent, the stop sequence `</function_calls>` never appears either, so generation runs to the 1024-token cap (38 no-tool replies hit the cap exactly).
- `2013q-cry-csawpad` r1 (raw): "```xml\n<invoke>\n<tool_name>run_command</tool_name>...\n<command>cat ~/ctf_files/csawpad.py", no wrapper, never ran.
- `2015q-cry-eps_part2`: r2 and r4 wrapped correctly (7 and 4 calls ran); r1 and r3 omitted the wrapper (0 ran). Same model, same challenge: a stochastic slip, not an inability.
- `2013f-cry-stfu` r1 (genuine prose): "we need to understand what a 'Secure Test File Unit' (STFU) is ... Let's start by examining the contents of the `stfu` file."

The no-tool rate by round is flat (it does not improve as context grows):

| round | no-tool / replies | invoke without wrapper | prose only |
|---|---|---|---|
| 1 | 26/47 (55%) | 18 | 8 |
| 2 | 24/46 (52%) | 17 | 7 |
| 3 | 26/44 (59%) | 17 | 9 |
| 4 | 25/44 (57%) | 17 | 8 |

After a no-tool reply and "Please proceed to the next step", the next reply had a tool call in only **10 of 76** transitions (66 stayed no-tool).

## Q3. Repeated tool calls (I1)

Using `run_report.py`'s key (name plus raw argument string): 114 of 261 (43.7%) repeats across 23 of 47 challenges. What the earlier matched call had returned:

| earlier call's result | repeats |
|---|---|
| real command output (non-error, non-empty) | 57 |
| `check_flag` verdict (`correct: False`) | 16 |
| error result | 25 |
| empty output | 15 |
| time-out | 1 |

The repeat's own result against the earlier call: **identical in 104 of 114**, different in 10 (network commands, container-state changes). Scope:

| scope | repeats |
|---|---|
| exact same command within the same reply (duplicate invokes in one block) | 56 |
| same command across rounds | 58 |

Longest chain: `2013f-web-historypeats`, `$'curl -I http://web.chal.csaw.io:80'` appears 5 times (r1 twice, then r2–r4), each with the identical error.
- `2013f-web-historypeats` r1–r4: the same curl/nikto/cat trio re-issued, every result identical `No such file or directory` (rc 127).
- `2014q-web-silkgoat` r3: `tar -xzf ~/ctf_files/silkgoat_templates.tar.gz` three times in one reply, each empty.
- `2014q-rev-aerosol_can` r1: `disassemble_function(path=~/ctf_files/aerosol_can)` four times in one reply, each identical "Function main not found".

## Q4. Tool errors (41) and time-outs (6)

| error class | count |
|---|---|
| argument parse: missing required argument (XML formatter) | 26 |
| `Function <x> not found in <binary>` (disassemble) | 13 |
| decompiler or disassembler output not available | 2 |
| unknown tool, command not found, missing file | 0 |

The 26 argument errors are one mistake: the model writes the tool-description schema inside the invoke, `<parameter><name>command</name><type>string</type><value>...</value></parameter>`, instead of `<command>...</command>`. `extract_params` does `invocation.find("command")`, finds no such tag, and raises `Missing required arguments ... {'command'}` (also `flag`, `path`).
- `2014f-msc-pps` r2: schema-style `<name>command</name>...<value>curl http://misc.chal.csaw.io:5665</value>`, missing-argument error.
- `2016f-pwn-detective` r2–r4: every reply mixes run_command, disassemble_function and check_flag in this broken form; all 12 calls error.
- `2016q-pwn-tutorial`: "Function 0x00000780 not found in ~/ctf_files/tutorial" (passes a radare2 address as the function name).

The 6 time-outs (10 s default) are blocking network reads and heavy pcap work, not harness faults: `nc rev.chal.csaw.io 12012` (2015q-rev-ftp r1); `nc crypto.chal.csaw.io 4242` (2016f-cry-katy r1, r3); `tshark ... POST | grep` (2013q-msc-networking_1 r3); `tshark ... tcp.port == 7179` (2015q-for-pcapin r4); `echo "hidden_message" | base64 -d` (2016q-for-yaar_haar r3). So 3 network reads, 2 tshark pcap scans, 1 other.

## Q5. Context-length endings (3) at 32k

All three overflowed on a single un-truncated tool output. `prompt_tokens` records only successful calls, so it understates the pressure: the overflowing call is never counted, which is why the logged values look small.

| challenge | calls logged (prompt_tokens) | largest single tool result | producing command |
|---|---|---|---|
| 2015q-cry-bricks_of_gold | 2 ([1054, 1343]) | 2,631,277 chars (about 0.66 M tokens) | `cat ~/ctf_files/bricks_of_gold_...` (a binary) |
| 2016q-for-watchword | 2 ([756, 2165]) | 167,243 chars (about 42k tokens) | `strings -t d ~/ctf_files/powpow.mp4` |
| 2015f-rev-wyvern2 | 1 ([1029]) | 17,618 chars (about 4.4k tokens) | `disassemble_function` in an 11-invoke reply (80,394 chars of results in total) |

Prevention: a per-output cap (truncate any single tool result to a few KB and say so) stops all three; a memory layer that summarizes rather than carrying outputs verbatim is the second line. wyvern2 also shows that one reply batching 11 invokes can overflow on its own.

## Q6. Per-category behaviour

rev/pwn (17): only 7 ever called a disassembler. `decompile_function` was never used; `disassemble_function` had 24 calls: 19 errors, 5 successes. The errors are wrong function names (`main` absent, or a radare2 hex address) and "not available". 10 of 17 planned radare2 through run_command, but the call then failed to register (missing wrapper).

web (8): only 3 got server output (`2013q-web-guess_harder` 4, `2014f-web-webroot` 2). Four (silkgoat, k_stairs, throwback, cloudb) issued zero HTTP commands because their calls never registered. `2013f-web-historypeats` issued 15 HTTP commands with zero output: it wrapped each whole command in ANSI-C quoting, e.g. `$'curl -I http://web.chal.csaw.io:80'`, which bash runs as one non-existent command (rc 127).

forensics/crypto: where a command registered, the first move fits the category: `tcpdump -r` (pcapin), `unzip` (evidence_zip), `file powpow.mp4` (watchword), `tshark` (yaar_haar), `tar -xzf` (onlythisprogram), `base64 -d < ...avi` (eps_part2), `nc` (katy). Several challenges with no first command are such only because the intended command sat in an unwrapped invoke that never ran.

## Q7. Prompt sizes

Last recorded `prompt_tokens` per challenge (n=47): min 1029, median 3272, mean 3252, max 7375. The four largest are multi-invoke runs (guess_harder 7375, yaar_haar 6916, networking_1 6314, tutorial 5428). No recorded call approached the 32,768 ceiling; the only overflows (Q5) were on unrecorded calls.

At 4096, vLLM rejects a request when prompt plus the 1024-token reply budget exceeds 4096, that is, prompt > 3072. **25 of 47 challenges** reached a recorded prompt > 3072 and would have failed at 4k. First offending call: call 2 in 1 challenge, call 3 in 10, call 4 in 14. So at 4k more than half the runs would be cut short, almost all at call 3–4 (a lower bound: the 3 that overflowed even at 32k would fail at 4k sooner). This matches the real I2 evidence (2013f-cry-stfu failing at 4k on call 3, prompt 3090).

## Q8. Other notable behaviour

- Multi-invoke blocks: of 80 tool-bearing replies, 41 carried 2 or more registered invokes, up to 16 in one reply (`2013f-web-historypeats` r3) and 11 (wyvern2). The model fires a batch with no observation between commands; wasteful, and (wyvern2) able to overflow the context in one step.
- "Download the binary" planning despite `~/ctf_files`: 7 replies, e.g. `2014q-pwn-ish` r1 "Download and analyze the binary", `2013q-pwn-csaw_diary` r1 "connecting to the server and downloading the binary".
- Garbled output: `2014f-msc-pps` r1 emitted `<value>c<replacement-byte>품</value>` (a CJK character) as the command, then failed to parse.
- Asking the user for input (no user exists in the loop): 3 replies, e.g. `2014f-rev-return_of_the_weiner` r1 "If you need any specific details or further analysis, let me know!".
- Hallucinated values submitted as flags (Q1) and as function targets (the radare2 address `0x00000780` given to `disassemble_function`).

## Implications for the memory and harness work

**I9 (no-tool replies): the implemented mitigation addresses the smaller half.** The 101 split into 69 malformed but genuine tool calls (invoke without `<function_calls>`) and 32 true prose replies. The implemented `no_tool_nudge` restates the format; that fits the 32 prose cases, but the generic "Please proceed" recovered only 10 of 76 follow-ups, so a nudge alone is weak. The 69 point to a harness parsing change needing no model change: accept a bare `<invoke>` (optionally inside a ```` ```xml ```` fence) as a tool call, with `</invoke>` as a stop sequence. `2015q-cry-eps_part2` wrapping correctly in r2/r4 but not r1/r3 shows it is a slip, not an inability. When measuring `no_tool_call.count`, separate "prose" from "unwrapped invoke"; they need different fixes.

**I1 (repeated actions): memory of outcomes is the missing piece.** 104 of 114 repeats reproduced identical output, that is, deterministic commands re-run after the result had already been seen. 73 of 114 followed a call that had produced a real result (57 outputs plus 16 check_flag verdicts), 25 followed an error, 15 an empty output, 1 a time-out; repeats follow outcomes the model should have retained, not only errors. This is the direct case for keeping outcomes in memory (`raw_tail`, the merged earlier-actions list, pinned facts) and for harness `repeat_detection`. Two cautions: 56 of 114 are duplicate invokes inside one reply, which `repeat_detection` catches by key but only `skip` avoids re-running; and 10 of 114 produced different output (mostly network reads), so `skip` returning a stored result can be stale, which makes `note` the safer default. These logs predate the `repeat_detection` and `no_tool_call` log fields.

**I2 (context length): fixed as the primary limiter, not as a risk.** At 4k, 25 of 47 would overflow (most at call 3–4); at 32k only 3 did, and all 3 were one oversized tool output, not accumulated history. The remaining need is per-output truncation (one `cat` produced 2.6 MB). Until outputs are capped, any context size can be overrun by one command, and a memory summarizer handed a 2.6 MB observation will truncate or overflow during summarization. Truncation at the tool boundary should precede or accompany the memory layer.

**Memory design.** (1) The thing most worth remembering is a command outcome: 104 of 114 repeats re-derived an identical result, so "command X already run, result Y" (verbatim for the latest action, summarized for older ones) targets the largest avoidable waste. (2) Memory needs material: 55.8% of replies produced no observation, so fixing the unwrapped-invoke parsing is a precondition; otherwise a memory run summarizes mostly empty rounds. (3) Memory must hold exact strings: the model already invents placeholder flags and function names, so a paraphrasing summarizer would make this worse; a pinned verbatim facts block (flags, addresses, file names) is indicated. (4) Cap per-output size in the memory input too: the 2.6 MB and 167 KB outputs that overflow raw context will dominate or break summarization unless capped first.
