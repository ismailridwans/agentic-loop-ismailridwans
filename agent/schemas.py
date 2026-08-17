"""JSON schemas for the model's replies.

Groq strict mode = constrained decoding, so these can't be violated. Two rules it
imposes: every property listed in `required`, and additionalProperties false.
So an "optional" field has to be nullable instead.
"""

CLAIM_TYPES = ["NUMBERS", "RULES", "DATES", "MEANING", "FACTS"]
GUARDS = ["none", "different_things", "superseded", "stated_exception", "same_value"]
ACTIONS = ["extract_claims", "find_related_claims", "compare_claims"]


def _obj(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


# reason(). Params are flat rather than nested under "params" - strict mode wants
# one fixed shape, and a params object that changes per action can't be expressed.
# reason() puts them back into {action, params, reasoning}.
PLAN_SCHEMA = _obj({
    "action": {"type": "string", "enum": ACTIONS},
    "chunk_ids": {
        "type": ["array", "null"],
        "items": {"type": "string"},
        "description": "For extract_claims. Null otherwise.",
    },
    "claim_id": {"type": ["string", "null"], "description": "For find_related_claims."},
    "claim_a": {"type": ["string", "null"], "description": "For compare_claims."},
    "claim_b": {"type": ["string", "null"], "description": "For compare_claims."},
    "reasoning": {"type": "string", "description": "Why this action, in a sentence."},
})

# extract_claims(). We ask for a quote, not offsets - the model can't count
# characters but it can copy text. The handler finds the quote in the chunk to get
# the real position, and drops anything it can't find.
EXTRACTION_SCHEMA = _obj({
    "claims": {
        "type": "array",
        "items": _obj({
            "quote": {"type": "string", "description": "Copied verbatim from the text."},
            "type": {"type": "string", "enum": CLAIM_TYPES},
            "subject": {
                "type": "string",
                "description": "What it's about, normalised: 'system log retention'.",
            },
            "value_raw": {
                "type": ["string", "null"],
                "description": "The measurable value, e.g. '30 days'. Null if none.",
            },
        }),
    }
})

COMPARISON_SCHEMA = _obj({
    "same_subject": {"type": "boolean"},
    "verdict": {"type": "string", "enum": ["contradiction", "no_contradiction"]},
    "type": {"type": "string", "enum": CLAIM_TYPES + ["NONE"]},
    "guard": {"type": "string", "enum": GUARDS},
    "confidence": {"type": "number"},
    "reasoning": {"type": "string"},
})

REFLECTION_SCHEMA = _obj({
    "is_done": {"type": "boolean"},
    "quality_score": {"type": "number", "description": "0.0 to 1.0."},
    "next_instruction": {"type": "string", "description": "What the next round should do."},
})
