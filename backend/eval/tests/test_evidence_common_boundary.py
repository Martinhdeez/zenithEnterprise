"""Packet evaluation can load the shared evidence harness without R1 modules."""

import os
import subprocess
import sys


def test_packet_runners_do_not_import_optional_r1_trials() -> None:
    script = (
        "import sys; import eval.qasper_packet_trial, eval.contract_nli_packet_trial; "
        "assert not {'eval.lossless_trial', 'eval.qasper_trial', "
        "'eval.contract_nli_trial'} & sys.modules.keys()"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and fixed test script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
