# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# Includes work from:
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import math
from threading import Lock

# pylint: disable=no-name-in-module
from opentelemetry.sdk.extension.aws.trace.sampler._clock import _Clock

_MICROS_PER_SECOND = 1_000_000


class _RateLimiter:  # pyright: ignore[reportUnusedClass]
    def __init__(self, max_balance_in_seconds: int, quota: int, clock: _Clock):
        # Wallet amounts are integer microseconds multiplied by quota, so one
        # unit of cost is exactly _MICROS_PER_SECOND and no division is needed.
        self._clock = clock
        self._quota = quota
        self._max_balance = max_balance_in_seconds * _MICROS_PER_SECOND * quota
        self.__wallet_floor = self._now()
        # current "wallet_balance" would be ceiling - floor

        self.__lock = Lock()

    def _now(self) -> int:
        micros = round(self._clock.now().timestamp() * _MICROS_PER_SECOND)
        return micros * self._quota

    def try_spend(self, cost: float) -> bool:
        if self._quota == 0:
            return False

        cost_scaled = math.ceil(cost * _MICROS_PER_SECOND)
        wallet_ceiling = self._now()

        with self.__lock:
            current_balance = min(
                wallet_ceiling - self.__wallet_floor, self._max_balance
            )
            pending_remaining_balance = current_balance - cost_scaled
            if pending_remaining_balance >= 0:
                self.__wallet_floor = (
                    wallet_ceiling - pending_remaining_balance
                )
                return True
            # No changes to the wallet state
            return False
