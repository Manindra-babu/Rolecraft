"use client";

import React, { useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, KeyRound, Plus, UserCheck } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { Alert, Badge, Button, Card, Skeleton } from "@/components/ui";
import { apiFetch, ApplicationSummary, MasterProfile } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const STATUS_TONE: Record<
  ApplicationSummary["status"],
  "neutral" | "accent" | "success" | "warning" | "danger"
> = {
  Draft: "neutral",
  Applied: "accent",
  Interview: "warning",
  Offer: "success",
  Rejected: "danger",
};

export default function DashboardPage() {
  const router = useRouter();
  const { user, isLoading } = useAuth();

  useEffect(() => {
    if (!isLoading && !user) {
      router.replace("/login");
    }
  }, [isLoading, user, router]);

  const { data: profile, isLoading: profileLoading } = useQuery<MasterProfile>({
    queryKey: ["master-profile"],
    queryFn: () => apiFetch<MasterProfile>("/profile"),
    enabled: Boolean(user),
  });

  const { data: applications, isLoading: appsLoading } = useQuery<
    ApplicationSummary[]
  >({
    queryKey: ["applications"],
    queryFn: () => apiFetch<ApplicationSummary[]>("/applications"),
    enabled: Boolean(user),
  });

  if (isLoading || !user) {
    return (
      <div className="mx-auto max-w-6xl space-y-6 px-4 py-12">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-36 w-full" />
      </div>
    );
  }

  const canAnalyze = user.demo_mode || user.gemini_key_status === "valid";

  return (
    <AppShell>
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="font-serif text-3xl font-semibold text-ink">
            Dashboard
          </h1>
          <p className="mt-1 text-sm text-ink-muted">
            Track tailored applications, skill gaps, and your Master Profile.
          </p>
        </div>

        {canAnalyze ? (
          <Link href="/applications/new">
            <Button>
              <Plus className="h-4 w-4" aria-hidden="true" />
              New application
            </Button>
          </Link>
        ) : (
          <Link href="/profile#api-key">
            <Button variant="secondary">
              <KeyRound className="h-4 w-4" aria-hidden="true" />
              Add Gemini Key to Start
            </Button>
          </Link>
        )}
      </div>

      {!canAnalyze && (
        <Alert
          tone="info"
          title="Analysis is paused until your Gemini API key is configured"
          action={
            <Link href="/profile#api-key">
              <Button size="sm">Open Profile &rarr; API Key</Button>
            </Link>
          }
        >
          ResumeIQ uses your own Google AI Studio Gemini key so your usage stays
          under your control. Follow the 3-step guide in Profile to add a free
          key.
        </Alert>
      )}

      <div className="grid gap-6 md:grid-cols-3">
        <Card className="space-y-4 md:col-span-2">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold">Applications</h2>
            <Badge tone="neutral">
              {applications?.length || 0} applications
            </Badge>
          </div>

          {appsLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : applications && applications.length > 0 ? (
            <div className="space-y-2.5">
              {applications.map((app) => (
                <Link
                  key={app.id}
                  href={`/applications/${app.id}`}
                  className="flex flex-col justify-between gap-2 rounded-md border border-border bg-canvas p-4 transition-colors duration-150 hover:border-accent sm:flex-row sm:items-center"
                >
                  <div className="space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-semibold text-ink">
                        {app.job_title}
                      </span>
                      {app.company && (
                        <span className="text-sm text-ink-muted">
                          at {app.company}
                        </span>
                      )}
                      <Badge tone={STATUS_TONE[app.status]}>{app.status}</Badge>
                    </div>
                    <p className="text-xs text-ink-muted">
                      Resume: {app.resume_filename || "Master Profile"}
                      {app.compatibility_before != null && (
                        <>
                          {" "}
                          &middot; Compatibility: {app.compatibility_before}%
                          {app.compatibility_after != null &&
                          app.compatibility_after !== app.compatibility_before
                            ? ` -> ${app.compatibility_after}%`
                            : ""}
                        </>
                      )}
                    </p>
                  </div>
                  <span className="inline-flex items-center gap-1 text-xs font-medium text-accent">
                    Open workspace
                    <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                  </span>
                </Link>
              ))}
            </div>
          ) : (
            <div className="rounded-md border border-dashed border-border bg-canvas p-8 text-center">
              <p className="text-sm font-medium text-ink">
                No tailored applications yet
              </p>
              <p className="mt-1 text-sm text-ink-muted">
                Start an application by uploading your DOCX resume and pasting a
                job posting URL.
              </p>
            </div>
          )}
        </Card>

        <Card className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold">Master Profile</h2>
            <UserCheck className="h-4 w-4 text-accent" aria-hidden="true" />
          </div>
          {profileLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          ) : (
            <div className="space-y-2 text-sm text-ink-muted">
              <p className="font-medium text-ink">
                {profile?.full_name || user.full_name}
              </p>
              <p>{profile?.email || user.email}</p>
              <p>
                {profile?.skills?.length || 0} skills &middot;{" "}
                {profile?.experience?.length || 0} roles &middot;{" "}
                {profile?.projects?.length || 0} projects
              </p>
              <div className="flex flex-col gap-2 pt-2">
                <Link href="/resumes">
                  <Button variant="secondary" size="sm" className="w-full">
                    Manage Resumes
                  </Button>
                </Link>
                <Link href="/profile">
                  <Button variant="ghost" size="sm" className="w-full">
                    Edit Profile &amp; API Key
                  </Button>
                </Link>
              </div>
            </div>
          )}
        </Card>
      </div>
    </AppShell>
  );
}
