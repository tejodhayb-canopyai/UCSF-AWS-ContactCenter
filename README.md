# UCSF GI Prep Voice Assistant ("Lucy")

A HIPAA-conscious, AWS-native voice assistant that answers patient
questions about colonoscopy and other GI procedure prep over the
phone. Patients call a toll-free number, ask their question in
natural language, and the assistant responds using only content from
approved UCSF prep documents.

This repository holds **three implementations** of the same
assistant. Only one is live; the others are kept on disk for
reference and rollback.

| Version | Folder | Status |
| --- | --- | --- |
| v1 | [`Lex/`](./Lex) | Frozen archive. Single-file Lambda, bilingual constants inline. Deployed in AWS as the rollback target for v3. |
| v2 | [`LexV2&AMZNTranslate/`](./LexV2%26AMZNTranslate) | Abandoned A/B experiment. English-only core wrapped with Amazon Translate. Lives on its own phone number; not in production. |
| v3 | [`AgenticRAG/`](./AgenticRAG) | **Current production.** LangGraph state machine + per-language markdown "skill" packs, packaged as a Lambda container image. |

Read the [handover document](./handover_document.md) end-to-end for
the full system overview, then dive into the per-version README for
implementation depth:

- [`AgenticRAG/README.md`](./AgenticRAG/README.md) — production stack, graph topology, runbooks
- [`Lex/README.md`](./Lex/README.md) — v1 architecture, prompt design, PHI policy
- [`LexV2&AMZNTranslate/README.md`](./LexV2%26AMZNTranslate/README.md) — v2 design and why it was rejected

Connect contact-flow snapshots (with one-paste rollback scripts) live
in [`_connect_flow_snapshots/`](./_connect_flow_snapshots).
