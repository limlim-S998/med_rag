# prompts

Prompt content was owned by the medical writers and a client-side SME. What
lives in this repo is the *harness*: the files are versioned here, the SHA of
this directory is `prompt_bundle_sha`, it is set as a Helm value, and it is
written into every audit row.

That is the whole claim, and it is the honest one — "I owned how prompts were
versioned, served and evaluated", not "I wrote the clinical prompt content".

## Why the SHA is a version axis

Three things can change what the system outputs, and each needs its own
identifier or you cannot attribute a regression:

| Axis | Identifier | Where it lives |
|---|---|---|
| Code | git SHA | image tag |
| Model | deployment name + version | Helm values |
| Prompt | `prompt_bundle_sha` | Helm values, from this directory |

A prompt edit that ships without changing an identifier is a silent behaviour
change. Six months later "which prompt produced this paragraph" is a question
someone will actually ask, and the audit row has to answer it.

## Files

- `section_draft.md` — connective prose around a fixed numeric spine. The
  instruction that matters: it may smooth transitions, it may not alter, add
  or remove any numeral.
- `structural_verdict.md` — the judgement layer. Returns JSON matching
  `verify.StructuralVerdict`; the schema is generated from the Pydantic model
  and pasted in, so the prompt cannot drift from the parser.
