"""Legal domain generator for legal document corpus.

Generates realistic legal documents (contracts, motions, complaints,
briefs, court opinions, corporate agreements, etc.) via LLM prompting.
"""

REQUIRED_SCENARIO_KEYS = {
    "scenario_id", "title", "doc_type", "setting", "weight",
    "context", "parties",
}

STAGE1_PROMPT = """\
You are a legal informatics expert. Generate a JSON array of legal document \
scenarios that represent a diverse mix of legal documents produced across \
different practice areas and jurisdictions. For each scenario provide:

- "scenario_id": short snake_case identifier (e.g. "commercial_contract", "federal_motion")
- "title": short descriptive title
- "doc_type": the legal document type (e.g. "Contract", "Motion", "Complaint", \
"Legal Brief", "Regulation", "Policy Document", "Corporate Agreement", \
"Court Opinion", "Deposition Summary", "Legal Memorandum")
- "setting": jurisdiction (e.g. "United States federal court", \
"California state court", "European regulatory body", \
"Corporate internal policy", "International arbitration")
- "context": 2-3 sentences describing the legal context and factual background
- "parties": the parties involved (companies, individuals, agencies) as a short string
- "weight": estimated proportion of this document type in a diverse legal corpus \
(floats summing to 1.0)

Include at least 10 scenario types spanning:
- Litigation (motions, complaints, briefs, court opinions)
- Transactional (contracts, corporate agreements, NDAs)
- Regulatory (regulations, policy documents, compliance memoranda)
- Advisory (legal memoranda, deposition summaries)

All scenarios must be plausible but fictional. Do NOT reference real people \
or real cases.

Return ONLY the JSON array, no markdown fences, no explanation."""

STAGE2_TEMPLATE = """\
Write a complete, realistic legal document based on the scenario below.

Document type: {doc_type}
Title: {title}
Jurisdiction: {setting}
Context: {context}
Parties: {parties}
Target length: approximately {token_target} tokens.

Requirements:
- Write in formal legal language appropriate to the document type.
- Use dense legal phrasing typical of real legal writing.
- Use long paragraphs and complex sentence structures.
- Include internal references such as sections, clauses, definitions, \
or prior statements.
- The document should include a mixture of narrative argument, clauses, \
and structured sections.

Structural variation requirements:
- Section headings should vary between documents.
- Section numbering style should vary between documents \
(e.g. 1.1, Article I, Section A, (a)(i)).
- Avoid repeating identical templates or clause blocks.

Content realism:
- Use the fictional companies, agencies, or individuals from the scenario.
- Do not reference real cases, real statutes by exact citation, or real people.
- Do not include meta-commentary. Output only the document itself.
- Write continuously until the document reaches approximately \
{token_target} tokens.
- This is document #{doc_number} for this scenario. Ensure it differs \
from other documents generated for this scenario in structure, \
clause ordering, and specific terms."""

CONTINUATION_TEMPLATE = """\
Continue writing the {doc_type} from exactly where you left off. \
Do NOT repeat any content already written. Keep the same parties, \
same case, same formatting. Write at least {remaining_tokens} more tokens \
of additional legal content: expand on arguments, add more clauses, \
definitions, recitals, representations and warranties, exhibits, \
or supplementary provisions. \
Output ONLY the continuation text, no preamble."""
