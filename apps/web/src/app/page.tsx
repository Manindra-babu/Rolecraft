import Link from "next/link";
import { CheckCircle2, FileText, GitBranch, ShieldCheck } from "lucide-react";
import { Button, Card } from "@/components/ui";

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-4 sm:px-6">
          <Link
            href="/"
            className="flex items-center gap-2 font-serif text-xl font-semibold text-ink"
          >
            <FileText className="h-5 w-5 text-accent" aria-hidden="true" />
            <span>ResumeIQ</span>
          </Link>
          <div className="flex items-center gap-3">
            <Link href="/login">
              <Button variant="ghost">Sign in</Button>
            </Link>
            <Link href="/register">
              <Button>Create account</Button>
            </Link>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-16 px-4 py-16 sm:px-6">
        <section className="max-w-3xl space-y-6">
          <p className="text-xs font-semibold uppercase tracking-wider text-accent">
            Evidence-Based Resume Intelligence
          </p>
          <h1 className="font-serif text-4xl font-semibold leading-tight text-ink sm:text-5xl">
            Tailor your resume to every role without inventing a single claim.
          </h1>
          <p className="text-lg leading-relaxed text-ink-muted">
            Upload your DOCX resume, optionally link your GitHub profile, and
            paste a job URL. ResumeIQ surfaces relevant projects you left off,
            explains every skill gap, and edits your original file while keeping
            your layout intact.
          </p>
          <div className="flex flex-wrap items-center gap-3 pt-2">
            <Link href="/register">
              <Button>Get started</Button>
            </Link>
            <Link href="/login">
              <Button variant="secondary">Sign in to workspace</Button>
            </Link>
          </div>
        </section>

        <section aria-labelledby="how-it-works" className="space-y-6">
          <h2 id="how-it-works" className="font-serif text-2xl font-semibold">
            How it works
          </h2>
          <div className="grid gap-6 md:grid-cols-3">
            <Card className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-accent">
                Step 1
              </p>
              <h3 className="text-base font-semibold">
                Upload resume &amp; connect job URL
              </h3>
              <p className="text-sm leading-relaxed text-ink-muted">
                We map every paragraph and bullet in your DOCX file so edits
                preserve your typography, spacing, and structure.
              </p>
            </Card>
            <Card className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-accent">
                Step 2
              </p>
              <h3 className="text-base font-semibold">
                Discover verified GitHub evidence
              </h3>
              <p className="text-sm leading-relaxed text-ink-muted">
                Public repositories are scored for real engineering depth and
                matched to the target job&apos;s stack—never tutorial clones.
              </p>
            </Card>
            <Card className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-accent">
                Step 3
              </p>
              <h3 className="text-base font-semibold">
                Review every diff &amp; export
              </h3>
              <p className="text-sm leading-relaxed text-ink-muted">
                Accept, reject, or edit every suggested bullet side-by-side.
                Download a page-checked DOCX and PDF when you approve.
              </p>
            </Card>
          </div>
        </section>

        <section aria-labelledby="principles" className="grid gap-6 md:grid-cols-3">
          <div className="flex items-start gap-3">
            <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-accent" />
            <div>
              <h3 className="text-sm font-semibold">Truthfulness enforced</h3>
              <p className="mt-1 text-sm text-ink-muted">
                An automated claim validator blocks unverified metrics, tools, or
                inflated titles before you ever review them.
              </p>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <GitBranch className="mt-0.5 h-5 w-5 shrink-0 text-accent" />
            <div>
              <h3 className="text-sm font-semibold">You control every word</h3>
              <p className="mt-1 text-sm text-ink-muted">
                Nothing is added or rewritten without your explicit approval in
                the change review workspace.
              </p>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-accent" />
            <div>
              <h3 className="text-sm font-semibold">Privacy &amp; BYO API key</h3>
              <p className="mt-1 text-sm text-ink-muted">
                Your Gemini key is encrypted at rest and never logged. Delete
                your account and all stored data anytime.
              </p>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
