from __future__ import annotations

from unittest import mock

from noalpha.cli import build_parser, main


def test_demo_leakage_parser_exposes_measurement_entrypoint() -> None:
    args = build_parser().parse_args(
        ["demo", "leakage", "--market", "us", "--top-k", "3"]
    )
    assert args.command == "demo"
    assert args.demo_command == "leakage"
    assert args.market == "us"
    assert args.top_k == 3


def test_demo_leakage_dispatches_to_packaged_demo() -> None:
    with mock.patch("noalpha.demos.leakage.main", return_value=0) as run_demo:
        result = main(["demo", "leakage", "--market", "us", "--top-k", "3"])

    assert result == 0
    run_demo.assert_called_once_with(["--market", "us", "--top-k", "3"])
