from __future__ import annotations

import sys

import pytest

pytest.importorskip("kopf")


def test_module_entrypoint_loads_existing_handlers_and_starts_kopf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lab_operator.__main__ as operator_main

    assert "lab_operator.handlers" in sys.modules
    started: list[bool] = []
    monkeypatch.setattr(operator_main.kopf, "run", lambda: started.append(True))

    operator_main.main()

    assert started == [True]
