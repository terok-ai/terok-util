# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Vulture whitelist — suppress false-positive dead-code findings.

terok-util is a foundation library — every public symbol is consumed
by sibling packages that vulture cannot see when run on this repo
alone.  Entries are added on a case-by-case basis when a real
false-positive surfaces; the file is intentionally empty otherwise.
"""
