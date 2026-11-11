"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Download,
  Edit3,
  ExternalLink,
  GitBranch,
  Sparkles,
  X,
} from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { Alert, Badge, Button, Card, Skeleton } from "@/components/ui";
import {
  API_BASE,
  apiFetch,
  ApiError,
  ApplicationDetail,
  ApplicationSummary,
  ChangeItem,
  getStoredToken,
  ResumeUpload,
  ResumeVersion,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

type TabId = "analysis" | "github" | "review" | "learning" | "export";

const CATEGORY_META: Array<{
  key: "demonstrated" | "under_represented" | "github_only" | "needs_development" | "missing";
  title: string;
  tone: "success" | "accent" | "warning" | "danger" | "neutral";
}> = [
  { key: "demonstrated", title: "1. Skills already demonstrated", tone: "success" },
  {
    key: "under_represented",
    title: "2. Present but insufficiently represented in resume",
    tone: "accent",
  },
  {
    key: "github_only",
    title: "3. Supported by GitHub evidence, absent from resume",
    tone: "accent",
  },
  { key: "needs_development", title: "4. Skills needing further development", tone: "warning" },
  { key: "missing", title: "5. Skills genuinely missing", tone: "danger" },
];

export default function ApplicationWorkspacePage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { user, isLoading } = useAuth();

  const appId = params.id;
  const [activeTab, setActiveTab] = useState<TabId>("analysis");
  const [changeFilter, setChangeFilter] = useState<string>("all");
  const [selectedChangeId, setSelectedChangeId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [notesDraft, setNotesDraft] = useState<string>("");

  useEffect(() => {
    if (!isLoading && !user) router.replace("/login");
  }, [isLoading, user, router]);

  const { data: app, isLoading: appLoading } = useQuery<ApplicationDetail>({
    queryKey: ["application", appId],
    queryFn: () => apiFetch<ApplicationDetail>(`/applications/${appId}`),
    enabled: Boolean(user && appId),
  });

  const { data: resume } = useQuery<ResumeUpload>({
    queryKey: ["resume", app?.resume_file_id],
    queryFn: () => apiFetch<ResumeUpload>(`/resumes/${app!.resume_file_id}`),
    enabled: Boolean(user && app?.resume_file_id),
  });

  useEffect(() => {
    if (app && notesDraft === "" && app.notes) {
      setNotesDraft(app.notes);
    }
  }, [app, notesDraft]);

  const latestVersion: ResumeVersion | undefined = app?.versions?.[0];
  const filteredChanges = useMemo(() => {
    const all = latestVersion?.changes || [];
    if (changeFilter === "all") return all;
    return all.filter((c) => c.change_type === changeFilter);
  }, [latestVersion, changeFilter]);

  const selectedChange: ChangeItem | undefined =
    filteredChanges.find((c) => c.id === selectedChangeId) || filteredChanges[0];

  useEffect(() => {
    if (!selectedChangeId && filteredChanges.length > 0) {
      setSelectedChangeId(filteredChanges[0].id);
    }
  }, [filteredChanges, selectedChangeId]);

  // Load authenticated preview PNG when exported
  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    async function loadPreview() {
      if (!latestVersion?.has_preview) {
        setPreviewUrl(null);
        return;
      }
      const token = getStoredToken();
      const resp = await fetch(
        `${API_BASE}/applications/${appId}/versions/${latestVersion.id}/download/preview`,
        { headers: token ? { Authorization: `Bearer ${token}` } : {} }
      );
      if (resp.ok && active) {
        const blob = await resp.blob();
        objectUrl = URL.createObjectURL(blob);
        setPreviewUrl(objectUrl);
      }
    }
    loadPreview();
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [appId, latestVersion?.id, latestVersion?.has_preview]);

  const invalidateApp = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ["application", appId] });
  }, [queryClient, appId]);

  const githubDecisionMutation = useMutation({
    mutationFn: (item: { repo_name: string; decision: "approved" | "rejected" }) =>
      apiFetch<ApplicationDetail>(`/applications/${appId}/github-decisions`, {
        method: "POST",
        body: JSON.stringify({ decisions: [item] }),
      }),
    onSuccess: invalidateApp,
  });

  const generateMutation = useMutation({
    mutationFn: () =>
      apiFetch<ApplicationDetail>(`/applications/${appId}/generate`, {
        method: "POST",
      }),
    onSuccess: async () => {
      setActionError(null);
      await invalidateApp();
      setActiveTab("review");
    },
    onError: (err) => {
      setActionError(err instanceof ApiError ? err.message : "Generation failed.");
    },
  });

  const changeDecisionMutation = useMutation({
    mutationFn: ({
      changeId,
      status,
      edited_text,
    }: {
      changeId: string;
      status: "accepted" | "rejected" | "edited";
      edited_text?: string;
    }) =>
      apiFetch<ChangeItem>(`/applications/${appId}/changes/${changeId}`, {
        method: "PATCH",
        body: JSON.stringify({ status, edited_text }),
      }),
    onSuccess: async () => {
      setActionError(null);
      setEditingText(null);
      await invalidateApp();
    },
    onError: (err) => {
      setActionError(err instanceof ApiError ? err.message : "Could not update change.");
    },
  });

  const bulkAcceptMutation = useMutation({
    mutationFn: (versionId: string) =>
      apiFetch<ResumeVersion>(`/applications/${appId}/versions/${versionId}/bulk-accept`, {
        method: "POST",
      }),
    onSuccess: invalidateApp,
  });

  const exportMutation = useMutation({
    mutationFn: (versionId: string) =>
      apiFetch<ResumeVersion>(`/applications/${appId}/versions/${versionId}/export`, {
        method: "POST",
      }),
    onSuccess: invalidateApp,
  });

  const statusMutation = useMutation({
    mutationFn: (payload: { status: ApplicationSummary["status"]; notes: string }) =>
      apiFetch<ApplicationDetail>(`/applications/${appId}/status`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: invalidateApp,
  });

  // Keyboard shortcuts in Review Workspace (A=Accept, R=Reject, J=Next, K=Prev)
  useEffect(() => {
    if (activeTab !== "review" || editingText !== null || !selectedChange) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      const key = e.key.toLowerCase();
      if (key === "a" && selectedChange.validation_status !== "blocked") {
        changeDecisionMutation.mutate({ changeId: selectedChange.id, status: "accepted" });
      } else if (key === "r") {
        changeDecisionMutation.mutate({ changeId: selectedChange.id, status: "rejected" });
      } else if (key === "j") {
        const idx = filteredChanges.findIndex((c) => c.id === selectedChange.id);
        if (idx >= 0 && idx + 1 < filteredChanges.length) {
          setSelectedChangeId(filteredChanges[idx + 1].id);
        }
      } else if (key === "k") {
        const idx = filteredChanges.findIndex((c) => c.id === selectedChange.id);
        if (idx > 0) {
          setSelectedChangeId(filteredChanges[idx - 1].id);
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activeTab, editingText, selectedChange, filteredChanges, changeDecisionMutation]);

  const handleDownload = async (kind: "docx" | "pdf") => {
    if (!latestVersion) return;
    setActionError(null);
    const token = getStoredToken();
    const resp = await fetch(
      `${API_BASE}/applications/${appId}/versions/${latestVersion.id}/download/${kind}`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} }
    );
    if (!resp.ok) {
      setActionError(`Could not download ${kind.toUpperCase()} file.`);
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `ResumeIQ_Optimized.${kind}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  if (isLoading || appLoading || !app) {
    return (
      <AppShell>
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-64 w-full" />
      </AppShell>
    );
  }

  const tabs: Array<{ id: TabId; label: string }> = [
    { id: "analysis", label: "Analysis & Skill Gaps" },
    { id: "github", label: `GitHub Projects (${app.github_recommendations.length})` },
    { id: "review", label: `Review Changes (${latestVersion?.changes.length || 0})` },
    { id: "learning", label: `Learning (${app.learning_plans.length})` },
    { id: "export", label: "Export & Tracker" },
  ];

  // Build live preview data by overlaying accepted/edited changes on parsed resume
  const acceptedChanges = (latestVersion?.changes || []).filter(
    (c) => (c.status === "accepted" || c.status === "edited") && c.validation_status !== "blocked"
  );
  const rewordMap = new Map<string, string>();
  for (const ch of acceptedChanges) {
    if (ch.change_type === "reworded") {
      rewordMap.set(ch.original_text, ch.edited_text || ch.new_text);
    }
  }
  const reorderedSkills = acceptedChanges.find(
    (c) => c.change_type === "reordered" && c.section === "skills"
  );
  const addedProjects = acceptedChanges.filter((c) => c.change_type === "added_from_github");
  const learningEntries = acceptedChanges.filter((c) => c.change_type === "user_confirmed_skill");

  return (
    <AppShell>
      <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-serif text-3xl font-semibold text-ink">
              {app.job_title}
            </h1>
            <Badge tone="accent">{app.status}</Badge>
          </div>
          <p className="mt-1 text-sm text-ink-muted">
            {app.company ? `${app.company} · ` : ""}Resume: {app.resume_filename}
          </p>
        </div>

        <Button
          onClick={() => generateMutation.mutate()}
          disabled={generateMutation.isPending}
        >
          {generateMutation.isPending
            ? "Generating tailored changes..."
            : latestVersion
              ? "Regenerate Tailored Changes"
              : "Generate Tailored Resume"}
        </Button>
      </div>

      {actionError && <Alert tone="danger">{actionError}</Alert>}

      <div role="tablist" className="flex flex-wrap gap-1 border-b border-border pb-2">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            type="button"
            aria-selected={activeTab === tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`rounded-md px-3.5 py-2 text-sm font-medium transition-colors duration-150 ${
              activeTab === tab.id
                ? "bg-accent text-white"
                : "text-ink-muted hover:bg-subtle hover:text-ink"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "analysis" && (
        <div className="space-y-6">
          <div className="grid gap-6 md:grid-cols-3">
            <Card className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-ink-muted">
                Compatibility Score
              </p>
              <div className="flex items-baseline gap-3">
                <span className="font-serif text-4xl font-semibold text-ink">
                  {app.score_breakdown?.overall_score ?? app.compatibility_before ?? 0}%
                </span>
                {app.compatibility_before != null &&
                  app.compatibility_after != null &&
                  app.compatibility_after > app.compatibility_before && (
                    <Badge tone="success">
                      Before GitHub: {app.compatibility_before}%
                    </Badge>
                  )}
              </div>
              <p className="text-xs text-ink-muted">
                Weighted across required skills, preferred skills, experience
                relevance, keywords, and project evidence.
              </p>
            </Card>

            <Card className="space-y-3 md:col-span-2">
              <h2 className="text-base font-semibold">Explainable Breakdown</h2>
              {app.score_breakdown &&
                [
                  app.score_breakdown.required_skills,
                  app.score_breakdown.preferred_skills,
                  app.score_breakdown.experience_relevance,
                  app.score_breakdown.keyword_coverage,
                  app.score_breakdown.project_relevance,
                ].map((sub) => (
                  <div key={sub.name} className="space-y-1">
                    <div className="flex justify-between text-xs font-medium">
                      <span className="text-ink">
                        {sub.name} ({Math.round(sub.weight * 100)}%)
                      </span>
                      <span className="text-ink">{sub.score}%</span>
                    </div>
                    <div className="h-2 w-full rounded bg-subtle">
                      <div
                        className="h-2 rounded bg-accent"
                        style={{ width: `${Math.min(100, sub.score)}%` }}
                      />
                    </div>
                    <p className="text-xs text-ink-muted">{sub.explanation}</p>
                  </div>
                ))}
            </Card>
          </div>

          <Card className="space-y-4">
            <h2 className="text-lg font-semibold">
              Five-Category Skill Gap Assessment
            </h2>
            <div className="grid gap-4 md:grid-cols-2">
              {CATEGORY_META.map((cat) => {
                const items = app.skill_assessments.filter(
                  (s) => s.category === cat.key
                );
                return (
                  <div
                    key={cat.key}
                    className="space-y-2 rounded-md border border-border bg-canvas p-4"
                  >
                    <div className="flex items-center justify-between">
                      <h3 className="text-sm font-semibold text-ink">
                        {cat.title}
                      </h3>
                      <Badge tone={cat.tone}>{items.length}</Badge>
                    </div>
                    {items.length > 0 ? (
                      <ul className="space-y-1.5 text-xs">
                        {items.map((item) => (
                          <li key={item.skill_name} className="space-y-0.5">
                            <span className="font-semibold text-ink">
                              {item.skill_name}
                            </span>{" "}
                            <span className="text-ink-muted">
                              ({item.importance}) — {item.evidence_summary}
                            </span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-xs text-ink-muted">None in this category.</p>
                    )}
                  </div>
                );
              })}
            </div>
          </Card>
        </div>
      )}

      {activeTab === "github" && (
        <div className="space-y-4">
          {app.github_recommendations.length === 0 ? (
            <Card className="py-10 text-center text-sm text-ink-muted">
              No GitHub repositories were linked or qualified for recommendation
              on this application. You can still generate tailored resume
              changes using your resume evidence.
            </Card>
          ) : (
            app.github_recommendations.map((rec) => (
              <Card key={rec.repo_name} className="space-y-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <GitBranch className="h-4 w-4 text-accent" aria-hidden="true" />
                      <a
                        href={rec.html_url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-base font-semibold text-ink hover:underline"
                      >
                        {rec.full_name}
                        <ExternalLink className="h-3.5 w-3.5" />
                      </a>
                      <Badge tone="accent">
                        Relevance {rec.relevance_score}%
                      </Badge>
                      <Badge tone="neutral">
                        Evidence Quality {rec.quality_score}/100
                      </Badge>
                      {rec.already_in_resume && (
                        <Badge tone="success">Already on resume</Badge>
                      )}
                    </div>
                    <p className="text-sm text-ink-muted">{rec.reason}</p>
                  </div>

                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant={rec.decision === "approved" ? "primary" : "secondary"}
                      onClick={() =>
                        githubDecisionMutation.mutate({
                          repo_name: rec.repo_name,
                          decision: "approved",
                        })
                      }
                    >
                      <Check className="h-4 w-4" />
                      Approve
                    </Button>
                    <Button
                      size="sm"
                      variant={rec.decision === "rejected" ? "danger" : "ghost"}
                      onClick={() =>
                        githubDecisionMutation.mutate({
                          repo_name: rec.repo_name,
                          decision: "rejected",
                        })
                      }
                    >
                      <X className="h-4 w-4" />
                      Reject
                    </Button>
                  </div>
                </div>

                {rec.draft_bullets.length > 0 && (
                  <ul className="list-disc space-y-1 rounded-md border border-border bg-canvas p-3 pl-7 text-xs text-ink-muted">
                    {rec.draft_bullets.map((b, i) => (
                      <li key={i}>{b}</li>
                    ))}
                  </ul>
                )}
              </Card>
            ))
          )}
        </div>
      )}

      {activeTab === "review" && (
        <div className="space-y-4">
          {!latestVersion ? (
            <Card className="space-y-3 py-10 text-center">
              <p className="text-sm text-ink-muted">
                No tailored change set generated yet. Approve any GitHub
                projects you want to include, then generate your tailored
                changes.
              </p>
              <div>
                <Button onClick={() => generateMutation.mutate()}>
                  Generate Tailored Resume Changes
                </Button>
              </div>
            </Card>
          ) : (
            <>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-1.5">
                  {[
                    { id: "all", label: "All" },
                    { id: "reworded", label: "Reworded" },
                    { id: "added_from_github", label: "Added from GitHub" },
                    { id: "reordered", label: "Reordered" },
                    { id: "user_confirmed_skill", label: "Confirmed Learning" },
                  ].map((f) => (
                    <button
                      key={f.id}
                      type="button"
                      onClick={() => setChangeFilter(f.id)}
                      className={`rounded border px-2.5 py-1 text-xs font-medium ${
                        changeFilter === f.id
                          ? "border-accent bg-accent-soft text-accent"
                          : "border-border bg-surface text-ink-muted hover:bg-subtle"
                      }`}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>

                <div className="flex items-center gap-3">
                  <span className="text-xs text-ink-muted">
                    Shortcuts: <kbd className="rounded border px-1">A</kbd> accept,{" "}
                    <kbd className="rounded border px-1">R</kbd> reject,{" "}
                    <kbd className="rounded border px-1">J/K</kbd> navigate
                  </span>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => bulkAcceptMutation.mutate(latestVersion.id)}
                  >
                    Bulk-accept reordering only
                  </Button>
                </div>
              </div>

              <div className="grid gap-4 lg:grid-cols-12">
                {/* Left: Change List */}
                <Card className="space-y-2 p-4 lg:col-span-3">
                  <h2 className="text-sm font-semibold">
                    Proposed Changes ({filteredChanges.length})
                  </h2>
                  {filteredChanges.length === 0 ? (
                    <p className="text-xs text-ink-muted">
                      No changes match this filter.
                    </p>
                  ) : (
                    <div className="space-y-1.5">
                      {filteredChanges.map((ch) => {
                        const active = selectedChange?.id === ch.id;
                        return (
                          <button
                            key={ch.id}
                            type="button"
                            onClick={() => {
                              setSelectedChangeId(ch.id);
                              setEditingText(null);
                            }}
                            className={`w-full rounded-md border p-2.5 text-left text-xs transition-colors duration-150 ${
                              active
                                ? "border-accent bg-accent-soft"
                                : "border-border bg-canvas hover:bg-subtle"
                            }`}
                          >
                            <div className="flex items-center justify-between gap-1">
                              <span className="font-semibold uppercase tracking-wide text-ink">
                                {ch.section}
                              </span>
                              <Badge
                                tone={
                                  ch.validation_status === "blocked"
                                    ? "danger"
                                    : ch.status === "accepted" || ch.status === "edited"
                                      ? "success"
                                      : ch.status === "rejected"
                                        ? "neutral"
                                        : "warning"
                                }
                              >
                                {ch.validation_status === "blocked" ? "blocked" : ch.status}
                              </Badge>
                            </div>
                            <p className="mt-1 line-clamp-2 text-ink-muted">
                              {ch.edited_text || ch.new_text}
                            </p>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </Card>

                {/* Center: Side-by-side Diff */}
                <Card className="space-y-4 p-5 lg:col-span-5">
                  {selectedChange ? (
                    <>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <Badge tone="accent">
                            {selectedChange.change_type.replace(/_/g, " ")}
                          </Badge>
                          <Badge
                            tone={
                              selectedChange.validation_status === "passed"
                                ? "success"
                                : selectedChange.validation_status === "flagged"
                                  ? "warning"
                                  : "danger"
                            }
                          >
                            Validator: {selectedChange.validation_status}
                          </Badge>
                        </div>
                        <span className="text-xs text-ink-muted">
                          Status: {selectedChange.status}
                        </span>
                      </div>

                      {selectedChange.validation_warnings.length > 0 && (
                        <Alert
                          tone={
                            selectedChange.validation_status === "blocked"
                              ? "danger"
                              : "warning"
                          }
                          title="Truthfulness check"
                        >
                          <ul className="list-disc pl-4 text-xs">
                            {selectedChange.validation_warnings.map((w, i) => (
                              <li key={i}>{w}</li>
                            ))}
                          </ul>
                        </Alert>
                      )}

                      <div className="grid gap-3 sm:grid-cols-2">
                        <div className="rounded-md border border-border bg-canvas p-3">
                          <p className="text-xs font-semibold uppercase tracking-wider text-ink-muted">
                            Original
                          </p>
                          <p className="mt-1.5 whitespace-pre-wrap text-xs leading-relaxed text-ink">
                            {selectedChange.original_text || "(New addition)"}
                          </p>
                        </div>
                        <div className="rounded-md border border-accent/30 bg-accent-soft/40 p-3">
                          <p className="text-xs font-semibold uppercase tracking-wider text-accent">
                            Proposed
                          </p>
                          {editingText !== null ? (
                            <textarea
                              rows={5}
                              className="mt-1.5 w-full rounded border border-border bg-surface p-2 text-xs text-ink"
                              value={editingText}
                              onChange={(e) => setEditingText(e.target.value)}
                            />
                          ) : (
                            <p className="mt-1.5 whitespace-pre-wrap text-xs leading-relaxed text-ink">
                              {selectedChange.edited_text || selectedChange.new_text}
                            </p>
                          )}
                        </div>
                      </div>

                      <div className="space-y-1 rounded-md border border-border bg-canvas p-3 text-xs">
                        <p className="font-semibold text-ink">
                          Why this is accurate &amp; relevant
                        </p>
                        <p className="text-ink-muted">{selectedChange.reason}</p>
                        {selectedChange.evidence_refs.length > 0 && (
                          <p className="text-ink-muted">
                            Evidence:{" "}
                            {selectedChange.evidence_refs
                              .map((e) => e.detail)
                              .filter(Boolean)
                              .join(" · ")}
                          </p>
                        )}
                      </div>

                      <div className="flex flex-wrap items-center gap-2 pt-1">
                        {editingText !== null ? (
                          <>
                            <Button
                              size="sm"
                              onClick={() =>
                                changeDecisionMutation.mutate({
                                  changeId: selectedChange.id,
                                  status: "edited",
                                  edited_text: editingText,
                                })
                              }
                            >
                              Save Edit
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => setEditingText(null)}
                            >
                              Cancel
                            </Button>
                          </>
                        ) : (
                          <>
                            <Button
                              size="sm"
                              disabled={selectedChange.validation_status === "blocked"}
                              onClick={() =>
                                changeDecisionMutation.mutate({
                                  changeId: selectedChange.id,
                                  status: "accepted",
                                })
                              }
                            >
                              <Check className="h-4 w-4" />
                              Accept
                            </Button>
                            <Button
                              size="sm"
                              variant="secondary"
                              onClick={() =>
                                setEditingText(
                                  selectedChange.edited_text || selectedChange.new_text
                                )
                              }
                            >
                              <Edit3 className="h-4 w-4" />
                              Edit
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() =>
                                changeDecisionMutation.mutate({
                                  changeId: selectedChange.id,
                                  status: "rejected",
                                })
                              }
                            >
                              <X className="h-4 w-4" />
                              Reject
                            </Button>
                          </>
                        )}
                      </div>
                    </>
                  ) : (
                    <p className="text-sm text-ink-muted">
                      Select a change from the left to review its diff.
                    </p>
                  )}
                </Card>

                {/* Right: Live Resume Preview */}
                <Card className="space-y-3 p-5 font-serif lg:col-span-4">
                  <div className="flex items-center justify-between font-sans">
                    <h2 className="text-sm font-semibold text-ink">
                      Live Preview
                    </h2>
                    <Badge tone="success">
                      {acceptedChanges.length} approved
                    </Badge>
                  </div>
                  <div className="space-y-3 rounded border border-border bg-white p-4 text-xs leading-relaxed text-ink">
                    {resume ? (
                      <>
                        <div>
                          <p className="text-base font-semibold">
                            {resume.parsed_structure.full_name}
                          </p>
                          <p className="font-sans text-[11px] text-ink-muted">
                            {[
                              resume.parsed_structure.email,
                              resume.parsed_structure.location,
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          </p>
                        </div>

                        {resume.parsed_structure.experience.length > 0 && (
                          <div className="space-y-1.5">
                            <p className="border-b border-border pb-0.5 font-sans text-[11px] font-semibold uppercase tracking-wider">
                              Experience
                            </p>
                            {resume.parsed_structure.experience.map((exp, i) => (
                              <div key={i} className="space-y-1">
                                <p className="font-semibold">
                                  {exp.title}{" "}
                                  {exp.company ? `— ${exp.company}` : ""}
                                </p>
                                <ul className="list-disc space-y-0.5 pl-4">
                                  {exp.bullets.map((b, bi) => (
                                    <li key={bi}>{rewordMap.get(b) || b}</li>
                                  ))}
                                </ul>
                              </div>
                            ))}
                          </div>
                        )}

                        {(resume.parsed_structure.projects.length > 0 ||
                          addedProjects.length > 0) && (
                          <div className="space-y-1.5">
                            <p className="border-b border-border pb-0.5 font-sans text-[11px] font-semibold uppercase tracking-wider">
                              Projects
                            </p>
                            {resume.parsed_structure.projects.map((p, i) => (
                              <div key={i}>
                                <p className="font-semibold">{p.name}</p>
                                <ul className="list-disc pl-4">
                                  {p.bullets.map((b, bi) => (
                                    <li key={bi}>{b}</li>
                                  ))}
                                </ul>
                              </div>
                            ))}
                            {addedProjects.map((ch) => (
                              <p key={ch.id} className="whitespace-pre-wrap">
                                {ch.edited_text || ch.new_text}
                              </p>
                            ))}
                          </div>
                        )}

                        {(reorderedSkills ||
                          resume.parsed_structure.skills.length > 0) && (
                          <div>
                            <p className="border-b border-border pb-0.5 font-sans text-[11px] font-semibold uppercase tracking-wider">
                              Skills
                            </p>
                            <p className="mt-1">
                              {reorderedSkills
                                ? reorderedSkills.edited_text ||
                                  reorderedSkills.new_text
                                : resume.parsed_structure.skills.join(", ")}
                            </p>
                          </div>
                        )}

                        {learningEntries.length > 0 && (
                          <div>
                            <p className="border-b border-border pb-0.5 font-sans text-[11px] font-semibold uppercase tracking-wider">
                              Skill Development
                            </p>
                            {learningEntries.map((ch) => (
                              <p key={ch.id} className="mt-1">
                                {ch.edited_text || ch.new_text}
                              </p>
                            ))}
                          </div>
                        )}
                      </>
                    ) : (
                      <Skeleton className="h-40 w-full" />
                    )}
                  </div>
                </Card>
              </div>
            </>
          )}
        </div>
      )}

      {activeTab === "learning" && (
        <div className="space-y-4">
          {app.learning_plans.length === 0 ? (
            <Card className="py-10 text-center text-sm text-ink-muted">
              No skill gaps requiring a learning plan were detected for this
              role.
            </Card>
          ) : (
            app.learning_plans.map((plan) => (
              <Card key={plan.skill_name} className="space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="text-base font-semibold">{plan.skill_name}</h3>
                  <Badge tone="warning">{plan.category.replace("_", " ")}</Badge>
                </div>
                <p className="text-xs text-ink-muted">{plan.how_to_demonstrate}</p>
                <p className="text-xs text-ink">
                  {plan.ordered_steps.length} steps &middot; Open the Learning tab
                  in the main nav to track step completion.
                </p>
              </Card>
            ))
          )}
        </div>
      )}

      {activeTab === "export" && (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card className="space-y-4">
            <h2 className="text-lg font-semibold">Export Optimized Resume</h2>
            {app.resume_file_type === "pdf" && (
              <Alert tone="warning">
                Because your input was a PDF, ResumeIQ will generate a clean,
                ATS-friendly DOCX approximating your content rather than editing
                PDF glyphs directly.
              </Alert>
            )}

            {latestVersion ? (
              <div className="space-y-4">
                <Button
                  onClick={() => exportMutation.mutate(latestVersion.id)}
                  disabled={exportMutation.isPending}
                >
                  {exportMutation.isPending
                    ? "Building DOCX & rendering PDF..."
                    : "Build Final DOCX & PDF"}
                </Button>

                {latestVersion.overflow_detected && (
                  <Alert tone="warning" title="Page overflow detected">
                    <ul className="list-disc pl-4 text-xs">
                      {latestVersion.overflow_suggestions.map((s, i) => (
                        <li key={i}>{s}</li>
                      ))}
                    </ul>
                  </Alert>
                )}

                {latestVersion.page_count_output != null && (
                  <div className="flex flex-wrap items-center gap-3 text-sm">
                    <Badge tone="neutral">
                      Original pages: {latestVersion.page_count_original ?? "—"}
                    </Badge>
                    <Badge
                      tone={
                        latestVersion.overflow_detected ? "warning" : "success"
                      }
                    >
                      Optimized pages: {latestVersion.page_count_output}
                    </Badge>
                  </div>
                )}

                {latestVersion.has_docx && (
                  <div className="flex flex-wrap gap-3 pt-2">
                    <Button onClick={() => handleDownload("docx")}>
                      <Download className="h-4 w-4" />
                      Download DOCX
                    </Button>
                    {latestVersion.has_pdf && (
                      <Button
                        variant="secondary"
                        onClick={() => handleDownload("pdf")}
                      >
                        <Download className="h-4 w-4" />
                        Download PDF
                      </Button>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <p className="text-sm text-ink-muted">
                Generate tailored changes in the Review tab before exporting.
              </p>
            )}

            <div className="space-y-3 border-t border-border pt-4">
              <h3 className="text-sm font-semibold">Application Tracker</h3>
              <div className="flex flex-wrap gap-2">
                {(
                  ["Draft", "Applied", "Interview", "Offer", "Rejected"] as const
                ).map((st) => (
                  <Button
                    key={st}
                    size="sm"
                    variant={app.status === st ? "primary" : "secondary"}
                    onClick={() =>
                      statusMutation.mutate({ status: st, notes: notesDraft })
                    }
                  >
                    {st}
                  </Button>
                ))}
              </div>
              <textarea
                rows={3}
                placeholder="Application notes (referral, follow-up date, interview notes)..."
                className="w-full rounded-md border border-border bg-surface p-2 text-xs text-ink"
                value={notesDraft}
                onChange={(e) => setNotesDraft(e.target.value)}
              />
              <Button
                size="sm"
                variant="secondary"
                onClick={() =>
                  statusMutation.mutate({ status: app.status, notes: notesDraft })
                }
              >
                Save Notes
              </Button>
            </div>
          </Card>

          <Card className="space-y-3">
            <h2 className="text-lg font-semibold">Rendered Page 1 Preview</h2>
            {previewUrl ? (
              <img
                src={previewUrl}
                alt="Rendered optimized resume page 1 preview"
                className="w-full rounded border border-border bg-white"
              />
            ) : (
              <div className="rounded-md border border-dashed border-border bg-canvas p-12 text-center text-sm text-ink-muted">
                Click &ldquo;Build Final DOCX &amp; PDF&rdquo; to render and
                verify page layout.
              </div>
            )}
          </Card>
        </div>
      )}
    </AppShell>
  );
}
