# Company Brain — Marketing & Go-to-Market Playbook

**Version:** 1.0
**Owner:** Marketing
**Last updated:** July 2026
**Companion doc:** [PRD.md](./PRD.md) (especially Section 3, Positioning & Competitive Landscape)

This is the single source of truth for how we position, message, and launch Company Brain. Everything here is grounded in the product as specified in the PRD — do not promise capabilities the PRD does not contain.

---

## 1. What We Sell (in plain language)

AI agents fail at company-specific work — refunds, escalations, pricing exceptions — because the rules live in Slack threads, Notion pages, and people's heads. Company Brain connects to those sources, extracts the rules into **human-reviewed, versioned "skills"** (structured decision logic with exceptions and actions), keeps them current automatically, and serves them to any AI agent through one API call.

The output is not search results. It is the answer to "what does my company actually do in this situation" — reviewed by a human, traceable to its source, and updated within five minutes when the policy changes.

**The one-liner:**

> **Company Brain turns your company's tribal knowledge into human-reviewed, executable skills your AI agents can actually trust.**

Shorter variants:

- "The policy brain for AI support agents."
- "Your AI agent's source of truth for how your company actually works."
- "Stop your agents from hallucinating policy."

---

## 2. Category & Market Timing (the "why now")

- Y Combinator published an official Request for Startups called **"Company Brain"** describing this exact product. The category is forming *right now* — multiple YC companies (Hyperspell F25, Hyper P26, Cerenovus W26) are funded and building. Timing pressure is real; so is validation. Use it: "YC asked for this. We built the version with a human in the loop."
- Companies deploying AI support/ops agents in 2026 are past the pilot phase and hitting the same wall: agents are fine on generic tasks, dangerous on company-specific edge cases.
- The market's loudest criticisms of the retrieval-first competitors — hallucinated facts, silent staleness, no audit trail, vendor lock-in — are our four strongest features. We do not need to invent a narrative; we need to amplify an existing one.

---

## 3. The Wedge (do not deviate at launch)

**Target: AI customer-support agents at 50–500 person B2B SaaS companies running Zendesk.**

Why this wedge:

- The pain is sharpest and most quantifiable in support: a hallucinated refund policy has a dollar cost and a churn cost.
- Zendesk + Slack + Notion is the canonical stack at this company size — exactly our connector set.
- The buyer can self-serve: one AI engineer with a tooling budget, no procurement.
- Our demo is a refund scenario. Our reviewer persona is a CS lead. The product already points here.

**What we do NOT lead with at launch:** engineering runbooks, incident response, general "company OS" ambitions, executive analytics. Those are expansion stories for later. Every landing page, ad, and outbound message targets the support-agent use case until we have 10 reference customers.

---

## 4. Audience & Personas

### Persona A — The buyer/user: "The Agent Engineer"

