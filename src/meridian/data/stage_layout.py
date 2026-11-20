"""Where staged inputs land inside a sandbox: /stage, one folder per area.

The image creates each folder (docker/agent.Dockerfile) and a sandbox policy lists only
the areas its zone may use (meridian.sandbox.policies). Kept here, not in the sandbox
package, because meridian-worker reads it and must not import the sandbox code.
"""

STAGE_ROOT = "/stage"
META = "meta"  # trigger.json and inputs_manifest.json
BLINDED = "blinded"
OPS = "ops"
UNBLINDED = "unblinded"  # reserved: nothing stages it yet
NARR = "narr"  # rbqm.narrator's job
LM = "lm"  # the local model's job
TMF = "tmf"  # the eTMF index of the cut (quality zone only)
AUDIT = "audit"  # the audit extract of the ledger (quality.audit_trail only)
SEND = "send"  # a nonclinical SEND study (preclinical zone only)
NOAEL = "noael"  # a study's NOAEL decisions, staged by the host (preclinical zone only)
INVENTORY = "inventory"  # an asset's nonclinical study inventory (preclinical zone only)
SIGNAL = "signal"  # recorded openFDA responses, for a replay (portfolio_signal only)
PORTFOLIO = "portfolio"  # the portfolio facts, as the portfolio lead sees them (portfolio)
STAGE_AREAS: tuple[str, ...] = (
    META,
    BLINDED,
    OPS,
    UNBLINDED,
    NARR,
    LM,
    TMF,
    AUDIT,
    SEND,
    NOAEL,
    INVENTORY,
    SIGNAL,
    PORTFOLIO,
)


def stage_path(area: str) -> str:
    return f"{STAGE_ROOT}/{area}"
