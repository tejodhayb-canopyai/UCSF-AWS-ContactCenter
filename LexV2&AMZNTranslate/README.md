# LexV2 + Amazon Translate edition

This folder holds the alternative architecture that simplifies the application
core to **English-only** and delegates all non-English caller support to
**Amazon Translate**. It is deployed in parallel with production so we can
A/B test the two patterns on real calls.

It is **not** a fork of prod; the two stacks share no AWS resources and run
on different phone numbers, so issues in this folder cannot affect the
production hotline.

## Difference from the prod stack at a glance

| Concern | Prod (`../`) | This folder |
| --- | --- | --- |
| Lambda code | Bilingual constants for every caller-visible string (`LANG_STRINGS`) + per-language Bedrock prompt template (`PROMPT_TEMPLATES`) | One English `CANNED_STRINGS` table, one English `PROMPT_TEMPLATE`, plus an Amazon Translate wrapper (`_translate`) |
| Bedrock prompt | Two prompt templates (en, es) -- the Spanish one asks Nova to translate the English source material on the fly | One English prompt template; the model never sees Spanish text |
| Bedrock answer language | Whatever the model produces given the locale-specific prompt | Always English from the model; Amazon Translate produces the Spanish version |
| Safety keyword lists | Per-language tuples that have to be maintained twice and kept clinically equivalent | One English tuple; Spanish utterances translate to English before matching |
| Adding a new language | Add a `LANG_STRINGS` block, a `PROMPT_TEMPLATES` block, an `_NAME_PREFIX_RE_<lang>`, and update Lex + Connect | Add the language to `SUPPORTED_LANGUAGES`, then update Lex + Connect; no other code changes |
| Lex locales | `en_US` + `es_US` | `en_US` + `es_US` (unchanged -- ASR must be language-specific) |
| Connect prompts (recordings + TTS) | Static per-language attributes set in `GI_Set_Attrs_EN` / `GI_Set_Attrs_ES` | Static per-language attributes set in `GI_Set_Attrs_EN` / `GI_Set_Attrs_ES` (unchanged) |
| Patient-info collection (name, date, time) | Captured as raw transcript, no translation | Captured as raw transcript, no translation (names + Lex-normalised dates/times are language-neutral) |

## AWS resources (test stack)

| Resource | Production | This stack |
| --- | --- | --- |
| Phone number | `+1 877-427-9082` | `+1 833-502-7528` |
| Connect contact flow | `GI_Inbound_Main` | `GI_Inbound_Main_test` (id `66ec0ac9-a804-4527-9490-e0a066798a78`) |
| Lex bot | `GIHealthcareBot` | `GIHealthcareBot_test` (id `4XWU77TLEP`) |
| Lex alias | `TestBotAlias` | `TestBotAlias` (id `TSTALIASID`) |
| Knowledge base | `GIHealthCareKB` (id `ZTUA1DRKXT`) | `GIHealthCareKB-test` (id `WWXDWQQUO0`) |
| Lambda | `GIHealthcareLexFulfillment` | `GIHealthCareLexfullfillment_test` |
| Lambda handler | `lambda_function.lambda_handler` | `lambda_function.lambda_handler` |
| Conversation log table | `GIConversationTurns` | `GIConversationTurns` (shared, distinguishable by `botId` in the audit row) |

## Lambda function spec

| Setting | Value |
| --- | --- |
| Runtime | `python3.13` |
| Architecture | `arm64` (Graviton2 -- ~20% cheaper + lower cold-start than x86_64) |
| Memory | `1024 MB` |
| Handler | `lambda_function.lambda_handler` |
| Code package | `lambda_function.zip` built from `lambda_function.py` in this folder |
| Execution role | `service-role/GIHealthCareLexfullfillment_test-role-ct3jyhhw` |

### Environment variables

