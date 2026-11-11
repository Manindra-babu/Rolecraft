export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ||
  (typeof window !== "undefined" &&
  window.location.hostname !== "localhost" &&
  window.location.hostname !== "127.0.0.1"
    ? "/api/v1"
    : "http://localhost:8000/api/v1");

export interface User {
  id: string;
  email: string;
  full_name: string;
  gemini_key_mask: string | null;
  gemini_key_status: "missing" | "valid" | "needs_attention";
  gemini_key_added_at: string | null;
  gemini_calls_month: number;
  gemini_calls_today: number;
  daily_limit: number;
  demo_mode: boolean;
  google_oauth_enabled: boolean;
  created_at: string;
}

export interface GeminiKeyStatus {
  has_key: boolean;
  status: "missing" | "valid" | "needs_attention";
  masked_key: string | null;
  added_at: string | null;
  calls_month: number;
  calls_today: number;
  daily_limit: number;
  demo_mode: boolean;
}

export interface MasterProfile {
  id: string;
  user_id: string;
  full_name: string;
  email: string;
  phone: string;
  location: string;
  summary: string;
  links: Array<{ label: string; url: string }>;
  education: Array<Record<string, unknown>>;
  experience: Array<Record<string, unknown>>;
  projects: Array<Record<string, unknown>>;
  skills: string[];
  achievements: string[];
  certifications: string[];
  confirmed_learning: Array<Record<string, unknown>>;
  updated_at: string;
}

export interface ParsedResumeStructure {
  full_name: string;
  email: string;
  phone: string;
  location: string;
  links: Array<{ label: string; url: string }>;
  summary: string;
  education: Array<{
    institution: string;
    degree: string;
    dates: string;
    details: string[];
    element_ids?: string[];
  }>;
  experience: Array<{
    company: string;
    title: string;
    dates: string;
    location: string;
    bullets: string[];
    element_ids?: string[];
  }>;
  projects: Array<{
    name: string;
    tech_stack: string[];
    url: string;
    bullets: string[];
    element_ids?: string[];
  }>;
  skills: string[];
  skill_categories?: Record<string, string[]>;
  achievements: string[];
  certifications: string[];
  completeness_warnings?: string[];
  sections_order: string[];
}

export interface ResumeUpload {
  id: string;
  filename: string;
  file_type: "docx" | "pdf";
  file_size: number;
  layout_type: string;
  layout_support: "full" | "partial" | "analysis_only";
  format_notice: string | null;
  page_count: number | null;
  parsed_structure: ParsedResumeStructure;
  element_count: number;
  created_at: string;
}

export interface JobPosting {
  id: string | null;
  status: "extracted" | "fallback_required";
  fallback_reason: string | null;
  url: string | null;
  normalized_url: string | null;
  extraction_method: string;
  confidence: number;
  title: string;
  company: string;
  location: string;
  experience_level: string;
  responsibilities: string[];
  required_skills: string[];
  preferred_skills: string[];
  or_groups?: string[][];
  keywords: string[];
  raw_text: string;
  fetched_at: string | null;
  cached?: boolean;
}

export interface AnalyzedRepo {
  name: string;
  full_name: string;
  html_url: string;
  description: string;
  primary_language: string;
  inferred_skills: string[];
  quality_score: number;
  quality_notes: string[];
  excluded_reason: string | null;
}

export interface GitHubAnalysis {
  id: string | null;
  skipped: boolean;
  github_username: string;
  profile_url: string;
  repos: AnalyzedRepo[];
  detected_skills: Array<{
    skill: string;
    repo_count: number;
    evidence_level: string;
  }>;
  other_links: string[];
}

export interface SubScoreExplanation {
  name: string;
  score: number;
  weight: number;
  explanation: string;
  matched_items: string[];
  missing_items: string[];
}

export interface CompatibilityBreakdown {
  overall_score: number;
  required_skills: SubScoreExplanation;
  preferred_skills: SubScoreExplanation;
  experience_relevance: SubScoreExplanation;
  keyword_coverage: SubScoreExplanation;
  project_relevance: SubScoreExplanation;
}

export interface SkillAssessmentItem {
  skill_name: string;
  category: "demonstrated" | "under_represented" | "github_only" | "needs_development" | "missing";
  importance: "required" | "preferred";
  evidence_summary: string;
  confidence: number;
}

export interface BulletEvidenceItem {
  bullet: string;
  source: string;
  detail: string;
}

