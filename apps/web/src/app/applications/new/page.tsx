"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2, FileText, GitBranch, Globe, Upload } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { Alert, Badge, Button, Card, Input, Skeleton } from "@/components/ui";
import {
  apiFetch,
  ApiError,
  ApplicationDetail,
  GitHubAnalysis,
  JobPosting,
  ResumeUpload,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function NewApplicationPage() {
  const router = useRouter();
  const { user, isLoading } = useAuth();

  const [step, setStep] = useState<1 | 2 | 3 | 4>(1);
  const [selectedResumeId, setSelectedResumeId] = useState<string>("");
  const [githubInput, setGithubInput] = useState<string>("");
  const [detectedGithubUrl, setDetectedGithubUrl] = useState<string>("");
  const [otherLink, setOtherLink] = useState<string>("");
  const [githubAnalysisId, setGithubAnalysisId] = useState<string | null>(null);

  const [jobUrl, setJobUrl] = useState<string>("");
  const [statusMessage, setStatusMessage] = useState<string>("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [fallbackRequired, setFallbackRequired] = useState<boolean>(false);
  const [fallbackReason, setFallbackReason] = useState<string>("");
  const [pastedJobText, setPastedJobText] = useState<string>("");

  const [jobRecord, setJobRecord] = useState<JobPosting | null>(null);
  const [reqSkillsText, setReqSkillsText] = useState<string>("");
  const [prefSkillsText, setPrefSkillsText] = useState<string>("");

  useEffect(() => {
    if (!isLoading && !user) router.replace("/login");
  }, [isLoading, user, router]);

  useEffect(() => {
    if (!user || typeof window === "undefined") return;
    const jobId = new URLSearchParams(window.location.search).get("job_id");
    if (!jobId || jobRecord?.id === jobId) return;
    apiFetch<JobPosting>(`/jobs/${jobId}`)
      .then((res) => {
        setJobRecord(res);
        setJobUrl(res.url || "");
        setReqSkillsText(res.required_skills.join(", "));
        setPrefSkillsText(res.preferred_skills.join(", "));
      })
      .catch(() => {
        // Ignore invalid prefill job_id; user can proceed through wizard normally
      });
  }, [user, jobRecord?.id]);

  const { data: resumes, refetch: refetchResumes } = useQuery<ResumeUpload[]>({
    queryKey: ["resumes"],
    queryFn: () => apiFetch<ResumeUpload[]>("/resumes"),
    enabled: Boolean(user),
  });

  useEffect(() => {
    if (resumes && resumes.length > 0 && !selectedResumeId) {
      setSelectedResumeId(resumes[0].id);
    }
  }, [resumes, selectedResumeId]);

  useEffect(() => {
    const selected = resumes?.find((r) => r.id === selectedResumeId);
    if (!selected?.parsed_structure) return;
    const fromLink = selected.parsed_structure.links?.find((l) =>
      (l.url || "").toLowerCase().includes("github.com/")
    )?.url;
    const fromProj = selected.parsed_structure.projects?.find((p) =>
      (p.url || "").toLowerCase().includes("github.com/")
    )?.url;
    const detected = fromLink || fromProj || "";
    setDetectedGithubUrl(detected);
    if (detected && !githubInput) {
      setGithubInput(detected);
    }
  }, [resumes, selectedResumeId, githubInput]);

  const uploadMutation = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return apiFetch<ResumeUpload>("/resumes/upload", {
        method: "POST",
        body: form,
      });
    },
    onSuccess: async (uploaded) => {
      setErrorMsg(null);
      await refetchResumes();
      setSelectedResumeId(uploaded.id);
    },
    onError: (err) => {
      setErrorMsg(err instanceof ApiError ? err.message : "Upload failed.");
    },
  });

  const handleGitHubStep = async () => {
    setErrorMsg(null);
    if (!githubInput.trim() && !otherLink.trim()) {
      setGithubAnalysisId(null);
      setStep(3);
      return;
    }
    setStatusMessage("Analyzing public GitHub repositories...");
    try {
      const res = await apiFetch<GitHubAnalysis>("/github/analyze", {
        method: "POST",
        body: JSON.stringify({
          profile_url_or_username: githubInput.trim(),
          other_links: otherLink.trim() ? [otherLink.trim()] : [],
        }),
      });
      setGithubAnalysisId(res.skipped ? null : res.id);
      setStatusMessage("");
      setStep(3);
    } catch (err) {
      setStatusMessage("");
      setErrorMsg(err instanceof ApiError ? err.message : "GitHub analysis failed.");
    }
  };

  const handleExtractUrl = async () => {
    setErrorMsg(null);
    setFallbackRequired(false);
    setStatusMessage("Fetching job page, checking structured JSON-LD / ATS endpoints...");
    try {
      const res = await apiFetch<JobPosting>("/jobs/extract-url", {
        method: "POST",
        body: JSON.stringify({ url: jobUrl.trim() }),
      });
      setStatusMessage("");
      if (res.status === "fallback_required") {
        setFallbackRequired(true);
        setFallbackReason(res.fallback_reason || "Automatic extraction could not read this page.");
        return;
      }
      setJobRecord(res);
      setReqSkillsText(res.required_skills.join(", "));
      setPrefSkillsText(res.preferred_skills.join(", "));
      setStep(4);
    } catch (err) {
      setStatusMessage("");
      setErrorMsg(err instanceof ApiError ? err.message : "Job extraction failed.");
    }
  };

  const handleExtractText = async () => {
    setErrorMsg(null);
    setStatusMessage("Structuring pasted job description...");
    try {
      const res = await apiFetch<JobPosting>("/jobs/extract-text", {
        method: "POST",
        body: JSON.stringify({
          url: jobUrl.trim() || null,
          raw_text: pastedJobText,
        }),
      });
      setStatusMessage("");
      if (res.status === "fallback_required") {
        setErrorMsg(res.fallback_reason || "Please paste the full job description.");
        return;
      }
      setJobRecord(res);
      setReqSkillsText(res.required_skills.join(", "));
      setPrefSkillsText(res.preferred_skills.join(", "));
      setStep(4);
    } catch (err) {
      setStatusMessage("");
      setErrorMsg(err instanceof ApiError ? err.message : "Could not parse text.");
    }
  };

  const handleExtractScreenshot = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setErrorMsg(null);
    setStatusMessage("Reading job screenshot...");
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await apiFetch<JobPosting>("/jobs/extract-screenshot", {
        method: "POST",
        body: form,
      });
      setStatusMessage("");
      if (res.status === "fallback_required") {
        setErrorMsg(res.fallback_reason || "Could not read job posting from screenshot.");
        return;
      }
      setJobRecord(res);
      setReqSkillsText(res.required_skills.join(", "));
      setPrefSkillsText(res.preferred_skills.join(", "));
      setStep(4);
    } catch (err) {
      setStatusMessage("");
      setErrorMsg(err instanceof ApiError ? err.message : "Screenshot parsing failed.");
    }
  };

  const handleConfirmAndAnalyze = async () => {
    if (!jobRecord?.id || !selectedResumeId) return;
    setErrorMsg(null);
    setStatusMessage("Saving confirmed job fields and computing compatibility & skill gaps...");
    try {
      await apiFetch<JobPosting>(`/jobs/${jobRecord.id}`, {
        method: "PUT",
        body: JSON.stringify({
          title: jobRecord.title,
          company: jobRecord.company,
          location: jobRecord.location,
          experience_level: jobRecord.experience_level,
          responsibilities: jobRecord.responsibilities,
          required_skills: reqSkillsText.split(",").map((s) => s.trim()).filter(Boolean),
          preferred_skills: prefSkillsText.split(",").map((s) => s.trim()).filter(Boolean),
          or_groups: jobRecord.or_groups ?? [],
          keywords: jobRecord.keywords,
        }),
      });
      const app = await apiFetch<ApplicationDetail>("/applications", {
        method: "POST",
        body: JSON.stringify({
          resume_file_id: selectedResumeId,
          job_posting_id: jobRecord.id,
          github_analysis_id: githubAnalysisId,
        }),
      });
      router.push(`/applications/${app.id}`);
    } catch (err) {
      setStatusMessage("");
      setErrorMsg(err instanceof ApiError ? err.message : "Failed to create application.");
    }
  };

  if (isLoading || !user) {
    return (
      <div className="mx-auto max-w-4xl space-y-6 px-4 py-12">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="space-y-1">
        <h1 className="font-serif text-3xl font-semibold text-ink">
          New Application
        </h1>
        <p className="text-sm text-ink-muted">
          Step {step} of 4 &middot; Choose your resume, optional GitHub profile,
          and target job posting.
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        {[
          { num: 1, label: "1. Resume" },
          { num: 2, label: "2. GitHub (Optional)" },
          { num: 3, label: "3. Job URL" },
          { num: 4, label: "4. Confirm Job" },
        ].map((item) => (
          <Badge key={item.num} tone={step === item.num ? "accent" : step > item.num ? "success" : "neutral"}>
            {item.label}
          </Badge>
        ))}
      </div>

      {errorMsg && <Alert tone="danger">{errorMsg}</Alert>}
      {statusMessage && <Alert tone="info">{statusMessage}</Alert>}

      {step === 1 && (
        <Card className="space-y-5">
          <div className="flex items-center gap-2">
            <FileText className="h-5 w-5 text-accent" aria-hidden="true" />
            <h2 className="text-lg font-semibold">Select or upload your resume</h2>
          </div>

          {resumes && resumes.length > 0 && (
            <div className="space-y-2">
              {resumes.map((r) => (
                <label
                  key={r.id}
                  className={`flex cursor-pointer items-center justify-between rounded-md border p-3 ${
                    selectedResumeId === r.id
                      ? "border-accent bg-accent-soft"
                      : "border-border bg-canvas"
                  }`}
                >
                  <div className="flex items-center gap-3">
                    <input
                      type="radio"
                      name="resume"
                      checked={selectedResumeId === r.id}
                      onChange={() => setSelectedResumeId(r.id)}
                    />
                    <div>
                      <p className="text-sm font-medium text-ink">{r.filename}</p>
                      <p className="text-xs text-ink-muted">
                        {r.file_type.toUpperCase()} &middot; {r.layout_type} ({r.layout_support})
                      </p>
                    </div>
                  </div>
                </label>
              ))}
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-3 pt-2">
            <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-surface px-4 py-2 text-sm font-medium text-ink hover:bg-subtle">
              <Upload className="h-4 w-4" aria-hidden="true" />
              <span>{uploadMutation.isPending ? "Uploading..." : "Upload new DOCX / PDF"}</span>
              <input
                type="file"
                accept=".docx,.pdf"
                className="sr-only"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) uploadMutation.mutate(file);
                }}
              />
            </label>

            <Button
              disabled={!selectedResumeId}
              onClick={() => setStep(2)}
            >
              Continue to GitHub
            </Button>
          </div>
        </Card>
      )}

      {step === 2 && (
        <Card className="space-y-5">
          <div className="flex items-center gap-2">
            <GitBranch className="h-5 w-5 text-accent" aria-hidden="true" />
            <h2 className="text-lg font-semibold">GitHub Profile (Optional)</h2>
          </div>
          <p className="text-sm text-ink-muted">
            Paste your public GitHub profile URL or username to discover
            projects missing from your resume. Leave blank to continue with your
            resume alone.
          </p>

          <Input
            label="GitHub profile URL or username"
            placeholder="https://github.com/your-username"
            hint={
              detectedGithubUrl
                ? `Detected from your resume: ${detectedGithubUrl}`
                : undefined
            }
            value={githubInput}
            onChange={(e) => setGithubInput(e.target.value)}
          />

          <Input
            label="Other evidence link (optional portfolio or Kaggle URL)"
            placeholder="https://kaggle.com/your-handle"
            hint="Stored as a plain link on your profile; not scraped or analyzed."
            value={otherLink}
            onChange={(e) => setOtherLink(e.target.value)}
          />

          <div className="flex items-center justify-between pt-2">
            <Button variant="secondary" onClick={() => setStep(1)}>
              Back
            </Button>
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                onClick={() => {
                  setGithubAnalysisId(null);
                  setStep(3);
                }}
              >
                Skip GitHub
              </Button>
              <Button onClick={handleGitHubStep} disabled={Boolean(statusMessage)}>
                Continue to Job URL
              </Button>
            </div>
          </div>
        </Card>
      )}

      {step === 3 && (
        <Card className="space-y-5">
          <div className="flex items-center gap-2">
            <Globe className="h-5 w-5 text-accent" aria-hidden="true" />
            <h2 className="text-lg font-semibold">Target Job Posting</h2>
          </div>

          {jobRecord && (
            <Alert tone="success">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span>
                  Loaded job capture: <strong>{jobRecord.title}</strong>
                  {jobRecord.company ? ` at ${jobRecord.company}` : ""}
                </span>
                <Button size="sm" onClick={() => setStep(4)}>
                  Review Captured Job
                </Button>
              </div>
            </Alert>
          )}

          <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <div className="flex-1">
              <Input
                label="Job or internship URL"
                type="url"
                placeholder="https://boards.greenhouse.io/company/jobs/12345"
                value={jobUrl}
                onChange={(e) => setJobUrl(e.target.value)}
              />
            </div>
            <Button
              onClick={handleExtractUrl}
              disabled={!jobUrl.trim() || Boolean(statusMessage)}
            >
              Extract Job Details
            </Button>
          </div>

          {fallbackRequired && (
            <Alert tone="warning" title="Automatic URL extraction needs a hand">
              <p>{fallbackReason}</p>
            </Alert>
          )}

          <div className="space-y-3 border-t border-border pt-4">
            <p className="text-xs font-semibold uppercase tracking-wider text-ink-muted">
              Fallback: Paste Job Description or Upload Screenshot
            </p>
            <textarea
              rows={6}
              placeholder="Paste the full job title, responsibilities, and requirements here..."
              className="w-full rounded-md border border-border bg-surface p-3 text-sm text-ink focus:border-accent"
              value={pastedJobText}
              onChange={(e) => setPastedJobText(e.target.value)}
            />
            <div className="flex flex-wrap items-center justify-between gap-3">
              <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium text-ink hover:bg-subtle">
                <Upload className="h-4 w-4" aria-hidden="true" />
                <span>Upload Job Screenshot</span>
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="sr-only"
                  onChange={handleExtractScreenshot}
                />
              </label>
              <Button
                variant="secondary"
                onClick={handleExtractText}
                disabled={pastedJobText.trim().length < 40 || Boolean(statusMessage)}
              >
                Use Pasted Text
              </Button>
            </div>
          </div>

          <div className="flex justify-start">
            <Button variant="ghost" onClick={() => setStep(2)}>
              Back
            </Button>
          </div>
        </Card>
      )}

      {step === 4 && jobRecord && (
        <Card className="space-y-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="h-5 w-5 text-status-success" aria-hidden="true" />
              <h2 className="text-lg font-semibold">Confirm Extracted Job Details</h2>
            </div>
            <Badge tone="neutral">
              Method: {jobRecord.extraction_method} &middot; Confidence{" "}
              {Math.round(jobRecord.confidence * 100)}%
            </Badge>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Input
              label="Role Title"
              value={jobRecord.title}
              onChange={(e) => setJobRecord({ ...jobRecord, title: e.target.value })}
            />
            <Input
              label="Company"
              value={jobRecord.company}
              onChange={(e) => setJobRecord({ ...jobRecord, company: e.target.value })}
            />
            <Input
              label="Location"
              value={jobRecord.location}
              onChange={(e) => setJobRecord({ ...jobRecord, location: e.target.value })}
            />
            <Input
              label="Experience Level"
              value={jobRecord.experience_level}
              onChange={(e) =>
                setJobRecord({ ...jobRecord, experience_level: e.target.value })
              }
            />
          </div>

          <Input
            label="Required Skills (comma-separated)"
            value={reqSkillsText}
            onChange={(e) => setReqSkillsText(e.target.value)}
          />
          <Input
            label="Preferred Skills (comma-separated)"
            value={prefSkillsText}
            onChange={(e) => setPrefSkillsText(e.target.value)}
          />

          <div className="flex items-center justify-between pt-2">
            <Button variant="secondary" onClick={() => setStep(3)}>
              Back
            </Button>
            <Button onClick={handleConfirmAndAnalyze} disabled={Boolean(statusMessage)}>
              Confirm &amp; Run Compatibility Analysis
            </Button>
          </div>
        </Card>
      )}
    </AppShell>
  );
}
