"""Exact conservative monetary admission shared by text providers."""

from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, localcontext


class TokenBudget:
    @property
    def reserved_tokens(self):
        return self.context_window_tokens + self.max_output_tokens

    @property
    def reservation_cost(self):
        return self.reservation_total(1)

    def reservation_total(self, count):
        return self.token_cost(
            count * self.context_window_tokens, count * self.max_output_tokens
        )

    def token_cost(self, input_tokens, output_tokens, denominator=1):
        # Money has at most eight fractional digits. Allow for aligning those
        # scales, integer multiplication and the carry from addition. Division
        # by a million terminates exactly. Do not inherit host exponent limits,
        # precision, traps or flags, and keep ALL arithmetic inside this scope.
        precision = (
            max(len(self.input_per_million), len(self.output_per_million))
            + max(len(str(input_tokens)), len(str(output_tokens)))
            + 16
        )
        with localcontext(Context(prec=precision, Emax=MAX_EMAX, Emin=MIN_EMIN)):
            return (
                Decimal(self.input_per_million) * input_tokens
                + Decimal(self.output_per_million) * output_tokens
            ) / (1000000 * denominator)

    def admit(self, count):
        if (
            count > self.max_requests
            or count * self.reserved_tokens > self.max_total_tokens
            or self.reservation_total(count) > Decimal(self.max_cost)
        ):
            raise ValueError("admission_exhausted")
