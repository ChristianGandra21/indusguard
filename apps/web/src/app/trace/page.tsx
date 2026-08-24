import { Suspense } from "react";

import { LoadingState } from "@/components/data-state";

import { TraceContent } from "./trace-content";

export default function TracePage() {
  return (
    <Suspense fallback={<LoadingState label="Preparando busca de trace" />}>
      <TraceContent />
    </Suspense>
  );
}
