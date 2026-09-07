# Verification, layered. No layer is "prompt it nicely".
#
# IMPLEMENTATION HELD BACK. numeric_fidelity in holding/ and on the
# implementation/retrieval-slice branch. The *layering* is the architecture
# and stays here; the matching rules are modelling.

import re

from pydantic import BaseModel, Field

NUMERAL = re.compile(r"\d+(?:\.\d+)?%?")


class StructuralVerdict(BaseModel):
    # Pydantic model -> JSON schema -> into the prompt -> parse -> validate.
    # On a validation error, retry once with the error text appended. Cheap,
    # and it removes most malformed-output incidents.
    compliant: bool
    missing_sections: list[str] = Field(default_factory=list)
    rationale: str


def numeric_fidelity(generated: str, allowed: dict) -> list[str]:
    # Layer 1, and the one that actually matters: every numeral in the output
    # must appear in the slot values that came from the parsed table. Returns
    # the offenders; a non-empty list fails the section rather than warning.
    ...


def structural_rules(section_path: str, text: str) -> list[str]:
    # Layer 2: E3 shell requirements are rules, not judgement. Deterministic,
    # testable, and they do not cost a token.
    ...


# Layer 3: the genuine judgement calls (does this narrative actually describe
# what the table shows) go to the LLM with a StructuralVerdict schema.
# Layer 4: every generated paragraph carries the chunk IDs it drew on, and the
# UI makes them clickable. The writer is the accountable author; the tool
# never touches a submission.