| Name | Value | Why |
| --- | --- | --- |
| `KNOWLEDGE_BASE_ID` | `WWXDWQQUO0` | Bedrock KB the function queries |
| `MODEL_ID` | `amazon.nova-lite-v1:0` | Generation model for RAG answers |
| `CONVERSATION_TABLE_NAME` | `GIConversationTurns` | Per-turn audit / analytics table |
| `STRICT_GROUNDING` | `true` | Enable the two-stage RAG grounding gate |
| `RETRIEVAL_MIN_SCORE` | `0.35` | Min vector-similarity score to allow generation |
| `RETRIEVAL_TOP_K` | `5` | Chunks returned per retrieval call |
| `VOICE_MAX_CHARS` | `650` | Hard ceiling on the spoken answer length |

`PATIENT_TABLE_NAME` is intentionally **unset**, matching prod's PHI-redaction
policy. The legacy `GIPatients` lookup path stays code-resident but inert
so a future caller-verification rollout can re-enable it without rebuilding.

### IAM permissions

Inline policy `GIHealthcareLexFulfillmentInline` on the execution role:

* `dynamodb:GetItem|BatchGetItem|PutItem|UpdateItem` on `GIPatients`
* `dynamodb:PutItem|Query` on `GIConversationTurns`
* `bedrock:Retrieve|RetrieveAndGenerate` (KB queries)
* `bedrock:InvokeModel|InvokeModelWithResponseStream` on
  `arn:aws:bedrock:us-east-1:642058032951:foundation-model/*`
* **`translate:TranslateText`** (new for this stack)
* `logs:CreateLogGroup|CreateLogStream|PutLogEvents`

## Per-turn flow

### English caller

```
Connect (Polly Danielle audio in)
  -> Lex en_US (ASR + NLU, English text)
    -> Lambda
       (lang_code == 'en' -> Translate short-circuits)
       Bedrock KB retrieve (English query against English KB)
       Bedrock KB generate (English prompt -> English answer)
    <- Lambda (English answer)
  <- Lex (English text)
Connect (Polly Danielle audio out)
```

### Spanish caller

```
Connect (Polly Lupe audio in)
  -> Lex es_US (ASR + NLU, Spanish text)
    -> Lambda
       Translate ES->EN (utterance into the application core)
       Bedrock KB retrieve (English query against English KB)
       Bedrock KB generate (English prompt -> English answer)
       Translate EN->ES (answer back to caller)
    <- Lambda (Spanish answer)
  <- Lex (Spanish text)
Connect (Polly Lupe audio out)
```

Patient-info collection intents (`CollectNameIntent`, `CollectDateIntent`,
`CollectTimeIntent`) and the `FallbackIntent` name guard skip Translate
entirely. Proper names and Lex-normalised slot values (`2026-05-22`,
`09:00`) are language-neutral.

## Translation caching

A given `(source, target, text)` triple is translated at most **once per
warm Lambda container**. Two caches:

* `_TRANSLATION_CACHE` -- generic memoization keyed by the full triple.
* `_CANNED_CACHE` -- pre-translated copies of the six canned bot strings
  (`follow_up_prompt`, `no_answer_fallback`, `empty_input_fallback`,
  `goodbye_message`, `escalation_message`, `bedrock_error_template`)
  keyed by `(string_key, language)`.

In the steady state, a Spanish caller pays exactly two Translate calls per
RAG turn (utterance + dynamic answer) -- the follow-up prompt always
hits the cache and the canned fallbacks hit the cache after the first time
they fire. English callers pay zero Translate calls.

Cold-start cost for the **first** non-English call on a freshly-spawned
container: ~6 Translate calls of ~100 ms each = ~600 ms one-time overhead,
on top of the normal Bedrock RAG cost. Subsequent non-English calls on
that container drop to the two-call steady state.

## Lex bot configuration (`GIHealthcareBot_test`)

* Both `en_US` and `es_US` locales are kept (required for language-specific
  ASR + NLU; neither Transcribe nor Polly perform translation).
* Voices: Danielle (`en_US`, neural), Lupe (`es_US`, neural).
* NLU intent confidence threshold: 0.4.
* **Assisted NLU** (`nluImprovement`) is enabled in **Fallback** mode on
  both locales. Lex routes utterances to the Bedrock-backed LLM only when
  the traditional NLU fails or returns confidence below 0.4, so the LLM
  cost stays bounded and clinical paths still use the deterministic
  intent classifier in the common case.
