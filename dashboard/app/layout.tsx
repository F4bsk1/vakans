import type { Metadata, Viewport } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Vakans — klinikvy",
  description: "Live-vy över bokningar, återbudsflöde och räddad intäkt.",
};

export const viewport: Viewport = {
  // Both, in this order: the page is styled for whichever the viewer's system is set to, and
  // HIG Dark Mode is explicit that an app should follow the system rather than offer its own
  // toggle. See app/globals.css.
  colorScheme: "light dark",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // `lang="sv"` is not cosmetic: it tells a screen reader to pronounce "Återbudsflöde" and
    // the practitioners' names in Swedish rather than spelling them out in English.
    <html lang="sv">
      <body>{children}</body>
    </html>
  );
}
