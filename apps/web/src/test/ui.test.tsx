import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import LandingPage from "@/app/page";

describe("LandingPage", () => {
  it("renders ResumeIQ value proposition and honest privacy copy", () => {
    render(<LandingPage />);
    expect(screen.getByText("ResumeIQ")).toBeInTheDocument();
    expect(
      screen.getByText(/Tailor your resume to every role without inventing a single claim/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/Truthfulness enforced/i)).toBeInTheDocument();
  });
});
