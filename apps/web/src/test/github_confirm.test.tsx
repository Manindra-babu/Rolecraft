import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ApplicationWorkspacePage from "@/app/applications/[id]/page";
import * as apiModule from "@/lib/api";

const mockPush = vi.fn();
const mockReplace = vi.fn();

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "app-1" }),
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
  usePathname: () => "/applications/app-1",
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    user: {
      id: "user-1",
      email: "alex@example.com",
      full_name: "Alex Candidate",
      gemini_key_mask: "AIza...1234",
      gemini_key_status: "valid",
      gemini_key_added_at: null,
      gemini_calls_month: 0,
      gemini_calls_today: 0,
      daily_limit: 100,
      demo_mode: true,
      google_oauth_enabled: false,
      created_at: "2026-11-11T00:00:00Z",
    },
    isLoading: false,
    setSession: vi.fn(),
    signOut: vi.fn(),
    refreshUser: vi.fn(),
  }),
}));

function makeBaseApp(overrides: Partial<apiModule.ApplicationDetail> = {}): apiModule.ApplicationDetail {
  return {
    id: "app-1",
    status: "Draft",
    notes: "",
    resume_file_id: "res-1",
    resume_filename: "alex.docx",
    resume_file_type: "docx",
    job_posting_id: "job-1",
    job_title: "Software Engineer",
    company: "PayPal",
    github_analysis_id: "gh-1",
    github_status: "analyzed (@Manindra-babu, 2 repos)",
    github_username: "Manindra-babu",
    github_repo_count: 2,
    compatibility_before: 70,
    compatibility_after: 85,
    score_breakdown: null,
    skill_assessments: [],
    github_recommendations: [
      {
        repo_name: "AI_Deck",
        full_name: "Manindra-babu/AI_Deck",
        html_url: "https://github.com/Manindra-babu/AI_Deck",
        quality_score: 86,
        relevance_score: 95.1,
        matched_skills: ["JavaScript", "Python", "FastAPI"],
        reason: "Matches required JavaScript, Python, FastAPI.",
        already_in_resume: false,
        draft_bullets: [
          "Built AI_Deck, an interactive presentation builder using FastAPI and React.",
          "Wrote automated tests covering slide generation (tests/test_slides.py).",
        ],
        bullet_evidence: [
          {
            bullet: "Built AI_Deck, an interactive presentation builder using FastAPI and React.",
            source: "README + dependencies",
            detail: "README overview & FastAPI/React dependencies",
          },
          {
            bullet: "Wrote automated tests covering slide generation (tests/test_slides.py).",
            source: "Automated tests",
            detail: "Test files: tests/test_slides.py",
          },
        ],
        decision: "pending",
      },
      {
        repo_name: "Decision-IQ",
        full_name: "Manindra-babu/Decision-IQ",
        html_url: "https://github.com/Manindra-babu/Decision-IQ",
        quality_score: 95,
        relevance_score: 87.4,
        matched_skills: ["React", "FastAPI"],
        reason: "Matches required React, FastAPI.",
        already_in_resume: false,
        draft_bullets: [
          "Built Decision-IQ, an AI roadmap generator with interactive node graphs.",
        ],
        bullet_evidence: [
          {
            bullet: "Built Decision-IQ, an AI roadmap generator with interactive node graphs.",
            source: "Repository description + dependencies",
            detail: "Description & dependencies",
          },
        ],
        decision: "pending",
      },
    ],
    learning_plans: [],
    versions: [],
    created_at: "2026-11-11T00:00:00Z",
    updated_at: "2026-11-11T00:00:00Z",
    ...overrides,
  };
}

function renderWorkspace() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ApplicationWorkspacePage />
    </QueryClientProvider>
  );
}

