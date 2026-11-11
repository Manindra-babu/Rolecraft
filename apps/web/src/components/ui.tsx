import React from "react";
import { AlertCircle, AlertTriangle, CheckCircle2, Info } from "lucide-react";

export function Button({
  children,
  variant = "primary",
  size = "md",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
}) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-md font-medium transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-50";
  const sizes = {
    sm: "px-3 py-1.5 text-sm",
    md: "px-4 py-2 text-sm",
  };
  const variants = {
    primary: "bg-accent text-white hover:bg-accent-hover",
    secondary:
      "border border-border bg-surface text-ink hover:bg-subtle",
    ghost: "bg-transparent text-ink-muted hover:bg-subtle hover:text-ink",
    danger:
      "border border-status-danger/30 bg-status-dangerBg text-status-danger hover:bg-red-100",
  };
  return (
    <button
      className={`${base} ${sizes[size]} ${variants[variant]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

export function Card({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-lg border border-border bg-surface p-6 ${className}`}
    >
      {children}
    </div>
  );
}

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement> & {
    label?: string;
    error?: string;
    hint?: string;
  }
>(function Input({ label, error, hint, id, className = "", ...props }, ref) {
  const inputId = id || props.name;
  return (
    <div className="space-y-1.5">
      {label && (
        <label
          htmlFor={inputId}
          className="block text-sm font-medium text-ink"
        >
          {label}
        </label>
      )}
      <input
        ref={ref}
        id={inputId}
        className={`w-full rounded-md border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 transition-colors duration-150 ${
          error ? "border-status-danger" : "border-border focus:border-accent"
        } ${className}`}
        {...props}
      />
      {hint && !error && (
        <p className="text-xs text-ink-muted">{hint}</p>
      )}
      {error && (
        <p className="text-xs text-status-danger" role="alert">
          {error}
        </p>
      )}
    </div>
  );
});

export function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "accent" | "success" | "warning" | "danger";
}) {
  const tones = {
    neutral: "border-border bg-subtle text-ink-muted",
    accent: "border-accent/20 bg-accent-soft text-accent",
    success: "border-status-success/20 bg-status-successBg text-status-success",
    warning: "border-status-warning/20 bg-status-warningBg text-status-warning",
    danger: "border-status-danger/20 bg-status-dangerBg text-status-danger",
  };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export function Alert({
  tone = "info",
  title,
  children,
  action,
}: {
  tone?: "info" | "success" | "warning" | "danger";
  title?: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  const styles = {
    info: "border-accent/25 bg-accent-soft text-ink",
    success: "border-status-success/25 bg-status-successBg text-ink",
    warning: "border-status-warning/30 bg-status-warningBg text-ink",
    danger: "border-status-danger/30 bg-status-dangerBg text-ink",
  };
  const icons = {
    info: <Info className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />,
    success: (
      <CheckCircle2
        className="h-4 w-4 shrink-0 text-status-success"
        aria-hidden="true"
      />
    ),
    warning: (
      <AlertTriangle
        className="h-4 w-4 shrink-0 text-status-warning"
        aria-hidden="true"
      />
    ),
    danger: (
      <AlertCircle
        className="h-4 w-4 shrink-0 text-status-danger"
        aria-hidden="true"
      />
    ),
  };

  return (
    <div
      role="status"
      className={`flex flex-col gap-3 rounded-lg border p-4 sm:flex-row sm:items-center sm:justify-between ${styles[tone]}`}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5">{icons[tone]}</div>
        <div className="space-y-0.5 text-sm">
          {title && <p className="font-semibold text-ink">{title}</p>}
          <div className="text-ink-muted">{children}</div>
        </div>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

export function Skeleton({ className = "h-5 w-full" }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-subtle ${className}`} />;
}