- **Who:** The engineer who owns the AI agent stack at a 50–500 person B2B SaaS company. Often the only one. Has tooling budget, no procurement gate.
- **Pain (use their words):** "Our agent handles 80% of tickets fine and then confidently invents a refund policy on the other 20%." "Every time policy changes I have to hunt down every prompt it touches." "I'm hand-maintaining a `context.md` of our policies and it's already stale."
- **What convinces them:** A working integration in under an hour. One MCP tool call (`query_brain`). Honest docs with latency budgets and failure modes spelled out. Portable markdown export (they've been burned by lock-in).
- **Where they live:** Hacker News, X/Twitter AI-agent circles, LangChain/LlamaIndex/MCP Discords, r/LocalLLaMA and r/AI_Agents, AI engineering newsletters (Latent Space, TLDR AI).

### Persona B — The champion-adjacent approver: "The CS Lead"

- **Who:** Non-technical CS or ops team lead who manages the review queue.
- **Pain:** They *are* the company brain today. Every exception routes through their pattern recognition. They can't take vacation.
- **What convinces them:** The review UI — approve a proposed skill in under 30 seconds, see exactly which Slack thread it came from, override anything. Message: "You stay in control; the machine does the reading."
- **Role in the sale:** They don't buy, but they can veto. Marketing to them = 60-second demo video of the review queue and the contradiction card.

---

## 5. Messaging Hierarchy

### Master narrative (elevator pitch, ~30 seconds)

> Every company runs on knowledge that exists nowhere a machine can read — the refund exception decided in a Slack thread 14 months ago, the escalation rule that lives in the CS lead's head. AI agents fail on company-specific work not because models are weak, but because this knowledge is inaccessible. Company Brain connects to Slack, Notion, Drive, GitHub, Jira, and Zendesk, extracts that knowledge into structured, human-reviewed skills, keeps them current within five minutes of a policy change, and serves them to any agent through a single API. Your agent stops hallucinating policy — and every decision it makes traces back to a reviewed, versioned source.

### The three message pillars

Every asset should ladder up to one of these:

**Pillar 1 — Executable skills, not search results.**
Retrieval tools make documents findable. We return decision logic: *IF order ≤ 30 days → approve; EXCEPTION: damaged in transit → approve regardless.* An agent can act on that. It cannot act on "here are 5 relevant documents."
*Proof points:* skill format with base logic + exceptions table + actions; the refund demo (agent correctly overrides the 30-day rule on a 38-day damaged-item case, citing skill and version).

**Pillar 2 — A human in the loop, by construction.**
Nothing enters your agent's brain without passing confidence routing, contradiction detection, and — for anything uncertain — human review. When two sources disagree, we don't guess; we show both to a human side by side.
*Proof points:* review queue, contradiction cards, confidence scoring with authority tiers, sweep never auto-publishes, full `skill_versions` audit trail.

**Pillar 3 — Always current, never locked in.**
Policy changed in Slack at 2pm? The skill updates by 2:05. And your brain is yours: every skill is plain versioned markdown, exportable anytime with one API call.
*Proof points:* 5-minute webhook-to-published latency; `GET /skills/export`.

### Taglines (tested against pillars)

- "Your agents, running on reviewed policy — not vibes."
- "The difference between an agent that searches and an agent that knows."
- "Policy changes at 2pm. Your agent knows by 2:05."
- "Every agent decision, traceable to a human-approved source."

---

## 6. Competitive Battlecards

General rule: **never name competitors in outbound or ads.** Use these when prospects raise them.

### vs. Hyperspell ("Your Company Brain," memory layer API)

- **They:** memory/recall API for agent developers. **We:** human-reviewed decision logic.
- Kill question to plant: *"When two sources in your memory layer disagree about the refund policy, what does the agent get?"* (We surface a contradiction card to a human; a memory layer returns both or the wrong one.)
- Concede: if the prospect only wants raw recall across personal tools, Hyperspell is fine — and they're not our ICP.

### vs. Hyper (knowledge graph with temporal facts)

- **They:** auto-extracted fact graph, no human gate. **We:** every published skill passed confidence routing or human review.
- Their public criticisms (HN launch): fact hallucination, silent staleness, no auditability, lock-in fear. Our answers, respectively: review queue, 5-minute event-driven updates + contradiction detection, `skill_versions` + source attribution on every skill, markdown export.
- Kill question: *"Can you show an auditor exactly why the agent approved that refund, which source authorized it, and who reviewed it?"*

### vs. Mem0 / Zep / Letta (agent memory infrastructure)

- Not head-to-head. **They store what an agent experienced; we encode how the company decides.** Position as complementary: "Use Mem0 for your agent's conversational memory. Use Company Brain for your company's policy." Avoid picking a fight with a $24M-funded infra player we can partner with.

### vs. Glean / Dust / enterprise search

- "A chatbot over documents is a solved problem. Ask it a question, get a summary. Ask *us* a question, get executable decision logic your agent can act on — with exceptions, actions, and an audit trail."
- Also: they sell to enterprise IT with procurement cycles; we self-serve to one engineer.

### vs. "we'll just put our policies in the system prompt"

- The real competitor at our ICP. Response: "That's what everyone does first. Then policy changes, and you're grepping prompts. Then two docs disagree, and your agent picks one at random. We're the system you build after the `context.md` file fails — connect your sources and skip that failure."

---

## 7. Objection Handling

| Objection | Response |
| --- | --- |
| "We already do RAG over our Notion." | RAG retrieves text; agents need decisions. Show the exceptions table. Ask what their RAG returns when two pages contradict each other. |
| "I don't want my company's data in your cloud." | Read-only OAuth scopes, source-by-source opt-in (you pick channels/folders), full export anytime. Roadmap honesty: PII redaction and enterprise compliance are on the enterprise roadmap, not MVP — say so plainly; our ICP respects honesty and isn't regulated. |
| "What if your extraction is wrong?" | It probably will be sometimes — that's why nothing uncertain publishes without human review, why we track reviewer rejection rate as a public quality metric, and why agent overrides automatically flag skills for re-review. We're the only ones in the category who assume the LLM can be wrong. |
| "Lock-in?" | `GET /skills/export`. Your entire brain as markdown, one call, anytime. Put this in the pricing page footer. |
| "How long does integration take?" | One MCP server, one tool (`query_brain`). If you have an agent framework running, under an hour. (Then prove it in docs.) |
| "Vector search over docs — isn't that a weekend project?" | The retrieval is. The extraction pipeline (two-pass, boundary classification, contradiction detection), review workflow, versioning, and living-currency webhooks are the product. Show the contradiction card — nobody builds that in a weekend. |

---

## 8. Channels & Launch Plan

### Phase L0 — Pre-launch (now → demo-ready)

- **Design partners:** recruit 3–5 from ICP via founder network + targeted outreach. Offer: free, white-glove onboarding, roadmap input. Goal: 2 quotable case studies with numbers ("policy override rate dropped X%", "killed our 400-line system prompt").
- **Landing page + waitlist:** hero message from Section 9. Capture role + agent stack in signup form.
- **Instrument everything:** the PRD's Success Metrics (Section 15) are also marketing proof points. Time-to-first-value (10 published skills in 1 hour) becomes a headline claim only once measured.

### Phase L1 — Developer launch

- **Launch HN / Show HN.** The category's buyers are literally on HN (Hyper launched there). Prepare for the exact criticisms Hyper got — our answers are the product. Founder writes the post; technical, honest, includes the latency budget and failure modes. Nothing sells to this audience like admitting what the product can't do.
- **Demo video (2 min max):** the refund scenario end-to-end — Slack thread → extracted skill → review approval → agent call → correct exception handling with citation. This is the whole pitch in one artifact.
- **Docs as marketing:** quickstart under 10 minutes, honest limits page, `query_brain` contract with latency budget. For this ICP, docs quality *is* brand.
- **Launch week content:** "Why your AI agent hallucinates policy (and prompting won't fix it)" — the problem essay. Post to HN, X, LinkedIn; syndicate to newsletters.

### Phase L2 — Sustained motion (post-launch)

- **Content engine (SEO + credibility), one strong piece every 2 weeks:**
  - Problem content: "The context.md anti-pattern," "What happens when your agent meets a policy exception."
  - Category content: "Memory layers vs. skill layers for AI agents," "Why human review is the missing piece of agentic AI" (ranks for competitor comparison searches without naming names in ads).
  - Build-in-public engineering posts: contradiction detection design, authority tiers, extraction quality metrics. This audience trusts teams that show their work.
- **Community presence:** MCP ecosystem (we're an MCP server — get listed in every MCP directory/registry), LangChain/LlamaIndex integration examples, agent-framework Discords. Integration guides for the top 3 agent frameworks.
- **Partnerships:** Zendesk marketplace listing (wedge channel); co-marketing with agent-framework vendors and with memory-infra players (Mem0 complement story).
- **Proof loop:** publish our own quality metrics quarterly (rejection rate, override rate). Nobody else in the category dares to publish accuracy numbers; doing so is a moat statement.

### Channel priorities (in order)

1. Launch HN + founder X presence (where the ICP is)
2. Docs + quickstart (conversion surface)
3. Content/SEO (compounding)
4. MCP directories + framework integrations (distribution where agents are built)
5. Zendesk marketplace (wedge-specific)
6. Paid — **not yet.** Self-serve dev tools at this stage waste paid spend; revisit after pricing is set and funnel converts.

---

## 9. Copy Blocks (ready to adapt)

### Landing page hero

> **Your AI agent is guessing your refund policy.**
> Company Brain extracts how your company *actually* works — from Slack, Notion, Drive, GitHub, Jira, and Zendesk — into human-reviewed, versioned skills your agents query with one API call. Policy changes at 2pm; your agent knows by 2:05.
>
> [Connect your sources →]   [Watch the 2-minute demo]

Sub-hero trust row: "Read-only access · You choose the channels · Human review before anything publishes · Export your brain anytime"

### Cold email (to Agent Engineer persona)

> Subject: your agent's 400-line system prompt
>
> Hi {name} — saw you're running {agent framework} for support at {company}.
>
> Most teams we talk to maintain a hand-written policy context file for their agent. It's stale within a month, and the agent improvises on whatever's missing.
>
> Company Brain connects to your Slack/Notion/Zendesk, extracts your actual policies into human-reviewed skills (decision logic + exceptions, not search results), and serves them to your agent via one MCP tool call. When policy changes in Slack, the skill updates in 5 minutes.
>
> Worth 20 minutes? Happy to show the refund-exception demo — agent correctly overrides a 30-day rule for a damaged item and cites the reviewed skill it came from.

### Show HN post (skeleton)

> **Show HN: Company Brain – human-reviewed executable skills for AI agents**
>
> We build AI support agents and kept hitting the same wall: the agent nails generic tasks and then confidently invents policy on company-specific edge cases, because the real rules live in Slack threads and a CS lead's head.
>
> Company Brain connects to Slack/Notion/Drive/GitHub/Jira/Zendesk, runs a two-pass extraction pipeline (fast classifier for relevance/decision-moments, Sonnet for structured extraction), and produces versioned markdown "skills": trigger, base logic, an exceptions table, and actions. Anything low-confidence or contradictory goes to a human review queue — when two sources disagree, a human picks, and that decision is versioned. Agents query it via one MCP tool.
>
> Things we intentionally do differently: nothing auto-publishes from historical data; contradictions block publication; every skill traces to its source; the whole corpus exports as markdown (your brain is yours). Things that are honest limitations: query-driven extraction has a 15s budget then goes async; PII redaction is not in the MVP; extraction is wrong sometimes — that's why the review queue exists and why we track our rejection rate.
>
> Demo: {link}. Happy to answer anything about the contradiction detection — it's the part we rewrote three times.

---

## 10. Pricing Guidance (input to the open PRD question)

Not decided (PRD Section 17). Marketing recommendation for launch:

- **Free design-partner tier** (manual, invite-only) → **flat monthly platform fee per workspace** (predictable for a single-buyer engineer; avoids per-query anxiety during integration) → usage-based only at scale tiers later.
- Anchor value against what it replaces: the engineer-hours maintaining prompt context and the cost of one bad agent decision, not against per-seat search tools.
- Whatever the model: **the export endpoint stays free on every tier, forever.** It's the anti-lock-in promise; charging for it would destroy Pillar 3.

---

## 11. Marketing KPIs

| Funnel stage | Metric | Notes |
| --- | --- | --- |
| Awareness | Launch HN front-page hours; demo video views; branded search volume | |
| Interest | Waitlist signups matching ICP (role + stack captured at signup) | Quality over volume |
| Activation | Source-connect completion rate; **time-to-first-published-skill ≤ 1 hour** | Shared with product (PRD Section 15) |
| Value | % agent queries answered by published skill ≥ 70% at week 2 | The north star — retention predictor |
| Advocacy | Design partners quotable; unsolicited HN/X mentions | 2 case studies with numbers by end of L1 |

---

## 12. Do / Don't (brand guardrails)

**Do:**

- Lead with the failure mode ("your agent is guessing") — it's instantly recognizable to the ICP.
- Show the contradiction card and exceptions table in every demo; they are the visual proof of differentiation.
- Be radically honest about limits (latency budget, no PII redaction yet, extraction errors exist). Honesty is a conversion tactic for this audience.
- Say "human-reviewed" in every asset. It is the one claim no funded competitor can make.

**Don't:**

- Don't say "AI-powered knowledge management" or "chat with your docs" — we die in that category.
- Don't name competitors in ads or outbound (battlecards are for live conversations).
- Don't promise regulated-industry compliance yet. "On the enterprise roadmap" is the line.
- Don't widen the wedge in public materials until we have 10 reference customers in support.
- Don't ever caveat the export promise.
