"""AI deal scoring via the Codex CLI (uses the ChatGPT subscription, so the
marginal cost per search is $0 and no Anthropic credits are spent at runtime).

One batched `codex exec` call per search, with --output-schema forcing a
JSON response. Any failure falls back to filters.rule_score so a search
never dies on the scoring stage.
"""

import json
import os
import subprocess
import tempfile

from worker.filters import rule_score

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("CODEX_MODEL")  # None = account default
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "300"))

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "verdict": {
                        "type": "string",
                        "enum": ["great deal", "good", "fair", "pass"],
                    },
                    "reason": {"type": "string"},
                    "flags": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "scam_risk",
                                "overpriced",
                                "great_value",
                                "incomplete_info",
                            ],
                        },
                    },
                },
                "required": ["id", "score", "verdict", "reason", "flags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["evaluations"],
    "additionalProperties": False,
}

PROMPT_TEMPLATE = """You are a sharp used-goods deal evaluator. A buyer is searching \
Facebook Marketplace with this request:

BUYER REQUEST: {query}
{budget_line}
Evaluate every listing below for how good a deal it is FOR THIS REQUEST, using \
your knowledge of typical resale values. Consider: price vs typical market value, \
relevance to the request, scam signals (too-good prices on high-demand items, \
vague titles), and drive time. Do not browse the web or run commands; just reply.

Score 0-100 (100 = exceptional deal, buy immediately). Keep each reason under \
15 words. Return an evaluation for EVERY listing id.

LISTINGS (JSON):
{listings_json}"""


def _compact(listing):
    slim = {
        "id": listing["listing_id"],
        "title": listing.get("title"),
        "price": listing.get("price"),
        "location": listing.get("location"),
        "drive_minutes": listing.get("drive_minutes"),
    }
    if listing.get("description"):
        slim["description"] = listing["description"][:400]
    return slim


def score_listings(user_query, listings, max_price=None):
    """Mutates listings in place, adding score / verdict / reason / flags.
    Returns 'codex' or 'fallback' describing which path produced the scores."""
    if not listings:
        return "codex"

    prompt = PROMPT_TEMPLATE.format(
        query=user_query,
        budget_line=f"BUDGET: ${max_price:g} max\n" if max_price else "",
        listings_json=json.dumps([_compact(l) for l in listings], ensure_ascii=False),
    )

    evaluations = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = os.path.join(tmp, "schema.json")
            out_path = os.path.join(tmp, "out.json")
            with open(schema_path, "w") as f:
                json.dump(RESPONSE_SCHEMA, f)
            cmd = [
                CODEX_BIN, "exec",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color", "never",
                "--output-schema", schema_path,
                "--output-last-message", out_path,
                "--cd", tmp,
            ]
            if CODEX_MODEL:
                cmd += ["--model", CODEX_MODEL]
            cmd.append(prompt)
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=CODEX_TIMEOUT
            )
            if proc.returncode == 0 and os.path.exists(out_path):
                raw = open(out_path).read().strip()
                if raw.startswith("```"):
                    raw = raw.strip("`").lstrip("json").strip()
                evaluations = {e["id"]: e for e in json.loads(raw)["evaluations"]}
            else:
                print(f"[scorer] codex exec failed (rc={proc.returncode}): "
                      f"{(proc.stderr or proc.stdout)[-500:]}")
    except Exception as exc:  # noqa: BLE001 — any scorer failure must not kill the job
        print(f"[scorer] codex scoring error: {exc}")

    mode = "codex" if evaluations else "fallback"
    for listing in listings:
        ev = (evaluations or {}).get(listing["listing_id"])
        if ev:
            listing["score"] = int(ev["score"])
            listing["verdict"] = ev["verdict"]
            listing["reason"] = ev["reason"]
            listing["flags"] = ev["flags"]
        else:
            listing["score"] = rule_score(listing, max_price=max_price)
            listing["verdict"] = "unscored" if mode == "fallback" else "fair"
            listing["reason"] = "Rule-based estimate (AI scoring unavailable)"
            listing["flags"] = []
    return mode
