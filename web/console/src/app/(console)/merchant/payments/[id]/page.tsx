"use client";

import { useParams } from "next/navigation";
import { hasAny, useSession } from "@/components/providers";
import { PaymentDetailView } from "@/components/payment-detail";

export default function PaymentPage() {
  const { id } = useParams<{ id: string }>();
  const session = useSession();
  return (
    <PaymentDetailView
      paymentId={id}
      source="merchant"
      canRefund={hasAny(session.data?.roles, ["merchant_admin", "merchant_developer"])}
    />
  );
}
