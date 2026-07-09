"""Domain generator registry.

Each domain module must expose:
  STAGE1_PROMPT: str          — prompt to generate scenario JSON array
  STAGE2_TEMPLATE: str        — prompt template for document generation
  CONTINUATION_TEMPLATE: str  — prompt template for continuation rounds
  REQUIRED_SCENARIO_KEYS: set — required keys in each scenario dict
"""

from corpus.generators import legal, medical, narrative

DOMAINS = {
    "legal": legal,
    "medical": medical,
    "narrative": narrative,
}


def get_domain(name: str):
    """Return the generator module for the given domain name."""
    if name not in DOMAINS:
        available = ", ".join(sorted(DOMAINS.keys()))
        raise ValueError(f"Unknown domain '{name}'. Available: {available}")
    return DOMAINS[name]


def list_domains() -> list:
    """Return sorted list of available domain names."""
    return sorted(DOMAINS.keys())
