# Eagle Auto-Tagger: AI vision tagging that converges on your own vocabulary

**The Problem**:

A large Eagle image library is only as useful as its tags, but hand-tagging thousands of references is unthinkable — and naive AI tagging invents a new, inconsistent vocabulary on every image, fragmenting the library instead of organizing it.

**The Solution**:

A Claude Vision pipeline that auto-tags untagged Eagle images with 40 tags (20 EN + 20 KO) while injecting the library's existing canonical vocabulary into the system prompt, so new tags converge onto your established axes instead of sprawling — and designated reference folders additionally receive full generative-AI reproduction prompts.

## Key Features

- **Vocabulary convergence** — the library's canonical tag map is built and injected into every request, steering Claude to reuse existing tags rather than freely inventing synonyms, keeping the whole library coherent.
- **Bilingual 40-tag output** — each image gets 20 English + 20 Korean tags spanning a 9-group classification axis (subject, mood, color, composition, style, etc.).
- **Folder-aware routing** — ordinary folders get tags only; designated photo/illustration folders also get a generative reproduction prompt, routed to a 9-layer cinematography protocol (photo) or a 10-layer illustration protocol.
- **Always-on daemon** — a `watchdog` file watcher + launchd agent tags new imports automatically in the background (port 41595 Eagle local API).
- **Subscription-OAuth, no API key** — deliberately strips `ANTHROPIC_API_KEY` and runs through the Claude Code CLI subscription, with parallel concurrency, dry-run sampling, and full tag-backup/migration tooling.

## Highlights

Runs in production against a live Eagle library, with canonical-map building, tag migration, and group-classification utilities around the core tagger. Built end-to-end as a real LLM-vision system — vocabulary control, prompt protocols, batching, and resilience — not a one-shot script.

## Tech Stack
Python, Claude Vision, Claude Code CLI, Eagle API, watchdog, Pillow, launchd

[View Source](https://github.com/baessu/eagle-auto-tagger)
