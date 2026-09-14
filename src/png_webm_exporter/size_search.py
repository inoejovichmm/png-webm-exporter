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
    max_trials: int = 9

    def __post_init__(self) -> None:
        if self.budget <= 0 or not 0 <= self.initial <= 63:
            raise ValueError("Invalid size search settings.")

    @property
    def best(self) -> int | None:
        fitting = [crf for crf, size in self.results.items() if size <= self.budget]
        return min(fitting) if fitting else None

    def next_crf(self) -> int | None:
        if len(self.results) >= self.max_trials:
            return None
        if not self.results:
            return self.initial
        overs = [crf for crf, size in self.results.items() if size > self.budget]
        fits = [crf for crf, size in self.results.items() if size <= self.budget]
        lower = max(overs) + 1 if overs else 0
        upper = min(fits) if fits else 63
        if lower > 63 or (fits and lower >= upper):
            return None
        candidate = (lower + upper) // 2
        return candidate if candidate not in self.results else None

    def record(self, crf: int, size: int) -> None:
        if not 0 <= crf <= 63 or size <= 0 or crf in self.results:
            raise ValueError("Invalid or repeated encoding trial.")
        self.results[crf] = size
