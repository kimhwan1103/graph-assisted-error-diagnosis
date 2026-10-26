"""Verify portable example reconstruction without invoking external services."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from replay_example import PROGRAMS, replay


@pytest.mark.parametrize("program", PROGRAMS)
def test_public_example_reconstructs_original_packets(program, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Publication examples must not call a provider or Docker")

    monkeypatch.setattr(
        "ai_error_check_agent.experiments.backends.GeminiBackend.generate", forbidden
    )
    monkeypatch.setattr(
        "ai_error_check_agent.experiments.backends.GeminiBackend.preflight", forbidden
    )
    monkeypatch.setattr("remediation_cross_program_docker_v4.CrossReplay.initialize", forbidden)
    monkeypatch.setattr("remediation_cross_program_docker_v4.CrossReplay.replay", forbidden)
    result = replay(program)
    assert result["saved_input_matches"] and result["saved_candidate_matches"]
    assert result["shacl_conforms"] and result["automatic_contract"] == "accept"
    assert result["model_calls"] == result["docker_calls"] == 0
