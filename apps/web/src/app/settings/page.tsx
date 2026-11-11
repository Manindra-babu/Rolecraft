"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Download, KeyRound, ShieldCheck, Trash2 } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { Alert, Button, Card, Skeleton } from "@/components/ui";
import { apiFetch, getStoredToken } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function SettingsPage() {
  const router = useRouter();
  const { user, isLoading, signOut } = useAuth();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [copiedToken, setCopiedToken] = useState(false);

  useEffect(() => {
    if (!isLoading && !user) router.replace("/login");
  }, [isLoading, user, router]);

  if (isLoading || !user) {
    return (
      <div className="mx-auto max-w-4xl space-y-6 px-4 py-12">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  const handleExportData = async () => {
    const data = await apiFetch<Record<string, unknown>>("/privacy/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "resumeiq_my_data.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleDeleteAccount = async () => {
    await apiFetch<void>("/privacy/account", { method: "DELETE" });
    signOut();
    router.push("/");
  };

  const handleCopyToken = async () => {
    const token = getStoredToken();
    if (!token) return;
    await navigator.clipboard.writeText(token);
    setCopiedToken(true);
    setTimeout(() => setCopiedToken(false), 2000);
  };

  return (
    <AppShell>
      <div className="space-y-1">
        <h1 className="font-serif text-3xl font-semibold text-ink">
          Settings &amp; Privacy
        </h1>
        <p className="text-sm text-ink-muted">
          Data portability, browser extension token, and full account deletion.
        </p>
      </div>

      <Card className="space-y-4">
        <div className="flex items-center gap-2">
          <KeyRound className="h-5 w-5 text-accent" aria-hidden="true" />
          <h2 className="text-lg font-semibold">Browser Extension Token</h2>
        </div>
        <p className="text-sm text-ink-muted">
          Use the ResumeIQ Chrome/Edge extension (<code>apps/extension</code>)
          to send job postings from LinkedIn, Indeed, or Glassdoor directly from
          your authenticated browser session.
        </p>
        <div>
          <Button variant="secondary" size="sm" onClick={handleCopyToken}>
            {copiedToken ? "Copied session token" : "Copy session token"}
          </Button>
        </div>
      </Card>

      <Card className="space-y-4">
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-5 w-5 text-accent" aria-hidden="true" />
          <h2 className="text-lg font-semibold">Privacy &amp; Retention</h2>
        </div>
        <ul className="list-disc space-y-1.5 pl-5 text-sm text-ink-muted">
          <li>
            Your Gemini API key is encrypted at rest and decrypted in memory
            only for your own requests.
          </li>
          <li>
            Phone numbers and street addresses are redacted before LLM rewrite
            requests when not required.
          </li>
          <li>
            ResumeIQ never logs resume contents or API keys and never trains
            models on your data.
          </li>
          <li>
            Cached job descriptions expire after 24 hours; deleting your account
            immediately deletes all uploaded files, generated outputs, and keys.
          </li>
        </ul>

        <div className="flex flex-wrap gap-3 pt-2">
          <Button variant="secondary" onClick={handleExportData}>
            <Download className="h-4 w-4" aria-hidden="true" />
            Export My Data (JSON)
          </Button>
        </div>
      </Card>

      <Card className="space-y-4 border-status-danger/30">
        <h2 className="text-lg font-semibold text-status-danger">
          Delete Account &amp; All Data
        </h2>
        <p className="text-sm text-ink-muted">
          This permanently removes your account, Master Profile, encrypted
          Gemini API key, uploaded resumes, tailored versions, and learning
          history.
        </p>

        {!confirmDelete ? (
          <Button variant="danger" onClick={() => setConfirmDelete(true)}>
            <Trash2 className="h-4 w-4" aria-hidden="true" />
            Delete my account
          </Button>
        ) : (
          <Alert
            tone="danger"
            title="Confirm permanent deletion"
            action={
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setConfirmDelete(false)}
                >
                  Cancel
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={handleDeleteAccount}
                >
                  Permanently Delete Everything
                </Button>
              </div>
            }
          >
            This action cannot be undone.
          </Alert>
        )}
      </Card>
    </AppShell>
  );
}
