# Memory and context management for LLM agents: literature review for the CTF agent

**Date of review:** 2026-11-03

**Scope.** This review covers published work on how LLM agents manage what goes into the model's context during long tool-using tasks. It is written for our fork of the NYU CTF Bench harness, which runs Qwen2.5-7B-Instruct (AWQ) on vLLM with a 4096-token context. The review covers four areas: (1) compression of observations and trajectories in tool-using agents, (2) memory architectures for agents, (3) how CTF and penetration-testing agents handle long outputs and history, and (4) evaluations of agent memory, including reports of when summarization helps or hurts. Each entry gives the source, a short description of the memory mechanism, and one line on what it means for our design (`llm_ctf/memory.py`, described in `docs/memory.md`). Every link and every number below was checked against the primary source (arXiv abstract or HTML page, proceedings page, or official documentation) during this review. Items that could not be verified are listed in [Verification notes](#verification-notes) and are not used as evidence.

## Contents

- [Our current design and its budget](#our-current-design-and-its-budget)
- [Key takeaways for our agent](#key-takeaways-for-our-agent)
- [Comparison of approaches](#comparison-of-approaches)
- [1. Observation and trajectory compression in tool-using agents](#1-observation-and-trajectory-compression-in-tool-using-agents)
- [2. Memory architectures for agents](#2-memory-architectures-for-agents)
- [3. CTF and offensive-security agents](#3-ctf-and-offensive-security-agents)
- [4. Evaluations of agent memory and evidence on when summarization helps or hurts](#4-evaluations-of-agent-memory-and-evidence-on-when-summarization-helps-or-hurts)
- [Suggested next experiments](#suggested-next-experiments)
- [Verification notes](#verification-notes)
- [References](#references)

## Our current design and its budget

Without memory, the harness resends the full conversation on every call; a vLLM "maximum context length" error ends the run with `finish_reason: context_length` (`llm_ctf/conversation.py`). The `run_command` tool returns stdout and stderr without truncation (`llm_ctf/tools/tools.py`). With action memory enabled, each model call receives the system prompt, the challenge description, one summary per past action (including the most recent one), and a next-step instruction. The `llm` summarizer makes one extra model call per action (≤ 80 words, copy exact values, no next steps); the `truncate` summarizer keeps the head and tail of each value. The memory block is capped at 4000 characters and drops the oldest summaries first.

Budget estimate from the numbers in `docs/memory.md`: 4096 tokens − about 850 (system prompt and tools) − 150 to 300 (challenge) − 1024 (reserved for the reply) leaves about 1900 to 2050 tokens for memory and the instruction. At 3 to 4 characters per token for English-like text (hex dumps, base64 and disassembly tokenize worse), the 4000-character memory block uses roughly 1000 to 1300 tokens. That leaves room for roughly 2000 to 3000 characters of raw recent output, or less for binary-looking output. These are estimates; the `prompt_tokens` field in our logs gives the real numbers.

## Key takeaways for our agent

1. **Keep the most recent observation verbatim and compress only older ones.** Most systems in this review that compress history keep a raw tail: SWE-agent keeps the last 5 observations [1], Cybench keeps the last 3 iterations [32], the Complexity Trap study keeps the last 10 turns [3], OpenHands' summarizing condenser keeps the first and last events [2], Anthropic's tool-result clearing keeps the 3 most recent tool uses by default [14], AgentDiet only reduces the step two steps back [4], D-CIPHER's executors keep the last 5 actions and observations [35], and AutoAttacker shows the latest action and result next to its running summary [40]. The exceptions are HackSynth, whose planner sees only a running summary [34], and ReSum, which restarts from the question plus a summary [5]; our design is in this minority. With our budget, k = 1 raw action with a head-and-tail cap of about 2000 to 3000 characters is what fits.

2. **Treat the no-model-call baseline as a serious contender.** On SWE-bench Verified, simple observation masking roughly halved cost relative to the raw agent in four of five model configurations, and its solve rate was within 1 point of LLM summarization in four configurations and 5 points higher in the fifth; most differences were not statistically significant, and summarization made trajectories longer [3]. In SWE-agent, collapsing old observations beat keeping full history (18.0% vs 15.0% resolved on SWE-bench Lite with GPT-4 Turbo) [1]. In EnIGMA, the LM summarizer gained 1.3 points over no summarizer, and the simple summarizer lost 2.6 points (Claude 3.5 Sonnet, four CTF benchmarks) [31]. Since our `llm` summarizer doubles model calls on a 7B model, it needs to beat `truncate` and masking in a paired comparison before we keep it.

3. **Summarize only long outputs, and make compression restorable.** EnIGMA summarizes only outputs above a line-count threshold, and both of its summarizers tell the agent how to reach the original output; the simple one saves it to a file [31]. ACON compresses an observation only when it exceeds a threshold [7], AgentDiet skips steps shorter than 500 tokens [4], and VulnBot extracts key information only from results over 8,000 characters [39]. Manus keeps the URL or file path whenever it drops content so the agent can restore it [15], and Anthropic recommends keeping lightweight identifiers and loading data just in time [13]. For us: save every full output to a file in the container and put the path in memory. This addresses our "no retrieval" limitation without a new tool.

4. **Make the summarizer task-aware and tune its instructions on failures.** EnIGMA's LM summarizer receives the challenge statement, the last command and its output [31]. ReSum's summary is conditioned on the original question [5]. ACON improves its compression guideline by comparing runs where full context succeeded and compressed context failed, and reports 26–54% lower peak tokens with better task success [7]. Anthropic advises tuning compaction prompts for recall first and precision second [13]. Our summarizer prompt asks for exact values but does not see the challenge description, so it cannot tell which values matter.

5. **Keep a small structured state separate from the action log.** Cybench makes the agent carry a "Research Plan and Status" and a "Log" of exact commands in every response [32]. PentestGPT keeps a pentesting task tree [33], VulnBot keeps a task graph plus a summarizer that records ports, versions, vulnerabilities and shell state [39], HiAgent organizes memory by subgoal [21], Letta keeps labeled memory blocks that are always in context [16], and the OpenHands-style summary prompt used in the Complexity Trap study tracks fields such as COMPLETED, PENDING and CODE_STATE [3]. In VulnBot's ablation, removing the summarizer had the largest effect (subtask success 55 → 27) [39]. For a 7B model, the safest version of this is a "facts" block filled by deterministic extraction (flag-format strings, paths, host:port, addresses, credentials) that is never evicted.

6. **When memory fills up, merge into itemized entries instead of rewriting everything.** ACE documents "context collapse": a full rewrite shrank a context from 18,282 to 122 tokens and dropped AppWorld accuracy from 66.7 to 57.1, below the 63.7 no-adaptation baseline [23]. Xiong et al. find that errors stored in memory propagate to later tasks [47]. Mem0 updates a fact store with explicit ADD/UPDATE/DELETE/NOOP operations [20]. Running-summary designs (HackSynth [34], AutoAttacker [40], MemGPT's recursive summary [16]) work but carry the same rewrite risk. Our current behavior (drop the oldest summaries) loses information silently; merging old summaries into a list of tried commands and outcomes, with facts kept separately, is the lower-risk next step.

7. **Small models are weak at managing their own memory unless trained for it.** MEM1 reports that an untrained Qwen2.5-7B-Instruct given MEM1's prompt and truncation scored 0.262 EM versus 0.709 for the RL-trained MEM1 on the 2-objective QA task [6]. In ReSum, the untuned Qwen3-30B was the weakest summary model in each comparison we checked [5]. ACON's distilled small compressors retained over 95% of the teacher's performance, and compression raised a Qwen3-14B agent on AppWorld from 25.6% to 33.9% [7]. Cyber-Zero runs EnIGMA with the simple summarizer instead of the LM summarizer and reports 2.0% pass@1 for zero-shot Qwen2.5-7B-Instruct on NYU CTF Bench [42]. For us this argues for deterministic memory operations first, and for a stronger or specially prompted summarizer only if it wins on our logs.

8. **Shorter, focused prompts help even inside the context window, and placement matters.** Models use information at the start and end of the prompt better than information in the middle [46]. Chroma's tests of 18 models found that performance becomes less reliable as input grows, even on simple tasks [45]. Du et al. report drops of 13.9% to 85% from input length alone, even with perfect retrieval [48]. Manus rewrites a todo list at the end of the context to keep the goal in recent attention [15]. For us: render the facts block and the plan just before "Choose your next action", not at the top of the memory.

9. **Context isolation through sub-tasks is the strongest structural approach in recent work, but it depends on capable or trained models.** D-CIPHER's planner sees only executor summaries; removing the planner cut Claude 3.5 Sonnet from 19.0% to 14.0% on NYU CTF Bench [35]. Context-Folding's branch/return actions reached 62.0% on BrowseComp-Plus and 58.0% on SWE-bench Verified with a 32K active context, against baselines with a 327K window, after RL training [8]. HiAgent's subgoal-level summarization doubled success rates with GPT-4 [21]. A light version for our 7B model is subgoal-scoped memory without a second agent.

10. **Cross-attempt memory and knowledge retrieval complement in-episode memory but compete for the same tokens.** Reflexion stores 1 to 3 verbal reflections between attempts [18]. The plain-agent study reached 95% on InterCode-CTF partly by using multiple attempts with full resets [37]. CRAKEN retrieves CTF writeups [36] and CTFAgent retrieves entries from a CTF knowledge base [41] into the prompt. With about 2000 tokens of history budget, any of these must be weighed against memory space.

11. **Measure what memory loses, not only the solve rate.** Useful signals from the literature: trajectory length (summarization lengthened trajectories in [3]), repeated commands, and how often runs end on context length (24.56% of GPT-3.5's failures in NYU CTF Bench [30]; 28 of 182 CTFAgent failures on NYU CTF [41]). Our logs already record `prompt_tokens`, per-action `full_chars`/`summary_chars`, and `finish_reason`, so these can be computed offline.

## Comparison of approaches

"Extra calls" counts model calls beyond the agent's own step. "Evidence" gives the strongest result found in the primary source, with benchmark and model.

| Approach | Mechanism | Kept raw | Summarized or dropped | Extra calls | Evidence |
|---|---|---|---|---|---|
| Our action memory, `llm` | Per-action LLM summary of every action | Nothing from past actions | All actions, including the latest; oldest summaries dropped at 4000 chars | 1 per action | Not yet measured |
| Our action memory, `truncate` | Head and tail of each value | Head/tail of each output | Middle of each output; oldest dropped | 0 | Not yet measured |
| NYU CTF Bench baseline [30] | Full conversation resent | Everything | Nothing; run ends at context limit | 0 | "Token exceeded" was 24.56% of GPT-3.5 failures, 4.61% of GPT-4 failures |
| SWE-agent [1] | Collapse old observations to one line | Last 5 observations; all actions and reasoning | Older observations | 0 | 18.0% vs 15.0% (full history), SWE-bench Lite, GPT-4 Turbo |
| Observation masking [3] | Placeholder for old observations | Last M = 10 turns; all reasoning and actions | Older observations | 0 | About half the raw agent's cost in 4 of 5 configs; solve rate within 1 point of LLM summary in 4 of 5 (5 points higher in the fifth), SWE-bench Verified |
| LLM summary with raw tail [2][3] | Summarize middle events when a size threshold is hit | First events and recent tail (M = 10 in [3]) | Middle events, into one running summary | 1 per condensation | OpenHands: 54% vs 53% on a SWE-bench Verified subset, up to 2x lower per-turn cost [2] |
| AgentDiet [4] | Separate cheap LLM removes useless, redundant, expired content | Last 2 steps; short steps (threshold 500 tokens) | Step s−2 if saving > 500 tokens | 1 per long step (GPT-5 mini) | 39.9–59.7% fewer input tokens, Pass% change −1.0 to +2.0, SWE-bench Verified and Multi-SWE-bench Flash |
| Tool-result clearing [14] | Server-side removal of old tool results | Last 3 tool uses (default), tool calls | Older tool results, replaced by a placeholder | 0 | Documentation; no benchmark given |
| EnIGMA simple summarizer [31] | Long output saved to file, opened in file viewer | Windowed view of the file | Rest of the output (still on disk) | 0 | 26.9% vs 28.2% (none), Claude 3.5 Sonnet, 4 CTF benchmarks |
| EnIGMA LM summarizer [31] | Second LM summarizes a long output given challenge + last command | Short outputs | Long outputs above a line threshold | 1 per long output | 29.5% vs 28.2% (none), same setting |
| Cybench agent [32] | Sliding window plus self-maintained plan and log | Initial prompt + last 3 responses and observations | Everything older (plan/log in responses carries state) | 0 | 40 professional CTF tasks; framework, not a memory ablation |
| HackSynth [34] | Running summary updated each step; planner sees only the summary | Nothing raw; new output truncated to 250–500 chars before summarizing | All outputs | 1 per step | GPT-4o 34.2% PicoCTF, 40.0% OverTheWire |
| AutoAttacker [40] | Running "situation" summary + retrieved past actions | Latest action and result | All earlier history | 1 per step + retrieval | Without summarizer: 1/3 vs 3/3 on a ransomware task, GPT-4 |
| PentestGPT [33] | Task tree in a reasoning session; separate parsing and generation sessions | Task tree | Tool outputs condensed by parsing module | 2+ per step | 228.6% higher task completion than GPT-3.5 baseline |
| VulnBot [39] | Phase summaries + task graph | Task graph | Results > 8,000 chars; phase outputs summarized | 1+ per phase | Without summarizer: subtask success 55 → 27, Llama3.1-405B |
| D-CIPHER [35] | Planner delegates to fresh executors that return summaries | Executor: last 5 actions/observations, reasoning | Executor histories (planner sees summaries); outputs > 25,000 chars truncated | Separate agents | 22.0% NYU CTF Bench, 22.5% Cybench, 44.0% HTB |
| HiAgent [21] | Subgoal chunks; finished subgoals replaced by summaries; retrieval on demand | Current subgoal's actions and observations | Completed subgoals | 1 per subgoal | 2x success rate, −3.8 steps, GPT-4, 5 AgentBoard tasks |
| Context-Folding [8] | Agent calls branch/return; branch steps folded into a return message | Main thread | Branch steps | 0 (learned by RL) | 62.0% BrowseComp-Plus, 58.0% SWE-bench Verified, 32K active context |
| ReSum [5] | Summarize everything when near the context limit, restart from question + summary | Question | Whole history | 1 per reset | +4.5% over ReAct without training; +8.2% more with ReSum-GRPO |
| MEM1 [6] | Constant-size internal state rewritten each turn | Latest internal state, query, result | All previous turns | 0 (learned by RL) | 3.5x performance, 3.7x less memory than Qwen2.5-14B-Instruct, 16-objective QA |
| ACON [7] | Compression guideline optimized from failures; history and observation compression above thresholds | Below-threshold content | History and long observations | 1 per compression | 26–54% lower peak tokens; Qwen3-14B AppWorld 25.6% → 33.9% |
| MemGPT [16] | OS-style paging between context and external stores | System instructions, working context, recent FIFO queue | Evicted messages (searchable in recall storage) + recursive summary | Tool calls by the agent | Document analysis and multi-session chat tasks |
| Generative Agents [17] | Memory stream + scored retrieval + periodic reflection | Top-scored memories that fit | Everything else (kept in store) | Importance scoring, reflection | Ablations show memory, reflection, planning each matter for believability |
| Reflexion [18] | Verbal reflection stored between attempts | Current trajectory + 1–3 reflections | Previous trajectories | 1 per failed attempt | HumanEval pass@1 91% vs 80% (GPT-4) |
| ACE [23] | Itemized "playbook" updated by incremental deltas | Playbook bullets | Nothing rewritten wholesale | Reflector + curator calls | +10.6% on agent benchmarks; context-collapse case study |
| LLMLingua family [10][11][12] | Token-level prompt pruning by a small model | High-information tokens | Low-information tokens | Small-model pass | Up to 20x compression with little loss (LLMLingua) |

## 1. Observation and trajectory compression in tool-using agents

### [1] SWE-agent
- **Title:** SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering
- **Authors, year, venue:** John Yang et al., 2024, NeurIPS 2024
- **Link:** https://arxiv.org/abs/2405.15793 ; history processor documentation: https://swe-agent.com/latest/reference/history_processor_config/
- **Mechanism:** SWE-agent keeps all of the agent's actions and reasoning but collapses every observation older than the last 5 into a single line, and keeps only the first of repeated past error messages. In its ablation on SWE-bench Lite with GPT-4 Turbo, the default "last 5 observations" setting resolved 18.0% versus 15.0% with full history. SWE-agent 1.x exposes this as the `LastNObservations` history processor, with tags that can force an observation to be kept or removed regardless of its age.
- **Relevance:** Direct evidence that dropping old observation text while keeping actions can improve results; our design drops the latest observation too, which SWE-agent never does.

### [2] OpenHands and its context condenser
- **Title:** OpenHands: An Open Platform for AI Software Developers as Generalist Agents; blog post "OpenHands Context Condensensation for More Efficient AI Agents" (spelling as published); SDK condenser documentation
- **Authors, year, venue:** Xingyao Wang et al., 2024, ICLR 2025; blog by Calvin Smith, 2025-04-09
- **Link:** https://arxiv.org/abs/2407.16741 ; https://openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents ; https://docs.openhands.dev/sdk/arch/condenser
- **Mechanism:** The default `LLMSummarizingCondenser` fires when the event history exceeds `max_size` (default 120 events), keeps the first `keep_first` events (default 4) and a recent tail, and replaces the middle with an LLM-written summary, which can come from a cheaper model than the agent's. After condensation the view holds about `max_size // 2` events. The blog reports that on a subset of SWE-bench Verified the condensed agent solved 54% versus 53% for the baseline, with up to 2x lower per-turn API cost and linear instead of quadratic cost growth. Summarization is triggered in batches so that prompt caching keeps working between condensations.
- **Relevance:** A tested pattern for "raw head + summary of middle + raw tail" that we could implement at our scale with a smaller threshold.

### [3] The Complexity Trap
- **Title:** The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management
- **Authors, year, venue:** Tobias Lindenbauer et al., 2025, DL4C workshop at NeurIPS 2025
- **Link:** https://arxiv.org/abs/2508.21433
- **Mechanism:** The study compares, in SWE-agent on SWE-bench Verified across five model configurations, (a) the raw agent, (b) observation masking (observations older than the last M = 10 turns are replaced by a placeholder while all reasoning and actions are kept), and (c) LLM summarization with a raw tail (summarize N = 21 turns at a time, keep M = 10 raw, OpenHands-style structured prompt). Masking roughly halved cost versus the raw agent in four of five configurations (the exception, Qwen3-32B with thinking, had short trajectories) and its solve rate was close to summarization's: raw / masking / summary were 53.4% / 54.8% / 53.8% for Qwen3-Coder 480B, 17.0% / 15.0% / 16.0% for Qwen3-32B, 32.8% / 35.6% / 36.0% for Gemini 2.5 Flash, and 40.4% / 36.4% / 31.4% for Gemini 2.5 Flash with thinking, where both strategies scored below the raw agent. Most differences were not statistically significant. Summarization lengthened trajectories (Gemini 2.5 Flash: mean 52 turns versus 44 for masking and 50 raw), and the summary calls cost up to 7.2% of the instance cost. A hybrid (masking first, summarization deferred to N = 43) was 7% cheaper than masking and 11% cheaper than summarization on a 50-instance subset with Qwen3-Coder 480B.
- **Relevance:** The closest controlled comparison to our `llm` versus `truncate` question; it says to test masking as a third arm and to watch trajectory length.

### [4] AgentDiet (trajectory reduction)
- **Title:** Reducing Cost of LLM Agents with Trajectory Reduction (v1 title: Improving the Efficiency of LLM Agent Systems through Trajectory Reduction)
- **Authors, year, venue:** Yuan-An Xiao et al., 2025, FSE 2026
- **Link:** https://arxiv.org/abs/2509.23586
- **Mechanism:** A separate reflection module, run with a cheaper model (GPT-5 mini), removes "useless, redundant, and expired" information from one past step at a time and replaces it with a short takeaway. At step s it only processes step s−2 (so the two most recent steps stay raw), sees one step before the target plus the steps after it, and skips steps whose reduction would save fewer than 500 tokens. The authors found that agents asked to erase their own context rarely did so. On SWE-bench Verified and Multi-SWE-bench Flash with Claude 4 Sonnet and Gemini 2.5 Pro, input tokens fell 39.9–59.7% and total cost 21.1–35.9%, with Pass% changing between −1.0 and +2.0 points.
- **Relevance:** Supports a delay before summarizing, a length threshold, and a separate summarizer model; also a warning that a 7B agent is unlikely to manage its own memory well.

### [5] ReSum
- **Title:** ReSum: Unlocking Long-Horizon Search Intelligence via Context Summarization
- **Authors, year, venue:** Xixi Wu et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2509.13313
- **Mechanism:** When the history approaches the context limit, a summary tool compresses the whole history (question, thoughts, actions, observations) into an "Essential Information" summary, and the agent restarts from the original question plus that summary. The authors trained a dedicated summarizer (ReSumTool-30B, from Qwen3-30B-A3B-Thinking distilled from GPT-OSS-120B) and an RL method (ReSum-GRPO). Training-free ReSum improved on ReAct by 4.5%, and ReSum-GRPO added 8.2%. In the summary-tool comparisons we checked (GAIA and BrowseComp-zh with WebSailor-3B, BrowseComp with WebSailor-30B), the untuned Qwen3-30B was the weakest summarizer (for example GAIA: 27.5 versus 35.3 for ReSumTool-30B and 40.5 for GPT-OSS-120B).
- **Relevance:** Summarizer quality changes outcomes, and an off-the-shelf mid-size model is a weak summarizer; our 7B summarizing itself is the weakest case.

### [6] MEM1
- **Title:** MEM1: Learning to Synergize Memory and Reasoning for Efficient Long-Horizon Agents
- **Authors, year, venue:** Zijian Zhou et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2506.15841
- **Mechanism:** At every turn the agent writes a new internal state (`<IS>`) that merges the previous state, its last query and the result; all tags from the previous turn are then removed, so the context stays roughly constant in size. The policy is trained with PPO from Qwen2.5-7B Base, using an attention mask that reproduces what was in memory at each generation step. On a 16-objective multi-hop QA task, MEM1-7B reported about 3.5x better performance and 3.7x lower memory use than Qwen2.5-14B-Instruct. The same prompt-and-prune rollout applied to an untrained Qwen2.5-7B-Instruct scored 0.262 EM versus 0.709 for MEM1 on the 2-objective task.
- **Relevance:** Shows that a 7B model can keep a compact working state, but only after RL training; prompting alone was not enough.

### [7] ACON
- **Title:** ACON: Optimizing Context Compression for Long-horizon LLM Agents
- **Authors, year, venue:** Minki Kang et al., 2025, ICML 2026
- **Link:** https://arxiv.org/abs/2510.00615
- **Mechanism:** ACON compresses the interaction history when it exceeds a threshold and compresses a new observation when it exceeds a separate threshold (the ablation found 4096 for history and 1024 for observations worked best). The compression guideline is optimized in natural language: the method collects tasks where the agent succeeded with full context but failed with compressed context, has an optimizer LLM explain what was lost, and updates the guideline; a second pass pushes toward shorter outputs. The optimized compressor is distilled into smaller models (Qwen3-14B, Qwen3-8B, Phi-4), which kept over 95% of the teacher's performance. The authors report 26–54% lower peak tokens on AppWorld, OfficeBench and multi-objective QA, and a Qwen3-14B agent improved on AppWorld from 25.6% to 33.9%; a naive FIFO truncation baseline reduced average AppWorld accuracy from 56.0 to 45.8.
- **Relevance:** A concrete method for improving our summarizer prompt from our own paired logs (cases where baseline solved and memory failed).

### [8] Context-Folding
- **Title:** Scaling Long-Horizon LLM Agent via Context-Folding
- **Authors, year, venue:** Weiwei Sun et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2510.11967
- **Mechanism:** The agent has two context tools: `branch(description, prompt)` starts a sub-trajectory for a subtask, and `return(message)` folds that sub-trajectory away, leaving only the return message in the main thread. The behavior is trained with FoldGRPO, an RL method with penalties for token-heavy work in the main thread and for leaving the subtask's scope. With Seed-OSS-36B-Instruct, a 32K active context and at most 10 branches, the agent reached 62.0% on BrowseComp-Plus and 58.0% on SWE-bench Verified, above ReAct baselines with a 327K context and above a summary agent that summarizes when the 32K context is full. The authors argue that summarizing at arbitrary points disrupts the working context, while folding at subtask boundaries does not.
- **Relevance:** Summaries aligned with subtask boundaries rather than with every action; our per-action summaries are the opposite granularity.

### [9] AgentFold
- **Title:** AgentFold: Long-Horizon Web Agents with Proactive Context Management
- **Authors, year, venue:** Rui Ye et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2510.24699
- **Mechanism:** At each step the agent performs a "folding" operation that either condenses recent details or consolidates several finished steps into one abstract entry. The behavior is learned by supervised fine-tuning only. AgentFold-30B-A3B scored 36.2% on BrowseComp and 47.3% on BrowseComp-ZH.
- **Relevance:** Supports multi-granularity memory (fine for recent steps, coarse for old ones), which our flat list of equal-sized summaries lacks.

### [10] LLMLingua
- **Title:** LLMLingua: Compressing Prompts for Accelerated Inference of Large Language Models
- **Authors, year, venue:** Huiqiang Jiang et al., 2023, EMNLP 2023
- **Link:** https://arxiv.org/abs/2310.05736
- **Mechanism:** A small language model scores tokens and removes low-information ones in a coarse-to-fine process, with a budget controller and iterative token-level compression. The authors report up to 20x compression with little performance loss on GSM8K, BBH, ShareGPT and Arxiv-March23.
- **Relevance:** Token pruning is designed for natural-language prompts; for CTF outputs where exact bytes matter (addresses, keys, flags), dropping tokens risks corrupting the values we most need to keep.

### [11] LongLLMLingua
- **Title:** LongLLMLingua: Accelerating and Enhancing LLMs in Long Context Scenarios via Prompt Compression
- **Authors, year, venue:** Huiqiang Jiang et al., 2023, ACL 2024
- **Link:** https://arxiv.org/abs/2310.06839
- **Mechanism:** A question-aware version of LLMLingua that keeps the parts of a long prompt relevant to the query. On NaturalQuestions with GPT-3.5-Turbo it reported up to 21.4% better performance with about 4x fewer tokens.
- **Relevance:** Question-aware compression is the token-level analogue of a task-aware summarizer.

### [12] LLMLingua-2
- **Title:** LLMLingua-2: Data Distillation for Efficient and Faithful Task-Agnostic Prompt Compression
- **Authors, year, venue:** Zhuoshi Pan et al., 2024, Findings of ACL 2024
- **Link:** https://arxiv.org/abs/2403.12968
- **Mechanism:** Compression is treated as token classification by a bidirectional encoder (XLM-RoBERTa-large or mBERT) trained on data distilled from an LLM. It is 3x to 6x faster than earlier prompt compressors and cut end-to-end latency by 1.6x to 2.9x at 2x to 5x compression.
- **Relevance:** A summarizer that needs no extra call to our vLLM server; worth testing only on natural-language outputs, not on hex or code.

### [13] Anthropic: Effective context engineering for AI agents
- **Title:** Effective context engineering for AI agents
- **Authors, year, venue:** Prithvi Rajasekaran et al. (Anthropic Applied AI team), 2025-09-29, engineering blog
- **Link:** https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- **Mechanism:** The post treats context as a limited "attention budget" and names three long-horizon techniques: compaction (summarize near the limit and restart; Claude Code keeps architectural decisions, unresolved bugs and implementation details and discards redundant tool outputs), structured note-taking to files outside the context (for example a NOTES.md or to-do list), and sub-agents that return condensed summaries. It recommends tool-result clearing as the lightest form of compaction and advises tuning compaction prompts to "Start by maximizing recall" before improving precision. It also recommends keeping "lightweight identifiers" (file paths, queries, links) and loading data at runtime.
- **Relevance:** Supports keeping file paths to full outputs in memory and writing notes the agent can re-read.

### [14] Anthropic API context editing
- **Title:** Context editing (Claude API documentation)
- **Authors, year, venue:** Anthropic, documentation, accessed 2026-11-03
- **Link:** https://platform.claude.com/docs/en/build-with-claude/context-editing
- **Mechanism:** The `clear_tool_uses_20250919` strategy clears old tool results once the prompt exceeds a trigger (default 100,000 input tokens), keeps the most recent `keep` tool uses (default 3), and replaces each cleared result with a placeholder; tool calls stay visible unless `clear_tool_inputs` is set. `exclude_tools` protects chosen tools, and `clear_at_least` avoids clearing too little to be worth breaking the prompt cache. When combined with the memory tool, the model receives a warning before clearing so it can save important results to memory files.
- **Relevance:** A production version of "observation masking with a raw tail of 3"; the pre-clearing warning is a cheap idea we can copy (ask the agent to record facts before old outputs disappear).

### [15] Manus: Context engineering lessons
- **Title:** Context Engineering for AI Agents: Lessons from Building Manus
- **Authors, year, venue:** Yichao "Peak" Ji, 2025-07-18, company blog
- **Link:** https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- **Mechanism:** Manus keeps the context append-only to preserve KV-cache hits, uses the file system as external memory, and requires compression to be restorable (drop a page's content but keep its URL; drop a document but keep its path). It rewrites a todo.md file during long tasks (about 50 tool calls on average) so the goal stays in recent attention, and it keeps failed actions and error traces in context so the model does not repeat them.
- **Relevance:** Supports saving full outputs to files with the path kept in memory, keeping errors visible, and restating the plan at the end of the prompt. Append-only context also matters for vLLM prefix caching, which our per-step rebuild of the memory block does not exploit.

## 2. Memory architectures for agents

### [16] MemGPT and Letta
- **Title:** MemGPT: Towards LLMs as Operating Systems; Letta documentation (memory blocks)
- **Authors, year, venue:** Charles Packer et al., 2023, arXiv preprint; Letta documentation accessed 2026-11-03
- **Link:** https://arxiv.org/abs/2310.08560 ; https://docs.letta.com/v1-sdk/memory/memory-blocks
- **Mechanism:** The main context is split into read-only system instructions, a fixed-size "working context" that the agent edits through function calls, and a FIFO message queue whose first entry is a recursive summary of evicted messages. When the prompt passes a warning threshold (for example 70% of the window) the agent receives a "memory pressure" message; at the flush threshold (for example 100%) a set fraction of messages (for example 50% of the window) is evicted to recall storage, which stays searchable, and the summary is regenerated. Archival storage is a separate searchable database. Letta, the successor project, exposes "memory blocks" with a label, description, value and size limit that are always in the context and edited with built-in tools.
- **Relevance:** The model for a two-tier design: a small always-visible block for facts and plan, plus a searchable store of full outputs (for us, files in the container).

### [17] Generative Agents
- **Title:** Generative Agents: Interactive Simulacra of Human Behavior
- **Authors, year, venue:** Joon Sung Park et al., 2023, UIST 2023
- **Link:** https://arxiv.org/abs/2304.03442
- **Mechanism:** Every observation goes into a natural-language memory stream. Retrieval scores each memory by recency (exponential decay, factor 0.995), importance (rated 1 to 10 by the LLM when the memory is created) and relevance (embedding cosine similarity), sums the normalized scores and inserts the top memories that fit. When the summed importance of recent events exceeds 150, the agent writes higher-level reflections that are stored as memories themselves.
- **Relevance:** Importance scoring is a principled replacement for our "drop the oldest" rule (for example, never drop an entry that contains a flag-like string or credentials).

### [18] Reflexion
- **Title:** Reflexion: Language Agents with Verbal Reinforcement Learning
- **Authors, year, venue:** Noah Shinn et al., 2023, NeurIPS 2023
- **Link:** https://arxiv.org/abs/2303.11366
- **Mechanism:** After a failed attempt, a self-reflection step writes a verbal lesson that is stored in long-term memory and given to the next attempt; the current trajectory is the short-term memory. Long-term memory is bounded to a few reflections (usually 1 to 3). The paper reports 91% pass@1 on HumanEval versus 80% for GPT-4.
- **Relevance:** Applies only if we run multiple attempts per challenge; then a short reflection ("what was tried, why it failed") is cheaper than carrying the full trajectory.

### [19] A-MEM
- **Title:** A-MEM: Agentic Memory for LLM Agents
- **Authors, year, venue:** Wujiang Xu et al., 2025, NeurIPS 2025
- **Link:** https://arxiv.org/abs/2502.12110
- **Mechanism:** Inspired by the Zettelkasten method, each new memory becomes a note with a context description, keywords and tags. The system links the note to related existing notes and can update the attributes of older notes as new information arrives. The authors report gains over prior memory systems across six foundation models.
- **Relevance:** Mostly designed for long conversations; the useful idea for us is updating older entries when new evidence arrives (for example marking a hypothesis as disproved).

### [20] Mem0
- **Title:** Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory
- **Authors, year, venue:** Prateek Chhikara et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2504.19413
- **Mechanism:** An extraction step turns the latest exchange (plus a rolling summary and the last m = 10 messages) into candidate facts. An update step compares each fact with the top s = 10 similar stored memories, and the LLM chooses ADD, UPDATE, DELETE or NOOP. On the LOCOMO benchmark the authors report a 26% relative improvement in LLM-as-a-judge score over the OpenAI baseline, 91% lower p95 latency and over 90% token savings compared with full context.
- **Relevance:** Explicit update operations over a fact list avoid both silent dropping and full rewrites; a deterministic version (dedupe by key, overwrite on change) is feasible for us.

### [21] HiAgent
- **Title:** HiAgent: Hierarchical Working Memory Management for Solving Long-Horizon Agent Tasks with Large Language Model
- **Authors, year, venue:** Mengkang Hu et al., 2024, ACL 2025
- **Link:** https://arxiv.org/abs/2408.09559
- **Mechanism:** Before acting, the agent states a subgoal. Action-observation pairs for the current subgoal stay in full; when the agent decides the subgoal is finished, its pairs are replaced by one summarized observation that also says whether the subgoal was met. A `retrieve(subgoal_id)` action brings a past subgoal's full pairs back when needed. With GPT-4 on five AgentBoard tasks, the authors report twice the success rate and 3.8 fewer steps on average, with context at 64.98% of the standard agent's; on Tyreworld, removing observation summarization cut success from 60.0 to 30.0 and removing trajectory retrieval cut it to 50.0.
- **Relevance:** The closest published design to "summaries plus retrieval of full outputs on demand", at the subgoal rather than the action level.

### [22] Voyager
- **Title:** Voyager: An Open-Ended Embodied Agent with Large Language Models
- **Authors, year, venue:** Guanzhi Wang et al., 2023, TMLR 2024
- **Link:** https://arxiv.org/abs/2305.16291
- **Mechanism:** Voyager stores successful behaviors as executable code in a skill library, retrieves skills for new tasks, and composes them into more complex programs. Skills persist across tasks, so the agent does not relearn them.
- **Relevance:** A procedural memory across challenges (for example verified helper scripts for common steps) is a separate project from in-episode memory, but it reuses the same prompt budget.

### [23] Agentic Context Engineering (ACE)
- **Title:** Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models
- **Authors, year, venue:** Qizheng Zhang et al., 2025, ICLR 2026
- **Link:** https://arxiv.org/abs/2510.04618
- **Mechanism:** ACE keeps a "playbook" of itemized bullets, each with an identifier and counters of how often it was helpful or harmful. A generator produces trajectories, a reflector extracts lessons, and a curator merges small delta updates into the playbook with non-LLM logic, plus embedding-based de-duplication, instead of rewriting the whole context. The paper documents "brevity bias" and "context collapse": in one AppWorld case, a full rewrite shrank the context from 18,282 to 122 tokens and accuracy fell from 66.7 to 57.1, below the 63.7 baseline without adaptation. ACE reports +10.6% on agent benchmarks and +8.6% on finance benchmarks.
- **Relevance:** Direct evidence against repeatedly re-summarizing our memory into shorter prose; merge into itemized entries instead.

### [24] Dynamic Cheatsheet
- **Title:** Dynamic Cheatsheet: Test-Time Learning with Adaptive Memory
- **Authors, year, venue:** Mirac Suzgun et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2504.07952
- **Mechanism:** A black-box model keeps an evolving memory of strategies, code snippets and insights across problems at inference time, without labels or weight updates. Reported gains include GPT-4o going from 10% to 99% on Game of 24 after reusing a Python solution.
- **Relevance:** Cross-challenge memory of reusable scripts; same caveat as Voyager about the prompt budget.

### [25] Agent Workflow Memory
- **Title:** Agent Workflow Memory
- **Authors, year, venue:** Zora Zhiruo Wang et al., 2024, arXiv preprint
- **Link:** https://arxiv.org/abs/2409.07429
- **Mechanism:** The agent induces reusable workflows (common sub-routines) from past trajectories, offline or online, and adds them to the prompt for later tasks. It reported 24.6% and 51.1% relative gains in success rate on Mind2Web and WebArena.
- **Relevance:** Per-category CTF workflows (for example a standard first pass for a pwn binary) are a form of this memory.

### [26] ExpeL
- **Title:** ExpeL: LLM Agents Are Experiential Learners
- **Authors, year, venue:** Andrew Zhao et al., 2023, AAAI 2024
- **Link:** https://arxiv.org/abs/2308.10144
- **Mechanism:** The agent collects experiences on training tasks, distills them into natural-language insights, and at inference uses those insights and retrieved past trajectories. Performance improves as experience accumulates, without fine-tuning.
- **Relevance:** Cross-task learning; relevant only after in-episode memory is working.

### [27] CoALA
- **Title:** Cognitive Architectures for Language Agents
- **Authors, year, venue:** Theodore R. Sumers et al., 2023, TMLR (v3 is the camera-ready version, 2024)
- **Link:** https://arxiv.org/abs/2309.02427
- **Mechanism:** A framework that describes agents in terms of working memory ("active and readily available information ... for the current decision cycle"), episodic memory (experience from earlier decision cycles), semantic memory (knowledge about the world and itself) and procedural memory (LLM weights and agent code), plus actions that read and write these stores.
- **Relevance:** Useful vocabulary: our action memory is episodic; we lack an explicit working memory (current facts and plan) and any semantic or procedural store.

### [28] Recursive summarization for dialogue memory
- **Title:** Recursively Summarizing Enables Long-Term Dialogue Memory in Large Language Models
- **Authors, year, venue:** Qingyue Wang et al., 2023, Neurocomputing
- **Link:** https://arxiv.org/abs/2308.15022
- **Mechanism:** The LLM summarizes small dialogue segments, then repeatedly updates the summary by combining the previous memory with new context; replies are generated from the latest memory. The authors report more consistent responses in long conversations.
- **Relevance:** The basic "merge old summaries" method; see ACE [23] for its failure mode.

## 3. CTF and offensive-security agents

### [29] InterCode and InterCode-CTF
- **Title:** InterCode: Standardizing and Benchmarking Interactive Coding with Execution Feedback; Language Agents as Hackers: Evaluating Cybersecurity Skills with Capture the Flag
- **Authors, year, venue:** John Yang et al., 2023, NeurIPS 2023 Datasets and Benchmarks; John Yang et al., 2023, NeurIPS 2023 MASEC workshop
- **Link:** https://arxiv.org/abs/2306.14898 ; https://neurips.cc/virtual/2023/75839
- **Mechanism:** InterCode treats interactive coding as an RL environment in Docker, with code as actions and execution output as observations. InterCode-CTF adds 100 picoCTF tasks in a Bash/Python environment. The sources available to us do not describe how the original InterCode-CTF agent bounded history; a later paper describes a baseline that "follows InterCode's TryAgain strategy" with up to 5 action-observation pairs and observations truncated to 500 characters [37].
- **Relevance:** Shows that early CTF baselines used fixed windows and hard truncation; it is the easiest benchmark in this group.

### [30] NYU CTF Bench and its baseline agent
- **Title:** NYU CTF Bench: A Scalable Open-Source Benchmark Dataset for Evaluating LLMs in Offensive Security
- **Authors, year, venue:** Minghao Shao et al., 2024, NeurIPS 2024 Datasets and Benchmarks
- **Link:** https://arxiv.org/abs/2406.05590
- **Mechanism:** The benchmark has 200 CSAW challenges and a baseline agent with tools `run_command`, `createfile`, `disassemble` and `decompile` (Ghidra), `check_flag` and `give_up`. The paper does not describe any history management; in our fork the baseline resends the full conversation and ends with `context_length` when the server rejects the prompt. The paper lists "Token exceeded" as a failure category: 24.56% of GPT-3.5's failures and 4.61% of GPT-4's failures, and none for Claude, Mixtral or LLaMA.
- **Relevance:** Our starting point; with a 4096-token window the context limit is a much larger problem for us than for the models evaluated in the paper.

### [31] EnIGMA
- **Title:** EnIGMA: Interactive Tools Substantially Assist LM Agents in Finding Security Vulnerabilities
- **Authors, year, venue:** Talor Abramovich et al., 2024, ICML 2025
- **Link:** https://arxiv.org/abs/2409.16165
- **Mechanism:** EnIGMA is built on SWE-agent, which keeps only the last five observations in full. It adds two summarizers for outputs longer than a line-count threshold: a simple summarizer that saves the output to a file and opens it in SWE-agent's file viewer (100 lines at a time), and an LM summarizer that receives only the challenge statement, the last command and its output, and is told to summarize "without any biases or recommendations for next steps". Both display a warning and instructions for reaching the original output. Interactive agent tools (a gdb session and a pwntools-based server connection) run alongside the shell. In the ablation with Claude 3.5 Sonnet over four benchmarks, the full agent (LM summarizer) solved 29.5%, the variant with the simple summarizer 26.9% and the variant with no summarizer 28.2%. The full agent solved 13.5% of NYU CTF Bench (pass@1); the paper's results table lists the original NYU baseline agent at 3.0% with the same model (marked as pass@5). The paper also reports "soliloquizing", where the model writes fake observations itself, in 48.4% of Claude 3.5 Sonnet's NYU CTF trajectories.
- **Relevance:** The closest prior design to ours; it summarizes only long outputs, gives the summarizer the challenge statement, and points the agent back to the full output, three things we do not yet do.

### [32] Cybench
- **Title:** Cybench: A Framework for Evaluating Cybersecurity Capabilities and Risks of Language Models
- **Authors, year, venue:** Andy K. Zhang et al., 2024, ICLR 2025 (oral)
- **Link:** https://arxiv.org/abs/2408.08926
- **Mechanism:** The agent's memory is the initial prompt plus the last three iterations of responses and observations; older history is dropped. Each response must contain Reflection, "Research Plan and Status" (the high-level plan, steps done and in progress, and short results of each step), Thought, Log ("An enumerated list of the exact commands you have run before") and Action, so state is carried forward by the agent's own text. Runs use an input limit of 6000 tokens, an output limit of 2000 tokens and 15 iterations in unguided mode.
- **Relevance:** A working design under a token budget close to ours: a short raw window plus an agent-maintained plan and command log.

### [33] PentestGPT
- **Title:** PentestGPT: Evaluating and Harnessing Large Language Models for Automated Penetration Testing (arXiv title: PentestGPT: An LLM-empowered Automatic Penetration Testing Tool)
- **Authors, year, venue:** Gelei Deng et al., 2023, USENIX Security 2024
- **Link:** https://arxiv.org/abs/2308.06782
- **Mechanism:** Three modules run in separate LLM sessions. The reasoning module keeps a Pentesting Task Tree written as layered bullets, checks that only leaf nodes were changed on each update, and picks the next subtask. The generation module starts a fresh session per subtask, and the parsing module condenses tool outputs, raw HTTP data and source code. The motivating study found that "session context loss" was the most common failure cause (74 of 195 categorized failures), and that models favor recent tasks and forget earlier findings. The paper reports a 228.6% increase in task completion over GPT-3.5.
- **Relevance:** Evidence that losing earlier findings is a main failure mode in long security tasks, and that a compact task tree plus output parsing addresses it.

### [34] HackSynth
- **Title:** HackSynth: LLM Agent and Evaluation Framework for Autonomous Penetration Testing
- **Authors, year, venue:** Lajos Muzsai et al., 2024, arXiv preprint
- **Link:** https://arxiv.org/abs/2412.01778
- **Mechanism:** A planner proposes one command per step; a summarizer receives the previous summary and the new output and returns an updated summary, and the planner sees only that summary. The new output is truncated to an "observation window" before summarization; the paper chose 250 characters for PicoCTF and 500 for OverTheWire, reporting that shorter windows miss information and longer ones put unneeded detail into summaries. With 20 steps, GPT-4o solved 34.2% of 120 PicoCTF and 40.0% of 80 OverTheWire challenges; Llama-3.1-8B solved 15.8% and 13.8%.
- **Relevance:** The design closest to our `llm` mode (summary replaces raw output) and evaluated with an 8B model; note the very small raw window it found best.

### [35] D-CIPHER
- **Title:** D-CIPHER: Dynamic Collaborative Intelligent Multi-Agent System with Planner and Heterogeneous Executors for Offensive Security
- **Authors, year, venue:** Meet Udeshi et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2502.10931
- **Mechanism:** An auto-prompter explores the challenge and writes the planner's prompt; the planner delegates tasks to executors, each started with a fresh history, which return a summary through a `FinishTask` call. Executor history is truncated to the last 5 actions and observations with reasoning kept, and observations are truncated to 25,000 characters. The paper notes that in the earlier baseline "the agent exhausts LLM context length when command output history grows." It reports 22.0% on NYU CTF Bench, 22.5% on Cybench and 44.0% on HackTheBox; removing the planner lowered Claude 3.5 Sonnet from 19.0% to 14.0% on NYU CTF Bench.
- **Relevance:** Context isolation by delegation works on our benchmark with strong models; whether a 7B planner can delegate reliably is untested.

### [36] CRAKEN
- **Title:** CRAKEN: Cybersecurity LLM Agent with Knowledge-Based Execution
- **Authors, year, venue:** Minghao Shao et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2505.17107
- **Mechanism:** CRAKEN extends D-CIPHER with retrieval from a database of 1,298 CTF writeups. A decomposition prompt turns the long conversation into a short task description, a search query and 3 to 5 keywords; a Self-RAG loop retrieves, grades and rewrites; the result is injected as hints into executor prompts at delegation time. With Claude 3.5 Sonnet it reports 21.0% on NYU CTF Bench (22% with Graph-RAG) versus 19.0% for D-CIPHER.
- **Relevance:** Its "decompose the conversation into a short query" step is a task-aware summary of history; retrieved knowledge would compete with memory for our prompt budget.

### [37] Hacking CTFs with Plain Agents
- **Title:** Hacking CTFs with Plain Agents
- **Authors, year, venue:** Rustem Turtayev et al., 2024, arXiv preprint
- **Link:** https://arxiv.org/abs/2412.02776
- **Mechanism:** The baseline kept up to 5 action-observation pairs with observations truncated to 500 characters. The final agent keeps the full history but caps each observation at 3500 characters ("about 10% of observations exceeded the model's context window"), adds a planning step, and uses multiple attempts with full resets. It reports 95% on InterCode-CTF.
- **Relevance:** Shows that a per-observation character cap plus full history is enough with large-context models; for us, a per-observation cap is the simplest first step but not sufficient alone.

### [38] PentestAgent
- **Title:** PentestAgent: Incorporating LLM Agents to Automated Penetration Testing
- **Authors, year, venue:** Xiangmin Shen et al., 2024, AsiaCCS 2025
- **Link:** https://arxiv.org/abs/2411.05185
- **Mechanism:** A multi-agent penetration-testing framework that uses retrieval-augmented generation for intelligence gathering, vulnerability analysis and exploitation. We did not verify details of how it manages history or long outputs (see verification notes).
- **Relevance:** Another example of retrieval plus agent separation in this domain.

### [39] VulnBot
- **Title:** VulnBot: Autonomous Penetration Testing for A Multi-Agent Collaborative Framework
- **Authors, year, venue:** He Kong et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2501.13411
- **Mechanism:** Testing is split into reconnaissance, scanning and exploitation phases with a Penetration Task Graph whose nodes record instruction, command, result and status. A summarizer carries key information to the next phase (open ports, service banners, OS fingerprints, software versions, flagged vulnerabilities, current shell state), and tool results over 8,000 characters are reduced to key information by the LLM. In the ablation with Llama3.1-405B on AutoPenBench real-world tasks, subtask success fell from 55 to 32 without role specialization, to 37 without the task graph, and to 27 without the summarizer.
- **Relevance:** Gives a concrete schema for security-relevant facts, and the summarizer was the most important component in its ablation.

### [40] AutoAttacker
- **Title:** AutoAttacker: A Large Language Model Guided System to Implement Automatic Cyber-attacks
- **Authors, year, venue:** Jiacen Xu et al., 2024, arXiv preprint
- **Link:** https://arxiv.org/abs/2403.01038
- **Mechanism:** A summarizer updates a "situation" each round from the previous situation and the latest output, with instructions to list all previous actions and keep it short; the planner sees the situation plus the latest action and result. An experience manager retrieves the top 3 similar past actions by embedding similarity, and a navigator chooses among them and the planner's action. In its ablation on a ransomware task with GPT-4, the full system succeeded 3/3, and removing the summarizer reduced success to 1/3.
- **Relevance:** Another running-summary design where the latest raw result is always shown alongside the summary, which our design lacks.

### [41] CTFAgent
- **Title:** Measuring and Augmenting Large Language Models for Solving Capture-the-Flag Challenges
- **Authors, year, venue:** Zimo Ji et al., 2025, ACM CCS 2025
- **Link:** https://arxiv.org/abs/2506.17644
- **Mechanism:** Built on the NYU CTF framework, CTFAgent adds two-stage retrieval (knowledge for understanding code, then exploitation strategies) and environment augmentation (tool-use hints, non-blocking netcat session tools, IDA Pro decompilation). It does not describe history management. On InterCode-CTF it improved from 39 to 73 solved out of 100. In its own failure analysis, "Context length exceeded" accounted for 4 of 28 failures on InterCode-CTF and 28 of 182 failures on the NYU CTF subset.
- **Relevance:** Even a strong, augmented agent on our framework loses about 15% of its failed NYU CTF runs to context length; memory and retrieval improvements are complementary.

### [42] Cyber-Zero
- **Title:** Cyber-Zero: Training Cybersecurity Agents without Runtime
- **Authors, year, venue:** Terry Yue Zhuo et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2508.00910
- **Mechanism:** Cyber-Zero generates training trajectories from public CTF writeups by simulation and fine-tunes open models; evaluation uses EnIGMA+, a modified EnIGMA capped at 40 turns that uses the simple summarizer instead of the LLM summarizer because "binary decompilation output can be extremely long to exceed the context window of some LLMs". Zero-shot Qwen2.5-7B-Instruct scored 34.1% on InterCode-CTF, 2.0% on NYU CTF Bench and 0.0% on Cybench (pass@1); fine-tuning raised these to 47.3%, 4.2% and 7.5%.
- **Relevance:** The most direct published baseline for our model on our benchmark, and evidence that a file-backed simple summarizer is a practical choice for small context windows.

## 4. Evaluations of agent memory and evidence on when summarization helps or hurts

### [43] LongMemEval
- **Title:** LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory
- **Authors, year, venue:** Di Wu et al., 2024, ICLR 2025
- **Link:** https://arxiv.org/abs/2410.10813
- **Mechanism:** 500 questions embedded in long user-assistant histories test information extraction, multi-session reasoning, temporal reasoning, knowledge updates and abstention. Commercial assistants and long-context LLMs lost about 30% accuracy when recalling information across sustained interactions. The paper proposes session decomposition, fact-augmented key expansion and time-aware query expansion.
- **Relevance:** "Fact-augmented keys" (index stored items by the facts they contain) maps to indexing saved outputs by extracted values.

### [44] LoCoMo
- **Title:** Evaluating Very Long-Term Conversational Memory of LLM Agents
- **Authors, year, venue:** Adyasha Maharana et al., 2024, ACL 2024
- **Link:** https://arxiv.org/abs/2402.17753
- **Mechanism:** Very long multi-session conversations (in the arXiv version, about 300 turns and 9K tokens on average, over up to 35 sessions) with question answering, event summarization and multimodal dialogue tasks. Long-context models and RAG improved results but stayed well below humans.
- **Relevance:** The benchmark behind Mem0's numbers [20]; it measures conversational recall, not tool-use success, so its results transfer to us only loosely.

### [45] Context Rot (Chroma)
- **Title:** Context Rot: How Increasing Input Tokens Impacts LLM Performance
- **Authors, year, venue:** Kelly Hong, Anton Troynikov, Jeff Huber, 2025-07-14, Chroma technical report
- **Link:** https://www.trychroma.com/research/context-rot
- **Mechanism:** Tests of 18 models (including GPT-4.1, Claude 4, Gemini 2.5 and Qwen3) found that performance "grows increasingly unreliable as input length grows", even on simple retrieval and text-repetition tasks; distractors reduced accuracy further. On 306 LongMemEval prompts, models scored much higher with focused prompts (about 300 tokens) than with the full prompts (about 113k tokens).
- **Relevance:** Supports removing irrelevant output even when it would fit.

### [46] Lost in the Middle
- **Title:** Lost in the Middle: How Language Models Use Long Contexts
- **Authors, year, venue:** Nelson F. Liu et al., 2023, TACL
- **Link:** https://arxiv.org/abs/2307.03172
- **Mechanism:** On multi-document QA and key-value retrieval, accuracy was highest when the relevant information was at the start or end of the input and lower when it was in the middle, including for long-context models.
- **Relevance:** Put the facts block and plan next to the final instruction rather than in the middle of a long memory block.

### [47] How memory management impacts LLM agents
- **Title:** How Memory Management Impacts LLM Agents: An Empirical Study of Experience-Following Behavior
- **Authors, year, venue:** Zidi Xiong et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2505.16067
- **Mechanism:** The study varies how memories are added and deleted. Agents follow retrieved experiences closely when inputs are similar ("experience-following"), errors in stored experiences propagate to later tasks, and some apparently correct stored executions mislead later ones. Controlling the quality of what enters memory matters.
- **Relevance:** A wrong summary (for example a misread address) will be followed; validate extracted values against the raw output and keep the raw output retrievable.

### [48] Context length alone hurts
- **Title:** Context Length Alone Hurts LLM Performance Despite Perfect Retrieval
- **Authors, year, venue:** Yufeng Du et al., 2025, Findings of EMNLP 2025
- **Link:** https://arxiv.org/abs/2510.05381
- **Mechanism:** Across 5 models on math, QA and coding, performance dropped by 13.9% to 85% as input length grew, even when all relevant evidence was retrieved and even when irrelevant tokens were replaced by whitespace or masked. Having the model recite the relevant evidence before solving improved GPT-4o by up to 4% on RULER.
- **Relevance:** Shorter prompts can help independently of what is kept; the recitation result supports restating key facts right before the next action.

### [49] MemoryAgentBench
- **Title:** Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions
- **Authors, year, venue:** Yuanzhe Hu et al., 2025, arXiv preprint
- **Link:** https://arxiv.org/abs/2507.05257
- **Mechanism:** The benchmark tests accurate retrieval, test-time learning, long-range understanding and selective forgetting by feeding information incrementally. No evaluated method (long context, RAG, or agents with external memory) mastered all four.
- **Relevance:** "Selective forgetting" (replacing outdated information) is the property our append-only summaries lack.

### [50] Survey of memory mechanisms
- **Title:** A Survey on the Memory Mechanism of Large Language Model based Agents
- **Authors, year, venue:** Zeyu Zhang et al., 2024, arXiv preprint
- **Link:** https://arxiv.org/abs/2404.13501
- **Mechanism:** A survey of what agent memory is, how memory modules are designed and evaluated, and where they are used.
- **Relevance:** Background reading; it predates most of the 2025 compression work above.

### Summary of evidence on when summarization helps or hurts

- **Helps:** when outputs are long relative to the window and the summarizer is strong or tuned (EnIGMA LM summarizer +1.3 points [31]; ACON +8.3 points for a Qwen3-14B agent [7]; ReSum +4.5% [5]); when summaries align with subtask boundaries (HiAgent [21], Context-Folding [8]); when the summary feeds a different consumer, such as a planner (D-CIPHER [35], VulnBot [39]).
- **Neutral or hurts:** when a no-model-call alternative exists (masking matched summarization at about half the cost [3]); when summaries replace short outputs or are produced by a weak model (EnIGMA simple summarizer −2.6 points [31]; untuned Qwen3-30B was the weakest ReSum summarizer [5]; untrained Qwen2.5-7B-Instruct with MEM1-style pruning [6]); when summaries are repeatedly rewritten (context collapse [23]); when summaries hide failure signals, which lengthened trajectories in [3].
- **Small versus large models:** the evidence that small models benefit comes from trained or distilled components (MEM1 [6], ACON distillation [7], ReSumTool [5], Cyber-Zero [42]). We found no study showing that an untrained 7B model summarizing its own tool outputs improves task success.

## Suggested next experiments

Ordered by expected value relative to effort. Each one names the limitation it addresses and the supporting sources. All can be compared with `scripts/paired_analysis.py` on the same challenge set.

1. **Keep the latest action raw (k = 1).** Show the most recent action's output verbatim with a head-and-tail cap (start at 2500 characters), and summarize an action only when it leaves the raw tail one step later, so the number of summarizer calls does not change; reduce `max_memory_chars` if needed to fit. Effort: small change in `ActionMemory.render` and the next-step prompt. Addresses: "latest output never seen verbatim". Support: [1], [3], [4], [14], [32], [35], [40].

2. **Add an observation-masking arm and compare four arms.** Arms: full history (baseline), `truncate`, `llm`, and masking (keep all reasoning and tool calls, replace outputs older than the raw tail with a one-line placeholder such as "output omitted, N lines"). Masking makes no extra model calls. Effort: small. Addresses: "doubles model calls" and tells us whether `llm` is worth its cost. Support: [1], [3], [31].

3. **Summarize only long outputs.** Keep outputs under a threshold (for example 600 characters) verbatim in memory; summarize or truncate only longer ones. Effort: small. Addresses: information loss on short outputs that are already compact (error messages, single values). Support: [4], [7], [31], [39].

4. **Make compression restorable.** Write every full tool output to a file in the container (for example `~/.agent_outputs/step_<n>.txt`) and include the path and line count in the summary, with an instruction to use `grep`, `sed -n` or `head` on it. Effort: small to medium (one extra `docker exec` write per action). Addresses: "no retrieval". Support: [13], [15], [16], [21], [31].

5. **Add a pinned facts block filled by deterministic extraction.** Extract flag-format matches (the challenge's flag format is known), file paths, URLs, host:port pairs, hex addresses and credential-like strings from each raw output with regular expressions, de-duplicate, and render them in a block that is never evicted, placed just before the next-step instruction. Effort: medium. Addresses: "no task-aware/structured memory" and loss of exact values; avoids relying on the 7B summarizer to copy values. Support: [5], [16], [20], [32], [39], [46], [48].

6. **Make the summarizer prompt task-aware.** Give the summarizer the challenge description and the current facts block, and ask what the action established or ruled out with respect to getting the flag; keep the rule against suggesting next steps. Then use ACON's method on our own logs: collect challenges solved without memory but not with it, compare full and summarized trajectories, and revise the prompt. Effort: small for the prompt change, medium for the failure-driven revision. Support: [3], [5], [7], [13], [31].

7. **Merge instead of dropping old summaries.** When the memory block exceeds its cap, compress the oldest summaries into an itemized "earlier actions" list (command, one-line outcome) rather than deleting them, with facts already preserved in the pinned block. Do not rewrite the whole list each time. Effort: medium. Addresses: "no merging of old summaries". Support: [16], [20], [23], [28], [47].

8. **Carry a short plan and status written by the agent.** Ask the agent to end each reply with a short "Plan and status" section (Cybench style) and keep only the latest one in the prompt. Measure format compliance for the 7B model before relying on it. Effort: medium. Support: [15], [21], [32], [33].

9. **Test a separate summarizer model.** If GPU memory allows, run the summarizer on a different model (a larger one for quality, or a smaller one for speed) via `summarizer_model`, and compare with experiments 2 and 6. Effort: medium to high (second vLLM server). Support: [2], [4], [5], [7].

10. **Subgoal-scoped memory.** Ask the agent to state a subgoal; keep that subgoal's actions raw (within the budget) and summarize them when the subgoal ends, as in HiAgent. A full planner-executor split (D-CIPHER) or folding (Context-Folding) is a larger change and the published results use much stronger or RL-trained models. Effort: high. Support: [8], [21], [35].

11. **Cross-attempt reflection.** If we run several attempts per challenge, carry a short reflection (what was tried, what failed) into the next attempt instead of starting cold. Effort: medium. Support: [18], [37].

12. **Retrieval of CTF knowledge.** Retrieve writeup snippets as hints. Effort: high, and it competes for the same 2000-token budget. Support: [36], [41].

13. **Training.** Fine-tune the 7B model to maintain its own memory (MEM1, ReSum-GRPO) or on CTF trajectories (Cyber-Zero). Effort: highest. Support: [5], [6], [42].

**What to measure in every experiment** (all available from current logs or easy to add): paired solve rate; `finish_reason` counts, especially `context_length`; mean and maximum `prompt_tokens`; rounds per run (summarization lengthened trajectories in [3]); fraction of commands that repeat an earlier command (a proxy for information lost from memory); for `llm` summaries, the fraction of flag-like strings, paths and addresses in the raw output that appear in the summary (computable from the logged transcript); and `summary_seconds` as the added cost.

## Verification notes

The following were checked and could not be confirmed, so they are omitted or marked in the text:

- **OpenHands legacy condensers** (`ObservationMaskingCondenser`, `LLMAttentionCondenser`, `StructuredSummaryCondenser`): found only in secondary sources with conflicting defaults; not used. Only the condensers in the current SDK documentation are described.
- **SWE-agent summarizer configuration page** (`princeton-nlp.github.io/SWE-agent/config/summarizers`): returned HTTP 404 during this review. EnIGMA's summarizer is described from the EnIGMA paper only. The default line threshold for EnIGMA's summarizers could not be read reliably from the paper and is not stated.
- **Original InterCode-CTF agent's history handling:** the workshop material we could access (a poster) does not describe it. The 5-pair and 500-character settings in [29] are attributed to the baseline in [37], which says it follows InterCode's TryAgain strategy.
- **Letta compaction algorithm:** the documentation page for memory blocks does not describe compaction; only the MemGPT paper's eviction mechanism is described.
- **PentestAgent context management:** only the abstract-level description (RAG, multi-agent) was verified.
- **AutoAttacker "episodic memory":** this label appears only in a secondary review; the entry uses the paper's own terms (summarizer, experience manager).
- **Voyager's quantitative results:** the arXiv and TMLR-indexed versions give different numbers for unique items; no numbers are quoted.
- **Cybersecurity AI (CAI) and similar frameworks:** not included because their context management could not be verified from a primary source.
- **Reflexion author list:** the arXiv version lists six authors including Edward Berman; the NeurIPS proceedings list five. The entry uses "Noah Shinn et al.".

## References

1. Yang et al. SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering. NeurIPS 2024. https://arxiv.org/abs/2405.15793 ; history processors: https://swe-agent.com/latest/reference/history_processor_config/
2. Wang et al. OpenHands: An Open Platform for AI Software Developers as Generalist Agents. ICLR 2025. https://arxiv.org/abs/2407.16741 ; Smith, OpenHands Context Condensensation for More Efficient AI Agents (2025-04-09): https://openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents ; condenser docs: https://docs.openhands.dev/sdk/arch/condenser
3. Lindenbauer et al. The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management. DL4C @ NeurIPS 2025. https://arxiv.org/abs/2508.21433
4. Xiao et al. Reducing Cost of LLM Agents with Trajectory Reduction (AgentDiet). FSE 2026. https://arxiv.org/abs/2509.23586
5. Wu et al. ReSum: Unlocking Long-Horizon Search Intelligence via Context Summarization. 2025. https://arxiv.org/abs/2509.13313
6. Zhou et al. MEM1: Learning to Synergize Memory and Reasoning for Efficient Long-Horizon Agents. 2025. https://arxiv.org/abs/2506.15841
7. Kang et al. ACON: Optimizing Context Compression for Long-horizon LLM Agents. ICML 2026. https://arxiv.org/abs/2510.00615
8. Sun et al. Scaling Long-Horizon LLM Agent via Context-Folding. 2025. https://arxiv.org/abs/2510.11967
9. Ye et al. AgentFold: Long-Horizon Web Agents with Proactive Context Management. 2025. https://arxiv.org/abs/2510.24699
10. Jiang et al. LLMLingua: Compressing Prompts for Accelerated Inference of Large Language Models. EMNLP 2023. https://arxiv.org/abs/2310.05736
11. Jiang et al. LongLLMLingua: Accelerating and Enhancing LLMs in Long Context Scenarios via Prompt Compression. ACL 2024. https://arxiv.org/abs/2310.06839
12. Pan et al. LLMLingua-2: Data Distillation for Efficient and Faithful Task-Agnostic Prompt Compression. Findings of ACL 2024. https://arxiv.org/abs/2403.12968
13. Rajasekaran et al. (Anthropic). Effective context engineering for AI agents. 2025-09-29. https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
14. Anthropic. Context editing (Claude API docs). https://platform.claude.com/docs/en/build-with-claude/context-editing
15. Ji (Manus). Context Engineering for AI Agents: Lessons from Building Manus. 2025-07-18. https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
16. Packer et al. MemGPT: Towards LLMs as Operating Systems. 2023. https://arxiv.org/abs/2310.08560 ; Letta memory blocks: https://docs.letta.com/v1-sdk/memory/memory-blocks
17. Park et al. Generative Agents: Interactive Simulacra of Human Behavior. UIST 2023. https://arxiv.org/abs/2304.03442
18. Shinn et al. Reflexion: Language Agents with Verbal Reinforcement Learning. NeurIPS 2023. https://arxiv.org/abs/2303.11366
19. Xu et al. A-MEM: Agentic Memory for LLM Agents. NeurIPS 2025. https://arxiv.org/abs/2502.12110
20. Chhikara et al. Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory. 2025. https://arxiv.org/abs/2504.19413
21. Hu et al. HiAgent: Hierarchical Working Memory Management for Solving Long-Horizon Agent Tasks with Large Language Model. ACL 2025. https://arxiv.org/abs/2408.09559 ; https://aclanthology.org/2025.acl-long.1575
22. Wang et al. Voyager: An Open-Ended Embodied Agent with Large Language Models. TMLR 2024. https://arxiv.org/abs/2305.16291
23. Zhang et al. Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models. ICLR 2026. https://arxiv.org/abs/2510.04618
24. Suzgun et al. Dynamic Cheatsheet: Test-Time Learning with Adaptive Memory. 2025. https://arxiv.org/abs/2504.07952
25. Wang et al. Agent Workflow Memory. 2024. https://arxiv.org/abs/2409.07429
26. Zhao et al. ExpeL: LLM Agents Are Experiential Learners. AAAI 2024. https://arxiv.org/abs/2308.10144
27. Sumers et al. Cognitive Architectures for Language Agents. TMLR. https://arxiv.org/abs/2309.02427
28. Wang et al. Recursively Summarizing Enables Long-Term Dialogue Memory in Large Language Models. Neurocomputing. https://arxiv.org/abs/2308.15022
29. Yang et al. InterCode: Standardizing and Benchmarking Interactive Coding with Execution Feedback. NeurIPS 2023 Datasets and Benchmarks. https://arxiv.org/abs/2306.14898 ; Yang et al. Language Agents as Hackers: Evaluating Cybersecurity Skills with Capture the Flag. NeurIPS 2023 MASEC workshop. https://neurips.cc/virtual/2023/75839
30. Shao et al. NYU CTF Bench: A Scalable Open-Source Benchmark Dataset for Evaluating LLMs in Offensive Security. NeurIPS 2024 Datasets and Benchmarks. https://arxiv.org/abs/2406.05590
31. Abramovich et al. EnIGMA: Interactive Tools Substantially Assist LM Agents in Finding Security Vulnerabilities. ICML 2025. https://arxiv.org/abs/2409.16165
32. Zhang et al. Cybench: A Framework for Evaluating Cybersecurity Capabilities and Risks of Language Models. ICLR 2025. https://arxiv.org/abs/2408.08926
33. Deng et al. PentestGPT: Evaluating and Harnessing Large Language Models for Automated Penetration Testing. USENIX Security 2024. https://arxiv.org/abs/2308.06782 ; https://www.usenix.org/conference/usenixsecurity24/presentation/deng
34. Muzsai et al. HackSynth: LLM Agent and Evaluation Framework for Autonomous Penetration Testing. 2024. https://arxiv.org/abs/2412.01778
35. Udeshi et al. D-CIPHER: Dynamic Collaborative Intelligent Multi-Agent System with Planner and Heterogeneous Executors for Offensive Security. 2025. https://arxiv.org/abs/2502.10931
36. Shao et al. CRAKEN: Cybersecurity LLM Agent with Knowledge-Based Execution. 2025. https://arxiv.org/abs/2505.17107
37. Turtayev et al. Hacking CTFs with Plain Agents. 2024. https://arxiv.org/abs/2412.02776
38. Shen et al. PentestAgent: Incorporating LLM Agents to Automated Penetration Testing. AsiaCCS 2025. https://arxiv.org/abs/2411.05185
39. Kong et al. VulnBot: Autonomous Penetration Testing for A Multi-Agent Collaborative Framework. 2025. https://arxiv.org/abs/2501.13411
40. Xu et al. AutoAttacker: A Large Language Model Guided System to Implement Automatic Cyber-attacks. 2024. https://arxiv.org/abs/2403.01038
41. Ji et al. Measuring and Augmenting Large Language Models for Solving Capture-the-Flag Challenges. ACM CCS 2025. https://arxiv.org/abs/2506.17644
42. Zhuo et al. Cyber-Zero: Training Cybersecurity Agents without Runtime. 2025. https://arxiv.org/abs/2508.00910
43. Wu et al. LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory. ICLR 2025. https://arxiv.org/abs/2410.10813
44. Maharana et al. Evaluating Very Long-Term Conversational Memory of LLM Agents. ACL 2024. https://arxiv.org/abs/2402.17753 ; https://aclanthology.org/2024.acl-long.747
45. Hong, Troynikov, Huber. Context Rot: How Increasing Input Tokens Impacts LLM Performance. Chroma, 2025-07-14. https://www.trychroma.com/research/context-rot
46. Liu et al. Lost in the Middle: How Language Models Use Long Contexts. TACL. https://arxiv.org/abs/2307.03172
47. Xiong et al. How Memory Management Impacts LLM Agents: An Empirical Study of Experience-Following Behavior. 2025. https://arxiv.org/abs/2505.16067
48. Du et al. Context Length Alone Hurts LLM Performance Despite Perfect Retrieval. Findings of EMNLP 2025. https://arxiv.org/abs/2510.05381
49. Hu, Wang, McAuley. Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions (MemoryAgentBench). 2025. https://arxiv.org/abs/2507.05257
50. Zhang et al. A Survey on the Memory Mechanism of Large Language Model based Agents. 2024. https://arxiv.org/abs/2404.13501
