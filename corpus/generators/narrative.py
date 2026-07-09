"""Narrative domain generator (stub).

To implement: define STAGE1_PROMPT, STAGE2_TEMPLATE, CONTINUATION_TEMPLATE,
and REQUIRED_SCENARIO_KEYS following the same interface as medical.py.

Example document types: short stories, news articles, technical writing,
academic papers, blog posts, dialogue-heavy fiction.
"""

REQUIRED_SCENARIO_KEYS = {"scenario_id", "title", "doc_type", "setting", "weight"}

STAGE1_PROMPT = None  # Not yet implemented
STAGE2_TEMPLATE = None
CONTINUATION_TEMPLATE = None