describe("GitHub recommendations Approve/Reject & Confirm flow", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("approves a repo, updates counter & Confirm state, confirms, and shows pending Added from GitHub change in Review workspace", async () => {
    let currentApp = makeBaseApp();
    let pollCount = 0;

    const apiSpy = vi.spyOn(apiModule, "apiFetch").mockImplementation(async (path, options) => {
      if (path === "/applications/app-1" && (!options || !options.method || options.method === "GET")) {
        return currentApp;
      }
      if (path === "/resumes/res-1") {
        return {
          id: "res-1",
          filename: "alex.docx",
          file_type: "docx",
          file_size: 1024,
          layout_type: "single_column",
          layout_support: "full",
          format_notice: null,
          page_count: 1,
          element_count: 10,
          created_at: "2026-11-11T00:00:00Z",
          parsed_structure: {
            full_name: "Alex Candidate",
            email: "alex@example.com",
            phone: "",
            location: "",
            links: [],
            summary: "Software developer",
            education: [],
            experience: [],
            projects: [],
            skills: ["Python", "JavaScript"],
            achievements: [],
            certifications: [],
            sections_order: ["experience", "projects", "skills"],
          },
        };
      }
      if (path === "/applications/app-1/github-decisions" && options?.method === "POST") {
        const body = JSON.parse(String(options.body));
        const dMap = new Map<string, "pending" | "approved" | "rejected">(
          body.decisions.map((d: { repo_name: string; decision: "pending" | "approved" | "rejected" }) => [
            d.repo_name,
            d.decision,
          ])
        );
        currentApp = makeBaseApp({
          github_recommendations: currentApp.github_recommendations.map((r) => ({
            ...r,
            decision: dMap.get(r.repo_name) ?? r.decision,
          })),
        });
        return currentApp;
      }
      if (path === "/applications/app-1/github-confirm" && options?.method === "POST") {
        return {
          job_id: "job-confirm-1",
          application_id: "app-1",
          status: "queued",
          progress_percent: 15,
          stage_message: "Queued approved projects...",
          repo_names: ["AI_Deck"],
          version_id: null,
          added_count: 0,
          error_code: null,
          error_message: null,
          created_at: "2026-11-11T00:00:00Z",
          updated_at: "2026-11-11T00:00:00Z",
        };
      }
      if (path === "/applications/app-1/github-confirm/job-confirm-1") {
        pollCount += 1;
        currentApp = makeBaseApp({
          github_recommendations: currentApp.github_recommendations.map((r) =>
            r.repo_name === "AI_Deck" ? { ...r, decision: "approved" } : r
          ),
          versions: [
            {
              id: "ver-1",
              version_number: 1,
              has_docx: false,
              has_pdf: false,
              has_preview: false,
              page_count_original: 1,
              page_count_output: null,
              overflow_detected: false,
              overflow_suggestions: [],
              changes: [
                {
                  id: "chg-gh-1",
                  section: "projects",
                  change_type: "added_from_github",
                  target_location: { insert_section: "projects", repo_name: "AI_Deck" },
                  original_text: "",
                  new_text:
                    "AI_Deck | JavaScript, Python, FastAPI | https://github.com/Manindra-babu/AI_Deck\n• Built AI_Deck, an interactive presentation builder using FastAPI and React.",
                  edited_text: null,
                  reason: "Matches required JavaScript, Python, FastAPI.",
                  evidence_refs: [
                    {
                      source: "README + dependencies",
                      detail: "Bullet 1: README overview & FastAPI/React dependencies",
                    },
                  ],
                  validation_status: "passed",
                  validation_warnings: [],
                  status: "pending",
                },
              ],
              created_at: "2026-11-11T00:00:00Z",
            },
          ],
        });
        return {
          job_id: "job-confirm-1",
          application_id: "app-1",
          status: "done",
          progress_percent: 100,
          stage_message: "Added 1 GitHub project to Review Changes.",
          repo_names: ["AI_Deck"],
          version_id: "ver-1",
          added_count: 1,
          error_code: null,
          error_message: null,
          created_at: "2026-11-11T00:00:00Z",
          updated_at: "2026-11-11T00:00:00Z",
        };
      }
      throw new Error(`Unexpected apiFetch call: ${path}`);
    });

    renderWorkspace();

    const ghTab = await screen.findByRole("tab", { name: /GitHub Projects/i });
    fireEvent.click(ghTab);

    // Initially Confirm is disabled with clear hint
    const confirmBtn = await screen.findByRole("button", { name: /^Confirm$/i });
    expect(confirmBtn).toBeDisabled();
    expect(screen.getByText(/0 approved, 0 rejected/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Approve at least one repository to add projects/i)
    ).toBeInTheDocument();
    // Evidence source is rendered alongside bullets
    expect(screen.getByText(/Evidence: README \+ dependencies/i)).toBeInTheDocument();

    // Approve first repo (AI_Deck) and Reject second repo (Decision-IQ)
    const approveButtons = screen.getAllByRole("button", { name: /^Approve$/i });
    fireEvent.click(approveButtons[0]);

    await waitFor(() => {
      expect(screen.getByText(/1 approved, 0 rejected/i)).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /^Approved$/i })).toHaveAttribute(
      "aria-pressed",
      "true"
    );

    const rejectButtons = screen.getAllByRole("button", { name: /^Reject$/i });
    fireEvent.click(rejectButtons[1]);
    await waitFor(() => {
      expect(screen.getByText(/1 approved, 1 rejected/i)).toBeInTheDocument();
    });

    // Confirm enables and transitions cleanly to Review Changes
    expect(confirmBtn).not.toBeDisabled();
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(screen.getByText(/Review Changes \(1\)/i)).toHaveAttribute("aria-selected", "true");
    });

    expect(pollCount).toBeGreaterThanOrEqual(1);
    expect(
      apiSpy.mock.calls.some(
        ([p, o]) =>
          p === "/applications/app-1/github-confirm" &&
          JSON.parse(String(o?.body)).repo_names[0] === "AI_Deck"
      )
    ).toBe(true);

    // New Added from GitHub pending change appears in Review workspace
    expect(screen.getAllByText(/Added from GitHub/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(
      screen.getAllByText(/AI_Deck \| JavaScript, Python, FastAPI/i).length
    ).toBeGreaterThanOrEqual(1);
  });

  it("displays an inline error and retry button when Confirm API returns an error", async () => {
    const initialApp = makeBaseApp({
      github_recommendations: [
        {
          ...makeBaseApp().github_recommendations[0],
          decision: "approved",
        },
      ],
    });

    vi.spyOn(apiModule, "apiFetch").mockImplementation(async (path, options) => {
      if (path === "/applications/app-1") return initialApp;
      if (path === "/resumes/res-1") {
        return {
          id: "res-1",
          filename: "alex.docx",
          file_type: "docx",
          file_size: 1024,
          layout_type: "single_column",
          layout_support: "full",
          format_notice: null,
          page_count: 1,
          element_count: 5,
          created_at: "2026-11-11T00:00:00Z",
          parsed_structure: {
            full_name: "Alex",
            email: "",
            phone: "",
            location: "",
            links: [],
            summary: "",
            education: [],
            experience: [],
            projects: [],
            skills: [],
            achievements: [],
            certifications: [],
            sections_order: [],
          },
        };
      }
      if (path === "/applications/app-1/github-confirm" && options?.method === "POST") {
        throw new apiModule.ApiError(
          "Your Gemini key needs attention.",
          403,
          "GEMINI_KEY_INVALID"
        );
      }
      throw new Error(`Unexpected apiFetch call: ${path}`);
    });

    renderWorkspace();

    const ghTab = await screen.findByRole("tab", { name: /GitHub Projects/i });
    fireEvent.click(ghTab);

    const confirmBtn = await screen.findByRole("button", { name: /^Confirm$/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(screen.getByText(/Your Gemini key needs attention\./i)).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
  });
});
