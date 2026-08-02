#!/usr/bin/env bash
#
# No shipped dependency under a copyleft or non-commercial licence. This fails rather
# than warns: an AGPL package in the tree is a legal problem, not a style nit.
#
# Scoped to what we actually ship. The evaluation harness pulls PyTorch, and on linux
# PyTorch pulls the proprietary NVIDIA CUDA wheels — an unscoped check failed the build
# over a laboratory dependency that never reaches a customer. It passed on macOS, where
# those wheels do not exist, which is why CI caught it and `make check` did not.
#
# One script rather than two copies, because the Makefile claimed to mirror CI exactly
# and the drift between them is what let that failure through.
set -euo pipefail

cd "$(dirname "$0")/../backend"

# The shipped closure: production dependencies with their transitive dependencies.
shipped=$(uv export --no-dev --no-hashes --no-emit-project |
    grep -E '^[a-zA-Z0-9]' | sed 's/[=;[].*//' | tr '\n' ' ')

# shellcheck disable=SC2086  # deliberately word-split into one argument per package
uv run pip-licenses --packages $shipped --fail-on="GNU General Public License v3 (GPLv3);\
GNU Affero General Public License v3;GNU Affero General Public License v3 or later (AGPLv3+);\
GNU General Public License v2 (GPLv2);Other/Proprietary License"
