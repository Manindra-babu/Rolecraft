"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, FileText, Trash2, Upload } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { Alert, Badge, Button, Card, Input, Skeleton } from "@/components/ui";
import {
  apiFetch,
  ApiError,
  MasterProfile,
  ParsedResumeStructure,
  ResumeUpload,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function ResumesPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { user, isLoading } = useAuth();

  const [selectedResume, setSelectedResume] = useState<ResumeUpload | null>(null);
  const [editable, setEditable] = useState<ParsedResumeStructure | null>(null);
  const [skillsText, setSkillsText] = useState("");
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [confirmSuccess, setConfirmSuccess] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoading && !user) {
      router.replace("/login");
    }
  }, [isLoading, user, router]);

  const { data: resumes, isLoading: resumesLoading } = useQuery<ResumeUpload[]>({
    queryKey: ["resumes"],
    queryFn: () => apiFetch<ResumeUpload[]>("/resumes"),
    enabled: Boolean(user),
  });

  useEffect(() => {
    if (resumes && resumes.length > 0 && !selectedResume) {
      setSelectedResume(resumes[0]);
      setEditable(resumes[0].parsed_structure);
      setSkillsText(resumes[0].parsed_structure.skills.join(", "));
    }
  }, [resumes, selectedResume]);

  const selectResume = (resume: ResumeUpload) => {
    setSelectedResume(resume);
    setEditable(resume.parsed_structure);
    setSkillsText(resume.parsed_structure.skills.join(", "));
    setConfirmSuccess(null);
  };

  const uploadMutation = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return apiFetch<ResumeUpload>("/resumes/upload", {
        method: "POST",
        body: form,
      });
    },
    onSuccess: async (created) => {
      setUploadError(null);
      setConfirmSuccess(null);
      selectResume(created);
      await queryClient.invalidateQueries({ queryKey: ["resumes"] });
    },
    onError: (err) => {
      setUploadError(
        err instanceof ApiError ? err.message : "Failed to upload resume."
      );
    },
  });

  const confirmMutation = useMutation({
    mutationFn: async () => {
      if (!selectedResume || !editable) return;
      const payload: ParsedResumeStructure = {
        ...editable,
        skills: skillsText
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      };
      return apiFetch<MasterProfile>(`/resumes/${selectedResume.id}/confirm`, {
        method: "POST",
        body: JSON.stringify({ parsed_structure: payload }),
      });
    },
    onSuccess: async () => {
      setConfirmSuccess(
        "Parsed resume saved to your Master Profile as the verified source of truth."
      );
      await queryClient.invalidateQueries({ queryKey: ["master-profile"] });
      await queryClient.invalidateQueries({ queryKey: ["resumes"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) =>
      apiFetch<void>(`/resumes/${id}`, { method: "DELETE" }),
    onSuccess: async () => {
      setSelectedResume(null);
      setEditable(null);
      await queryClient.invalidateQueries({ queryKey: ["resumes"] });
    },
  });

  if (isLoading || !user) {
    return (
      <div className="mx-auto max-w-6xl space-y-6 px-4 py-12">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploadError(null);
    uploadMutation.mutate(file);
  };

  return (
    <AppShell>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="font-serif text-3xl font-semibold text-ink">
            Resumes &amp; Structure
          </h1>
          <p className="mt-1 text-sm text-ink-muted">
            Upload a DOCX resume (recommended for exact layout preservation) or
            PDF for analysis, then confirm the extracted structure.
          </p>
        </div>
      </div>

      <Card className="space-y-4">
        <div className="flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
          <div className="space-y-1">
            <h2 className="text-base font-semibold">Upload resume</h2>
            <p className="text-sm text-ink-muted">
              DOCX files are parsed at paragraph and run level so tailored
              versions keep your fonts, spacing, and tables intact.
            </p>
          </div>
          <label className="inline-flex cursor-pointer items-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-accent-hover">
            <Upload className="h-4 w-4" aria-hidden="true" />
            <span>
              {uploadMutation.isPending ? "Parsing..." : "Choose DOCX or PDF"}
            </span>
            <input
              type="file"
              accept=".docx,.pdf"
              className="sr-only"
              onChange={handleFileChange}
              disabled={uploadMutation.isPending}
            />
          </label>
        </div>

        {uploadError && <Alert tone="danger">{uploadError}</Alert>}
      </Card>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="space-y-3">
          <h2 className="text-base font-semibold">Uploaded Files</h2>
          {resumesLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : resumes && resumes.length > 0 ? (
            <div className="space-y-2">
              {resumes.map((r) => {
                const active = selectedResume?.id === r.id;
                return (
                  <div
                    key={r.id}
                    className={`flex items-center justify-between rounded-md border p-3 transition-colors duration-150 ${
                      active
                        ? "border-accent bg-accent-soft"
                        : "border-border bg-canvas hover:bg-subtle"
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => selectResume(r)}
                      className="flex flex-1 items-start gap-2 text-left"
                    >
                      <FileText
                        className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                        aria-hidden="true"
                      />
                      <div>
                        <p className="text-sm font-medium text-ink">
                          {r.filename}
                        </p>
                        <p className="text-xs text-ink-muted">
                          {r.file_type.toUpperCase()} &middot; {r.layout_type} &middot;{" "}
                          {r.element_count} mapped elements
                        </p>
                      </div>
                    </button>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Delete ${r.filename}`}
                      onClick={() => deleteMutation.mutate(r.id)}
                    >
                      <Trash2 className="h-4 w-4 text-ink-muted" />
                    </Button>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-sm text-ink-muted">
              No resumes uploaded yet. Upload your DOCX file above.
            </p>
          )}
        </Card>

        <Card className="space-y-5 lg:col-span-2">
          {selectedResume && editable ? (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 className="text-lg font-semibold">
                    Confirm Parsed Resume
                  </h2>
                  <p className="text-xs text-ink-muted">
                    Review and correct extracted fields before saving to your
                    Master Profile.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge
                    tone={
                      selectedResume.layout_support === "full"
                        ? "success"
                        : "warning"
                    }
                  >
                    {selectedResume.layout_type} ({selectedResume.layout_support})
                  </Badge>
                  <Badge tone="neutral">
                    {selectedResume.element_count} mapped nodes
                  </Badge>
                </div>
              </div>

              {selectedResume.format_notice && (
                <Alert tone="warning">{selectedResume.format_notice}</Alert>
              )}
              {confirmSuccess && (
                <Alert tone="success">
                  <span className="inline-flex items-center gap-1.5">
                    <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                    {confirmSuccess}
                  </span>
                </Alert>
              )}

              <div className="grid gap-4 sm:grid-cols-2">
                <Input
                  label="Full Name"
                  value={editable.full_name}
                  onChange={(e) =>
                    setEditable({ ...editable, full_name: e.target.value })
                  }
                />
                <Input
                  label="Email"
                  value={editable.email}
                  onChange={(e) =>
                    setEditable({ ...editable, email: e.target.value })
                  }
                />
                <Input
                  label="Phone"
                  value={editable.phone}
                  onChange={(e) =>
                    setEditable({ ...editable, phone: e.target.value })
                  }
                />
                <Input
                  label="Location"
                  value={editable.location}
                  onChange={(e) =>
                    setEditable({ ...editable, location: e.target.value })
                  }
                />
              </div>

              <div className="space-y-1.5">
                <label className="block text-sm font-medium text-ink">
                  Summary
                </label>
                <textarea
                  rows={3}
                  className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent"
                  value={editable.summary}
                  onChange={(e) =>
                    setEditable({ ...editable, summary: e.target.value })
                  }
                />
              </div>

              <Input
                label="Skills (comma-separated)"
                value={skillsText}
                onChange={(e) => setSkillsText(e.target.value)}
              />

              <div className="space-y-3">
                <h3 className="text-sm font-semibold text-ink">
                  Experience ({editable.experience.length})
                </h3>
                {editable.experience.map((exp, idx) => (
                  <div
                    key={idx}
                    className="space-y-2 rounded-md border border-border bg-canvas p-3"
                  >
                    <p className="text-sm font-medium text-ink">
                      {exp.title} {exp.company ? `— ${exp.company}` : ""}{" "}
                      {exp.dates ? `(${exp.dates})` : ""}
                    </p>
                    <ul className="list-disc space-y-1 pl-5 text-xs text-ink-muted">
                      {exp.bullets.map((b, bIdx) => (
                        <li key={bIdx}>{b}</li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>

              <div className="space-y-3">
                <h3 className="text-sm font-semibold text-ink">
                  Projects ({editable.projects.length})
                </h3>
                {editable.projects.map((proj, idx) => (
                  <div
                    key={idx}
                    className="space-y-1 rounded-md border border-border bg-canvas p-3"
                  >
                    <p className="text-sm font-medium text-ink">{proj.name}</p>
                    <ul className="list-disc space-y-1 pl-5 text-xs text-ink-muted">
                      {proj.bullets.map((b, bIdx) => (
                        <li key={bIdx}>{b}</li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>

              <div className="flex items-center justify-end gap-3 pt-2">
                <Button
                  onClick={() => confirmMutation.mutate()}
                  disabled={confirmMutation.isPending}
                >
                  {confirmMutation.isPending
                    ? "Saving to Master Profile..."
                    : "Confirm & Save to Master Profile"}
                </Button>
              </div>
            </>
          ) : (
            <div className="py-12 text-center text-sm text-ink-muted">
              Upload or select a resume on the left to inspect and confirm its
              parsed structure.
            </div>
          )}
        </Card>
      </div>
    </AppShell>
  );
}
