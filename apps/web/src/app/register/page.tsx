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

const registerSchema = z.object({
  full_name: z.string().min(2, "Enter your full name"),
  email: z.string().email("Enter a valid email address"),
  password: z.string().min(8, "Password must be at least 8 characters"),
});

type RegisterForm = z.infer<typeof registerSchema>;

export default function RegisterPage() {
  const router = useRouter();
  const { setSession } = useAuth();
  const [serverError, setServerError] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<RegisterForm>({
    resolver: zodResolver(registerSchema),
  });

  const onSubmit = async (values: RegisterForm) => {
    setServerError(null);
    try {
      const data = await apiFetch<{ access_token: string; user: User }>(
        "/auth/register",
        {
          method: "POST",
          body: JSON.stringify(values),
        }
      );
      setSession(data.access_token, data.user);
      router.push("/profile#api-key");
    } catch (err) {
      setServerError(
        err instanceof ApiError
          ? err.message
          : "Unable to create account right now."
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
            <h1 className="font-serif text-2xl font-semibold">
              Create your account
            </h1>
            <p className="text-sm text-ink-muted">
              Set up your workspace, then add your personal Gemini API key in
              Profile.
            </p>
          </div>

          {serverError && <Alert tone="danger">{serverError}</Alert>}

          <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
            <Input
              label="Full name"
              type="text"
              autoComplete="name"
              placeholder="Aarav Mehta"
              error={errors.full_name?.message}
              {...register("full_name")}
            />
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
              autoComplete="new-password"
              hint="Minimum 8 characters"
              error={errors.password?.message}
              {...register("password")}
            />
            <Button type="submit" className="w-full" disabled={isSubmitting}>
              {isSubmitting ? "Creating account..." : "Create account"}
            </Button>
          </form>

          <p className="text-center text-sm text-ink-muted">
            Already have an account?{" "}
            <Link href="/login" className="font-medium text-accent hover:underline">
              Sign in
            </Link>
          </p>
        </Card>
      </div>
    </div>
  );
}
