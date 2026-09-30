"use client";

import { useParams } from "next/navigation";
import { PaymentDetailView } from "@/components/payment-detail";

export default function OpsPaymentPage() {
  const { id } = useParams<{ id: string }>();
  return <PaymentDetailView paymentId={id} source="ops" canRefund={false} />;
}
