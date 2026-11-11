"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { FileText } from "lucide-react";
import { Alert, Button, Card, Input } from "@/components/ui";
import { apiFetch, ApiError, User } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const loginSchema = z.object({
  email: z.string().email("Enter a valid email address"),
  password: z.string().min(1, "Enter your password"),
});

type LoginForm = z.infer<typeof loginSchema>;

export default function LoginPage() {
  const router = useRouter();
  const { setSession } = useAuth();
  const [serverError, setServerError] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginForm>({
    resolver: zodResolver(loginSchema),
  });

  const onSubmit = async (values: LoginForm) => {
    setServerError(null);
    try {
      const data = await apiFetch<{ access_token: string; user: User }>(
        "/auth/login",
        {
          method: "POST",
          body: JSON.stringify(values),
        }
      );
      setSession(data.access_token, data.user);
      router.push("/dashboard");
    } catch (err) {
      setServerError(
        err instanceof ApiError ? err.message : "Unable to sign in right now."
      );
    }
  };

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-canvas px-4 py-12">
      <div className="w-full max-w-md space-y-6">
        <Link
          href="/"
          className="flex items-center justify-center gap-2 font-serif text-xl font-semibold text-ink"
        >
          <FileText className="h-5 w-5 text-accent" aria-hidden="true" />
          <span>ResumeIQ</span>
        </Link>

        <Card className="space-y-5">
          <div className="space-y-1">
            <h1 className="font-serif text-2xl font-semibold">Sign in</h1>
            <p className="text-sm text-ink-muted">
              Continue to your ResumeIQ workspace.
            </p>
          </div>

          {serverError && <Alert tone="danger">{serverError}</Alert>}

          <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
            <Input
              label="Email"
              type="email"
              autoComplete="email"
              placeholder="you@example.com"
              error={errors.email?.message}
              {...register("email")}
            />
            <Input
              label="Password"
              type="password"
              autoComplete="current-password"
              error={errors.password?.message}
              {...register("password")}
            />
            <Button type="submit" className="w-full" disabled={isSubmitting}>
              {isSubmitting ? "Signing in..." : "Sign in"}
            </Button>
          </form>

          <p className="text-center text-sm text-ink-muted">
            New to ResumeIQ?{" "}
            <Link href="/register" className="font-medium text-accent hover:underline">
              Create an account
            </Link>
          </p>
        </Card>
      </div>
    </div>
  );
}
