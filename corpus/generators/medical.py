"""Medical domain generator for clinical document corpus.

Generates realistic clinical documents (ED notes, discharge summaries,
progress notes, imaging reports, lab results, etc.) via LLM prompting.
"""

REQUIRED_SCENARIO_KEYS = {"scenario_id", "title", "doc_type", "setting", "weight"}

STAGE1_PROMPT = """\
You are a health informatics expert. Generate a JSON array of medical document \
scenarios that represent the mix of clinical documents in a typical US hospital \
EHR system. For each scenario provide:

- "scenario_id": short snake_case identifier
- "title": human-readable title
- "doc_type": the clinical document type (e.g. "ED Note", "Discharge Summary")
- "setting": clinical setting (e.g. "Emergency Department", "Primary Care")
- "weight": estimated proportion of all clinical documents (floats summing to 1.0)

Include at least 12 scenario types spanning inpatient, outpatient, emergency, \
surgical, imaging, lab, specialty, and behavioral health settings.

Return ONLY the JSON array, no markdown fences, no explanation."""

STAGE2_TEMPLATE = """\
Write a complete, realistic {doc_type} for a fictional patient.

Setting: {setting}
Scenario: {title}
Target length: approximately {token_target} tokens (write a thorough, detailed document).

Requirements:
- Invent realistic but fictional patient demographics, history, findings, and plans.
- Use proper medical terminology, abbreviations, and formatting conventions \
that would appear in an actual EHR system.
- Include all standard sections for this document type, but vary their ordering \
and depth naturally. Not every document follows the same rigid template.
- Include realistic vital signs, lab values with units and reference ranges, \
medication names with dosages, routes, and frequencies.
- Vary your writing style: some sections should be terse shorthand (e.g. \
"A&Ox3, NAD, PERRL"), others narrative paragraphs, others structured lists.
- Use a mix of abbreviations and spelled-out forms as a real clinician would.
- Do NOT include any meta-commentary. Output only the document itself.
- Write continuously until you have produced a detailed, complete document \
of approximately {token_target} tokens.
- This is document #{doc_number} for this scenario. Use a different patient, \
different chief complaint, different findings, and different clinical course \
than any other document in this batch."""

CONTINUATION_TEMPLATE = """\
Continue writing the {doc_type} from exactly where you left off. \
Do NOT repeat any content already written. Keep the same patient, \
same case, same formatting. Write at least {remaining_tokens} more tokens \
of additional clinical detail: expand on findings, add more lab results, \
imaging reads, nursing notes, consult responses, or follow-up documentation. \
Output ONLY the continuation text, no preamble."""
