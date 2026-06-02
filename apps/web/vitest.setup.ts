// Registers @testing-library/jest-dom's custom matchers (toBeInTheDocument,
// toHaveAttribute, …) on Vitest's `expect`, and augments the matcher types
// so they typecheck across the suite.
import "@testing-library/jest-dom/vitest";
