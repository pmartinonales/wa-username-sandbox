import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "username-sandbox",
  description: "WhatsApp usernames/BSUID mock API — sandbox dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-muted/40 font-sans antialiased">{children}</body>
    </html>
  );
}