export interface RepoRecommendation {
  repo_name: string;
  full_name: string;
  html_url: string;
  quality_score: number;
  relevance_score: number;
  matched_skills: string[];
  reason: string;
  already_in_resume: boolean;
  draft_bullets: string[];
  bullet_evidence?: BulletEvidenceItem[];
  decision: "pending" | "approved" | "rejected";
}

export interface GitHubConfirmJob {
  job_id: string;
  application_id: string;
  status: "queued" | "generating" | "validating" | "done" | "failed";
  progress_percent: number;
  stage_message: string;
  repo_names: string[];
  version_id: string | null;
  added_count: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface LearningPlanDetail {
  id: string | null;
  skill_name: string;
  category: string;
  technologies: string[];
  ordered_steps: Array<{
    id: string;
    title: string;
    description: string;
    estimated_hours: number;
  }>;
  resources: Array<{
    title: string;
    provider: string;
    url: string;
    verified_url: boolean;
    note: string;
  }>;
  project_ideas: string[];
  certifications: string[];
  how_to_demonstrate: string;
  completed_steps: string[];
  user_confirmed_learning: boolean;
  confirmation_note: string;
}

export interface ChangeItem {
  id: string;
  section: string;
  change_type: "reworded" | "added_from_resume" | "added_from_github" | "reordered" | "removed" | "user_confirmed_skill";
  target_location: Record<string, unknown>;
  original_text: string;
  new_text: string;
  edited_text: string | null;
  reason: string;
  evidence_refs: Array<{ source?: string; detail?: string }>;
  validation_status: "passed" | "flagged" | "blocked";
  validation_warnings: string[];
  status: "pending" | "accepted" | "rejected" | "edited";
}

export interface ResumeVersion {
  id: string;
  version_number: number;
  has_docx: boolean;
  has_pdf: boolean;
  has_preview: boolean;
  has_orig_preview?: boolean;
  page_count_original: number | null;
  page_count_output: number | null;
  overflow_detected: boolean;
  overflow_suggestions: string[];
  similarity_score?: number | null;
  unchanged_similarity_score?: number | null;
  theme_warnings?: string[];
  export_blocked?: boolean;
  export_cache_key?: string | null;
  changes: ChangeItem[];
  created_at: string;
}

export interface ApplicationSummary {
  id: string;
  status: "Draft" | "Applied" | "Interview" | "Offer" | "Rejected";
  job_title: string;
  company: string;
  resume_filename: string;
  compatibility_before: number | null;
  compatibility_after: number | null;
  created_at: string;
  updated_at: string;
}

export interface ApplicationDetail {
  id: string;
  status: "Draft" | "Applied" | "Interview" | "Offer" | "Rejected";
  notes: string;
  resume_file_id: string | null;
  resume_filename: string;
  resume_file_type: "docx" | "pdf";
  job_posting_id: string | null;
  job_title: string;
  company: string;
  github_analysis_id: string | null;
  github_status?: string;
  github_username?: string | null;
  github_repo_count?: number;
  compatibility_before: number | null;
  compatibility_after: number | null;
  score_breakdown: CompatibilityBreakdown | null;
  skill_assessments: SkillAssessmentItem[];
  github_recommendations: RepoRecommendation[];
  learning_plans: LearningPlanDetail[];
  versions: ResumeVersion[];
  created_at: string;
  updated_at: string;
}

const TOKEN_STORAGE_KEY = "resumeiq_access_token";

export function getStoredToken(): string | null {
  if (typeof window === "undefined" || typeof window.localStorage?.getItem !== "function") {
    return null;
  }
  return window.localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setStoredToken(token: string | null): void {
  if (typeof window === "undefined" || typeof window.localStorage?.setItem !== "function") {
    return;
  }
  if (token) {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } else {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

export class ApiError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getStoredToken();
  const headers = new Headers(options.headers || {});
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (
    options.body &&
    !(options.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });

  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    let code: string | undefined;
    try {
      const errBody = await res.json();
      if (typeof errBody.detail === "string") {
        message = errBody.detail;
      } else if (errBody.detail && typeof errBody.detail === "object") {
        message = errBody.detail.message || message;
        code = errBody.detail.code;
      }
    } catch {
      // ignore json parse errors
    }
    throw new ApiError(message, res.status, code);
  }

  if (res.status === 204) {
    return undefined as T;
  }
  return res.json() as Promise<T>;
}