* Intent and slot **descriptions** are written for the LLM (per Lex
  Assisted NLU best practices) -- short, action-oriented, conversational.
* `FallbackIntent` fulfillment hook is `enabled: true, active: true` so
  the Lambda receives FallbackIntent invocations and can run the
  `collectionMode='name'` guard.
* `CollectNameIntent` has both a DialogCodeHook (drives the
  SpellByLetter one-shot retry for English callers) and a
  FulfillmentCodeHook (captures the raw transcript into
  `patientNameRaw`).
* `CollectDateIntent` and `CollectTimeIntent` have only the
  FulfillmentCodeHook (the slot types `AMAZON.Date` / `AMAZON.Time`
  do all the normalisation we need).

## Connect contact flow (`GI_Inbound_Main_test`)

Functionally identical to `GI_Inbound_Main`; copied at the time the test
stack was created. Key wiring:

* Language gate (DTMF 1 = English, 2 = Spanish) sets `langCode` and the
  per-language `namePrompt` / `datePrompt` / `timePrompt` / `goAheadMsg`
  contact attributes.
* `UpdateContactTextToSpeechVoice` swaps Polly to Danielle (EN) or
  Lupe (ES) immediately after the language gate so all subsequent
  prompts speak in the caller's language.
* The Lex bot is `GIHealthcareBot_test:TestBotAlias` (both locales
  associated with the Lambda).

## Generative AI compliance posture

This stack uses Amazon Bedrock (via both the Knowledge Base **and** Lex's
Assisted NLU) and Amazon Translate. All three are HIPAA-eligible under
the AWS BAA.

* Bedrock **cross-region inference** can route Assisted NLU requests to
  models hosted outside `us-east-1` to manage capacity. The data stays
  inside AWS BAA boundary regardless of which region serves the request.
  Confirm your BAA covers all regions Bedrock may route to before going
  live with PHI in production.
* Amazon Translate is HIPAA-eligible (it stores no customer data after
  the synchronous response is returned). All translations happen
  in-region (`us-east-1`).

## Smoke tests

The four event files in this folder reproduce the live-call scenarios
exercised before the first real call:

* `_smoke_en_qa.json` -- English caller asking a prep question. Verifies
  RAG works end-to-end without Translate.
* `_smoke_es_qa.json` -- Spanish caller asking a prep question. Verifies
  the Translate roundtrip (ES->EN->RAG->EN->ES) and the per-container
  cache.
* `_smoke_es_collect_name.json` -- Spanish caller in name collection
  with a non-dictionary name ("Tejodhay"). Verifies the prefix-strip
  ("Me llamo Tejodhay" -> "Tejodhay") and that no Translate call fires
  for proper names.
* `_smoke_es_goodbye.json` -- Spanish caller ending the call. Verifies
  the keyword-detection path: the Spanish utterance translates to
  English, the English keyword list fires, and the cached Spanish
  goodbye message comes back.

Run a smoke test:

```powershell
cd LexV2`&AMZNTranslate
aws lambda invoke `
  --function-name GIHealthCareLexfullfillment_test `
  --payload fileb://_smoke_es_qa.json `
  --cli-binary-format raw-in-base64-out `
  _smoke_es_qa.out.json
python -c "import json;print(json.load(open('_smoke_es_qa.out.json',encoding='utf-8'))['messages'][0]['content'])"
```

## Redeploying after a code change

```powershell
cd LexV2`&AMZNTranslate
Compress-Archive -Path lambda_function.py -DestinationPath lambda_function.zip -Force
aws lambda update-function-code `
  --function-name GIHealthCareLexfullfillment_test `
  --zip-file fileb://lambda_function.zip
```

If you're changing the runtime, architecture, memory, handler, env vars,
or IAM policy, run `update-function-configuration` / `put-role-policy`
first and wait for `LastUpdateStatus == Successful` before calling
`update-function-code`.
