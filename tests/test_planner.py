import pytest

from local_agent.core.planner import Planner


@pytest.fixture
def planner():
    return Planner(model_manager=None, max_plan_steps=16)


def test_parse_valid_json_plan(planner):
    content = """
    {
        "plan": [
            "Search for three reliable sources",
            "Read the sources",
            "Create the DOCX",
            "Verify the document"
        ],
        "reasoning": "Research followed by document creation."
    }
    """

    plan, reasoning = planner._parse_plan(content)

    assert len(plan) == 4
    assert plan[0] == "Search for three reliable sources"
    assert plan[3] == "Verify the document"
    assert reasoning == "Research followed by document creation."


def test_parse_numbered_list(planner):
    content = """
    1. Search for reliable sources
    2. Collect relevant information
    3. Create a DOCX document
    4. Verify the document
    """

    plan, reasoning = planner._parse_plan(content)

    assert len(plan) == 4
    assert plan[0] == "Search for reliable sources"
    assert plan[2] == "Create a DOCX document"
    assert "numbered-list" in reasoning


def test_parse_bulleted_list(planner):
    content = """
    - Search for sources
    - Collect information
    - Create the document
    """

    plan, _ = planner._parse_plan(content)

    assert len(plan) == 3


def test_reject_empty_plan(planner):
    content = '{"plan": [], "reasoning": "Nothing planned"}'

    with pytest.raises(ValueError, match="empty or invalid plan"):
        planner._parse_plan(content)


def test_reject_invalid_plain_text(planner):
    content = "Something went wrong. I cannot create a plan."

    with pytest.raises(ValueError, match="empty or invalid plan"):
        planner._parse_plan(content)


def test_respect_maximum_plan_steps(planner):
    content = """
    {
        "plan": [
            "Step 1",
            "Step 2",
            "Step 3",
            "Step 4",
            "Step 5"
        ],
        "reasoning": "Test plan"
    }
    """

    limited_planner = Planner(model_manager=None, max_plan_steps=3)

    plan, _ = limited_planner._parse_plan(content)

    assert len(plan) == 3
    assert plan == ["Step 1", "Step 2", "Step 3"]