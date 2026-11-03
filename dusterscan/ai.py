"""AI deep analysis of a scan report, using Claude."""

from __future__ import annotations

import json

from .i18n import UserError, normalize, resolve

MODEL = "claude-opus-5-5"
MODULE_VALUES_FOR_AI = 250   # per module

# How the prompt names each app language
PROMPT_LANGUAGE = {"en": "English", "es": "Spanish (as spoken in Colombia)"}


class AIError(UserError):
    pass


SYSTEM_PROMPT = """You are a senior automotive diagnostic technician who specialises in Renault and \
Dacia vehicles. You are given the full result of a read-only diagnostic scan made with an \
ELM327-compatible adapter (Vgate vLinker FS) on the owner's car, plus a first pass of automatic \
rule-based checks. The owner is not a mechanic. Your job is to turn the data into a clear, \
prioritised action plan: what is wrong, how sure we are, what to test to confirm it, what to fix, \
and what to keep an eye on.

Vehicle context:
- The owner says it is a Renault Duster 2018 (Latin American market). Use the VIN, calibration IDs \
and ECU identification in the data to infer the engine (for example the 1.6 16V K4M or the 2.0 16V \
F4R petrol engines) and gearbox where possible, and say when you are inferring.

How to reason:
- Base every finding on evidence in the data and quote the specific values or codes. Separate what \
the data shows from what you infer. If the data is insufficient, say so instead of guessing.
- Connect related symptoms into one root-cause hypothesis when they fit together (for example a lean \
fuel trim plus a lean code plus a misfire on one cylinder), rather than listing each code separately.
- Codes are decoded generically. Manufacturer-specific codes (P1xxx, B/C/U codes from body, airbag, \
ABS modules) may have Renault-specific meanings; give the most likely meaning for this vehicle and \
state your confidence. The 3rd byte "failure type" (UDS) and the status (active now / stored / \
intermittent) matter: an active fault outweighs an old stored one.
- Module fault codes are already filtered to tests that actually failed (UDS status mask 0xAF); \
"state" says whether a code is failing now, stored, pending, or failed earlier since the last clear. \
Communication and supply-voltage U codes stored together in several modules (for example U3003 \
battery voltage with U0100 lost communication with the engine computer) usually record one event - \
a voltage dip, a jump start, a battery disconnected with the ignition on, a loose terminal or ground. \
Treat them as one hypothesis, check it against the battery voltages in the data (adapter readings \
with the engine off and running), and look for other codes with voltage-related failure types that \
the same event can explain before suspecting a module.
- obd.obd_ecus lists the computers that answer OBD-II (7E8 engine, 7E9 automatic gearbox) and each \
OBD code names the computer that reported it. vehicle.module_vins is the VIN each module was coded \
with; a module with a different VIN was probably replaced with a used part.
- When a module has a "definition", it was read with Renault's own definition from the owner's \
DDT4All database: "renault_name" is Renault's name for that code in that module (prefer it over the \
generic description), "extended" holds what the module recorded with the code (mileage_km when it \
was stored, occurrences), and "module_data" holds the module's own measurements, states and \
configuration. meta.odometer_km is the current mileage, so mileage_km tells how long ago a code \
happened; one old occurrence matters far less than a recent or repeated one. Trust a definition \
with tier "exact" or "same_software" chosen automatically; when it was chosen by the user with a \
weaker tier, values may be misdecoded, so cross-check them before relying on them. Modules without \
a definition were read generically.
- module_data supply voltages below the OBD socket voltage (adapter readings) point to a poor ground \
or connector on that module. battery_test, when present, is a guided test: battery at rest, lowest \
voltage while starting (the key indicator of battery health), charging at idle and under load.
- Order likely causes from most to least probable, and order tests from cheapest and simplest to \
most involved. Avoid replacing parts on a guess; prefer a test that confirms the cause first. Give \
concrete test procedures with expected values (for example resistance, voltage, swap test, smoke \
test, fuel pressure) that an owner with a multimeter, or a workshop, can follow.
- Safety systems (airbag, brakes/ABS, steering) and anything that can cause engine damage (misfire \
with the catalytic converter, overheating, oil pressure) get the highest priority.
- Note scan conditions that limit the conclusions: engine off during the scan, codes cleared \
recently, modules that did not answer (often simply not fitted on this trim), self-tests not complete.
- This tool can only read; it cannot clear codes or run tests on the car. Do not tell the user to \
use it for that. Clearing codes should only happen after a repair.
- Be honest and calm. Do not exaggerate risk, and do not dismiss real safety issues.
"""

