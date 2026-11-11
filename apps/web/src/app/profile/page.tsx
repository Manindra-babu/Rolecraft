"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ExternalLink, KeyRound, ShieldAlert, Trash2 } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { Alert, Badge, Button, Card, Input, Skeleton } from "@/components/ui";
import { apiFetch, ApiError, GeminiKeyStatus, MasterProfile } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function ProfilePage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { user, isLoading, refreshUser } = useAuth();
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [keyError, setKeyError] = useState<string | null>(null);
  const [keySuccess, setKeySuccess] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoading && !user) {
      router.replace("/login");
    }
  }, [isLoading, user, router]);

  const { data: keyStatus, isLoading: keyLoading } = useQuery<GeminiKeyStatus>({
    queryKey: ["gemini-key-status"],
    queryFn: () => apiFetch<GeminiKeyStatus>("/profile/api-key"),
    enabled: Boolean(user),
  });

  const { data: profile } = useQuery<MasterProfile>({
    queryKey: ["master-profile"],
    queryFn: () => apiFetch<MasterProfile>("/profile"),
    enabled: Boolean(user),
  });

  const saveKeyMutation = useMutation({
    mutationFn: (api_key: string) =>
      apiFetch<GeminiKeyStatus>("/profile/api-key", {
        method: "PUT",
        body: JSON.stringify({ api_key }),
      }),
    onSuccess: async () => {
      setApiKeyInput("");
      setKeyError(null);
      setKeySuccess("Gemini API key verified and saved securely.");
      await queryClient.invalidateQueries({ queryKey: ["gemini-key-status"] });
      await refreshUser();
    },
    onError: (err) => {
      setKeySuccess(null);
      setKeyError(
        err instanceof ApiError
          ? err.message
          : "Unable to verify Gemini API key."
      );
    },
  });

  const deleteKeyMutation = useMutation({
    mutationFn: () =>
      apiFetch<GeminiKeyStatus>("/profile/api-key", { method: "DELETE" }),
    onSuccess: async () => {
      setKeyError(null);
      setKeySuccess("Gemini API key removed from your account.");
      await queryClient.invalidateQueries({ queryKey: ["gemini-key-status"] });
      await refreshUser();
    },
  });

  if (isLoading || !user) {
    return (
      <div className="mx-auto max-w-5xl space-y-6 px-4 py-12">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const handleSaveKey = (e: React.FormEvent) => {
    e.preventDefault();
    setKeyError(null);
    setKeySuccess(null);
    if (!apiKeyInput.trim()) {
      setKeyError("Paste your Gemini API key before saving.");
      return;
    }
    saveKeyMutation.mutate(apiKeyInput.trim());
  };

  return (
    <AppShell>
      <div className="space-y-1">
        <h1 className="font-serif text-3xl font-semibold text-ink">
          Profile &amp; API Key
        </h1>
        <p className="text-sm text-ink-muted">
          Manage your personal Gemini API key and review your Master Profile.
        </p>
      </div>

      <section id="api-key" className="grid gap-6 lg:grid-cols-3">
        <Card className="space-y-5 lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <KeyRound className="h-5 w-5 text-accent" aria-hidden="true" />
              <h2 className="text-lg font-semibold">Gemini API Key</h2>
            </div>
            {keyLoading ? (
              <Skeleton className="h-5 w-24" />
            ) : keyStatus?.status === "valid" ? (
              <Badge tone="success">
                <CheckCircle2 className="h-3 w-3" aria-hidden="true" />
                Verified
              </Badge>
            ) : keyStatus?.status === "needs_attention" ? (
              <Badge tone="danger">
                <ShieldAlert className="h-3 w-3" aria-hidden="true" />
                Needs attention
              </Badge>
            ) : (
              <Badge tone="warning">Not configured</Badge>
            )}
          </div>

          {keyError && <Alert tone="danger">{keyError}</Alert>}
          {keySuccess && <Alert tone="success">{keySuccess}</Alert>}

          {keyStatus?.has_key && (
            <div className="flex flex-wrap items-center justify-between gap-4 rounded-md border border-border bg-canvas px-4 py-3">
              <div className="space-y-0.5">
                <p className="text-xs font-medium uppercase tracking-wider text-ink-muted">
                  Saved key
                </p>
                <p className="font-mono text-sm font-medium text-ink">
                  {keyStatus.masked_key}
                </p>
                {keyStatus.added_at && (
                  <p className="text-xs text-ink-muted">
                    Added {new Date(keyStatus.added_at).toLocaleDateString()} &middot;{" "}
                    {keyStatus.calls_month} calls this month ({keyStatus.calls_today}/
                    {keyStatus.daily_limit} today)
                  </p>
                )}
              </div>
              <Button
                variant="danger"
                size="sm"
                onClick={() => deleteKeyMutation.mutate()}
                disabled={deleteKeyMutation.isPending}
              >
                <Trash2 className="h-4 w-4" aria-hidden="true" />
                Remove key
              </Button>
            </div>
          )}

          <form onSubmit={handleSaveKey} className="space-y-3">
            <Input
              label={keyStatus?.has_key ? "Replace Gemini API key" : "Paste your Gemini API key"}
              type="password"
              name="api_key"
              placeholder="AIzaSy..."
              value={apiKeyInput}
              onChange={(e) => setApiKeyInput(e.target.value)}
              autoComplete="off"
            />
            <div className="flex items-center gap-3">
              <Button type="submit" disabled={saveKeyMutation.isPending}>
                {saveKeyMutation.isPending
                  ? "Verifying with Google..."
                  : "Verify & Save Key"}
              </Button>
            </div>
          </form>

          <p className="rounded-md border border-border bg-canvas p-3 text-xs leading-relaxed text-ink-muted">
            Your key is stored encrypted and used only to process your own
            resumes. Requests go to Google under your own Gemini account and
            its terms, so free-tier keys may be subject to Google&apos;s
            data-use policy.
          </p>
        </Card>

        <Card className="space-y-4">
          <h3 className="text-base font-semibold">
            How to get your Gemini API key
          </h3>
          <ol className="list-decimal space-y-2.5 pl-4 text-sm text-ink-muted">
            <li>
              Open{" "}
              <a
                href="https://aistudio.google.com/apikey"
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 font-medium text-accent hover:underline"
              >
                Google AI Studio
                <ExternalLink className="h-3 w-3" aria-hidden="true" />
              </a>{" "}
              and sign in with your Google account.
            </li>
            <li>
              Click <strong className="text-ink">Create API key</strong> and
              select or create a Google Cloud project.
            </li>
            <li>
              Copy the key starting with{" "}
              <code className="rounded bg-subtle px-1 py-0.5 text-xs">AIza</code>{" "}
              and paste it on the left. We run one quick check to confirm it
              works.
            </li>
          </ol>
          <p className="text-xs text-ink-muted">
            ResumeIQ caches identical extraction prompts so repeated runs do not
            consume extra calls.
          </p>
        </Card>
      </section>

      <Card className="space-y-3">
        <h2 className="text-lg font-semibold">Master Profile Summary</h2>
        <div className="grid gap-4 text-sm sm:grid-cols-2">
          <div>
            <p className="text-xs font-medium uppercase tracking-wider text-ink-muted">
              Name
            </p>
            <p className="mt-0.5 font-medium text-ink">
              {profile?.full_name || user.full_name}
            </p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wider text-ink-muted">
              Email
            </p>
            <p className="mt-0.5 font-medium text-ink">
              {profile?.email || user.email}
            </p>
          </div>
        </div>
        <p className="text-sm text-ink-muted">
          Your Master Profile is automatically populated when you upload your
          first resume in Phase 1 and stays editable here.
        </p>
      </Card>
    </AppShell>
  );
}
