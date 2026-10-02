"""`spoken_amount` reads rates the way a dispatcher says them."""

from __future__ import annotations

import pytest

from app.api.phrasing import spoken_amount


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (1050, "ten fifty, that's one thousand fifty dollars"),
        (1500, "fifteen hundred dollars"),
        (975, "nine seventy-five dollars"),
        (2050, "twenty fifty, that's two thousand fifty dollars"),
        (2225, "twenty-two twenty-five, that's two thousand two hundred twenty-five dollars"),
        (2400, "twenty-four hundred dollars"),
        (100, "one hundred dollars"),
        (25, "twenty-five dollars"),
        (5125, "fifty-one twenty-five, that's five thousand one hundred twenty-five dollars"),
    ],
)
def test_spoken_amount(amount, expected):
    assert spoken_amount(amount) == expected