_CAUSE = {
    "type": "object",
    "properties": {
        "cause": {"type": "string"},
        "likelihood": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["cause", "likelihood"],
    "additionalProperties": False,
}

_FINDING = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
        "system": {"type": "string"},
        "related_codes": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "string", "description": "The specific data (codes, values) that supports this finding"},
        "explanation": {"type": "string", "description": "Plain-language explanation for a non-mechanic"},
        "likely_causes": {"type": "array", "items": _CAUSE},
        "tests": {"type": "array", "items": {"type": "string"},
                  "description": "Ordered tests to confirm the cause, cheapest first, with expected values"},
        "fixes": {"type": "array", "items": {"type": "string"}},
        "diy_difficulty": {"type": "string", "enum": ["easy", "moderate", "hard", "workshop_only"]},
        "urgency": {"type": "string", "enum": ["immediately", "soon", "next_service", "monitor"]},
    },
    "required": ["title", "severity", "system", "related_codes", "evidence", "explanation",
                 "likely_causes", "tests", "fixes", "diy_difficulty", "urgency"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "overall_status": {"type": "string", "enum": ["good", "attention", "problems", "critical"]},
        "overall_assessment": {"type": "string",
                               "description": "3-6 sentence summary of the car's condition and the main priorities"},
        "safe_to_drive": {"type": "string", "enum": ["yes", "yes_with_caution", "no"]},
        "safe_to_drive_reason": {"type": "string"},
        "findings": {"type": "array", "items": _FINDING,
                     "description": "Ordered from most to least important"},
        "watch_list": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"item": {"type": "string"}, "why": {"type": "string"},
                               "how_to_monitor": {"type": "string"}},
                "required": ["item", "why", "how_to_monitor"],
                "additionalProperties": False,
            },
        },
        "data_gaps": {"type": "array", "items": {"type": "string"},
                      "description": "What this scan could not show and how to get that information"},
        "maintenance_tips": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overall_status", "overall_assessment", "safe_to_drive", "safe_to_drive_reason",
                 "findings", "watch_list", "data_gaps", "maintenance_tips"],
    "additionalProperties": False,
}


def _prepare(report: dict) -> dict:
    """English text instead of message keys; drop what does not help (scan log, raw bytes)."""
    r = resolve(report, "en")
    r.pop("scan_log", None)
    r.pop("adapter_log", None)
    for ecu in r.get("ecus", []):
        ecu.get("identification", {}).pop("raw", None)
        ecu.pop("module_raw", None)
        data = ecu.get("module_data")
        if data:  # keep the request size sane for modules with hundreds of values (groups come best first)
            budget = MODULE_VALUES_FOR_AI
            for g in data.get("groups", []):
                g["values"] = [{"name": v["item"], "value": v["value"], "unit": v.get("unit", "")}
                               for v in g["values"][:max(0, budget)]]
                budget -= len(g["values"])
            data["groups"] = [g for g in data["groups"] if g["values"]]
    return r


def analyze(report: dict, api_key: str | None = None, lang: str = "en",
            symptoms: str = "") -> dict:
    # Imported here, not at the top: the SDK takes seconds to load and is only
    # needed when the user asks for an analysis, so the app window opens faster.
    import anthropic

    lang = normalize(lang)
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    user = (
        f"Write every text field of your answer in {PROMPT_LANGUAGE[lang]}.\n\n"
        + (f"The owner describes these symptoms:\n<symptoms>\n{symptoms.strip()}\n</symptoms>\n\n"
           if symptoms.strip() else "The owner did not describe any symptoms.\n\n")
        + "Scan report (JSON):\n<scan_report>\n"
        + json.dumps(_prepare(report), ensure_ascii=False, indent=1)
        + "\n</scan_report>"
    )
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise AIError("error.ai_auth")
    except anthropic.PermissionDeniedError:
        raise AIError("error.ai_permission")
    except anthropic.RateLimitError:
        raise AIError("error.ai_rate_limit")
    except anthropic.BadRequestError as e:
        raise AIError("error.ai_bad_request", detail=e.message)
    except anthropic.APIStatusError as e:
        raise AIError("error.ai_server", status=e.status_code)
    except anthropic.APIConnectionError:
        raise AIError("error.ai_connection")

    if msg.stop_reason == "refusal":
        raise AIError("error.ai_refusal")
    if msg.stop_reason == "max_tokens":
        raise AIError("error.ai_truncated")
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise AIError("error.ai_format")
    result["_model"] = msg.model
    result["_language"] = lang
    result["_usage"] = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
    return result
