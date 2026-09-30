"""Request and response models for the merchant payment API."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from libs.money import MAX_SAFE_INTEGER
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Vpa = Annotated[
    str,
    StringConstraints(
        min_length=4,
        max_length=130,
        pattern=r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$",
    ),
]


class CreatePaymentIntent(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    amount_minor: Annotated[int, Field(strict=True, gt=0, le=MAX_SAFE_INTEGER)]
    currency: Literal["INR"]
    payment_method_type: Literal["card", "upi"]
    payment_method_token: Annotated[str, Field(min_length=8, max_length=200)] | None = None
    payer_vpa: Vpa | None = None
    payee_vpa: Vpa | None = None

    @model_validator(mode="after")
    def validate_method_fields(self) -> CreatePaymentIntent:
        if self.payment_method_type == "card":
            if not self.payment_method_token or self.payer_vpa or self.payee_vpa:
                raise ValueError("card payments require a token and no VPA fields")
        elif self.payment_method_token or not self.payer_vpa or not self.payee_vpa:
            raise ValueError("UPI payments require payer and payee VPAs and no card token")
        return self


class PaymentIntentResponse(BaseModel):
    payment_id: UUID
    amount_minor: int
    currency: str
    payment_method_type: str
    status: str
    created_at: str | None = None


class ConfirmResponse(BaseModel):
    payment_id: UUID
    status: str
    next_action: str | None = None
