# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# Includes work from:
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import datetime
from unittest import TestCase

# pylint: disable=no-name-in-module
from opentelemetry.sdk.extension.aws.trace.sampler._rate_limiter import (
    _RateLimiter,
)

from ._mock_clock import MockClock


class TestRateLimiter(TestCase):
    def test_try_spend(self):
        time_now = datetime.datetime.fromtimestamp(1707551387.0)
        clock = MockClock(time_now)
        rate_limiter = _RateLimiter(1, 30, clock)

        spent = 0
        for _ in range(0, 100):
            if rate_limiter.try_spend(1):
                spent += 1
        self.assertEqual(spent, 0)

        spent = 0
        clock.add_time(0.5)
        for _ in range(0, 100):
            if rate_limiter.try_spend(1):
                spent += 1
        self.assertEqual(spent, 15)

        spent = 0
        clock.add_time(1000)
        for _ in range(0, 100):
            if rate_limiter.try_spend(1):
                spent += 1
        self.assertEqual(spent, 30)

    def test_try_spend_zero_quota(self):
        clock = MockClock(datetime.datetime.fromtimestamp(1707551387.0))
        rate_limiter = _RateLimiter(1, 0, clock)

        clock.add_time(10)
        self.assertFalse(rate_limiter.try_spend(1))

    def test_try_spend_exact_balance_without_rounding_loss(self):
        # 1000 / 7 ms per unit is not representable as a float; a full second
        # must still pay for exactly 7 units.
        clock = MockClock(datetime.datetime.fromtimestamp(1707551387.0))
        rate_limiter = _RateLimiter(1, 7, clock)

        clock.add_time(1)
        spent = sum(1 for _ in range(100) if rate_limiter.try_spend(1))
        self.assertEqual(spent, 7)

        clock.add_time(3)
        spent = sum(1 for _ in range(100) if rate_limiter.try_spend(1))
        self.assertEqual(spent, 7)

    def test_try_spend_balance_capped_at_max_balance(self):
        clock = MockClock(datetime.datetime.fromtimestamp(1707551387.0))
        rate_limiter = _RateLimiter(2, 5, clock)

        clock.add_time(60)
        spent = sum(1 for _ in range(100) if rate_limiter.try_spend(1))
        self.assertEqual(spent, 10)

    def test_try_spend_fractional_cost(self):
        clock = MockClock(datetime.datetime.fromtimestamp(1707551387.0))
        rate_limiter = _RateLimiter(1, 4, clock)

        clock.add_time(1)
        self.assertTrue(rate_limiter.try_spend(2.5))
        self.assertTrue(rate_limiter.try_spend(1.5))
        self.assertFalse(rate_limiter.try_spend(0.25))
