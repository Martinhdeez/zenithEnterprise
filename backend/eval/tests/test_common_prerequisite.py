"""The shared evidence harness loads before optional R1 or packet trials."""

import subprocess
import sys


def test_neutral_helpers_import_without_optional_trials() -> None:
    script = (
        "import sys; import eval.evidence_common, eval.qasper_common, "
        "eval.contract_nli_common; "
        "assert not {'eval.lossless_trial', 'eval.qasper_trial', "
        "'eval.contract_nli_trial'} & sys.modules.keys()"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and fixed code
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
