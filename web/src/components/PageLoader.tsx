import { Suspense, type ComponentType } from "react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

interface PageLoaderProps {
  children?: React.ReactNode;
}

export function PageLoader({ children }: PageLoaderProps) {
  return (
    <div className="flex h-full w-full items-center justify-center p-8">
      <div className="flex flex-col items-center gap-3 text-muted-foreground">
        <Spinner className="h-8 w-8" />
        <span className="text-sm">Loading page…</span>
        {children}
      </div>
    </div>
  );
}

export function withSuspense<T extends object>(
  Component: ComponentType<T>,
  fallback?: React.ReactNode,
) {
  return function SuspenseWrapper(props: T) {
    return (
      <Suspense fallback={fallback ?? <PageLoader />}>
        <Component {...props} />
      </Suspense>
    );
  };
}
