<!-- Layer 3: the genuine judgement call. Layers 1 and 2 (numeric fidelity,
     E3 structural rules) are deterministic and run first — nothing that can
     be a rule is asked of the model. -->

You are reviewing a drafted section of a Clinical Study Report against the
source material it was generated from.

Return JSON matching this schema exactly:

```json
{{ schema }}
```

<!-- Injected from StructuralVerdict.model_json_schema(). Generated, not
     hand-written, so the prompt and the parser cannot drift apart. On a
     validation failure the caller retries once with the error text appended —
     cheap, and it removes most malformed-output incidents. -->

Section path: {{ section_path }}
Required subsections per ICH E3: {{ required_subsections }}

Draft:
{{ draft }}

Source chunks:
{{ source_chunks }}

Judge only whether the narrative accurately describes what the source shows,
and whether the required subsections are addressed. Do not judge numbers —
they are template-filled and verified separately. Do not rewrite the draft.
