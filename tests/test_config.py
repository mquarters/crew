"""Configuration is the substrate everything else trusts, so it is checked for
internal consistency rather than merely for being parseable."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from crew_org.config import CONFIG_DIR, load_org

ROOT = Path(__file__).resolve().parents[1]


def tracked_files() -> list[Path]:
    """Files git actually tracks.

    The secret scans must look here and nowhere else: .env is gitignored and
    holds a real token by design, so walking the filesystem would fail on a
    correctly configured checkout.
    """
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [ROOT / name for name in out.split("\0") if name]


AGENTS = CONFIG_DIR / "agents.yaml"
LITELLM = ROOT / "deploy" / "litellm" / "config.yaml"
CONSTITUTION = ROOT / "docs" / "ways-of-working.md"


@pytest.fixture(scope="module")
def agents() -> dict:
    return yaml.safe_load(AGENTS.read_text())


def test_org_config_loads_and_validates():
    org = load_org()
    assert org["board"]["columns"][0] == "Inbox (Goals)"
    assert org["board"]["columns"][-1] == "Done"


def test_blocked_is_off_flow_not_a_column():
    org = load_org()
    assert org["board"]["blocked_column"] not in org["board"]["columns"]


def test_wip_limits_only_name_real_columns():
    org = load_org()
    assert set(org["wip_limits"]) <= set(org["board"]["columns"])


def test_every_agent_llm_alias_exists_in_the_litellm_config(agents):
    """A typo'd alias would otherwise surface as a confusing runtime failure."""
    declared = {m["model_name"] for m in yaml.safe_load(LITELLM.read_text())["model_list"]}
    used = {a["llm"] for a in agents.values()}
    assert used <= declared, f"undeclared model aliases: {sorted(used - declared)}"


def test_no_anthropic_api_key_anywhere_in_the_repo():
    """The cost guarantee is structural: escalation must not be able to bill."""
    # Assembled at runtime so this test's own source is not a false positive.
    needle = "ANTHROPIC_API_" + "KEY="
    # The pre-commit hook's whole job is to grep for this string, so it is the
    # one file expected to contain it.
    detector = ROOT / "scripts" / "pre-commit"
    offenders = []
    for path in tracked_files():
        if not path.is_file() or path == detector:
            continue
        text = path.read_text(errors="ignore")
        # The docs explain the absence; an actual assignment is the problem.
        if needle in text and "no anthropic_api_key" not in text.lower():
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"{needle} assigned in: {offenders}"


def test_every_role_in_the_constitution_has_an_agent_definition(agents):
    """The constitution's authority table and the agent roster must not drift."""
    text = CONSTITUTION.read_text()
    roles = {a["role"] for a in agents.values()}
    missing = [r for r in roles if r not in text]
    assert not missing, f"roles absent from the constitution: {missing}"


def test_agents_declare_the_fields_crewai_requires(agents):
    for key, agent in agents.items():
        for field in ("role", "goal", "backstory", "llm"):
            assert agent.get(field), f"{key} is missing {field}"


def test_no_credential_shaped_strings_in_the_repo():
    """CI enforcement of what scripts/pre-commit blocks locally.

    Deliberately narrow: short placeholders like "sk-not-used" are fine,
    real-length tokens are not.
    """
    import re

    pattern = re.compile(
        r"sk-ant-[a-zA-Z0-9-]{20,}"
        r"|sk-[a-zA-Z0-9]{32,}"
        r"|gh[pousr]_[a-zA-Z0-9]{30,}"
        r"|github_pat_[a-zA-Z0-9_]{20,}"
        r"|AKIA[0-9A-Z]{16}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY"
    )
    offenders = []
    for path in tracked_files():
        if not path.is_file() or path.name == Path(__file__).name:
            continue
        if pattern.search(path.read_text(errors="ignore")):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"credential-shaped strings in: {offenders}"


def test_secret_hygiene_rules_are_actually_installed():
    assert (ROOT / "scripts" / "pre-commit").exists(), "secret-blocking hook is missing"
    gitignore = (ROOT / ".gitignore").read_text()
    for rule in (".env", "*.pem", "*.key"):
        assert rule in gitignore, f".gitignore does not exclude {rule}"
    assert "!.env.example" in gitignore, ".env.example must stay committed as the template"


def test_an_unrecognised_sandbox_mode_fails_at_startup(tmp_path):
    """A typo would otherwise silently disable the sandbox."""
    import yaml

    from crew_org.config import load_org

    org = yaml.safe_load((CONFIG_DIR / "org.yaml").read_text())
    org["sandbox"]["mode"] = "requried"
    path = tmp_path / "org.yaml"
    path.write_text(yaml.safe_dump(org))
    with pytest.raises(ValueError, match="sandbox.mode must be"):
        load_org(path)


def test_the_shipped_config_sandboxes_by_default():
    assert load_org()["sandbox"]["mode"] == "required"
