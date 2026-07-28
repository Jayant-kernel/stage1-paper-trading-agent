import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Stage 1 Paper Operations",
  description: "Private read-only control room for the Stage 1 paper-trading agent.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
