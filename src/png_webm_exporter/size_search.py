import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_FLOOR


def megabytes_to_bytes(value: str) -> int:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0:
            raise ValueError
        result = int((amount * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
        if result < 1:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise ValueError("Enter a positive target size in MB (1 MB = 1,000,000 bytes).") from error


@dataclass
class SizeSearch:
    budget: int
    initial: int = 12
    results: dict[int, int] = field(default_factory=dict)
    minimum: int = 0

    def __post_init__(self) -> None:
        if self.budget <= 0 or not self.minimum <= self.initial <= 63 or not 0 <= self.minimum <= 63:
            raise ValueError("Invalid size search settings.")

    @property
    def best(self) -> int | None:
        fitting = [crf for crf, size in self.results.items() if size <= self.budget]
        return min(fitting) if fitting else None

    @property
    def preferred(self) -> int | None:
        return self.best if self.best is not None else min(self.results, key=self.results.get, default=None)

    def next_crf(self) -> int | None:
        if not self.results:
            return self.initial
        overs = [crf for crf, size in self.results.items() if size > self.budget]
        fits = [crf for crf, size in self.results.items() if size <= self.budget]
        lower = max(overs) + 1 if overs else self.minimum
        upper = min(fits) if fits else 63
        if lower > 63 or (fits and lower >= upper):
            return None
        if not fits:
            # Not bracketed yet: step up from the closest over-budget trial instead of
            # bisecting blindly to the far end of the range, so a near-miss takes a small
            # nudge rather than a huge jump (e.g. CRF 3 barely over target doesn't leap to 33).
            closest_crf = max(overs)
            ratio = self.results[closest_crf] / self.budget
            step = max(1, round(6 * math.log2(ratio)))
            return min(closest_crf + step, upper)
        candidate = (lower + upper) // 2
        return candidate if candidate not in self.results else None

    def record(self, crf: int, size: int) -> None:
        if not self.minimum <= crf <= 63 or size <= 0 or crf in self.results:
            raise ValueError("Invalid or repeated encoding trial.")
        self.results[crf] = size
