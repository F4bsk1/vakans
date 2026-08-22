import type { Metadata, Viewport } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Vakans — klinikvy",
  description: "Live-vy över bokningar, återbudsflöde och räddad intäkt.",
};

export const viewport: Viewport = {
  // Light only. This screen is shown to a customer for fifteen minutes, often on their laptop
  // or a meeting-room projector — it has to look the same every time, and someone whose machine
  // is set to dark should not see a different product. See app/globals.css.
  colorScheme: "light",
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
