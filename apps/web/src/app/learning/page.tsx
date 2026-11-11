"use client";

import React, { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, CheckSquare, ExternalLink, Square } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { Badge, Card, Skeleton } from "@/components/ui";
import { apiFetch, LearningPlanDetail } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function LearningPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { user, isLoading } = useAuth();

  useEffect(() => {
    if (!isLoading && !user) router.replace("/login");
  }, [isLoading, user, router]);

  const { data: plans, isLoading: plansLoading } = useQuery<
    LearningPlanDetail[]
  >({
    queryKey: ["learning-plans"],
    queryFn: () => apiFetch<LearningPlanDetail[]>("/learning"),
    enabled: Boolean(user),
  });

  const progressMutation = useMutation({
    mutationFn: ({
      planId,
      completed_steps,
      user_confirmed_learning,
      confirmation_note,
    }: {
      planId: string;
      completed_steps: string[];
      user_confirmed_learning: boolean;
      confirmation_note: string;
    }) =>
      apiFetch<LearningPlanDetail>(`/learning/${planId}/progress`, {
        method: "PUT",
        body: JSON.stringify({
          completed_steps,
          user_confirmed_learning,
          confirmation_note,
        }),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["learning-plans"] });
    },
  });

  if (isLoading || !user) {
    return (
      <div className="mx-auto max-w-5xl space-y-6 px-4 py-12">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="space-y-1">
        <h1 className="font-serif text-3xl font-semibold text-ink">
          Learning Roadmaps
        </h1>
        <p className="text-sm text-ink-muted">
          Personalized plans for skills identified as missing or needing
          development. Confirm active study to include an honest &ldquo;Currently
          learning&rdquo; entry.
        </p>
      </div>

      {plansLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : plans && plans.length > 0 ? (
        <div className="space-y-6">
          {plans.map((plan) => (
            <Card key={plan.id} className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <BookOpen className="h-5 w-5 text-accent" aria-hidden="true" />
                  <h2 className="text-lg font-semibold">{plan.skill_name}</h2>
                </div>
                <Badge tone={plan.category === "missing" ? "danger" : "warning"}>
                  {plan.category.replace("_", " ")}
                </Badge>
              </div>

              <div className="space-y-2">
                <p className="text-xs font-semibold uppercase tracking-wider text-ink-muted">
                  Ordered Steps ({plan.completed_steps.length}/
                  {plan.ordered_steps.length} complete)
                </p>
                {plan.ordered_steps.map((step) => {
                  const done = plan.completed_steps.includes(step.id);
                  return (
                    <button
                      key={step.id}
                      type="button"
                      onClick={() => {
                        if (!plan.id) return;
                        const next = done
                          ? plan.completed_steps.filter((id) => id !== step.id)
                          : [...plan.completed_steps, step.id];
                        progressMutation.mutate({
                          planId: plan.id,
                          completed_steps: next,
                          user_confirmed_learning: plan.user_confirmed_learning,
                          confirmation_note: plan.confirmation_note,
                        });
                      }}
                      className="flex w-full items-start gap-3 rounded-md border border-border bg-canvas p-3 text-left hover:border-accent"
                    >
                      {done ? (
                        <CheckSquare className="mt-0.5 h-4 w-4 shrink-0 text-status-success" />
                      ) : (
                        <Square className="mt-0.5 h-4 w-4 shrink-0 text-ink-muted" />
                      )}
                      <div>
                        <p className="text-sm font-medium text-ink">
                          {step.title}{" "}
                          <span className="text-xs font-normal text-ink-muted">
                            (~{step.estimated_hours}h)
                          </span>
                        </p>
                        <p className="text-xs text-ink-muted">
                          {step.description}
                        </p>
                      </div>
                    </button>
                  );
                })}
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <p className="text-xs font-semibold uppercase tracking-wider text-ink-muted">
                    Resources
                  </p>
                  {plan.resources.map((res, idx) => (
                    <div key={idx} className="text-sm">
                      {res.url ? (
                        <a
                          href={res.url}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 font-medium text-accent hover:underline"
                        >
                          {res.title}
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      ) : (
                        <span className="font-medium text-ink">{res.title}</span>
                      )}
                      <span className="ml-2 text-xs text-ink-muted">
                        ({res.verified_url ? "Verified" : "Unverified link"})
                      </span>
                    </div>
                  ))}
                </div>

                <div className="space-y-1.5">
                  <p className="text-xs font-semibold uppercase tracking-wider text-ink-muted">
                    How to Demonstrate Afterward
                  </p>
                  <p className="text-xs leading-relaxed text-ink-muted">
                    {plan.how_to_demonstrate}
                  </p>
                </div>
              </div>

              <div className="flex items-center justify-between border-t border-border pt-3">
                <label className="flex cursor-pointer items-center gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    checked={plan.user_confirmed_learning}
                    onChange={(e) => {
                      if (!plan.id) return;
                      progressMutation.mutate({
                        planId: plan.id,
                        completed_steps: plan.completed_steps,
                        user_confirmed_learning: e.target.checked,
                        confirmation_note:
                          plan.confirmation_note || "official documentation",
                      });
                    }}
                  />
                  <span>
                    Confirm I am actively learning {plan.skill_name} (adds to
                    Skill Development section only)
                  </span>
                </label>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Card className="py-12 text-center text-sm text-ink-muted">
          No learning plans yet. Create an application to see skill-gap
          roadmaps tailored to your target job.
        </Card>
      )}
    </AppShell>
  );
}
