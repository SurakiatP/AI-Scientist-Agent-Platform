import pytest

from scilab.skill_catalog import get_skill_pack


def test_general_research_returns_the_approved_pinned_skills() -> None:
    assert get_skill_pack("general-research") == [
        "paper-lookup",
        "scientific-writing",
        "peer-review",
        "experimental-design",
        "statistical-analysis",
        "scientific-critical-thinking",
        "hypothesis-generation",
    ]


def test_unknown_skill_pack_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported skill pack"):
        get_skill_pack("all-research-skills")
