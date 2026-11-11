"use client";

import React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { BookOpen, FileText, KeyRound, LayoutDashboard, LogOut, Settings, User as UserIcon } from "lucide-react";
import { useAuth } from "@/lib/auth-context";
import { Alert, Badge, Button } from "@/components/ui";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, signOut } = useAuth();

  const navItems = [
    { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
    { href: "/resumes", label: "Resumes", icon: FileText },
    { href: "/learning", label: "Learning", icon: BookOpen },
    { href: "/profile", label: "Profile & Key", icon: UserIcon },
    { href: "/settings", label: "Settings", icon: Settings },
  ];

  const handleSignOut = () => {
    signOut();
    router.push("/login");
  };

  const showKeyMissingBanner =
    user && !user.demo_mode && user.gemini_key_status === "missing";
  const showKeyAttentionBanner =
    user && !user.demo_mode && user.gemini_key_status === "needs_attention";

  return (
    <div className="min-h-screen bg-canvas">
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3 sm:px-6">
          <div className="flex items-center gap-6">
            <Link
              href={user ? "/dashboard" : "/"}
              className="flex items-center gap-2 font-serif text-lg font-semibold text-ink"
            >
              <FileText className="h-5 w-5 text-accent" aria-hidden="true" />
              <span>ResumeIQ</span>
            </Link>
            {user && (
              <nav aria-label="Main navigation" className="flex items-center gap-1">
                {navItems.map((item) => {
                  const Icon = item.icon;
                  const active = pathname === item.href;
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-150 ${
                        active
                          ? "bg-accent-soft text-accent"
                          : "text-ink-muted hover:bg-subtle hover:text-ink"
                      }`}
                    >
                      <Icon className="h-4 w-4" aria-hidden="true" />
                      <span>{item.label}</span>
                    </Link>
                  );
                })}
              </nav>
            )}
          </div>

          <div className="flex items-center gap-3">
            {user && (
              <>
                {user.demo_mode ? (
                  <Badge tone="accent">Demo Mode</Badge>
                ) : user.gemini_key_status === "valid" ? (
                  <Badge tone="success">
                    <KeyRound className="h-3 w-3" aria-hidden="true" />
                    Key Active
                  </Badge>
                ) : (
                  <Link href="/profile#api-key">
                    <Badge tone="warning">
                      <KeyRound className="h-3 w-3" aria-hidden="true" />
                      Key Required
                    </Badge>
                  </Link>
                )}
                <span className="hidden text-sm text-ink-muted sm:inline">
                  {user.full_name}
                </span>
                <Button variant="ghost" size="sm" onClick={handleSignOut}>
                  <LogOut className="h-4 w-4" aria-hidden="true" />
                  <span className="sr-only sm:not-sr-only">Sign out</span>
                </Button>
              </>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-6 px-4 py-8 sm:px-6">
        {showKeyMissingBanner && pathname !== "/profile" && (
          <Alert
            tone="warning"
            title="Add your Gemini API key to unlock analysis"
            action={
              <Link href="/profile#api-key">
                <Button size="sm">Add Gemini Key</Button>
              </Link>
            }
          >
            You can view your dashboard and master profile now. Add your own free
            Google AI Studio Gemini key in Profile to analyze jobs, GitHub repos,
            and tailor resumes.
          </Alert>
        )}

        {showKeyAttentionBanner && (
          <Alert
            tone="danger"
            title="Your Gemini key needs attention"
            action={
              <Link href="/profile#api-key">
                <Button size="sm">Update Key in Profile</Button>
              </Link>
            }
          >
            A recent Gemini request failed due to an invalid, revoked, or
            quota-exhausted key. Update or replace your key in Profile.
          </Alert>
        )}

        {children}
      </main>
    </div>
  );
}
